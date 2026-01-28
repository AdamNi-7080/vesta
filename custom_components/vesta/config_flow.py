"""Config flow for Vesta integration."""

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import selector

from .const import (
    CONF_BOILER_ENTITY,
    CONF_BOOST_TEMP,
    CONF_BERMUDA_THRESHOLD,
    CONF_MIN_CYCLE,
    CONF_MAINTENANCE_DAY,
    CONF_MAINTENANCE_TIME,
    CONF_VALVE_MAINTENANCE,
    CONF_WEATHER_ENTITY,
    DEFAULT_BOOST_TEMP,
    DEFAULT_BERMUDA_THRESHOLD,
    DEFAULT_MAINTENANCE_DAY,
    DEFAULT_MAINTENANCE_TIME,
    DEFAULT_MIN_CYCLE,
    DEFAULT_VALVE_MAINTENANCE,
    DOMAIN,
    MAINTENANCE_DAY_BY_INDEX,
    MAINTENANCE_DAY_INDEX_BY_NAME,
)


class VestaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Vesta."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry):
        return VestaOptionsFlowHandler(config_entry)

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            if any(value is None for value in user_input.values()):
                errors["base"] = "missing"
            else:
                return self.async_create_entry(title="Vesta", data=user_input)

        data_schema = vol.Schema(
            {
                vol.Required(CONF_BOILER_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain=["climate", "switch", "input_boolean"]
                    )
                ),
                vol.Required(CONF_WEATHER_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=["weather"])
                ),
                vol.Optional(CONF_BOOST_TEMP, default=DEFAULT_BOOST_TEMP): vol.All(
                    vol.Coerce(int), vol.Range(min=15, max=30)
                ),
                vol.Optional(CONF_MIN_CYCLE, default=DEFAULT_MIN_CYCLE): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=15)
                ),
                vol.Optional(
                    CONF_VALVE_MAINTENANCE, default=DEFAULT_VALVE_MAINTENANCE
                ): selector.BooleanSelector(),
                vol.Optional(
                    CONF_BERMUDA_THRESHOLD, default=DEFAULT_BERMUDA_THRESHOLD
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        max=20,
                        step=0.1,
                        mode=selector.NumberSelectorMode.BOX,
                        unit_of_measurement="m",
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="user", data_schema=data_schema, errors=errors
        )


class VestaOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle Vesta options flow."""

    def __init__(self, config_entry):
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        errors = {}

        if user_input is not None:
            if any(value is None for value in user_input.values()):
                errors["base"] = "missing"
            else:
                return self.async_create_entry(title="", data=user_input)

        def _default(key):
            return self.config_entry.options.get(key, self.config_entry.data.get(key))

        day_default = _default(CONF_MAINTENANCE_DAY)
        if isinstance(day_default, str):
            day_default = MAINTENANCE_DAY_INDEX_BY_NAME.get(
                day_default.casefold(), DEFAULT_MAINTENANCE_DAY
            )
        if day_default is None:
            day_default = DEFAULT_MAINTENANCE_DAY

        time_default = (
            _default(CONF_MAINTENANCE_TIME) or DEFAULT_MAINTENANCE_TIME
        )

        data_schema = vol.Schema(
            {
                vol.Optional(CONF_BOOST_TEMP, default=_default(CONF_BOOST_TEMP)): vol.All(
                    vol.Coerce(int), vol.Range(min=15, max=30)
                ),
                vol.Optional(CONF_MIN_CYCLE, default=_default(CONF_MIN_CYCLE)): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=15)
                ),
                vol.Optional(
                    CONF_VALVE_MAINTENANCE,
                    default=_default(CONF_VALVE_MAINTENANCE),
                ): selector.BooleanSelector(),
                vol.Optional(
                    CONF_BERMUDA_THRESHOLD,
                    default=_default(CONF_BERMUDA_THRESHOLD),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        max=20,
                        step=0.1,
                        mode=selector.NumberSelectorMode.BOX,
                        unit_of_measurement="m",
                    )
                ),
                vol.Optional(
                    CONF_WEATHER_ENTITY,
                    default=_default(CONF_WEATHER_ENTITY),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=["weather"])
                ),
                vol.Optional(
                    CONF_MAINTENANCE_TIME,
                    default=time_default,
                ): selector.TimeSelector(),
                vol.Optional(
                    CONF_MAINTENANCE_DAY,
                    default=day_default,
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            {"label": name, "value": index}
                            for index, name in MAINTENANCE_DAY_BY_INDEX.items()
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="init", data_schema=data_schema, errors=errors
        )
