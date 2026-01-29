"""Virtual thermostat entities for Vesta."""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta
import logging
import re

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_TEMPERATURE,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    CONF_DEVICE_ID,
    CONF_TYPE,
    EVENT_HOMEASSISTANT_START,
    STATE_HOME,
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
        self._override_target: float | None = None
        self._override_type: str | None = None
        self._override_expires = None
        self._user_hvac_off = False

        self._window_open = False
        self._window_hold_until = None
        self._window_hold_unsub = None
        self._boost_unsub = None
        self._preheat_start_unsub = None
        self._preheat_apply_unsub = None
        self._maintenance_unsub = None
        self._maintenance_task = None
        self._maintenance_active = False

        self._presence_on = False
        self._temp_history: list[tuple[dt_util.dt.datetime, float]] = []
        self._demand = False
        self._preheat_active = False
        self._preheat_target: float | None = None
        self._preheat_effective_at: dt_util.dt.datetime | None = None
        self._pending_target: float | None = None
        self._pending_effective_at: dt_util.dt.datetime | None = None
        self._calendar_last_signature: tuple[dt_util.dt.datetime, float] | None = None
        self._calendar_suppressed_signature: (
            tuple[dt_util.dt.datetime, float] | None
        ) = None
        self._battery_lock = False
        self._health_state = "OK"
        self._demand_since: dt_util.dt.datetime | None = None
        self._demand_start_temp: float | None = None
        self._idle_since: dt_util.dt.datetime | None = None
        self._idle_start_temp: float | None = None
        self._last_trv_warning: dt_util.dt.datetime | None = None
        self._retry_unsub = None
        self._startup_done = False

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

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._unsubs.append(
            self.hass.bus.async_listen(EVENT_SCHEDULE_UPDATE, self._handle_schedule)
        )

        tracked = set(
            self._temp_sensors
            + self._humidity_sensors
            + self._window_sensors
            + self._presence_sensors
            + self._distance_sensors
            + self._battery_sensors
            + self._trvs
            + [GUEST_SWITCH, MASTER_SWITCH, ECO_NUMBER, HOME_ZONE]
        )
        if tracked:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, list(tracked), self._handle_state_change
                )
            )

        if self.hass.state == CoreState.running:
            await self.async_startup()
        else:
            self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_START,
                lambda _: self.hass.async_create_task(self.async_startup()),
            )

    async def async_startup(self) -> None:
        if self._startup_done:
            return
        self._startup_done = True

        await self._load_schedule_target()
        self._refresh_presence()
        self._refresh_window_state()
        await self._refresh_battery_state()
        await self._update_current_temperature()
        await self._update_current_humidity()

        if self._calendar_entity:
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

        await self._apply_output()

    async def async_will_remove_from_hass(self) -> None:
        for unsub in self._unsubs:
            unsub()
        if self._boost_unsub:
            self._boost_unsub()
            self._boost_unsub = None
        if self._retry_unsub:
            self._retry_unsub()
            self._retry_unsub = None
        if self._window_hold_unsub:
            self._window_hold_unsub()
            self._window_hold_unsub = None
        self._cancel_preheat()
        if self._maintenance_task:
            self._maintenance_task.cancel()
            self._maintenance_task = None

    async def async_set_temperature(self, **kwargs) -> None:
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        if self._battery_lock:
            _LOGGER.warning(
                "Vesta battery failsafe active for %s: ignoring manual override",
                self._area_name,
            )
            return
        new_temp = float(temperature)
        schedule_target = self._schedule_target or self._off_temp

        self._suppress_calendar_event()
        if new_temp > schedule_target:
            self._set_boost_override(new_temp)
        elif new_temp < schedule_target:
            self._set_save_override(new_temp)
        else:
            self._clear_override()

        await self._apply_output()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if self._battery_lock:
            _LOGGER.warning(
                "Vesta battery failsafe active for %s: ignoring HVAC mode change",
                self._area_name,
            )
            return
        self._user_hvac_off = hvac_mode == HVACMode.OFF
        if self._user_hvac_off:
            self._cancel_preheat()
        await self._apply_output()

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
        self._suppress_calendar_event()
        if effective_at and effective_at > dt_util.utcnow():
            await self._schedule_future_target(target_value, effective_at)
            return
        self._schedule_target = target_value
        self._cancel_preheat()
        if self._override_type == "save":
            self._clear_override()
        await self._apply_output()

    async def _handle_state_change(self, event) -> None:
        entity_id = event.data.get("entity_id")
        if entity_id in self._temp_sensors or entity_id in self._trvs:
            await self._update_current_temperature()
        elif entity_id in self._humidity_sensors:
            await self._update_current_humidity()
        elif entity_id in self._window_sensors:
            self._refresh_window_state()
        elif entity_id in self._battery_sensors:
            await self._refresh_battery_state()
        elif entity_id in self._distance_sensors or entity_id in self._presence_sensors or entity_id in (
            GUEST_SWITCH,
            HOME_ZONE,
        ):
            self._refresh_presence()
        await self._apply_output()

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
            if not self._window_sensors:
                self._record_temperature(self._current_temperature)

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

    def _record_temperature(self, temperature: float) -> None:
        now = dt_util.utcnow()
        self._temp_history.append((now, temperature))
        cutoff = now - timedelta(minutes=3)
        self._temp_history = [
            (ts, temp) for ts, temp in self._temp_history if ts >= cutoff
        ]
        if len(self._temp_history) < 2:
            return

        oldest_ts, oldest_temp = self._temp_history[0]
        minutes = (now - oldest_ts).total_seconds() / 60
        if minutes <= 0:
            return
        drop = oldest_temp - temperature
        rate = drop / minutes
        if drop >= 0.5 or rate >= self._window_threshold:
            self._trigger_window_hold()

    def _trigger_window_hold(self) -> None:
        self._window_hold_until = dt_util.utcnow() + WINDOW_HOLD_DURATION
        if self._window_hold_unsub:
            self._window_hold_unsub()

        async def _clear_hold(_now):
            self._window_hold_unsub = None
            self._window_hold_until = None
            await self._apply_output()

        self._window_hold_unsub = async_call_later(
            self.hass, WINDOW_HOLD_DURATION.total_seconds(), _clear_hold
        )
        self._fire_event(TYPE_WINDOW)

    def _refresh_window_state(self) -> None:
        self._window_open = any(
            self.hass.states.get(entity_id).state == STATE_ON
            for entity_id in self._window_sensors
            if self.hass.states.get(entity_id) is not None
        )

    def _refresh_presence(self) -> None:
        guest_mode = self._is_guest_mode()
        zone_home = self._is_home() or guest_mode
        previous_presence = self._presence_on
        self._presence_on = False
        if not zone_home:
            return
        if self._distance_sensors:
            hysteresis = 0.5 if previous_presence else 0.0
            for entity_id in self._distance_sensors:
                value = _state_to_float(self.hass.states.get(entity_id))
                if value is None or not math.isfinite(value):
                    continue
                if value < self._bermuda_threshold + hysteresis:
                    self._presence_on = True
                    return

        for entity_id in self._presence_sensors:
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
            if entity_id.startswith("binary_sensor."):
                if state.state in (STATE_ON, STATE_HOME):
                    self._presence_on = True
                    return
                continue
            if state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
                continue
            state_value = str(state.state).casefold()
            if state_value in (self._area_name.casefold(), self._slug.casefold()):
                self._presence_on = True
                return

    async def _refresh_battery_state(self) -> None:
        if not self._battery_sensors:
            return
        low = False
        for entity_id in self._battery_sensors:
            value = _state_to_float(self.hass.states.get(entity_id))
            if value is None:
                continue
            if value < BATTERY_THRESHOLD:
                low = True
                break
        if low and not self._battery_lock:
            self._battery_lock = True
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
            await self._load_schedule_target()
            await self._apply_output()

    def _set_boost_override(self, target: float) -> None:
        self._cancel_preheat()
        self._override_type = "boost"
        self._override_target = target
        self._override_expires = dt_util.utcnow() + BOOST_DURATION
        if self._boost_unsub:
            self._boost_unsub()

        async def _expire(_now):
            self._boost_unsub = None
            self._clear_override()
            await self._apply_output()

        self._boost_unsub = async_call_later(
            self.hass, BOOST_DURATION.total_seconds(), _expire
        )

    def _set_save_override(self, target: float) -> None:
        self._cancel_preheat()
        if self._boost_unsub:
            self._boost_unsub()
            self._boost_unsub = None
        self._override_type = "save"
        self._override_target = target
        self._override_expires = None

    def _clear_override(self) -> None:
        if self._boost_unsub:
            self._boost_unsub()
            self._boost_unsub = None
        self._override_type = None
        self._override_target = None
        self._override_expires = None

    def _is_home(self) -> bool:
        state = self.hass.states.get(HOME_ZONE)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return False
        try:
            return int(float(state.state)) > 0
        except (TypeError, ValueError):
            return False

    def _is_guest_mode(self) -> bool:
        state = self.hass.states.get(GUEST_SWITCH)
        return state is not None and state.state == STATE_ON

    def _is_master_enabled(self) -> bool:
        state = self.hass.states.get(MASTER_SWITCH)
        return state is None or state.state == STATE_ON

    def _is_forced_off(self) -> bool:
        if self._battery_lock:
            return False
        if not self._is_master_enabled():
            return True
        if self._user_hvac_off:
            return True
        if self._window_open:
            return True
        if self._window_hold_until and dt_util.utcnow() < self._window_hold_until:
            return True
        return False

    def _effective_target(self) -> float | None:
        if self._battery_lock:
            return VALVE_MAINTENANCE_HIGH
        if self._override_target is not None:
            return self._override_target
        if self._preheat_active and self._preheat_target is not None:
            return self._preheat_target

        schedule_target = self._schedule_target
        if schedule_target is None:
            schedule_target = self._off_temp

        if not self._is_guest_mode() and not self._is_home():
            schedule_target = self._eco_temp()

        if (
            self._presence_sensors
            and self._presence_on
            and schedule_target <= self._off_temp
        ):
            schedule_target = max(schedule_target, self._comfort_temp)

        return schedule_target

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
            await self._apply_output()
            return

        async def _apply_future(_now):
            self._preheat_apply_unsub = None
            await self._apply_future_target(target, effective_at)

        self._preheat_apply_unsub = async_call_later(
            self.hass, delay, _apply_future
        )

        start_at = self._compute_preheat_start(target, effective_at)
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
            await self._apply_output()

    async def _poll_calendar(self, _now) -> None:
        if not self._calendar_entity:
            return
        if self.hass.states.get(self._calendar_entity) is None:
            _LOGGER.debug("Calendar entity %s not ready yet", self._calendar_entity)
            return
        if self._battery_lock:
            return
        now = dt_util.utcnow()
        start_search = now - timedelta(hours=24)
        end = now + timedelta(days=7)
        events = await self._fetch_calendar_events(start_search, end)
        _LOGGER.debug(
            "Calendar fetch: %s events found between %s and %s",
            len(events),
            start_search,
            end,
        )
        if not events:
            return
        next_event, is_active = _next_calendar_event(self.hass, now, events)
        if not next_event:
            return
        start = _event_start(self.hass, next_event)
        if start is None:
            return
        end_time = _event_end(self.hass, next_event)
        if is_active:
            target = _event_target(next_event)
            if target is None:
                return
            if self._schedule_target != target:
                _LOGGER.info(
                    "Found active calendar event: Setting target to %s", target
                )
                await self._apply_future_target(target, now)
            return
        if start <= now:
            return
        target = _event_target(next_event)
        if target is None:
            return

        signature = (start, target)
        if self._calendar_suppressed_signature == signature:
            return
        if signature == self._calendar_last_signature:
            return

        self._calendar_last_signature = signature
        await self._schedule_future_target(target, start)

    async def _fetch_calendar_events(
        self, start: dt_util.dt.datetime, end: dt_util.dt.datetime
    ) -> list[dict]:
        try:
            response = await self.hass.services.async_call(
                "calendar",
                "get_events",
                {
                    "entity_id": self._calendar_entity,
                    "start_date_time": start.isoformat(),
                    "end_date_time": end.isoformat(),
                },
                blocking=True,
                return_response=True,
            )
        except Exception as err:  # pragma: no cover - defensive
            _LOGGER.warning("Calendar poll failed: %s", err)
            return []
        return _extract_calendar_events(response, self._calendar_entity)

    def _compute_preheat_start(
        self, target: float, effective_at: dt_util.dt.datetime
    ) -> dt_util.dt.datetime | None:
        if not (self._is_home() or self._is_guest_mode()):
            return None
        if self._current_temperature is None:
            return None
        if target <= self._current_temperature:
            return None
        rate = self._learning.get_rate(
            self._zone_id, self._get_outdoor_temp(), self._is_sunny()
        )
        if rate <= 0:
            return None
        seconds = ((target - self._current_temperature) / rate) * 3600
        if seconds <= 0:
            return None
        return effective_at - timedelta(seconds=seconds)

    async def _start_preheat(
        self, target: float, effective_at: dt_util.dt.datetime
    ) -> None:
        if dt_util.utcnow() >= effective_at:
            return
        if self._battery_lock:
            return
        if self._override_target is not None:
            return
        if self._current_temperature is not None and target <= self._current_temperature:
            return
        self._preheat_active = True
        self._preheat_target = target
        self._preheat_effective_at = effective_at
        self._fire_event(TYPE_PREHEAT, {"target": target})
        await self._apply_output()

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

        await self._apply_output()

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
        if self._calendar_last_signature is None:
            return
        self._calendar_suppressed_signature = self._calendar_last_signature

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
            await self._apply_output()

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

    async def _apply_output(self) -> None:
        valid_trvs = self._get_valid_trvs() if self._trvs else []
        if self._battery_lock:
            if self._trvs and not valid_trvs:
                self._warn_no_trvs()
                await self._update_demand(None)
                self.async_write_ha_state()
                return
            await self._set_trvs_temp(FAILSAFE_TEMP)
            await self._update_demand(None)
            self.async_write_ha_state()
            return

        if self._maintenance_active:
            self.async_write_ha_state()
            return

        target = self._effective_target()
        forced_off = self._is_forced_off()

        if self._trvs:
            if not valid_trvs:
                self._warn_no_trvs()
                self._schedule_apply_retry()
                await self._update_demand(target)
                self.async_write_ha_state()
                return
            if self._retry_unsub:
                self._retry_unsub()
                self._retry_unsub = None
            if forced_off:
                await self.hass.services.async_call(
                    "climate",
                    SERVICE_SET_HVAC_MODE,
                    {ATTR_ENTITY_ID: valid_trvs, "hvac_mode": HVACMode.OFF},
                    blocking=True,
                )
                await self.hass.services.async_call(
                    "climate",
                    SERVICE_SET_TEMPERATURE,
                    {
                        ATTR_ENTITY_ID: valid_trvs,
                        ATTR_TEMPERATURE: self._off_temp,
                    },
                    blocking=True,
                )
            elif target is not None:
                send_target = target
                if self._temp_sensors and self._current_temperature is not None:
                    error = target - self._current_temperature
                    compensated_target = target + (error * 2.0)
                    send_target = max(5.0, min(30.0, compensated_target))
                    _LOGGER.debug(
                        "Room is %s, Target %s. Error %s. Sending %s to TRVs",
                        round(self._current_temperature, 2),
                        round(target, 2),
                        round(error, 2),
                        round(send_target, 2),
                    )
                await self.hass.services.async_call(
                    "climate",
                    SERVICE_SET_HVAC_MODE,
                    {ATTR_ENTITY_ID: valid_trvs, "hvac_mode": HVACMode.HEAT},
                    blocking=True,
                )
                await self.hass.services.async_call(
                    "climate",
                    SERVICE_SET_TEMPERATURE,
                    {ATTR_ENTITY_ID: valid_trvs, ATTR_TEMPERATURE: send_target},
                    blocking=True,
                )

        await self._update_demand(target)
        self.async_write_ha_state()

    async def _set_trvs_temp(self, temperature: float) -> None:
        if not self._trvs:
            return
        valid_trvs = self._get_valid_trvs()
        if not valid_trvs:
            self._warn_no_trvs()
            return
        await self.hass.services.async_call(
            "climate",
            SERVICE_SET_HVAC_MODE,
            {ATTR_ENTITY_ID: valid_trvs, "hvac_mode": HVACMode.HEAT},
            blocking=True,
        )
        await self.hass.services.async_call(
            "climate",
            SERVICE_SET_TEMPERATURE,
            {ATTR_ENTITY_ID: valid_trvs, ATTR_TEMPERATURE: temperature},
            blocking=True,
        )

    async def _update_demand(self, target: float | None) -> None:
        demand = False
        if not self._is_forced_off() and target is not None:
            if (
                self._current_temperature is not None
                and self._current_temperature + 0.1 < target
            ):
                demand = True

        now = dt_util.utcnow()
        if demand != self._demand:
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
            await self._coordinator.async_update_demand(self._zone_id, demand)
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


def _parse_effective_at(hass, value) -> dt_util.dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt_value = value
    else:
        dt_value = dt_util.parse_datetime(str(value))
        if dt_value is None:
            return None
    if dt_value.tzinfo is None:
        tz = dt_util.get_time_zone(hass.config.time_zone)
        dt_value = dt_value.replace(tzinfo=tz)
    return dt_util.as_utc(dt_value)


def _extract_calendar_events(response, entity_id: str | None) -> list[dict]:
    if not response:
        return []
    if isinstance(response, dict):
        if isinstance(response.get("events"), list):
            return response["events"]
        if entity_id and entity_id in response:
            data = response[entity_id]
            if isinstance(data, dict) and isinstance(data.get("events"), list):
                return data["events"]
            if isinstance(data, list):
                return data
    if isinstance(response, list):
        return response
    return []


def _next_calendar_event(
    hass, now: dt_util.dt.datetime, events: list[dict]
) -> tuple[dict | None, bool]:
    active_event = None
    active_start = None
    next_event = None
    next_start = None
    for event in events:
        start = _event_start(hass, event)
        if start is None:
            continue
        end = _event_end(hass, event)
        if end is not None and end <= now:
            continue
        if start <= now and (end is None or now < end):
            if active_start is None or start < active_start:
                active_start = start
                active_event = event
            continue
        if start > now:
            if next_start is None or start < next_start:
                next_start = start
                next_event = event
    if active_event is not None:
        return active_event, True
    return next_event, False


def _event_end(hass, event: dict) -> dt_util.dt.datetime | None:
    if not isinstance(event, dict):
        return None
    end = event.get("end")
    if isinstance(end, dict):
        end = end.get("dateTime") or end.get("date")
    if end is None:
        end = event.get("end_time")
    dt_value = _parse_effective_at(hass, end)
    if dt_value is not None:
        return dt_value
    date_value = dt_util.parse_date(str(end)) if end is not None else None
    if date_value is None:
        return None
    tz = dt_util.get_time_zone(hass.config.time_zone)
    dt_value = datetime.combine(date_value, datetime.min.time()).replace(tzinfo=tz)
    return dt_util.as_utc(dt_value)


def _event_start(hass, event: dict) -> dt_util.dt.datetime | None:
    if not isinstance(event, dict):
        return None
    start = event.get("start")
    if isinstance(start, dict):
        start = start.get("dateTime") or start.get("date")
    if start is None:
        start = event.get("start_time")
    dt_value = _parse_effective_at(hass, start)
    if dt_value is not None:
        return dt_value
    date_value = dt_util.parse_date(str(start)) if start is not None else None
    if date_value is None:
        return None
    tz = dt_util.get_time_zone(hass.config.time_zone)
    dt_value = datetime.combine(date_value, datetime.min.time()).replace(tzinfo=tz)
    return dt_util.as_utc(dt_value)


def _event_target(event: dict) -> float | None:
    if not isinstance(event, dict):
        return None
    text = event.get("summary") or event.get("description") or ""
    match = re.search(r"-?\\d+(?:\\.\\d+)?", str(text))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None
