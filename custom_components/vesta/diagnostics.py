"""Diagnostics support for Vesta."""

from __future__ import annotations

import copy

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict:
    """Return diagnostics for a config entry."""
    data = hass.data.get(DOMAIN, {})
    areas = copy.deepcopy(data.get("areas", {}))
    learning = copy.deepcopy(getattr(data.get("learning"), "_data", {}))
    return {
        "areas": areas,
        "learning": learning,
    }
