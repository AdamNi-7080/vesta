from __future__ import annotations

from datetime import timedelta

import pytest

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
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


async def _create_area_entities(hass, *, area_name: str) -> str:
    area_reg = ar.async_get(hass)
    area = area_reg.async_create(area_name)
    ent_reg = er.async_get(hass)

    ent_reg.async_get_or_create(
        "climate",
        "test",
        f"{area_name}_trv",
        suggested_object_id=f"{area_name.lower()}_trv",
        area_id=area.id,
    )

    ent_reg.async_get_or_create(
        "sensor",
        "test",
        f"{area_name}_temp",
        suggested_object_id=f"{area_name.lower()}_temp",
        area_id=area.id,
        original_device_class="temperature",
    )

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

    hass.states.async_set("sensor.bedroom_temp", 18.0)
    hass.states.async_set("climate.bedroom_trv", "heat")
    hass.states.async_set("switch.boiler", STATE_OFF)
    hass.states.async_set("weather.home", "cloudy", {"temperature": 5.0})
    hass.states.async_set("switch.vesta_master_heating", STATE_ON)

    climate_temp_calls = async_mock_service(hass, "climate", "set_temperature")
    async_mock_service(hass, "climate", "set_hvac_mode")
    switch_on_calls = async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")

    await _setup_vesta_entry(
        hass,
        boiler_entity="switch.boiler",
        weather_entity="weather.home",
    )

    climate_temp_calls.clear()
    switch_on_calls.clear()

    hass.states.async_set("sensor.bedroom_temp", 10.0)
    await hass.async_block_till_done()

    now = dt_util.utcnow()
    async_fire_time_changed(hass, now + timedelta(seconds=6))
    await hass.async_block_till_done()
    async_fire_time_changed(hass, now + timedelta(seconds=12))
    await hass.async_block_till_done()

    assert any(
        "climate.bedroom_trv" in call.data.get("entity_id", [])
        for call in climate_temp_calls
    )
    assert any(
        call.data.get("entity_id") == "switch.boiler" for call in switch_on_calls
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

    ent_reg.async_get_or_create(
        "climate",
        "test",
        "office_trv",
        suggested_object_id="office_trv",
        area_id=area.id,
    )

    ent_reg.async_get_or_create(
        "sensor",
        "test",
        "office_temp",
        suggested_object_id="office_temp",
        area_id=area.id,
        original_device_class="temperature",
    )

    ignored_entry = ent_reg.async_get_or_create(
        "sensor",
        "test",
        "office_ignored_temp",
        suggested_object_id="office_ignored_temp",
        area_id=area.id,
        original_device_class="temperature",
    )
    ent_reg.async_update_entity(ignored_entry.entity_id, labels={label.label_id})

    hass.states.async_set("sensor.office_temp", 19.0)
    hass.states.async_set("sensor.office_ignored_temp", 21.0)
    hass.states.async_set("climate.office_trv", "heat")
    hass.states.async_set("switch.boiler", STATE_OFF)
    hass.states.async_set("weather.home", "cloudy", {"temperature": 5.0})

    async_mock_service(hass, "climate", "set_hvac_mode")
    async_mock_service(hass, "climate", "set_temperature")
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")

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
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_BOILER_ENTITY: None,
            CONF_WEATHER_ENTITY: "weather.home",
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["base"] == "missing"
