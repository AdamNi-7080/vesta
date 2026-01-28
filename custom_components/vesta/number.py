"""Number entities for Vesta."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity
from homeassistant.const import STATE_UNKNOWN, STATE_UNAVAILABLE
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DEFAULT_ECO_TEMP, DOMAIN, EVENT_SCHEDULE_UPDATE


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Vesta number entities from a config entry."""
    data = hass.data[DOMAIN]
    entities: list[NumberEntity] = []

    for area in data.get("areas", {}).values():
        entities.append(VestaScheduleNumber(area))

    entities.append(VestaEcoTempNumber())

    async_add_entities(entities)


class VestaScheduleNumber(NumberEntity, RestoreEntity):
    """Schedule target number for an area."""

    _attr_has_entity_name = False
    _attr_native_min_value = 5
    _attr_native_max_value = 30
    _attr_native_step = 0.5
    _attr_unit_of_measurement = "°C"

    def __init__(self, area: dict):
        self._area_id = area["id"]
        self._area_name = area["name"]
        self._attr_name = f"{self._area_name} Schedule Target"
        self._attr_unique_id = f"vesta_{self._area_id}_schedule_target"
        self._attr_native_value = None

    async def async_added_to_hass(self) -> None:
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            try:
                self._attr_native_value = float(last_state.state)
            except ValueError:
                self._attr_native_value = None

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = float(value)
        self.async_write_ha_state()
        self.hass.bus.async_fire(
            EVENT_SCHEDULE_UPDATE,
            {"area_id": self._area_id, "target": float(value)},
        )


class VestaEcoTempNumber(NumberEntity, RestoreEntity):
    """Global eco temperature for away mode."""

    _attr_has_entity_name = False
    _attr_name = "Vesta Eco Temp"
    _attr_unique_id = "vesta_eco_temp"
    _attr_native_min_value = 5
    _attr_native_max_value = 25
    _attr_native_step = 0.5
    _attr_unit_of_measurement = "°C"

    def __init__(self):
        self._attr_native_value = DEFAULT_ECO_TEMP

    async def async_added_to_hass(self) -> None:
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            try:
                self._attr_native_value = float(last_state.state)
            except ValueError:
                self._attr_native_value = DEFAULT_ECO_TEMP

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = float(value)
        self.async_write_ha_state()
