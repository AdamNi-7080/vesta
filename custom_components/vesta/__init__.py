"""Vesta integration initialization and discovery."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    entity_registry as er,
    label_registry as lr,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.util import slugify

from .const import (
    CONF_BOILER_ENTITY,
    DOMAIN,
    EVENT_SCHEDULE_UPDATE,
    PLATFORMS,
    SERVICE_SET_SCHEDULE,
)
from .coordinator import BoilerCoordinator
from .learning import VestaLearning

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry) -> bool:
    """Set up Vesta from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    data = hass.data[DOMAIN]

    data["config"] = {**entry.data, **entry.options}
    data["entry_id"] = entry.entry_id

    data["areas"] = _discover_areas(hass, entry.data)

    learning = VestaLearning(hass)
    await learning.async_load()
    data["learning"] = learning

    coordinator = BoilerCoordinator(hass, entry)
    data["coordinator"] = coordinator
    await coordinator.async_force_off()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    _register_services(hass)

    return True


def _discover_areas(hass: HomeAssistant, config: dict) -> dict[str, dict]:
    area_reg = ar.async_get(hass)
    entity_reg = er.async_get(hass)
    label_reg = lr.async_get(hass)
    boiler_entity = config.get(CONF_BOILER_ENTITY)
    skip_substrings = ("cpu", "processor", "chip", "battery", "device", "internal")
    ignore_label_ids = {
        label_id
        for label_id, label in label_reg.labels.items()
        if label.name.lower() == "vesta_ignore"
    }

    entities_by_area: dict[str, list[er.RegistryEntry]] = {}
    for entity in entity_reg.entities.values():
        if entity.area_id:
            entities_by_area.setdefault(entity.area_id, []).append(entity)

    areas: dict[str, dict] = {}
    for area in area_reg.areas.values():
        entries = entities_by_area.get(area.id, [])
        climate_entities: list[str] = []
        climate_device_ids: set[str] = set()
        temp_sensors: list[str] = []
        humidity_sensors: list[str] = []
        window_sensors: list[str] = []
        presence_sensors: list[str] = []
        generic_presence_sensors: list[str] = []
        calendar_entities: list[str] = []
        distance_sensors: list[str] = []
        battery_sensors: list[str] = []

        for entry in entries:
            if _has_ignore_label(entry, ignore_label_ids):
                continue
            if entry.domain == "climate" and entry.platform != DOMAIN:
                if boiler_entity and entry.entity_id == boiler_entity:
                    continue
                climate_entities.append(entry.entity_id)
                if entry.device_id:
                    climate_device_ids.add(entry.device_id)
                continue

            device_class = getattr(entry, "device_class", None) or getattr(
                entry, "original_device_class", None
            )
            if entry.domain == "sensor":
                if device_class == "temperature":
                    if boiler_entity and entry.entity_id == boiler_entity:
                        continue
                    entity_id_lower = entry.entity_id.lower()
                    if any(token in entity_id_lower for token in skip_substrings):
                        continue
                    temp_sensors.append(entry.entity_id)
                elif "distance" in entry.entity_id:
                    distance_sensors.append(entry.entity_id)
                elif device_class == "humidity":
                    humidity_sensors.append(entry.entity_id)
                elif device_class != "battery":
                    generic_presence_sensors.append(entry.entity_id)
            elif entry.domain == "binary_sensor" and device_class == "window":
                window_sensors.append(entry.entity_id)
            elif entry.domain == "binary_sensor" and device_class in (
                "presence",
                "occupancy",
            ):
                presence_sensors.append(entry.entity_id)
            elif entry.domain == "calendar":
                calendar_entities.append(entry.entity_id)

        if climate_device_ids:
            for entry in entity_reg.entities.values():
                if entry.device_id in climate_device_ids:
                    if _has_ignore_label(entry, ignore_label_ids):
                        continue
                    device_class = getattr(entry, "device_class", None) or getattr(
                        entry, "original_device_class", None
                    )
                    if device_class == "battery":
                        battery_sensors.append(entry.entity_id)

        if not climate_entities:
            continue

        areas[area.id] = {
            "id": area.id,
            "name": area.name,
            "slug": slugify(area.name),
            "climate_entities": climate_entities,
            "temp_sensors": temp_sensors,
            "humidity_sensors": humidity_sensors,
            "window_sensors": window_sensors,
            "presence_sensors": presence_sensors + generic_presence_sensors,
            "battery_sensors": sorted(set(battery_sensors)),
            "distance_sensors": sorted(set(distance_sensors)),
            "calendar_entity": sorted(calendar_entities)[0]
            if calendar_entities
            else None,
        }

    _LOGGER.debug("Discovered Vesta areas: %s", list(areas.keys()))
    return areas


def _has_ignore_label(entry: er.RegistryEntry, ignore_label_ids: set[str]) -> bool:
    labels = getattr(entry, "labels", None)
    if not labels or not ignore_label_ids:
        return False
    return bool(set(labels) & ignore_label_ids)


def _register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_SET_SCHEDULE):
        return

    schema = vol.Schema(
        {
            vol.Optional("area_id"): cv.string,
            vol.Optional("area_name"): cv.string,
            vol.Required("target"): vol.Coerce(float),
            vol.Optional("effective_at"): cv.datetime,
        }
    )

    async def _handle_set_schedule(call) -> None:
        data = hass.data.get(DOMAIN, {})
        areas = data.get("areas", {})

        area_id = call.data.get("area_id")
        if not area_id:
            area_name = call.data.get("area_name")
            if area_name:
                for area in areas.values():
                    if area["name"].lower() == area_name.lower():
                        area_id = area["id"]
                        break

        if not area_id or area_id not in areas:
            _LOGGER.warning("Vesta schedule update ignored: unknown area")
            return

        payload = {
            "area_id": area_id,
            "target": float(call.data["target"]),
        }

        effective_at = call.data.get("effective_at")
        if effective_at is not None:
            payload["effective_at"] = effective_at.isoformat()

        hass.bus.async_fire(EVENT_SCHEDULE_UPDATE, payload)

    hass.services.async_register(
        DOMAIN, SERVICE_SET_SCHEDULE, _handle_set_schedule, schema=schema
    )


async def async_reload_entry(hass: HomeAssistant, entry) -> None:
    """Reload Vesta config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)


async def async_unload_entry(hass: HomeAssistant, entry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.pop(DOMAIN, None)
    return unload_ok
