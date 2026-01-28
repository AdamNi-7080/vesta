"""Adaptive heating learning storage for Vesta."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import STORAGE_KEY, STORAGE_VERSION

DEFAULT_RATE = 1.5
DEFAULT_COOLING_RATE = 0.5


@dataclass
class _Cycle:
    start_time: datetime
    start_temp: float
    outdoor_temp: float | None
    is_sunny: bool


class VestaLearning:
    """Learning manager for heating and cooling rate per zone and weather bucket."""

    def __init__(self, hass):
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._heating_rates: dict[str, dict[str, float]] = {}
        self._cooling_rates: dict[str, dict[str, float]] = {}
        self._active_heating: dict[str, _Cycle] = {}
        self._active_cooling: dict[str, _Cycle] = {}

    async def async_load(self) -> None:
        loaded = await self._store.async_load()
        if isinstance(loaded, dict):
            if "heating_rates" in loaded or "cooling_rates" in loaded:
                self._heating_rates = loaded.get("heating_rates", {}) or {}
                self._cooling_rates = loaded.get("cooling_rates", {}) or {}
            else:
                self._heating_rates = loaded
                self._cooling_rates = {}

    async def async_save(self) -> None:
        await self._store.async_save(
            {
                "heating_rates": self._heating_rates,
                "cooling_rates": self._cooling_rates,
            }
        )

    def _bucket(self, outdoor_temp: float | None, is_sunny: bool = False) -> str:
        if outdoor_temp is None:
            bucket = "cool"
        elif outdoor_temp < 0:
            bucket = "cold"
        elif outdoor_temp < 10:
            bucket = "cool"
        elif outdoor_temp < 15:
            bucket = "mild"
        else:
            bucket = "warm"
        if is_sunny:
            return f"{bucket}_sunny"
        return bucket

    def get_rate(
        self, zone_id: str, outdoor_temp: float | None, is_sunny: bool = False
    ) -> float:
        bucket = self._bucket(outdoor_temp, is_sunny)
        fallback_bucket = self._bucket(outdoor_temp, False)
        zone = self._heating_rates.get(zone_id, {})
        if bucket in zone:
            return zone[bucket]
        if is_sunny and fallback_bucket in zone:
            return zone[fallback_bucket]
        return DEFAULT_RATE

    def get_cooling_rate(
        self, zone_id: str, outdoor_temp: float | None, is_sunny: bool = False
    ) -> float:
        bucket = self._bucket(outdoor_temp, is_sunny)
        fallback_bucket = self._bucket(outdoor_temp, False)
        zone = self._cooling_rates.get(zone_id, {})
        if bucket in zone:
            return zone[bucket]
        if is_sunny and fallback_bucket in zone:
            return zone[fallback_bucket]
        return DEFAULT_COOLING_RATE

    async def async_start_cycle(
        self,
        zone_id: str,
        start_temp: float,
        outdoor_temp: float | None,
        is_sunny: bool = False,
    ) -> None:
        self._active_heating[zone_id] = _Cycle(
            start_time=dt_util.utcnow(),
            start_temp=start_temp,
            outdoor_temp=outdoor_temp,
            is_sunny=is_sunny,
        )

    async def async_end_cycle(self, zone_id: str, end_temp: float) -> None:
        cycle = self._active_heating.pop(zone_id, None)
        if cycle is None:
            return
        duration = dt_util.utcnow() - cycle.start_time
        hours = duration.total_seconds() / 3600
        if hours <= 0.25:
            return
        delta = end_temp - cycle.start_temp
        if delta <= 0:
            return
        observed_rate = delta / hours
        bucket = self._bucket(cycle.outdoor_temp, cycle.is_sunny)
        zone = self._heating_rates.setdefault(zone_id, {})
        old_rate = zone.get(bucket, DEFAULT_RATE)
        new_rate = (old_rate * 0.7) + (observed_rate * 0.3)
        zone[bucket] = round(new_rate, 3)
        await self.async_save()

    async def async_start_cooling_cycle(
        self,
        zone_id: str,
        start_temp: float,
        outdoor_temp: float | None,
        is_sunny: bool = False,
    ) -> None:
        self._active_cooling[zone_id] = _Cycle(
            start_time=dt_util.utcnow(),
            start_temp=start_temp,
            outdoor_temp=outdoor_temp,
            is_sunny=is_sunny,
        )

    async def async_end_cooling_cycle(self, zone_id: str, end_temp: float) -> None:
        cycle = self._active_cooling.pop(zone_id, None)
        if cycle is None:
            return
        duration = dt_util.utcnow() - cycle.start_time
        hours = duration.total_seconds() / 3600
        if hours <= 0.25:
            return
        delta = cycle.start_temp - end_temp
        if delta <= 0:
            return
        observed_rate = delta / hours
        bucket = self._bucket(cycle.outdoor_temp, cycle.is_sunny)
        zone = self._cooling_rates.setdefault(zone_id, {})
        old_rate = zone.get(bucket, DEFAULT_COOLING_RATE)
        new_rate = (old_rate * 0.7) + (observed_rate * 0.3)
        zone[bucket] = round(new_rate, 3)
        await self.async_save()
