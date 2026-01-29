"""Virtual thermostat entities for Vesta."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import (
    ATTR_TEMPERATURE,
    CONF_DEVICE_ID,
    CONF_TYPE,
    EVENT_HOMEASSISTANT_START,
    STATE_ON,
    STATE_OFF,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.core import CoreState
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .calendar_handler import CalendarHandler, _parse_effective_at
from .commands import SetTrvModeAndTempCommand, StandardValveControlStrategy
from .domain.climate import (
    calculate_temperature_compensation,
    compute_preheat_start,
)
from .manager import PresenceManager, WindowManager
from .target_modes import (
    BoostTargetMode,
    EcoTargetMode,
    FailsafeTargetMode,
    PreheatTargetMode,
    SaveTargetMode,
    ScheduledTargetMode,
    TargetContext,
    TargetMode,
)
from .const import (
    CONF_COMFORT_TEMP,
    CONF_MAINTENANCE_DAY,
    CONF_MAINTENANCE_TIME,
    CONF_OFF_TEMP,
    CONF_WEATHER_ENTITY,
    CONF_WINDOW_THRESHOLD,
    CONF_BERMUDA_THRESHOLD,
    CONF_VALVE_MAINTENANCE,
    DEFAULT_COMFORT_TEMP,
    DEFAULT_BERMUDA_THRESHOLD,
    DEFAULT_ECO_TEMP,
    DEFAULT_MAINTENANCE_DAY,
    DEFAULT_MAINTENANCE_TIME,
    DEFAULT_OFF_TEMP,
    DEFAULT_VALVE_MAINTENANCE,
    DEFAULT_WINDOW_THRESHOLD,
    DOMAIN,
    DOMAIN_EVENT,
    EVENT_SCHEDULE_UPDATE,
    MAINTENANCE_DAY_INDEX_BY_NAME,
    TYPE_FAILURE,
    TYPE_PREHEAT,
    TYPE_WINDOW,
)

BOOST_DURATION = timedelta(minutes=90)
WINDOW_HOLD_DURATION = timedelta(minutes=15)
OUTPUT_UPDATE_DEBOUNCE = timedelta(seconds=5)
CALENDAR_POLL_INTERVAL = timedelta(minutes=15)
VALVE_MAINTENANCE_HIGH = 30.0
VALVE_MAINTENANCE_LOW = 5.0
VALVE_MAINTENANCE_STEP = 120  # seconds
BATTERY_THRESHOLD = 5.0
FAILSAFE_TEMP = 15.0
HEALTH_CHECK_INTERVAL = timedelta(minutes=15)
TRV_WARNING_INTERVAL = timedelta(minutes=10)

GUEST_SWITCH = "switch.vesta_guest_mode"
MASTER_SWITCH = "switch.vesta_master_heating"
ECO_NUMBER = "number.vesta_eco_temp"
HOME_ZONE = "zone.home"

_LOGGER = logging.getLogger(__name__)


class ClimateState:
    async def set_temperature(self, climate: "VestaClimate", **kwargs) -> None:
        raise NotImplementedError

    async def set_hvac_mode(
        self, climate: "VestaClimate", hvac_mode: HVACMode
    ) -> None:
        raise NotImplementedError

    async def apply_output(
        self, climate: "VestaClimate", *, immediate_demand: bool
    ) -> None:
        raise NotImplementedError


class _OperationalState(ClimateState):
    async def set_temperature(self, climate: "VestaClimate", **kwargs) -> None:
        await climate._handle_set_temperature(**kwargs)

    async def set_hvac_mode(
        self, climate: "VestaClimate", hvac_mode: HVACMode
    ) -> None:
        await climate._handle_set_hvac_mode(hvac_mode)

    async def apply_output(
        self, climate: "VestaClimate", *, immediate_demand: bool
    ) -> None:
        await climate._apply_output_internal(immediate_demand=immediate_demand)


class _BatteryCriticalState(ClimateState):
    async def set_temperature(self, climate: "VestaClimate", **kwargs) -> None:
        _LOGGER.warning(
            "Vesta battery failsafe active for %s: ignoring manual override",
            climate._area_name,
        )

    async def set_hvac_mode(
        self, climate: "VestaClimate", hvac_mode: HVACMode
    ) -> None:
        _LOGGER.warning(
            "Vesta battery failsafe active for %s: ignoring HVAC mode change",
            climate._area_name,
        )

    async def apply_output(
        self, climate: "VestaClimate", *, immediate_demand: bool
    ) -> None:
        valid_trvs = climate._get_valid_trvs() if climate._trvs else []
        if climate._trvs and not valid_trvs:
            climate._warn_no_trvs()
            await climate._update_demand(None, immediate=immediate_demand)
            climate.async_write_ha_state()
            return
        await climate._set_trvs_temp(FAILSAFE_TEMP)
        await climate._update_demand(None, immediate=immediate_demand)
        climate.async_write_ha_state()


class _MaintenanceState(ClimateState):
    async def set_temperature(self, climate: "VestaClimate", **kwargs) -> None:
        await _OPERATIONAL_STATE.set_temperature(climate, **kwargs)

    async def set_hvac_mode(
        self, climate: "VestaClimate", hvac_mode: HVACMode
    ) -> None:
        await _OPERATIONAL_STATE.set_hvac_mode(climate, hvac_mode)

    async def apply_output(
        self, climate: "VestaClimate", *, immediate_demand: bool
    ) -> None:
        climate.async_write_ha_state()


_OPERATIONAL_STATE = _OperationalState()
_BATTERY_CRITICAL_STATE = _BatteryCriticalState()
_MAINTENANCE_STATE = _MaintenanceState()


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Vesta climate entities from a config entry."""
    data = hass.data[DOMAIN]
    coordinator = data["coordinator"]
    learning = data["learning"]
    config = data["config"]

    entities: list[VestaClimate] = []
    for area in data.get("areas", {}).values():
        entities.append(VestaClimate(hass, area, coordinator, learning, config))

    async_add_entities(entities)


class VestaClimate(ClimateEntity, RestoreEntity):
    """Virtual thermostat per Home Assistant Area."""

    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 0.5
    _attr_should_poll = False

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._zone_id)},
            "name": f"{self._area_name} Vesta",
            "manufacturer": "Vesta Community",
            "model": "Virtual Thermostat",
            "suggested_area": self._area_name,
        }

    def __init__(self, hass, area: dict, coordinator, learning, config: dict):
        self.hass = hass
        self._zone_id = area["id"]
        self._area_name = area["name"]
        self._slug = area["slug"]
        self._trvs = area["climate_entities"]
        self._temp_sensors = area["temp_sensors"]
        self._humidity_sensors = area.get("humidity_sensors", [])
        self._window_sensors = area["window_sensors"]
        self._presence_sensors = area["presence_sensors"]
        self._battery_sensors = area.get("battery_sensors", [])
        self._distance_sensors = area.get("distance_sensors", [])
        self._schedule_entity_id = f"number.{self._slug}_schedule_target"
        self._coordinator = coordinator
        self._command_executor = coordinator.command_executor
        self._valve_strategy = StandardValveControlStrategy()
        self._learning = learning
        self._weather_entity = config.get(CONF_WEATHER_ENTITY)
        self._calendar_entity = area.get("calendar_entity")
        self._off_temp = config.get(CONF_OFF_TEMP, DEFAULT_OFF_TEMP)
        self._comfort_temp = config.get(CONF_COMFORT_TEMP, DEFAULT_COMFORT_TEMP)
        self._window_threshold = config.get(
            CONF_WINDOW_THRESHOLD, DEFAULT_WINDOW_THRESHOLD
        )
        self._valve_maintenance = config.get(
            CONF_VALVE_MAINTENANCE, DEFAULT_VALVE_MAINTENANCE
        )
        maintenance_time = config.get(
            CONF_MAINTENANCE_TIME, DEFAULT_MAINTENANCE_TIME
        )
        maintenance_day = config.get(
            CONF_MAINTENANCE_DAY, DEFAULT_MAINTENANCE_DAY
        )
        if isinstance(maintenance_day, str):
            maintenance_day = MAINTENANCE_DAY_INDEX_BY_NAME.get(
                maintenance_day.casefold(), DEFAULT_MAINTENANCE_DAY
            )
        self._maintenance_time = maintenance_time
        self._maintenance_day = (
            maintenance_day
            if isinstance(maintenance_day, int)
            else DEFAULT_MAINTENANCE_DAY
        )
        self._bermuda_threshold = config.get(
            CONF_BERMUDA_THRESHOLD, DEFAULT_BERMUDA_THRESHOLD
        )

        self._attr_name = f"{self._area_name} Vesta"
        self._attr_unique_id = f"vesta_{self._zone_id}_climate"

        self._current_temperature: float | None = None
        self._current_humidity: float | None = None
        self._schedule_target: float | None = None
        self._override_mode: TargetMode | None = None
        self._user_hvac_off = False

        self._boost_unsub = None
        self._preheat_start_unsub = None
        self._preheat_apply_unsub = None
        self._maintenance_unsub = None
        self._maintenance_task = None
        self._maintenance_active = False

        self._demand = False
        self._preheat_active = False
        self._preheat_target: float | None = None
        self._preheat_effective_at: dt_util.dt.datetime | None = None
        self._pending_target: float | None = None
        self._pending_effective_at: dt_util.dt.datetime | None = None
        self._battery_lock = False
        self._health_state = "OK"
        self._demand_since: dt_util.dt.datetime | None = None
        self._demand_start_temp: float | None = None
        self._idle_since: dt_util.dt.datetime | None = None
        self._idle_start_temp: float | None = None
        self._last_trv_warning: dt_util.dt.datetime | None = None
        self._retry_unsub = None
        self._output_update_unsub = None
        self._startup_done = False

        self._window_manager = WindowManager(
            hass,
            window_sensors=self._window_sensors,
            window_threshold=self._window_threshold,
            hold_duration=WINDOW_HOLD_DURATION,
            on_hold_cleared=self._handle_window_hold_cleared,
            on_hold_triggered=self._handle_window_hold_triggered,
        )
        self._presence_manager = PresenceManager(
            hass,
            area_name=self._area_name,
            slug=self._slug,
            presence_sensors=self._presence_sensors,
            distance_sensors=self._distance_sensors,
            bermuda_threshold=self._bermuda_threshold,
            guest_entity_id=GUEST_SWITCH,
            home_entity_id=HOME_ZONE,
        )
        self._calendar_handler = (
            CalendarHandler(hass, self._calendar_entity)
            if self._calendar_entity
            else None
        )

        self._unsubs: list[callable] = []

    @property
    def current_temperature(self) -> float | None:
        return self._current_temperature

    @property
    def current_humidity(self) -> float | None:
        if not self._humidity_sensors:
            return None
        return self._current_humidity

    @property
    def target_temperature(self) -> float | None:
        return self._effective_target()

    @property
    def hvac_mode(self) -> HVACMode:
        if self._is_forced_off():
            return HVACMode.OFF
        return HVACMode.HEAT

    @property
    def hvac_action(self) -> HVACAction | None:
        if self.hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        return HVACAction.HEATING if self._demand else HVACAction.IDLE

    @property
    def extra_state_attributes(self) -> dict:
        temp_sources = self._temp_sensors if self._temp_sensors else self._trvs
        presence_sources = sorted(
            set(self._presence_sensors + self._distance_sensors)
        )
        heating_slope, heating_intercept = self._learning.get_heating_regression(
            self._zone_id
        )
        cooling_slope, cooling_intercept = self._learning.get_cooling_regression(
            self._zone_id
        )
        return {
            "vesta_active_trvs": list(self._get_valid_trvs()),
            "vesta_temp_sensors": list(temp_sources),
            "vesta_humidity_sensors": list(self._humidity_sensors),
            "vesta_window_sensors": list(self._window_sensors),
            "vesta_presence_sensors": list(presence_sources),
            "vesta_battery_sensors": list(self._battery_sensors),
            "vesta_calendar_entity": self._calendar_entity,
            "vesta_health": self._health_state,
            "vesta_heating_rate": self._learning.get_rate(
                self._zone_id, self._get_outdoor_temp(), self._is_sunny()
            ),
            "vesta_cooling_rate": self._learning.get_cooling_rate(
                self._zone_id, self._get_outdoor_temp(), self._is_sunny()
            ),
            "vesta_heating_slope": heating_slope,
            "vesta_heating_intercept": heating_intercept,
            "vesta_cooling_slope": cooling_slope,
            "vesta_cooling_intercept": cooling_intercept,
            "vesta_next_schedule_time": self._pending_effective_at,
            "vesta_next_schedule_target": self._pending_target,
            "vesta_is_preheating": self._preheat_active,
        }

    def _fire_event(self, event_type: str, data: dict | None = None) -> None:
        device_reg = dr.async_get(self.hass)
        device = device_reg.async_get_device(identifiers={(DOMAIN, self._zone_id)})
        payload = {
            CONF_TYPE: event_type,
            "entity_id": self.entity_id,
        }
        if device:
            payload[CONF_DEVICE_ID] = device.id
        if data:
            payload.update(data)
        self.hass.bus.async_fire(DOMAIN_EVENT, payload)

    def _handle_window_hold_triggered(self) -> None:
        _LOGGER.info("Window hold triggered for %s", self._area_name)
        self._fire_event(TYPE_WINDOW)

    async def _handle_window_hold_cleared(self) -> None:
        _LOGGER.info("Window hold cleared for %s", self._area_name)
        self._schedule_output_update()

    async def _handle_window_manager_update(self, _window_open: bool) -> None:
        _LOGGER.debug("Window state changed for %s", self._area_name)
        self._schedule_output_update()

    async def _handle_presence_manager_update(self, _presence_on: bool) -> None:
        _LOGGER.debug("Presence state changed for %s", self._area_name)
        self._schedule_output_update()

    def _schedule_output_update(
        self, *, immediate: bool = False, immediate_demand: bool = False
    ) -> None:
        if immediate:
            if self._output_update_unsub:
                self._output_update_unsub()
                self._output_update_unsub = None
            _LOGGER.debug(
                "Immediate output update for %s (immediate_demand=%s)",
                self._area_name,
                immediate_demand,
            )
            self.hass.async_create_task(
                self._apply_output(immediate_demand=immediate_demand)
            )
            return
        if self._output_update_unsub:
            return

        async def _run(_now):
            self._output_update_unsub = None
            await self._apply_output()

        _LOGGER.debug(
            "Debouncing output update for %s by %.0fs",
            self._area_name,
            OUTPUT_UPDATE_DEBOUNCE.total_seconds(),
        )
        self._output_update_unsub = async_call_later(
            self.hass, OUTPUT_UPDATE_DEBOUNCE.total_seconds(), _run
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._unsubs.append(
            self.hass.bus.async_listen(EVENT_SCHEDULE_UPDATE, self._handle_schedule)
        )

        tracked = set(
            self._temp_sensors
            + self._humidity_sensors
            + self._battery_sensors
            + self._trvs
            + [MASTER_SWITCH, ECO_NUMBER]
        )
        if tracked:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, list(tracked), self._handle_state_change
                )
            )

        self._window_manager.add_observer(self._handle_window_manager_update)
        self._presence_manager.add_observer(self._handle_presence_manager_update)
        self._window_manager.async_start_listeners()
        self._presence_manager.async_start_listeners()

        if self.hass.state == CoreState.running:
            await self.async_startup()
        else:
            async def _startup_listener(_event) -> None:
                await self.async_startup()

            self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_START,
                _startup_listener,
            )

    async def async_startup(self) -> None:
        if self._startup_done:
            return
        self._startup_done = True

        await self._load_schedule_target()
        self._presence_manager.refresh_state()
        self._window_manager.refresh_state()
        await self._refresh_battery_state()
        await self._update_current_temperature()
        await self._update_current_humidity()

        if self._calendar_handler:
            await self._poll_calendar(None)
            self._unsubs.append(
                async_track_time_interval(
                    self.hass, self._poll_calendar, CALENDAR_POLL_INTERVAL
                )
            )

        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._check_system_health, HEALTH_CHECK_INTERVAL
            )
        )

        if self._valve_maintenance:
            maintenance_time = self._maintenance_time_args()
            self._maintenance_unsub = async_track_time_change(
                self.hass, self._handle_maintenance_time, **maintenance_time
            )
            self._unsubs.append(self._maintenance_unsub)

        await self._apply_output(immediate_demand=True)

    async def async_will_remove_from_hass(self) -> None:
        for unsub in self._unsubs:
            unsub()
        if self._boost_unsub:
            self._boost_unsub()
            self._boost_unsub = None
        if self._retry_unsub:
            self._retry_unsub()
            self._retry_unsub = None
        if self._output_update_unsub:
            self._output_update_unsub()
            self._output_update_unsub = None
        self._window_manager.async_will_remove_from_hass()
        self._presence_manager.async_will_remove_from_hass()
        self._cancel_preheat()
        if self._maintenance_task:
            self._maintenance_task.cancel()
            self._maintenance_task = None

    async def async_set_temperature(self, **kwargs) -> None:
        await self._select_state().set_temperature(self, **kwargs)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self._select_state().set_hvac_mode(self, hvac_mode)

    async def _handle_set_temperature(self, **kwargs) -> None:
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        new_temp = float(temperature)
        schedule_target = self._schedule_target or self._off_temp

        _LOGGER.info(
            "Manual target request for %s: %s (schedule %s)",
            self._area_name,
            new_temp,
            schedule_target,
        )
        self._suppress_calendar_event()
        if new_temp > schedule_target:
            self._set_boost_override(new_temp)
        elif new_temp < schedule_target:
            self._set_save_override(new_temp)
        else:
            self._clear_override()

        await self._apply_output(immediate_demand=True)

    async def _handle_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self._user_hvac_off = hvac_mode == HVACMode.OFF
        if self._user_hvac_off:
            _LOGGER.info("HVAC turned off for %s", self._area_name)
            self._cancel_preheat()
        await self._apply_output(immediate_demand=True)

    async def _handle_schedule(self, event) -> None:
        if event.data.get("area_id") != self._zone_id:
            return
        target = event.data.get("target")
        if target is None:
            return
        if self._battery_lock:
            _LOGGER.warning(
                "Vesta battery failsafe active for %s: ignoring schedule update",
                self._area_name,
            )
            return
        effective_at = _parse_effective_at(
            self.hass, event.data.get("effective_at")
        )
        try:
            target_value = float(target)
        except (TypeError, ValueError):
            return
        _LOGGER.info(
            "Schedule update for %s: target=%s effective_at=%s",
            self._area_name,
            target_value,
            effective_at,
        )
        self._suppress_calendar_event()
        if effective_at and effective_at > dt_util.utcnow():
            await self._schedule_future_target(target_value, effective_at)
            return
        self._schedule_target = target_value
        self._cancel_preheat()
        if isinstance(self._override_mode, SaveTargetMode):
            self._clear_override()
        await self._apply_output(immediate_demand=True)

    async def _handle_state_change(self, event) -> None:
        entity_id = event.data.get("entity_id")
        _LOGGER.debug(
            "State change detected for %s (%s)",
            self._area_name,
            entity_id,
        )
        if entity_id in self._temp_sensors or entity_id in self._trvs:
            await self._update_current_temperature()
        elif entity_id in self._humidity_sensors:
            await self._update_current_humidity()
        elif entity_id in self._battery_sensors:
            if not await self._refresh_battery_state():
                return
        self._schedule_output_update()

    async def _load_schedule_target(self) -> None:
        state = self.hass.states.get(self._schedule_entity_id)
        if state and state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            try:
                self._schedule_target = float(state.state)
                return
            except ValueError:
                pass
        self._schedule_target = self._comfort_temp

    async def _update_current_temperature(self) -> None:
        temps: list[float] = []

        if self._temp_sensors:
            for entity_id in self._temp_sensors:
                temp = _state_to_float(self.hass.states.get(entity_id))
                if temp is not None:
                    temps.append(temp)
        if not temps and self._trvs:
            for entity_id in self._trvs:
                state = self.hass.states.get(entity_id)
                if state is None:
                    continue
                temp = state.attributes.get("current_temperature")
                if temp is not None:
                    try:
                        temps.append(float(temp))
                    except (TypeError, ValueError):
                        continue

        if temps:
            self._current_temperature = sum(temps) / len(temps)
            _LOGGER.debug(
                "Current temperature for %s: %.2f",
                self._area_name,
                self._current_temperature,
            )
            if not self._window_sensors:
                self._window_manager.record_temperature(self._current_temperature)

    async def _update_current_humidity(self) -> None:
        if not self._humidity_sensors:
            self._current_humidity = None
            return

        humidities: list[float] = []
        for entity_id in self._humidity_sensors:
            humidity = _state_to_float(self.hass.states.get(entity_id))
            if humidity is not None:
                humidities.append(humidity)

        if humidities:
            self._current_humidity = sum(humidities) / len(humidities)
        else:
            self._current_humidity = None

    async def _refresh_battery_state(self) -> bool:
        if not self._battery_sensors:
            return False
        low = False
        for entity_id in self._battery_sensors:
            value = _state_to_float(self.hass.states.get(entity_id))
            if value is None:
                continue
            if value < BATTERY_THRESHOLD:
                low = True
                break
        changed = False
        if low and not self._battery_lock:
            self._battery_lock = True
            changed = True
            self._cancel_preheat()
            if self._maintenance_task:
                self._maintenance_task.cancel()
                self._maintenance_task = None
            _LOGGER.warning(
                "Vesta battery failsafe active for %s: forcing TRVs to safety temp (15°C)",
                self._area_name,
            )
        elif not low and self._battery_lock:
            self._battery_lock = False
            changed = True
            await self._load_schedule_target()
        return changed

    def _set_boost_override(self, target: float) -> None:
        self._cancel_preheat()
        self._override_mode = BoostTargetMode(target)
        if self._boost_unsub:
            self._boost_unsub()

        async def _expire(_now):
            self._boost_unsub = None
            self._clear_override()
            self._schedule_output_update(
                immediate=True, immediate_demand=True
            )

        self._boost_unsub = async_call_later(
            self.hass, BOOST_DURATION.total_seconds(), _expire
        )

    def _set_save_override(self, target: float) -> None:
        self._cancel_preheat()
        if self._boost_unsub:
            self._boost_unsub()
            self._boost_unsub = None
        self._override_mode = SaveTargetMode(target)

    def _clear_override(self) -> None:
        if self._boost_unsub:
            self._boost_unsub()
            self._boost_unsub = None
        self._override_mode = None

    def _is_master_enabled(self) -> bool:
        state = self.hass.states.get(MASTER_SWITCH)
        if state is None:
            return True
        if state.state in (STATE_ON, STATE_UNKNOWN, STATE_UNAVAILABLE):
            return True
        if state.state not in (STATE_OFF, STATE_ON):
            _LOGGER.warning(
                "Master heating switch state %s is unexpected; treating as ON",
                state.state,
            )
            return True
        return False

    def _is_forced_off(self) -> bool:
        if self._battery_lock:
            return False
        if not self._is_master_enabled():
            return True
        if self._user_hvac_off:
            return True
        if self._window_manager.is_forced_off(dt_util.utcnow()):
            return True
        return False

    def _select_state(self) -> ClimateState:
        if self._battery_lock:
            return _BATTERY_CRITICAL_STATE
        if self._maintenance_active:
            return _MAINTENANCE_STATE
        return _OPERATIONAL_STATE

    def _target_context(self) -> TargetContext:
        return TargetContext(
            schedule_target=self._schedule_target,
            off_temp=self._off_temp,
            comfort_temp=self._comfort_temp,
            eco_temp=self._eco_temp(),
            has_presence_sensors=bool(self._presence_sensors),
            presence_on=self._presence_manager.is_present(),
        )

    def _select_target_mode(self) -> TargetMode:
        if self._battery_lock:
            return FailsafeTargetMode(VALVE_MAINTENANCE_HIGH)
        if self._override_mode is not None:
            return self._override_mode
        if self._preheat_active and self._preheat_target is not None:
            return PreheatTargetMode(self._preheat_target)
        if (
            not self._presence_manager.is_guest_mode()
            and not self._presence_manager.is_home()
        ):
            return EcoTargetMode()
        return ScheduledTargetMode()

    def _effective_target(self) -> float | None:
        mode = self._select_target_mode()
        return mode.calculate_final_target(self._target_context())

    async def _schedule_future_target(
        self, target: float, effective_at: dt_util.dt.datetime
    ) -> None:
        self._cancel_preheat()
        self._pending_target = target
        self._pending_effective_at = effective_at

        now = dt_util.utcnow()
        delay = (effective_at - now).total_seconds()
        if delay <= 0:
            self._schedule_target = target
            await self._apply_output(immediate_demand=True)
            return

        async def _apply_future(_now):
            self._preheat_apply_unsub = None
            await self._apply_future_target(target, effective_at)

        self._preheat_apply_unsub = async_call_later(
            self.hass, delay, _apply_future
        )

        allow_preheat = (
            self._presence_manager.is_home()
            or self._presence_manager.is_guest_mode()
        )
        rate = 0.0
        if allow_preheat:
            rate = self._learning.get_rate(
                self._zone_id, self._get_outdoor_temp(), self._is_sunny()
            )
        start_at = compute_preheat_start(
            current_temp=self._current_temperature,
            target_temp=target,
            effective_at=effective_at,
            heating_rate=rate,
            allow_preheat=allow_preheat,
        )
        if start_at is None:
            return
        if start_at <= now:
            await self._start_preheat(target, effective_at)
            return

        async def _start(_now):
            self._preheat_start_unsub = None
            await self._start_preheat(target, effective_at)

        self._preheat_start_unsub = async_call_later(
            self.hass, (start_at - now).total_seconds(), _start
        )

    async def _handle_maintenance_time(self, now) -> None:
        if now.weekday() != self._maintenance_day:
            return
        if self._maintenance_task and not self._maintenance_task.done():
            return
        self._maintenance_task = self.hass.async_create_task(
            self._run_valve_maintenance()
        )

    async def _run_valve_maintenance(self) -> None:
        if not self._valve_maintenance or self._battery_lock:
            return
        if self.hvac_action == HVACAction.HEATING:
            return
        if self._maintenance_active:
            return
        self._maintenance_active = True
        try:
            await self._set_trvs_temp(VALVE_MAINTENANCE_HIGH)
            await asyncio.sleep(VALVE_MAINTENANCE_STEP)
            await self._set_trvs_temp(VALVE_MAINTENANCE_LOW)
            await asyncio.sleep(VALVE_MAINTENANCE_STEP)
        finally:
            self._maintenance_active = False
            await self._apply_output(immediate_demand=True)

    async def _poll_calendar(self, _now) -> None:
        if not self._calendar_handler:
            return
        if self._battery_lock:
            return
        now = dt_util.utcnow()
        decision = await self._calendar_handler.async_poll(now)
        if decision is None:
            return
        if decision.is_active:
            if self._schedule_target != decision.target:
                _LOGGER.info(
                    "Found active calendar event: Setting target to %s",
                    decision.target,
                )
                await self._apply_future_target(decision.target, now)
            return
        await self._schedule_future_target(decision.target, decision.start)

    async def _start_preheat(
        self, target: float, effective_at: dt_util.dt.datetime
    ) -> None:
        if dt_util.utcnow() >= effective_at:
            return
        if self._battery_lock:
            return
        if self._override_mode is not None:
            return
        if self._current_temperature is not None and target <= self._current_temperature:
            return
        self._preheat_active = True
        self._preheat_target = target
        self._preheat_effective_at = effective_at
        self._fire_event(TYPE_PREHEAT, {"target": target})
        await self._apply_output(immediate_demand=True)

    async def _apply_future_target(
        self, target: float, effective_at: dt_util.dt.datetime
    ) -> None:
        self._preheat_active = False
        self._preheat_target = None
        self._preheat_effective_at = None
        self._pending_target = None
        self._pending_effective_at = None
        self._schedule_target = target

        await self.hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": self._schedule_entity_id, "value": target},
            blocking=False,
        )

        await self._apply_output(immediate_demand=True)

    def _cancel_preheat(self) -> None:
        if self._preheat_start_unsub:
            self._preheat_start_unsub()
            self._preheat_start_unsub = None
        if self._preheat_apply_unsub:
            self._preheat_apply_unsub()
            self._preheat_apply_unsub = None
        self._preheat_active = False
        self._preheat_target = None
        self._preheat_effective_at = None
        self._pending_target = None
        self._pending_effective_at = None

    def _suppress_calendar_event(self) -> None:
        if self._calendar_handler:
            self._calendar_handler.suppress_last_event()

    def _eco_temp(self) -> float:
        state = self.hass.states.get(ECO_NUMBER)
        if state and state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            try:
                return float(state.state)
            except ValueError:
                pass
        return DEFAULT_ECO_TEMP

    def _get_outdoor_temp(self) -> float | None:
        if not self._weather_entity:
            return None
        state = self.hass.states.get(self._weather_entity)
        if state is None:
            return None
        temp = state.attributes.get("temperature")
        if temp is None:
            return None
        try:
            return float(temp)
        except (TypeError, ValueError):
            return None

    def _is_sunny(self) -> bool:
        if not self._weather_entity:
            return False
        sun_state = self.hass.states.get("sun.sun")
        if sun_state and sun_state.state == "below_horizon":
            return False
        state = self.hass.states.get(self._weather_entity)
        if state is None:
            return False
        weather_state = str(state.state).casefold()
        if weather_state in ("clear-night", "partlycloudy-night"):
            return False
        if weather_state == "sunny":
            return True
        cloud_coverage = state.attributes.get("cloud_coverage")
        if cloud_coverage is None:
            return False
        try:
            return float(cloud_coverage) < 20
        except (TypeError, ValueError):
            return False

    def _get_valid_trvs(self) -> list[str]:
        valid: list[str] = []
        for entity_id in self._trvs:
            state = self.hass.states.get(entity_id)
            if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                _LOGGER.debug("Ignoring unreachable TRV: %s", entity_id)
                continue
            valid.append(entity_id)
        return valid

    def _warn_no_trvs(self) -> None:
        now = dt_util.utcnow()
        if (
            self._last_trv_warning is None
            or now - self._last_trv_warning >= TRV_WARNING_INTERVAL
        ):
            _LOGGER.warning(
                "No reachable TRVs in %s - cannot set heating", self._area_name
            )
            self._last_trv_warning = now

    def _schedule_apply_retry(self) -> None:
        if self._retry_unsub:
            return

        async def _retry(_now):
            self._retry_unsub = None
            await self._apply_output(immediate_demand=True)

        self._retry_unsub = async_call_later(self.hass, 30, _retry)

    def _maintenance_time_args(self) -> dict[str, int]:
        value = self._maintenance_time
        if isinstance(value, str):
            parsed = dt_util.parse_time(value)
            if parsed is not None:
                value = parsed
        if isinstance(value, dict):
            hour = int(value.get("hour", DEFAULT_MAINTENANCE_TIME.hour))
            minute = int(value.get("minute", DEFAULT_MAINTENANCE_TIME.minute))
            second = int(value.get("second", DEFAULT_MAINTENANCE_TIME.second))
            return {"hour": hour, "minute": minute, "second": second}
        if hasattr(value, "hour"):
            return {
                "hour": int(value.hour),
                "minute": int(value.minute),
                "second": int(getattr(value, "second", 0)),
            }
        return {
            "hour": DEFAULT_MAINTENANCE_TIME.hour,
            "minute": DEFAULT_MAINTENANCE_TIME.minute,
            "second": DEFAULT_MAINTENANCE_TIME.second,
        }

    async def _apply_output(self, *, immediate_demand: bool = False) -> None:
        await self._select_state().apply_output(
            self, immediate_demand=immediate_demand
        )

    async def _apply_output_internal(
        self, *, immediate_demand: bool = False
    ) -> None:
        valid_trvs = self._get_valid_trvs() if self._trvs else []
        target = self._effective_target()
        forced_off = self._is_forced_off()

        _LOGGER.debug(
            "Applying output for %s: target=%s forced_off=%s trvs=%s",
            self._area_name,
            target,
            forced_off,
            valid_trvs,
        )

        if self._trvs:
            if not valid_trvs:
                self._warn_no_trvs()
                self._schedule_apply_retry()
                await self._update_demand(target, immediate=immediate_demand)
                self.async_write_ha_state()
                return
            if self._retry_unsub:
                self._retry_unsub()
                self._retry_unsub = None
            if forced_off:
                command = SetTrvModeAndTempCommand(
                    valid_trvs,
                    HVACMode.OFF,
                    self._off_temp,
                    self._valve_strategy,
                )
                await self._command_executor.execute(command, propagate=True)
            elif target is not None:
                send_target = target
                if self._temp_sensors and self._current_temperature is not None:
                    compensation = calculate_temperature_compensation(
                        target_temp=target,
                        current_temp=self._current_temperature,
                    )
                    send_target = compensation.clamped_target
                    _LOGGER.debug(
                        "Room is %s, Target %s. Error %s. Sending %s to TRVs",
                        round(self._current_temperature, 2),
                        round(target, 2),
                        round(compensation.error, 2),
                        round(send_target, 2),
                    )
                command = SetTrvModeAndTempCommand(
                    valid_trvs,
                    HVACMode.HEAT,
                    send_target,
                    self._valve_strategy,
                )
                await self._command_executor.execute(command, propagate=True)

        await self._update_demand(target, immediate=immediate_demand)
        self.async_write_ha_state()

    async def _set_trvs_temp(self, temperature: float) -> None:
        if not self._trvs:
            return
        valid_trvs = self._get_valid_trvs()
        if not valid_trvs:
            self._warn_no_trvs()
            return
        command = SetTrvModeAndTempCommand(
            valid_trvs, HVACMode.HEAT, temperature, self._valve_strategy
        )
        await self._command_executor.execute(command, propagate=True)

    async def _update_demand(
        self, target: float | None, *, immediate: bool = False
    ) -> None:
        demand = False
        if not self._is_forced_off() and target is not None:
            if (
                self._current_temperature is not None
                and self._current_temperature + 0.1 < target
            ):
                demand = True

        now = dt_util.utcnow()
        if demand != self._demand:
            _LOGGER.debug(
                "Demand change for %s: %s -> %s (target=%s current=%s)",
                self._area_name,
                self._demand,
                demand,
                target,
                self._current_temperature,
            )
            start_temp = self._current_temperature or target or 0
            baseline_temp = (
                self._current_temperature
                if self._current_temperature is not None
                else None
            )
            is_sunny = self._is_sunny()
            if demand:
                await self._learning.async_end_cooling_cycle(
                    self._zone_id, start_temp
                )
                await self._learning.async_start_cycle(
                    self._zone_id,
                    start_temp,
                    self._get_outdoor_temp(),
                    is_sunny,
                )
                self._demand_since = now
                self._demand_start_temp = baseline_temp
                self._idle_since = None
                self._idle_start_temp = None
            else:
                await self._learning.async_end_cycle(self._zone_id, start_temp)
                await self._learning.async_start_cooling_cycle(
                    self._zone_id,
                    start_temp,
                    self._get_outdoor_temp(),
                    is_sunny,
                )
                self._idle_since = now
                self._idle_start_temp = baseline_temp
                self._demand_since = None
                self._demand_start_temp = None
            self._demand = demand
            await self._coordinator.async_update_demand(
                self._zone_id, demand, immediate=immediate
            )
        else:
            if demand and self._demand_since is None:
                if self._current_temperature is not None:
                    self._demand_since = now
                    self._demand_start_temp = self._current_temperature
            elif not demand and self._idle_since is None:
                if self._current_temperature is not None:
                    self._idle_since = now
                    self._idle_start_temp = self._current_temperature

        await self._check_system_health()

    async def _check_system_health(self, now=None) -> None:
        current_temp = self._current_temperature
        if current_temp is None:
            if self._health_state != "OK":
                self._health_state = "OK"
                self.async_write_ha_state()
            return

        check_time = now or dt_util.utcnow()
        health = "OK"

        if self._demand:
            if self._demand_since and self._demand_start_temp is not None:
                duration = (check_time - self._demand_since).total_seconds()
                if duration >= 7200:
                    if current_temp - self._demand_start_temp < 0.2:
                        health = "POSSIBLE_BOILER_FAILURE"
        else:
            if self._idle_since and self._idle_start_temp is not None:
                duration = (check_time - self._idle_since).total_seconds()
                if duration >= 3600:
                    if current_temp - self._idle_start_temp > 1.0:
                        health = "VALVE_STUCK_OPEN"

        if health != self._health_state:
            self._health_state = health
            if health != "OK":
                self._fire_event(TYPE_FAILURE, {"status": health})
            self.async_write_ha_state()


def _state_to_float(state) -> float | None:
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None
