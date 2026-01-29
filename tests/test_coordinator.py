import asyncio
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
    const.ATTR_ENTITY_ID = "entity_id"
    const.ATTR_TEMPERATURE = "temperature"
    const.STATE_ON = "on"
    const.STATE_OFF = "off"
    const.STATE_UNAVAILABLE = "unavailable"
    const.STATE_UNKNOWN = "unknown"
    sys.modules["homeassistant.const"] = const
    homeassistant.const = const

    components = types.ModuleType("homeassistant.components")
    sys.modules["homeassistant.components"] = components
    homeassistant.components = components

    climate = types.ModuleType("homeassistant.components.climate")
    sys.modules["homeassistant.components.climate"] = climate
    components.climate = climate

    climate_const = types.ModuleType("homeassistant.components.climate.const")

    class HVACMode:
        HEAT = "heat"
        OFF = "off"

    climate_const.HVACMode = HVACMode
    climate_const.ATTR_HVAC_MODES = "hvac_modes"
    climate_const.SERVICE_SET_HVAC_MODE = "set_hvac_mode"
    climate_const.SERVICE_SET_TEMPERATURE = "set_temperature"
    sys.modules["homeassistant.components.climate.const"] = climate_const
    climate.const = climate_const

    core = types.ModuleType("homeassistant.core")

    class HomeAssistant:
        pass

    core.HomeAssistant = HomeAssistant
    sys.modules["homeassistant.core"] = core
    homeassistant.core = core

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

    helpers_update = types.ModuleType("homeassistant.helpers.update_coordinator")

    class DataUpdateCoordinator:
        def __init__(self, hass, logger, name=None):
            self.hass = hass

    helpers_update.DataUpdateCoordinator = DataUpdateCoordinator
    sys.modules["homeassistant.helpers.update_coordinator"] = helpers_update
    helpers.update_coordinator = helpers_update

    area_registry = types.ModuleType("homeassistant.helpers.area_registry")
    device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")
    label_registry = types.ModuleType("homeassistant.helpers.label_registry")
    config_validation = types.ModuleType("homeassistant.helpers.config_validation")
    sys.modules["homeassistant.helpers.area_registry"] = area_registry
    sys.modules["homeassistant.helpers.device_registry"] = device_registry
    sys.modules["homeassistant.helpers.entity_registry"] = entity_registry
    sys.modules["homeassistant.helpers.label_registry"] = label_registry
    sys.modules["homeassistant.helpers.config_validation"] = config_validation
    helpers.area_registry = area_registry
    helpers.device_registry = device_registry
    helpers.entity_registry = entity_registry
    helpers.label_registry = label_registry
    helpers.config_validation = config_validation

    util = types.ModuleType("homeassistant.util")
    dt_util = types.ModuleType("homeassistant.util.dt")

    def utcnow():
        return datetime.now(timezone.utc)

    dt_util.utcnow = utcnow
    util.slugify = lambda value: str(value).strip().lower().replace(" ", "_")
    sys.modules["homeassistant.util"] = util
    sys.modules["homeassistant.util.dt"] = dt_util
    util.dt = dt_util
    homeassistant.util = util

    voluptuous = types.ModuleType("voluptuous")
    sys.modules["voluptuous"] = voluptuous


_install_fake_homeassistant()

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE

from custom_components.vesta.const import CONF_BOILER_ENTITY, CONF_MIN_CYCLE
from custom_components.vesta.coordinator import BoilerCoordinator, MASTER_SWITCH_ENTITY
from custom_components.vesta import coordinator as coordinator_module
from custom_components.vesta.commands import CommandResult


@pytest.fixture(autouse=True)
def _stub_async_call_later(monkeypatch):
    monkeypatch.setattr(
        coordinator_module,
        "async_call_later",
        lambda hass, delay, action: (lambda: None),
    )


class FakeState:
    def __init__(self, state, attributes=None):
        self.state = state
        self.attributes = attributes or {}


class FakeStates:
    def __init__(self, data=None):
        self._data = data or {}

    def get(self, entity_id):
        return self._data.get(entity_id)

    def set(self, entity_id, state):
        self._data[entity_id] = state


class FakeServices:
    def __init__(self, available=None):
        self.calls = []
        self._available = set(available or [])

    def has_service(self, domain, service):
        if not self._available:
            return True
        return (domain, service) in self._available or domain in self._available

    async def async_call(self, domain, service, data, blocking=True):
        self.calls.append((domain, service, data, blocking))


class FakeHass:
    def __init__(self, states, services):
        self.states = states
        self.services = services

    def async_create_task(self, coro):
        return coro


class FakeEntry:
    def __init__(self, data, options=None):
        self.data = data
        self.options = options or {}


def _make_coordinator(hass, *, boiler_entity="switch.boiler", min_cycle=1):
    entry = FakeEntry({CONF_BOILER_ENTITY: boiler_entity, CONF_MIN_CYCLE: min_cycle})
    return BoilerCoordinator(hass, entry)


def test_coordinator_fires_on_demand(monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(coordinator_module.dt_util, "utcnow", lambda: now)

    states = FakeStates(
        {
            MASTER_SWITCH_ENTITY: FakeState(STATE_ON),
            "switch.boiler": FakeState(STATE_OFF),
        }
    )
    services = FakeServices()
    hass = FakeHass(states, services)
    coordinator = _make_coordinator(hass)

    asyncio.run(coordinator.async_update_demand("zone1", True, immediate=True))

    assert coordinator._state.name == "firing"


def test_coordinator_enters_anti_cycle_on_cancel(monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(coordinator_module.dt_util, "utcnow", lambda: now)

    states = FakeStates(
        {
            MASTER_SWITCH_ENTITY: FakeState(STATE_ON),
            "switch.boiler": FakeState(STATE_OFF),
        }
    )
    services = FakeServices()
    hass = FakeHass(states, services)
    coordinator = _make_coordinator(hass, min_cycle=1)

    asyncio.run(coordinator.async_update_demand("zone1", True, immediate=True))
    asyncio.run(coordinator.async_update_demand("zone1", False, immediate=True))

    assert coordinator._state.name == "anti_cycle_cooldown"
    assert coordinator._cooldown_until is not None
    assert coordinator._cooldown_until > now


def test_coordinator_holds_demand_during_anti_cycle(monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    time_controller = {"now": now}
    monkeypatch.setattr(
        coordinator_module.dt_util, "utcnow", lambda: time_controller["now"]
    )

    states = FakeStates(
        {
            MASTER_SWITCH_ENTITY: FakeState(STATE_ON),
            "switch.boiler": FakeState(STATE_OFF),
        }
    )
    services = FakeServices()
    hass = FakeHass(states, services)
    coordinator = _make_coordinator(hass, min_cycle=1)

    asyncio.run(coordinator.async_update_demand("zone1", True, immediate=True))
    asyncio.run(coordinator.async_update_demand("zone1", False, immediate=True))

    calls_before = len(services.calls)
    asyncio.run(coordinator.async_update_demand("zone1", True, immediate=True))

    assert coordinator._state.name == "anti_cycle_cooldown"
    assert len(services.calls) == calls_before

    time_controller["now"] = now + timedelta(minutes=1, seconds=1)
    asyncio.run(coordinator.async_recalculate())
    assert coordinator._state.name == "firing"
    assert len(services.calls) == calls_before + 1


def test_circuit_breaker_blocks_after_failures(monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    time_controller = {"now": now}
    monkeypatch.setattr(
        coordinator_module.dt_util, "utcnow", lambda: time_controller["now"]
    )

    states = FakeStates(
        {
            MASTER_SWITCH_ENTITY: FakeState(STATE_ON),
            "switch.boiler": FakeState(STATE_UNAVAILABLE),
        }
    )
    services = FakeServices()
    hass = FakeHass(states, services)
    coordinator = _make_coordinator(hass)

    calls = []

    async def fake_execute(command, propagate=False):
        calls.append(command)
        return CommandResult(False, error="entity unavailable")

    coordinator._command_executor.execute = fake_execute

    asyncio.run(coordinator._turn_boiler_on())
    asyncio.run(coordinator._turn_boiler_on())
    asyncio.run(coordinator._turn_boiler_on())
    asyncio.run(coordinator._turn_boiler_on())

    assert len(calls) == 3

    time_controller["now"] = now + timedelta(seconds=300)
    asyncio.run(coordinator._turn_boiler_on())
    assert len(calls) == 4


def test_master_switch_unavailable_defaults_to_safety_on(monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(coordinator_module.dt_util, "utcnow", lambda: now)

    states = FakeStates(
        {
            MASTER_SWITCH_ENTITY: FakeState(STATE_UNAVAILABLE),
            "switch.boiler": FakeState(STATE_OFF),
        }
    )
    services = FakeServices()
    hass = FakeHass(states, services)
    coordinator = _make_coordinator(hass)

    asyncio.run(coordinator.async_update_demand("zone1", True, immediate=True))

    assert coordinator._state.name == "firing"
