"""Device triggers for Vesta."""

from __future__ import annotations

from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_PLATFORM,
    CONF_TYPE,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import event as event_helper
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, DOMAIN_EVENT, TYPE_FAILURE, TYPE_PREHEAT, TYPE_WINDOW

TRIGGER_TYPES = (TYPE_PREHEAT, TYPE_WINDOW, TYPE_FAILURE)


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, str]]:
    """Return device triggers for Vesta devices."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return []
    if DOMAIN not in {identifier[0] for identifier in device.identifiers}:
        return []
    return [
        {
            CONF_PLATFORM: "device",
            CONF_DOMAIN: DOMAIN,
            CONF_DEVICE_ID: device_id,
            CONF_TYPE: trigger_type,
        }
        for trigger_type in TRIGGER_TYPES
    ]


@callback
def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action,
    trigger_info,
):
    """Attach a device trigger."""

    device_id = config[CONF_DEVICE_ID]
    trigger_type = config[CONF_TYPE]

    @callback
    def _handle_event(event):
        if event.data.get(CONF_DEVICE_ID) != device_id:
            return
        if event.data.get(CONF_TYPE) != trigger_type:
            return
        hass.async_create_task(
            action(
                {
                    "trigger": {
                        CONF_PLATFORM: "device",
                        CONF_DOMAIN: DOMAIN,
                        CONF_DEVICE_ID: device_id,
                        CONF_TYPE: trigger_type,
                        "event": event.data,
                    }
                }
            )
        )

    return event_helper.async_track_event(hass, DOMAIN_EVENT, _handle_event)
