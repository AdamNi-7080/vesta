"""Boiler coordinator for Vesta."""

from __future__ import annotations

from datetime import timedelta
import asyncio
from enum import Enum
import inspect
import logging
from typing import Awaitable, Callable

from homeassistant.const import (
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .commands import (
    CommandExecutor,
    TurnBoilerOffCommand,
    TurnBoilerOnCommand,
    build_boiler_driver,
)
from .const import (
    CONF_BOILER_ENTITY,
    CONF_BOOST_TEMP,
    CONF_MIN_CYCLE,
    CONF_OFF_TEMP,
    DEFAULT_BOOST_TEMP,
    DEFAULT_MIN_CYCLE,
    DEFAULT_OFF_TEMP,
)

_LOGGER = logging.getLogger(__name__)

MASTER_SWITCH_ENTITY = "switch.vesta_master_heating"
FAILSAFE_RETRY_SECONDS = 60
RETRY_MAX_SECONDS = 3600
DEMAND_UPDATE_DEBOUNCE_SECONDS = 5
CIRCUIT_BREAKER_FAILURES = 3
CIRCUIT_BREAKER_RESET_SECONDS = 300


class _BoilerState:
    name = "base"

    async def handle_demand(
        self, coordinator: "BoilerCoordinator", now: dt_util.dt.datetime, demand: bool
    ) -> None:
        if demand:
            await coordinator._ensure_boiler_on(now)
        else:
            await coordinator._ensure_boiler_off()


class _IdleState(_BoilerState):
    name = "idle"


class _AntiCycleState(_BoilerState):
    name = "anti_cycle_cooldown"


class _FiringState(_BoilerState):
    name = "firing"


class _FailsafeState(_BoilerState):
    name = "failsafe"


_IDLE_STATE = _IdleState()
_ANTI_CYCLE_STATE = _AntiCycleState()
_FIRING_STATE = _FiringState()
_FAILSAFE_STATE = _FailsafeState()

BoilerStateObserver = Callable[[str, str], Awaitable[None] | None]


class CircuitBreakerState(Enum):
    """Circuit breaker states for boiler service calls."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Simple circuit breaker for boiler service calls."""

    def __init__(self, *, failure_threshold: int, reset_timeout: timedelta) -> None:
        self._failure_threshold = max(1, failure_threshold)
        self._reset_timeout = reset_timeout
        self._state = CircuitBreakerState.CLOSED
        self._failure_count = 0
        self._opened_until: dt_util.dt.datetime | None = None
        self._half_open_in_flight = False

    def can_attempt(self, now: dt_util.dt.datetime) -> bool:
        if self._state == CircuitBreakerState.OPEN:
            if self._opened_until is not None and now >= self._opened_until:
                self._state = CircuitBreakerState.HALF_OPEN
                self._half_open_in_flight = False
            else:
                return False
        if self._state == CircuitBreakerState.HALF_OPEN:
            if self._half_open_in_flight:
                return False
            self._half_open_in_flight = True
            return True
        return True

    def record_success(self) -> None:
        if self._state != CircuitBreakerState.CLOSED:
            _LOGGER.info("Boiler circuit breaker closed after successful call")
        self._state = CircuitBreakerState.CLOSED
        self._failure_count = 0
        self._opened_until = None
        self._half_open_in_flight = False

    def record_failure(self, now: dt_util.dt.datetime) -> None:
        if self._state == CircuitBreakerState.HALF_OPEN:
            self._open(now, reason="half-open test failed")
            return
        self._failure_count += 1
        if self._failure_count >= self._failure_threshold:
            self._open(now, reason="failure threshold reached")

    def next_attempt_in(self, now: dt_util.dt.datetime) -> float:
        if self._state != CircuitBreakerState.OPEN or self._opened_until is None:
            return 0.0
        return max(0.0, (self._opened_until - now).total_seconds())

    def _open(self, now: dt_util.dt.datetime, *, reason: str) -> None:
        self._state = CircuitBreakerState.OPEN
        self._opened_until = now + self._reset_timeout
        self._failure_count = 0
        self._half_open_in_flight = False
        _LOGGER.warning(
            "Boiler circuit breaker opened (%s); retrying after %.0fs",
            reason,
            self._reset_timeout.total_seconds(),
        )


class BoilerCoordinator(DataUpdateCoordinator):
    """Central safety controller for the boiler."""

    def __init__(self, hass: HomeAssistant, entry):
        super().__init__(hass, _LOGGER, name="vesta_boiler")
        config = {**entry.data, **entry.options}
        self._boiler_entity = config[CONF_BOILER_ENTITY]
        self._boost_temp = config.get(CONF_BOOST_TEMP, DEFAULT_BOOST_TEMP)
        self._off_temp = config.get(CONF_OFF_TEMP, DEFAULT_OFF_TEMP)
        self._min_cycle = config.get(CONF_MIN_CYCLE, DEFAULT_MIN_CYCLE)
        self._boiler_driver = build_boiler_driver(
            self._boiler_entity, self._boost_temp, self._off_temp
        )
        self._demand: dict[str, bool] = {}
        self._pending_demand: dict[str, bool] = {}
        self._demand_update_unsub = None
        self._state = _IDLE_STATE
        self._cooldown_until: dt_util.dt.datetime | None = None
        self._breaker = CircuitBreaker(
            failure_threshold=CIRCUIT_BREAKER_FAILURES,
            reset_timeout=timedelta(seconds=CIRCUIT_BREAKER_RESET_SECONDS),
        )
        self._command_executor = CommandExecutor(hass)
        self._observers: list[BoilerStateObserver] = []
        self._retry_unsub = None
        self._retry_attempts = 0
        self._master_state_warned = False
        self._state_lock = asyncio.Lock()

    @property
    def command_executor(self) -> CommandExecutor:
        return self._command_executor

    def add_observer(self, observer: BoilerStateObserver) -> None:
        if observer not in self._observers:
            self._observers.append(observer)

    def remove_observer(self, observer: BoilerStateObserver) -> None:
        if observer in self._observers:
            self._observers.remove(observer)

    def _set_state(self, state: _BoilerState) -> None:
        if self._state is state:
            return
        previous = self._state
        self._state = state
        if state is _FAILSAFE_STATE:
            _LOGGER.warning(
                "Boiler state transition: %s -> %s",
                previous.name,
                state.name,
            )
        else:
            _LOGGER.debug(
                "Boiler state transition: %s -> %s",
                previous.name,
                state.name,
            )
        self._notify_observers(previous, state)

    def _notify_observers(
        self, previous: _BoilerState, current: _BoilerState
    ) -> None:
        if not self._observers:
            return
        for observer in list(self._observers):
            result = observer(current.name, previous.name)
            if inspect.isawaitable(result):
                self.hass.async_create_task(result)

    async def async_update_demand(
        self, zone_id: str, demand: bool, *, immediate: bool = False
    ) -> None:
        """Update demand from a zone, optionally batching updates."""
        self._pending_demand[zone_id] = demand
        _LOGGER.debug(
            "Demand update queued: zone=%s demand=%s immediate=%s",
            zone_id,
            demand,
            immediate,
        )
        if immediate:
            self._cancel_demand_update()
            if self._apply_pending_demand_updates():
                await self._recalculate()
            return
        self._schedule_demand_update()

    async def async_recalculate(self) -> None:
        """Public method to force a recalculation."""
        await self._recalculate()

    def _cancel_demand_update(self) -> None:
        if self._demand_update_unsub is None:
            return
        self._demand_update_unsub()
        self._demand_update_unsub = None

    def _schedule_demand_update(self) -> None:
        if self._demand_update_unsub is not None:
            return

        async def _flush(_now):
            self._demand_update_unsub = None
            if self._apply_pending_demand_updates():
                await self._recalculate()

        _LOGGER.debug(
            "Debouncing demand updates for %.0fs",
            DEMAND_UPDATE_DEBOUNCE_SECONDS,
        )
        self._demand_update_unsub = async_call_later(
            self.hass, DEMAND_UPDATE_DEBOUNCE_SECONDS, _flush
        )

    def _apply_pending_demand_updates(self) -> bool:
        if not self._pending_demand:
            return False
        changed = False
        for zone_id, demand in self._pending_demand.items():
            if self._demand.get(zone_id) != demand:
                changed = True
            self._demand[zone_id] = demand
        self._pending_demand.clear()
        if changed:
            _LOGGER.debug("Demand map updated: %s", self._demand)
        return changed

    async def _recalculate(self) -> None:
        async with self._state_lock:
            if self._pending_demand:
                self._cancel_demand_update()
                self._apply_pending_demand_updates()
            master_state = self.hass.states.get(MASTER_SWITCH_ENTITY)
            if master_state is None or master_state.state in (
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
            ):
                if not self._master_state_warned:
                    _LOGGER.warning(
                        "Master heating switch is unavailable or unknown. Defaulting to HEATING ENABLED for safety."
                    )
                    self._master_state_warned = True
            elif master_state.state == STATE_OFF:
                _LOGGER.info(
                    "Master heating switch is off; forcing boiler off"
                )
                await self._ensure_boiler_off(force=True)
                return
            elif master_state.state != STATE_ON:
                _LOGGER.warning(
                    "Master heating switch state %s is unexpected; defaulting to HEATING ENABLED",
                    master_state.state,
                )

            now = dt_util.utcnow()
            self._update_cooldown_state(now)

            await self._state.handle_demand(
                self, now, any(self._demand.values())
            )

    def _cooldown_remaining(self, now: dt_util.dt.datetime) -> float:
        if self._cooldown_until is None:
            return 0.0
        return max(0.0, (self._cooldown_until - now).total_seconds())

    def _enter_cooldown(self, now: dt_util.dt.datetime) -> None:
        if self._min_cycle <= 0:
            self._cooldown_until = None
            self._set_state(_IDLE_STATE)
            return
        self._cooldown_until = now + timedelta(minutes=self._min_cycle)
        self._set_state(_ANTI_CYCLE_STATE)

    def _update_cooldown_state(self, now: dt_util.dt.datetime) -> None:
        if self._cooldown_until is None:
            if self._state is _ANTI_CYCLE_STATE:
                self._set_state(_IDLE_STATE)
            return
        if now >= self._cooldown_until:
            self._cooldown_until = None
            if self._state is _ANTI_CYCLE_STATE:
                self._set_state(_IDLE_STATE)
            return
        if self._state not in (_FIRING_STATE, _FAILSAFE_STATE):
            self._set_state(_ANTI_CYCLE_STATE)

    def _cancel_retry(self) -> None:
        if self._retry_unsub is None:
            return
        self._retry_unsub()
        self._retry_unsub = None

    def _schedule_retry(self, delay: float, *, replace: bool = False) -> None:
        _ = replace
        if delay <= 0:
            return
        if self._retry_unsub is not None:
            return

        _LOGGER.debug("Scheduling boiler retry in %.0fs", delay)

        async def _retry(_now):
            self._retry_unsub = None
            await self._recalculate()

        self._retry_unsub = async_call_later(self.hass, delay, _retry)

    async def async_force_off(self) -> None:
        """Force the boiler to an off state and start anti-cycle cooldown."""
        async with self._state_lock:
            await self._ensure_boiler_off(force=True)

    async def _ensure_boiler_on(self, now: dt_util.dt.datetime) -> None:
        remaining = self._cooldown_remaining(now)
        breaker_delay = self._breaker.next_attempt_in(now)
        delay = max(remaining, breaker_delay)
        if delay > 0:
            _LOGGER.debug(
                "Boiler on suppressed (cooldown=%.0fs breaker=%.0fs)",
                remaining,
                breaker_delay,
            )
            if breaker_delay > 0:
                self._set_state(_FAILSAFE_STATE)
            elif self._state is not _FAILSAFE_STATE:
                self._set_state(_ANTI_CYCLE_STATE)
            self._schedule_retry(delay, replace=True)
            return
        if self._state is _ANTI_CYCLE_STATE:
            self._cooldown_until = None
            self._set_state(_IDLE_STATE)

        success = await self._turn_boiler_on()
        if success:
            _LOGGER.info("Boiler turn_on successful")
            self._cancel_retry()
            self._set_state(_FIRING_STATE)
            return

        self._retry_attempts += 1
        self._set_state(_FAILSAFE_STATE)
        self._schedule_retry(self._failsafe_delay(now), replace=True)

    async def _ensure_boiler_off(self, *, force: bool = False) -> None:
        now = dt_util.utcnow()
        success, was_on = await self._turn_boiler_off()
        if not success:
            self._retry_attempts += 1
            self._set_state(_FAILSAFE_STATE)
            self._schedule_retry(self._failsafe_delay(now), replace=True)
            return

        _LOGGER.info("Boiler turn_off successful")
        self._cancel_retry()
        if was_on or force or self._state is _FIRING_STATE:
            self._enter_cooldown(now)
            return
        if self._state is _FAILSAFE_STATE:
            if self._cooldown_remaining(now) > 0:
                self._set_state(_ANTI_CYCLE_STATE)
            else:
                self._cooldown_until = None
                self._set_state(_IDLE_STATE)
            return
        self._update_cooldown_state(now)

    def _failsafe_delay(self, now: dt_util.dt.datetime) -> float:
        breaker_delay = self._breaker.next_attempt_in(now)
        backoff = FAILSAFE_RETRY_SECONDS * (2**self._retry_attempts)
        backoff = min(backoff, RETRY_MAX_SECONDS)
        if breaker_delay > 0:
            return max(breaker_delay, backoff)
        return backoff

    async def _turn_boiler_on(self) -> bool:
        now = dt_util.utcnow()
        if not self._breaker.can_attempt(now):
            _LOGGER.debug("Boiler circuit breaker open; skipping turn_on")
            return False

        command = TurnBoilerOnCommand(self._boiler_driver)
        result = await self._command_executor.execute(command, propagate=False)

        if result.success:
            self._breaker.record_success()
            self._retry_attempts = 0
            return True

        if result.error == "entity unavailable":
            _LOGGER.warning(
                "Boiler entity %s unavailable; scheduling retry",
                self._boiler_entity,
            )
        elif result.error == "set_temperature unavailable":
            _LOGGER.warning(
                "Climate service set_temperature unavailable; skipping boiler on"
            )
        else:
            _LOGGER.warning("Boiler turn_on failed: %s", result.error or "unknown error")

        self._breaker.record_failure(now)
        return False

    async def _turn_boiler_off(self) -> tuple[bool, bool]:
        now = dt_util.utcnow()
        if not self._breaker.can_attempt(now):
            _LOGGER.debug("Boiler circuit breaker open; skipping turn_off")
            return False, False

        command = TurnBoilerOffCommand(self._boiler_driver)
        result = await self._command_executor.execute(command, propagate=False)
        was_on = False
        if result.data and "was_on" in result.data:
            was_on = bool(result.data["was_on"])

        if result.success:
            self._breaker.record_success()
            self._retry_attempts = 0
            return True, was_on

        if result.error == "entity unavailable":
            _LOGGER.warning(
                "Boiler entity %s unavailable; scheduling retry",
                self._boiler_entity,
            )
        elif result.error == "set_temperature unavailable":
            _LOGGER.warning(
                "Climate service set_temperature unavailable; skipping boiler off"
            )
        else:
            _LOGGER.warning(
                "Boiler turn_off failed: %s", result.error or "unknown error"
            )

        self._breaker.record_failure(now)
        return False, was_on
