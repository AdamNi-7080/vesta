from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sys
import types

import pytest


def _install_fake_homeassistant() -> None:
    try:
        import homeassistant  # noqa: F401
        return
    except Exception:
        pass

    if "homeassistant" in sys.modules:
        return

    homeassistant = types.ModuleType("homeassistant")
    sys.modules["homeassistant"] = homeassistant

    const = types.ModuleType("homeassistant.const")
    const.STATE_HOME = "home"
    const.STATE_ON = "on"
    const.STATE_UNAVAILABLE = "unavailable"
    const.STATE_UNKNOWN = "unknown"
    sys.modules["homeassistant.const"] = const
    homeassistant.const = const

    helpers = types.ModuleType("homeassistant.helpers")
    sys.modules["homeassistant.helpers"] = helpers
    homeassistant.helpers = helpers

    helpers_event = types.ModuleType("homeassistant.helpers.event")

    def async_call_later(hass, delay, action):
        def _unsub():
            return None

        return _unsub

    helpers_event.async_call_later = async_call_later
    sys.modules["homeassistant.helpers.event"] = helpers_event
    helpers.event = helpers_event

    util = types.ModuleType("homeassistant.util")
    dt_util = types.ModuleType("homeassistant.util.dt")

    def utcnow():
        return datetime.now(timezone.utc)

    dt_util.utcnow = utcnow
    sys.modules["homeassistant.util"] = util
    sys.modules["homeassistant.util.dt"] = dt_util
    util.dt = dt_util
    homeassistant.util = util


_install_fake_homeassistant()

import custom_components.vesta.manager as manager_module
from custom_components.vesta.manager import PresenceManager, WindowManager


class FakeState:
    def __init__(self, state):
        self.state = state


class FakeStates:
    def __init__(self, data=None):
        self._data = data or {}

    def get(self, entity_id):
        return self._data.get(entity_id)

    def set(self, entity_id, state):
        self._data[entity_id] = state


class FakeHass:
    def __init__(self, states):
        self.states = states


@pytest.mark.asyncio
async def test_window_manager_slow_drop_stays_closed(monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    clock = {"now": now}
    monkeypatch.setattr(
        manager_module.dt_util, "utcnow", lambda: clock["now"]
    )

    hass = FakeHass(FakeStates())
    manager = WindowManager(
        hass,
        window_sensors=[],
        window_threshold=0.2,
        hold_duration=timedelta(minutes=15),
    )

    manager.record_temperature(20.0)
    clock["now"] = now + timedelta(minutes=1)
    triggered = manager.record_temperature(19.95)

    assert triggered is False
    assert manager.is_forced_off() is False


@pytest.mark.asyncio
async def test_window_manager_fast_drop_holds_and_clears(monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    clock = {"now": now}
    monkeypatch.setattr(
        manager_module.dt_util, "utcnow", lambda: clock["now"]
    )

    scheduled = {}

    def fake_async_call_later(_hass, delay, action):
        scheduled["delay"] = delay
        scheduled["action"] = action

        def _unsub():
            scheduled["cancelled"] = True

        return _unsub

    monkeypatch.setattr(manager_module, "async_call_later", fake_async_call_later)

    hass = FakeHass(FakeStates())
    manager = WindowManager(
        hass,
        window_sensors=[],
        window_threshold=0.2,
        hold_duration=timedelta(minutes=15),
    )

    manager.record_temperature(20.0)
    clock["now"] = now + timedelta(minutes=1)
    triggered = manager.record_temperature(18.0)

    assert triggered is True
    assert manager.is_forced_off() is True

    await scheduled["action"](clock["now"] + timedelta(minutes=15))

    assert manager.is_forced_off() is False
    assert manager.window_hold_until is None


def test_presence_manager_motion_on():
    states = FakeStates(
        {
            "binary_sensor.motion": FakeState("on"),
            "zone.home": FakeState("1"),
            "switch.vesta_guest_mode": FakeState("off"),
        }
    )
    hass = FakeHass(states)
    manager = PresenceManager(
        hass,
        area_name="Bedroom",
        slug="bedroom",
        presence_sensors=["binary_sensor.motion"],
        distance_sensors=[],
        bermuda_threshold=2.5,
        guest_entity_id="switch.vesta_guest_mode",
        home_entity_id="zone.home",
    )

    manager.refresh_state()

    assert manager.is_present() is True


def test_presence_manager_zone_home_keeps_presence_true():
    states = FakeStates(
        {
            "binary_sensor.motion": FakeState("off"),
            "zone.home": FakeState("1"),
            "switch.vesta_guest_mode": FakeState("off"),
        }
    )
    hass = FakeHass(states)
    manager = PresenceManager(
        hass,
        area_name="Bedroom",
        slug="bedroom",
        presence_sensors=["binary_sensor.motion"],
        distance_sensors=["zone.home"],
        bermuda_threshold=2.5,
        guest_entity_id="switch.vesta_guest_mode",
        home_entity_id="zone.home",
    )

    manager.refresh_state()

    assert manager.is_present() is True
