"""Boiler coordinator for Vesta."""

from __future__ import annotations

import asyncio
from enum import Enum
import logging
from datetime import timedelta

from homeassistant.components.climate.const import (
    ATTR_HVAC_MODES,
    HVACMode,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_TEMPERATURE,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

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
DEMAND_UPDATE_DEBOUNCE_SECONDS = 5


class BoilerState(Enum):
    """Explicit boiler states to prevent racing transitions."""

    IDLE = "idle"
    ANTI_CYCLE = "anti_cycle_cooldown"
    FIRING = "firing"
    FAILSAFE = "failsafe"


class BoilerCoordinator(DataUpdateCoordinator):
    """Central safety controller for the boiler."""

    def __init__(self, hass: HomeAssistant, entry):
        super().__init__(hass, _LOGGER, name="vesta_boiler")
        config = {**entry.data, **entry.options}
        self._boiler_entity = config[CONF_BOILER_ENTITY]
        self._boost_temp = config.get(CONF_BOOST_TEMP, DEFAULT_BOOST_TEMP)
        self._off_temp = config.get(CONF_OFF_TEMP, DEFAULT_OFF_TEMP)
        self._min_cycle = config.get(CONF_MIN_CYCLE, DEFAULT_MIN_CYCLE)
        self._demand: dict[str, bool] = {}
        self._pending_demand: dict[str, bool] = {}
        self._demand_update_unsub = None
        self._state = BoilerState.IDLE
        self._cooldown_until: dt_util.dt.datetime | None = None
        self._retry_unsub = None
        self._master_state_warned = False
        self._state_lock = asyncio.Lock()

    async def async_update_demand(
        self, zone_id: str, demand: bool, *, immediate: bool = False
    ) -> None:
        """Update demand from a zone, optionally batching updates."""
        self._pending_demand[zone_id] = demand
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
                await self._ensure_boiler_off(force=True)
                return

            now = dt_util.utcnow()
            self._update_cooldown_state(now)

            if any(self._demand.values()):
                await self._ensure_boiler_on(now)
            else:
                await self._ensure_boiler_off()

    def _cooldown_remaining(self, now: dt_util.dt.datetime) -> float:
        if self._cooldown_until is None:
            return 0.0
        return max(0.0, (self._cooldown_until - now).total_seconds())

    def _enter_cooldown(self, now: dt_util.dt.datetime) -> None:
        if self._min_cycle <= 0:
            self._cooldown_until = None
            self._state = BoilerState.IDLE
            return
        self._cooldown_until = now + timedelta(minutes=self._min_cycle)
        self._state = BoilerState.ANTI_CYCLE

    def _update_cooldown_state(self, now: dt_util.dt.datetime) -> None:
        if self._cooldown_until is None:
            if self._state == BoilerState.ANTI_CYCLE:
                self._state = BoilerState.IDLE
            return
        if now >= self._cooldown_until:
            self._cooldown_until = None
            if self._state == BoilerState.ANTI_CYCLE:
                self._state = BoilerState.IDLE
            return
        if self._state not in (BoilerState.FIRING, BoilerState.FAILSAFE):
            self._state = BoilerState.ANTI_CYCLE

    def _cancel_retry(self) -> None:
        if self._retry_unsub is None:
            return
        self._retry_unsub()
        self._retry_unsub = None

    def _schedule_retry(self, delay: float, *, replace: bool = False) -> None:
        if delay <= 0:
            return
        if self._retry_unsub is not None:
            if not replace:
                return
            self._retry_unsub()
            self._retry_unsub = None

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
        if remaining > 0:
            if self._state == BoilerState.FAILSAFE:
                self._schedule_retry(
                    min(remaining, FAILSAFE_RETRY_SECONDS), replace=True
                )
            else:
                self._state = BoilerState.ANTI_CYCLE
                self._schedule_retry(remaining)
            return
        if self._state == BoilerState.ANTI_CYCLE:
            self._cooldown_until = None
            self._state = BoilerState.IDLE

        success = await self._turn_boiler_on()
        if success:
            self._cancel_retry()
            self._state = BoilerState.FIRING
            return

        self._state = BoilerState.FAILSAFE
        self._schedule_retry(FAILSAFE_RETRY_SECONDS, replace=True)

    async def _ensure_boiler_off(self, *, force: bool = False) -> None:
        success, was_on = await self._turn_boiler_off()
        if not success:
            self._state = BoilerState.FAILSAFE
            self._schedule_retry(FAILSAFE_RETRY_SECONDS, replace=True)
            return

        self._cancel_retry()
        now = dt_util.utcnow()
        if was_on or force or self._state == BoilerState.FIRING:
            self._enter_cooldown(now)
            return
        if self._state == BoilerState.FAILSAFE:
            if self._cooldown_remaining(now) > 0:
                self._state = BoilerState.ANTI_CYCLE
            else:
                self._cooldown_until = None
                self._state = BoilerState.IDLE
            return
        self._update_cooldown_state(now)

    async def _turn_boiler_on(self) -> bool:
        domain = self._boiler_entity.split(".", 1)[0]
        state = self.hass.states.get(self._boiler_entity)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            _LOGGER.warning(
                "Boiler entity %s unavailable; scheduling retry",
                self._boiler_entity,
            )
            return False

        already_on = False
        if domain == "climate":
            current_temp = state.attributes.get(ATTR_TEMPERATURE)
            try:
                current_temp = float(current_temp)
            except (TypeError, ValueError):
                current_temp = None
            if (
                state.state == HVACMode.HEAT
                and current_temp is not None
                and abs(current_temp - self._boost_temp) < 0.1
            ):
                already_on = True
        elif state.state == STATE_ON:
            already_on = True

        if already_on:
            return True
        if domain == "climate":
            if not self.hass.services.has_service("climate", SERVICE_SET_TEMPERATURE):
                _LOGGER.warning(
                    "Climate service set_temperature unavailable; skipping boiler on"
                )
                return False
            if self.hass.services.has_service("climate", SERVICE_SET_HVAC_MODE):
                await self.hass.services.async_call(
                    "climate",
                    SERVICE_SET_HVAC_MODE,
                    {ATTR_ENTITY_ID: self._boiler_entity, "hvac_mode": HVACMode.HEAT},
                    blocking=True,
                )
            await self.hass.services.async_call(
                "climate",
                SERVICE_SET_TEMPERATURE,
                {
                    ATTR_ENTITY_ID: self._boiler_entity,
                    ATTR_TEMPERATURE: self._boost_temp,
                },
                blocking=True,
            )
        else:
            await self.hass.services.async_call(
                domain,
                "turn_on",
                {ATTR_ENTITY_ID: self._boiler_entity},
                blocking=True,
            )
        return True

    async def _turn_boiler_off(self) -> tuple[bool, bool]:
        domain = self._boiler_entity.split(".", 1)[0]
        state = self.hass.states.get(self._boiler_entity)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            _LOGGER.warning(
                "Boiler entity %s unavailable; scheduling retry",
                self._boiler_entity,
            )
            return False, False

        was_on = False
        if domain == "climate":
            was_on = state.state == HVACMode.HEAT
        else:
            was_on = state.state == STATE_ON

        if domain == "climate":
            if not self.hass.services.has_service("climate", SERVICE_SET_TEMPERATURE):
                _LOGGER.warning(
                    "Climate service set_temperature unavailable; skipping boiler off"
                )
                return False, was_on
            state = self.hass.states.get(self._boiler_entity)
            hvac_modes = []
            if state is not None:
                hvac_modes = state.attributes.get(ATTR_HVAC_MODES, [])
            if HVACMode.OFF in hvac_modes:
                if self.hass.services.has_service("climate", SERVICE_SET_HVAC_MODE):
                    await self.hass.services.async_call(
                        "climate",
                        SERVICE_SET_HVAC_MODE,
                        {ATTR_ENTITY_ID: self._boiler_entity, "hvac_mode": HVACMode.OFF},
                        blocking=True,
                    )
            await self.hass.services.async_call(
                "climate",
                SERVICE_SET_TEMPERATURE,
                {
                    ATTR_ENTITY_ID: self._boiler_entity,
                    ATTR_TEMPERATURE: self._off_temp,
                },
                blocking=True,
            )
        else:
            await self.hass.services.async_call(
                domain,
                "turn_off",
                {ATTR_ENTITY_ID: self._boiler_entity},
                blocking=True,
            )
        return True, was_on
