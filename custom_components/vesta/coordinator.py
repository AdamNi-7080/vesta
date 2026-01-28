"""Boiler coordinator for Vesta."""

from __future__ import annotations

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


class BoilerCoordinator(DataUpdateCoordinator):
    """Central safety controller for the boiler."""

    def __init__(self, hass: HomeAssistant, entry):
        super().__init__(hass, _LOGGER, name="vesta_boiler")
        self._boiler_entity = entry.data[CONF_BOILER_ENTITY]
        self._boost_temp = entry.data.get(CONF_BOOST_TEMP, DEFAULT_BOOST_TEMP)
        self._off_temp = entry.data.get(CONF_OFF_TEMP, DEFAULT_OFF_TEMP)
        self._min_cycle = entry.data.get(CONF_MIN_CYCLE, DEFAULT_MIN_CYCLE)
        self._demand: dict[str, bool] = {}
        self._last_off: dt_util.dt.datetime | None = None
        self._boiler_on = False
        self._retry_unsub = None
        self._master_state_warned = False

    async def async_update_demand(self, zone_id: str, demand: bool) -> None:
        """Update demand from a zone and recalculate boiler state."""
        if self._demand.get(zone_id) == demand:
            return
        self._demand[zone_id] = demand
        await self._recalculate()

    async def async_recalculate(self) -> None:
        """Public method to force a recalculation."""
        await self._recalculate()

    async def _recalculate(self) -> None:
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
            await self._turn_boiler_off()
            return

        if any(self._demand.values()):
            if self._can_turn_on():
                await self._turn_boiler_on()
            else:
                self._schedule_retry()
        else:
            await self._turn_boiler_off()

    def _can_turn_on(self) -> bool:
        if self._boiler_on:
            return True
        if self._last_off is None:
            return True
        delta = dt_util.utcnow() - self._last_off
        return delta.total_seconds() >= self._min_cycle * 60

    def _schedule_retry(self) -> None:
        if self._retry_unsub is not None:
            return
        if self._last_off is None:
            return
        remaining = (self._min_cycle * 60) - (
            dt_util.utcnow() - self._last_off
        ).total_seconds()
        if remaining <= 0:
            return

        async def _retry(_now):
            self._retry_unsub = None
            await self._recalculate()

        self._retry_unsub = async_call_later(self.hass, remaining, _retry)

    async def async_force_off(self) -> None:
        """Force the boiler to an off state and start anti-cycle cooldown."""
        await self._turn_boiler_off(force=True)

    async def _turn_boiler_on(self) -> None:
        if self._boiler_on:
            return
        domain = self._boiler_entity.split(".", 1)[0]
        if domain == "climate":
            await self.hass.services.async_call(
                "climate",
                SERVICE_SET_HVAC_MODE,
                {ATTR_ENTITY_ID: self._boiler_entity, "hvac_mode": HVACMode.HEAT},
                blocking=False,
            )
            await self.hass.services.async_call(
                "climate",
                SERVICE_SET_TEMPERATURE,
                {
                    ATTR_ENTITY_ID: self._boiler_entity,
                    ATTR_TEMPERATURE: self._boost_temp,
                },
                blocking=False,
            )
        else:
            await self.hass.services.async_call(
                domain,
                "turn_on",
                {ATTR_ENTITY_ID: self._boiler_entity},
                blocking=False,
            )
        self._boiler_on = True

    async def _turn_boiler_off(self, force: bool = False) -> None:
        if not self._boiler_on and not force:
            return
        domain = self._boiler_entity.split(".", 1)[0]
        if domain == "climate":
            state = self.hass.states.get(self._boiler_entity)
            hvac_modes = []
            if state is not None:
                hvac_modes = state.attributes.get(ATTR_HVAC_MODES, [])
            if HVACMode.OFF in hvac_modes:
                await self.hass.services.async_call(
                    "climate",
                    SERVICE_SET_HVAC_MODE,
                    {ATTR_ENTITY_ID: self._boiler_entity, "hvac_mode": HVACMode.OFF},
                    blocking=False,
                )
            await self.hass.services.async_call(
                "climate",
                SERVICE_SET_TEMPERATURE,
                {
                    ATTR_ENTITY_ID: self._boiler_entity,
                    ATTR_TEMPERATURE: self._off_temp,
                },
                blocking=False,
            )
        else:
            await self.hass.services.async_call(
                domain,
                "turn_off",
                {ATTR_ENTITY_ID: self._boiler_entity},
                blocking=False,
            )
        self._boiler_on = False
        self._last_off = dt_util.utcnow()
