"""State managers for presence and window handling."""

from __future__ import annotations

from datetime import timedelta
import inspect
import math
import logging
from typing import Awaitable, Callable

from homeassistant.const import (
    STATE_HOME,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

class _WindowState:
    window_open = False
    hold_active = False

    def on_sensor_change(self, window_open: bool) -> "_WindowState":
        return self

    def on_hold_started(self) -> "_WindowState":
        return self

    def on_hold_cleared(self) -> "_WindowState":
        return self


class _MonitoringState(_WindowState):
    def on_sensor_change(self, window_open: bool) -> "_WindowState":
        return _SENSOR_OPEN if window_open else self

    def on_hold_started(self) -> "_WindowState":
        return _HOLD


class _SensorOpenState(_WindowState):
    window_open = True

    def on_sensor_change(self, window_open: bool) -> "_WindowState":
        return self if window_open else _MONITORING

    def on_hold_started(self) -> "_WindowState":
        return _SENSOR_OPEN_HOLD


class _HoldState(_WindowState):
    hold_active = True

    def on_sensor_change(self, window_open: bool) -> "_WindowState":
        return _SENSOR_OPEN_HOLD if window_open else self

    def on_hold_cleared(self) -> "_WindowState":
        return _MONITORING


class _SensorOpenHoldState(_WindowState):
    window_open = True
    hold_active = True

    def on_sensor_change(self, window_open: bool) -> "_WindowState":
        return self if window_open else _HOLD

    def on_hold_cleared(self) -> "_WindowState":
        return _SENSOR_OPEN


_MONITORING = _MonitoringState()
_SENSOR_OPEN = _SensorOpenState()
_HOLD = _HoldState()
_SENSOR_OPEN_HOLD = _SensorOpenHoldState()


class _BaseSensorManager:
    def __init__(self, hass) -> None:
        self._hass = hass
        self._active = False
        self._observers: list[Callable[[bool], Awaitable[None] | None]] = []
        self._state_change_unsub = None

    def add_observer(
        self, observer: Callable[[bool], Awaitable[None] | None]
    ) -> None:
        if observer not in self._observers:
            self._observers.append(observer)

    def remove_observer(
        self, observer: Callable[[bool], Awaitable[None] | None]
    ) -> None:
        if observer in self._observers:
            self._observers.remove(observer)

    def async_start_listeners(self) -> None:
        if self._state_change_unsub is not None:
            return
        entities = self.tracked_entities()
        if not entities:
            return
        self._state_change_unsub = async_track_state_change_event(
            self._hass, entities, self._handle_state_change
        )

    def async_will_remove_from_hass(self) -> None:
        if self._state_change_unsub:
            self._state_change_unsub()
            self._state_change_unsub = None

    def tracked_entities(self) -> list[str]:
        raise NotImplementedError

    def _iter_state_entities(self) -> list[str]:
        return self.tracked_entities()

    def refresh_state(self) -> bool:
        previous = self._get_active()
        context = self._pre_refresh(previous)
        active = False
        for entity_id in self._iter_state_entities():
            state = self._hass.states.get(entity_id)
            if state is None:
                continue
            if self._is_active_state(entity_id, state, context):
                active = True
                break
        self._set_active(active, context)
        current = self._get_active()
        changed = current != previous
        if changed:
            _LOGGER.debug(
                "%s state changed: %s -> %s",
                self.__class__.__name__,
                previous,
                current,
            )
            self._on_state_change(current, previous, context)
        return changed

    async def _handle_state_change(self, event) -> None:
        self.refresh_state()
        await self._notify_observers()

    async def _notify_observers(self) -> None:
        if not self._observers:
            return
        active = self._get_active()
        for observer in list(self._observers):
            result = observer(active)
            if inspect.isawaitable(result):
                await result

    def _pre_refresh(self, previous: bool):
        return None

    def _is_active_state(self, entity_id: str, state, context) -> bool:
        raise NotImplementedError

    def _set_active(self, active: bool, context) -> None:
        self._active = active

    def _get_active(self) -> bool:
        return self._active

    def _on_state_change(self, current: bool, previous: bool, context) -> None:
        return


class WindowManager(_BaseSensorManager):
    """Track window sensors and inferred window-open events."""

    def __init__(
        self,
        hass,
        *,
        window_sensors: list[str],
        window_threshold: float,
        hold_duration: timedelta,
        on_hold_cleared: Callable[[], Awaitable[None]] | None = None,
        on_hold_triggered: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(hass)
        self._window_sensors = list(window_sensors)
        self._sensor_set = set(self._window_sensors)
        self._window_threshold = window_threshold
        self._hold_duration = hold_duration
        self._on_hold_cleared = on_hold_cleared
        self._on_hold_triggered = on_hold_triggered

        self._state = _MONITORING
        self._window_hold_until: dt_util.dt.datetime | None = None
        self._window_hold_unsub = None
        self._temp_history: list[tuple[dt_util.dt.datetime, float]] = []

    def tracked_entities(self) -> list[str]:
        return list(self._window_sensors)

    def handles(self, entity_id: str | None) -> bool:
        return entity_id in self._sensor_set if entity_id else False

    @property
    def window_open(self) -> bool:
        return self._state.window_open

    @property
    def window_hold_until(self) -> dt_util.dt.datetime | None:
        return self._window_hold_until

    def is_hold_active(self, now: dt_util.dt.datetime | None = None) -> bool:
        if not self._state.hold_active or self._window_hold_until is None:
            return False
        if now is None:
            now = dt_util.utcnow()
        return now < self._window_hold_until

    def is_forced_off(self, now: dt_util.dt.datetime | None = None) -> bool:
        return self._state.window_open or self.is_hold_active(now)

    def _is_active_state(self, entity_id: str, state, context) -> bool:
        return state.state == STATE_ON

    def _set_active(self, active: bool, context) -> None:
        self._state = self._state.on_sensor_change(active)

    def _get_active(self) -> bool:
        return self._state.window_open

    def record_temperature(self, temperature: float) -> bool:
        now = dt_util.utcnow()
        self._temp_history.append((now, temperature))
        cutoff = now - timedelta(minutes=3)
        self._temp_history = [
            (ts, temp) for ts, temp in self._temp_history if ts >= cutoff
        ]
        if len(self._temp_history) < 2:
            return False

        oldest_ts, oldest_temp = self._temp_history[0]
        minutes = (now - oldest_ts).total_seconds() / 60
        if minutes <= 0:
            return False
        drop = oldest_temp - temperature
        rate = drop / minutes
        if drop >= 0.5 or rate >= self._window_threshold:
            location = "unknown"
            config = getattr(self._hass, "config", None)
            if config is not None:
                location = getattr(config, "location_name", location)
            _LOGGER.info(
                "Window inferred in %s: drop=%.2f rate=%.2f",
                location,
                drop,
                rate,
            )
            self._trigger_window_hold()
            self._temp_history = []
            return True
        return False

    def async_will_remove_from_hass(self) -> None:
        super().async_will_remove_from_hass()
        if self._window_hold_unsub:
            self._window_hold_unsub()
            self._window_hold_unsub = None

    def _trigger_window_hold(self) -> None:
        self._window_hold_until = dt_util.utcnow() + self._hold_duration
        self._state = self._state.on_hold_started()
        if self._window_hold_unsub:
            self._window_hold_unsub()

        async def _clear_hold(_now):
            self._window_hold_unsub = None
            self._window_hold_until = None
            self._state = self._state.on_hold_cleared()
            _LOGGER.info("Window hold cleared")
            if self._on_hold_cleared:
                await self._on_hold_cleared()

        self._window_hold_unsub = async_call_later(
            self._hass, self._hold_duration.total_seconds(), _clear_hold
        )
        if self._on_hold_triggered:
            _LOGGER.info("Window hold started")
            self._on_hold_triggered()


class PresenceManager(_BaseSensorManager):
    """Track room presence using zone, guest, and sensor inputs."""

    def __init__(
        self,
        hass,
        *,
        area_name: str,
        slug: str,
        presence_sensors: list[str],
        distance_sensors: list[str],
        bermuda_threshold: float,
        guest_entity_id: str,
        home_entity_id: str,
    ) -> None:
        super().__init__(hass)
        self._area_name = area_name
        self._slug = slug
        self._presence_sensors = list(presence_sensors)
        self._distance_sensors = list(distance_sensors)
        self._bermuda_threshold = bermuda_threshold
        self._guest_entity_id = guest_entity_id
        self._home_entity_id = home_entity_id
        self._tracked_entities = set(
            self._presence_sensors
            + self._distance_sensors
            + [self._guest_entity_id, self._home_entity_id]
        )
        self._strategies = self._build_strategies()
        self._presence_on = False

    def tracked_entities(self) -> list[str]:
        return list(self._tracked_entities)

    def handles(self, entity_id: str | None) -> bool:
        return entity_id in self._tracked_entities if entity_id else False

    def is_present(self) -> bool:
        return self._presence_on

    def is_home(self) -> bool:
        state = self._hass.states.get(self._home_entity_id)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return False
        try:
            return int(float(state.state)) > 0
        except (TypeError, ValueError):
            return False

    def is_guest_mode(self) -> bool:
        state = self._hass.states.get(self._guest_entity_id)
        return state is not None and state.state == STATE_ON

    def _iter_state_entities(self) -> list[str]:
        return self._distance_sensors + self._presence_sensors

    def _pre_refresh(self, previous: bool):
        guest_mode = self.is_guest_mode()
        return {
            "previous": previous,
            "zone_home": self.is_home() or guest_mode,
        }

    def _is_active_state(self, entity_id: str, state, context) -> bool:
        if not context["zone_home"]:
            return False
        strategy = self._strategies.get(entity_id)
        if strategy is None:
            return False
        return strategy.is_present(entity_id, state, context)

    def _set_active(self, active: bool, context) -> None:
        self._presence_on = active

    def _get_active(self) -> bool:
        return self._presence_on

    def _build_strategies(self) -> dict[str, "PresenceDetectionStrategy"]:
        strategies: dict[str, PresenceDetectionStrategy] = {}
        proximity = ProximityPresenceStrategy(self._bermuda_threshold)
        area_match = AreaPresenceStrategy(self._area_name, self._slug)
        binary = BinaryPresenceStrategy()
        for entity_id in self._distance_sensors:
            strategies[entity_id] = proximity
        for entity_id in self._presence_sensors:
            if entity_id.startswith("binary_sensor."):
                strategies[entity_id] = binary
            else:
                strategies[entity_id] = area_match
        return strategies


class PresenceDetectionStrategy:
    def is_present(self, entity_id: str, state, context) -> bool:
        raise NotImplementedError


class BinaryPresenceStrategy(PresenceDetectionStrategy):
    def is_present(self, entity_id: str, state, context) -> bool:
        return state.state in (STATE_ON, STATE_HOME)


class ProximityPresenceStrategy(PresenceDetectionStrategy):
    def __init__(self, threshold: float) -> None:
        self._threshold = threshold

    def is_present(self, entity_id: str, state, context) -> bool:
        value = _state_to_float(state)
        if value is None or not math.isfinite(value):
            return False
        hysteresis = 0.5 if context["previous"] else 0.0
        return value < self._threshold + hysteresis


class AreaPresenceStrategy(PresenceDetectionStrategy):
    def __init__(self, area_name: str, slug: str) -> None:
        self._area_name = area_name.casefold()
        self._slug = slug.casefold()

    def is_present(self, entity_id: str, state, context) -> bool:
        if state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return False
        state_value = str(state.state).casefold()
        return state_value in (self._area_name, self._slug)


def _state_to_float(state) -> float | None:
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None
