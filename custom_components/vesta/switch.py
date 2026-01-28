"""Switch entities for Vesta."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Vesta switches from a config entry."""
    coordinator = hass.data[DOMAIN]["coordinator"]
    async_add_entities([
        VestaGuestModeSwitch(),
        VestaMasterHeatingSwitch(coordinator),
    ])


class _BaseVestaSwitch(SwitchEntity, RestoreEntity):
    """Base class for Vesta switches."""

    _attr_has_entity_name = False

    def __init__(self, name: str, unique_id: str, default_on: bool = False):
        self._attr_name = name
        self._attr_unique_id = unique_id
        self._attr_is_on = default_on

    async def async_added_to_hass(self) -> None:
        last_state = await self.async_get_last_state()
        if last_state is not None:
            self._attr_is_on = last_state.state == "on"

    async def async_turn_on(self, **kwargs) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()


class VestaGuestModeSwitch(_BaseVestaSwitch):
    """Guest mode switch."""

    def __init__(self):
        super().__init__("Vesta Guest Mode", "vesta_guest_mode")


class VestaMasterHeatingSwitch(_BaseVestaSwitch):
    """Master heating switch."""

    def __init__(self, coordinator):
        super().__init__("Vesta Master Heating", "vesta_master_heating", True)
        self._coordinator = coordinator

    async def async_turn_on(self, **kwargs) -> None:
        await super().async_turn_on(**kwargs)
        await self._coordinator.async_recalculate()

    async def async_turn_off(self, **kwargs) -> None:
        await super().async_turn_off(**kwargs)
        await self._coordinator.async_recalculate()
