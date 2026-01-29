from __future__ import annotations

import sys

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

_ha_module = sys.modules.get("homeassistant")
if _ha_module is not None and not hasattr(_ha_module, "config_entries"):
    pytest.skip(
        "homeassistant package not available (stubbed module detected)",
        allow_module_level=True,
    )
pytest.importorskip("homeassistant")

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from homeassistant.core import EVENT_CALL_SERVICE
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_mock_service,
)

from custom_components.vesta.const import (
    CONF_BERMUDA_THRESHOLD,
    CONF_BOILER_ENTITY,
    CONF_BOOST_TEMP,
    CONF_MIN_CYCLE,
    CONF_VALVE_MAINTENANCE,
    CONF_WEATHER_ENTITY,
    DOMAIN,
)


def _prepare_boiler(hass, *, entity_id: str = "switch.boiler"):
    hass.states.async_set(entity_id, STATE_OFF)
    switch_on_calls = async_mock_service(hass, "switch", "turn_on")
    switch_off_calls = async_mock_service(hass, "switch", "turn_off")
    return switch_on_calls, switch_off_calls


def _extract_event_entity_ids(event) -> list[str]:
    data = event.data.get("service_data", {})
    entity_ids = data.get("entity_id")
    if entity_ids is None:
        return []
    if isinstance(entity_ids, str):
        return [entity_ids]
    return list(entity_ids)


async def _create_area_entities(hass, *, area_name: str) -> str:
    area_reg = ar.async_get(hass)
    area = area_reg.async_create(area_name)
    ent_reg = er.async_get(hass)

    trv_entry = ent_reg.async_get_or_create(
        "climate",
        "test",
        f"{area_name}_trv",
        suggested_object_id=f"{area_name.lower()}_trv",
    )
    ent_reg.async_update_entity(trv_entry.entity_id, area_id=area.id)

    temp_entry = ent_reg.async_get_or_create(
        "sensor",
        "test",
        f"{area_name}_temp",
        suggested_object_id=f"{area_name.lower()}_temp",
        original_device_class="temperature",
    )
    ent_reg.async_update_entity(temp_entry.entity_id, area_id=area.id)

    return area.id


async def _setup_vesta_entry(hass, *, boiler_entity: str, weather_entity: str):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_BOILER_ENTITY: boiler_entity,
            CONF_WEATHER_ENTITY: weather_entity,
        },
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.mark.asyncio
async def test_integration_heating_loop(hass):
    await _create_area_entities(hass, area_name="Bedroom")
    events = async_capture_events(hass, EVENT_CALL_SERVICE)

    hass.states.async_set("sensor.bedroom_temp", 10.0)
    hass.states.async_set("climate.bedroom_trv", "heat")
    hass.states.async_set("weather.home", "cloudy", {"temperature": 5.0})
    hass.states.async_set("switch.vesta_master_heating", STATE_ON)

    _prepare_boiler(hass)

    await _setup_vesta_entry(
        hass,
        boiler_entity="switch.boiler",
        weather_entity="weather.home",
    )

    await hass.services.async_call(
        DOMAIN,
        "set_schedule",
        {"area_name": "Bedroom", "target": 21},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert any(
        event.data.get("domain") == "climate"
        and event.data.get("service") == "set_temperature"
        and "climate.bedroom_trv" in _extract_event_entity_ids(event)
        for event in events
    )
    assert any(
        event.data.get("domain") == "switch"
        and event.data.get("service") == "turn_on"
        and "switch.boiler" in _extract_event_entity_ids(event)
        for event in events
    )


@pytest.mark.asyncio
async def test_ignore_label_excludes_sensor(hass):
    area_reg = ar.async_get(hass)
    area = area_reg.async_create("Office")
    ent_reg = er.async_get(hass)
    label_reg = lr.async_get(hass)

    label = None
    for candidate in label_reg.labels.values():
        if candidate.name.lower() == "vesta_ignore":
            label = candidate
            break
    if label is None:
        label = label_reg.async_create(
            name="vesta_ignore", color="#db4c4c", icon="mdi:eye-off"
        )

    trv_entry = ent_reg.async_get_or_create(
        "climate",
        "test",
        "office_trv",
        suggested_object_id="office_trv",
    )
    ent_reg.async_update_entity(trv_entry.entity_id, area_id=area.id)

    temp_entry = ent_reg.async_get_or_create(
        "sensor",
        "test",
        "office_temp",
        suggested_object_id="office_temp",
        original_device_class="temperature",
    )
    ent_reg.async_update_entity(temp_entry.entity_id, area_id=area.id)

    ignored_entry = ent_reg.async_get_or_create(
        "sensor",
        "test",
        "office_ignored_temp",
        suggested_object_id="office_ignored_temp",
        original_device_class="temperature",
    )
    ent_reg.async_update_entity(ignored_entry.entity_id, area_id=area.id)
    ent_reg.async_update_entity(ignored_entry.entity_id, labels={label.label_id})

    hass.states.async_set("sensor.office_temp", 19.0)
    hass.states.async_set("sensor.office_ignored_temp", 21.0)
    hass.states.async_set("climate.office_trv", "heat")
    hass.states.async_set("weather.home", "cloudy", {"temperature": 5.0})

    async_mock_service(hass, "climate", "set_hvac_mode")
    async_mock_service(hass, "climate", "set_temperature")
    _prepare_boiler(hass)

    await _setup_vesta_entry(
        hass,
        boiler_entity="switch.boiler",
        weather_entity="weather.home",
    )

    climate_states = [
        state
        for state in hass.states.async_all("climate")
        if state.attributes.get("vesta_temp_sensors") is not None
    ]
    assert climate_states, "Expected Vesta climate entity to be created"
    temp_sensors = climate_states[0].attributes.get("vesta_temp_sensors", [])
    assert "sensor.office_ignored_temp" not in temp_sensors
    assert "sensor.office_temp" in temp_sensors


@pytest.mark.asyncio
async def test_config_flow_creates_entry(hass):
    hass.states.async_set("weather.home", "cloudy", {"temperature": 5.0})
    _prepare_boiler(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM

    user_input = {
        CONF_BOILER_ENTITY: "switch.boiler",
        CONF_WEATHER_ENTITY: "weather.home",
        CONF_BOOST_TEMP: 25,
        CONF_MIN_CYCLE: 5,
        CONF_VALVE_MAINTENANCE: True,
        CONF_BERMUDA_THRESHOLD: 2.5,
    }
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=user_input
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_BOILER_ENTITY] == "switch.boiler"


@pytest.mark.asyncio
async def test_config_flow_rejects_missing(hass):
    hass.states.async_set("weather.home", "cloudy", {"temperature": 5.0})
    _prepare_boiler(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM

    from homeassistant.data_entry_flow import InvalidData

    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_BOILER_ENTITY: None,
                CONF_WEATHER_ENTITY: "weather.home",
            },
        )
