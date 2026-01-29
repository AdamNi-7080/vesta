"""State managers for presence and window handling."""

from __future__ import annotations

from datetime import timedelta
import math
from typing import Awaitable, Callable

from homeassistant.const import (
    STATE_HOME,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util


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


class WindowManager:
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
        self._hass = hass
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

    def refresh_state(self) -> bool:
        window_open = False
        for entity_id in self._window_sensors:
            state = self._hass.states.get(entity_id)
            if state is None:
                continue
            if state.state == STATE_ON:
                window_open = True
                break
        changed = window_open != self._state.window_open
        self._state = self._state.on_sensor_change(window_open)
        return changed

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
            self._trigger_window_hold()
            return True
        return False

    def async_will_remove_from_hass(self) -> None:
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
            if self._on_hold_cleared:
                await self._on_hold_cleared()

        self._window_hold_unsub = async_call_later(
            self._hass, self._hold_duration.total_seconds(), _clear_hold
        )
        if self._on_hold_triggered:
            self._on_hold_triggered()


class PresenceManager:
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
        self._hass = hass
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

    def refresh_state(self) -> bool:
        previous_presence = self._presence_on
        guest_mode = self.is_guest_mode()
        zone_home = self.is_home() or guest_mode
        self._presence_on = False
        if not zone_home:
            return self._presence_on != previous_presence
        if self._distance_sensors:
            hysteresis = 0.5 if previous_presence else 0.0
            for entity_id in self._distance_sensors:
                value = _state_to_float(self._hass.states.get(entity_id))
                if value is None or not math.isfinite(value):
                    continue
                if value < self._bermuda_threshold + hysteresis:
                    self._presence_on = True
                    return self._presence_on != previous_presence

        for entity_id in self._presence_sensors:
            state = self._hass.states.get(entity_id)
            if state is None:
                continue
            if entity_id.startswith("binary_sensor."):
                if state.state in (STATE_ON, STATE_HOME):
                    self._presence_on = True
                    return self._presence_on != previous_presence
                continue
            if state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
                continue
            state_value = str(state.state).casefold()
            if state_value in (self._area_name.casefold(), self._slug.casefold()):
                self._presence_on = True
                return self._presence_on != previous_presence
        return self._presence_on != previous_presence


def _state_to_float(state) -> float | None:
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None
