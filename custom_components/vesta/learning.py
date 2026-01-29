"""Adaptive heating learning storage for Vesta."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import inspect
from typing import Awaitable, Callable

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import STORAGE_KEY, STORAGE_VERSION

DEFAULT_RATE = 1.5
DEFAULT_COOLING_RATE = 0.5
MIN_CYCLE_HOURS = 0.25
SMOOTHING_WEIGHT = 0.3


class _ThermalLearningBase:
    """Template for cycle learning logic."""

    def __init__(
        self,
        parent: "VestaLearning",
        *,
        rates: dict[str, dict[str, float]],
        active_cycles: dict[str, _Cycle],
        default_rate: float,
        kind: str,
    ) -> None:
        self._parent = parent
        self._rates = rates
        self._active_cycles = active_cycles
        self._default_rate = default_rate
        self._kind = kind

    async def end_cycle(self, zone_id: str, end_temp: float) -> None:
        cycle = self._active_cycles.pop(zone_id, None)
        if cycle is None:
            return
        duration = dt_util.utcnow() - cycle.start_time
        hours = duration.total_seconds() / 3600
        if hours <= MIN_CYCLE_HOURS:
            return
        delta = self._calculate_delta(cycle.start_temp, end_temp)
        if delta <= 0:
            return
        observed_rate = delta / hours
        bucket = self._parent._bucket(cycle.outdoor_temp, cycle.is_sunny)
        zone = self._rates.setdefault(zone_id, {})
        old_rate = zone.get(bucket, self._default_rate)
        new_rate = (old_rate * (1 - SMOOTHING_WEIGHT)) + (
            observed_rate * SMOOTHING_WEIGHT
        )
        zone[bucket] = round(new_rate, 3)
        await self._parent.async_save()
        await self._parent._notify_rate_update(
            LearningUpdate(
                zone_id=zone_id,
                kind=self._kind,
                bucket=bucket,
                rate=zone[bucket],
            )
        )

    def _calculate_delta(self, start_temp: float, end_temp: float) -> float:
        raise NotImplementedError


class _HeatingLearning(_ThermalLearningBase):
    def _calculate_delta(self, start_temp: float, end_temp: float) -> float:
        return end_temp - start_temp


class _CoolingLearning(_ThermalLearningBase):
    def _calculate_delta(self, start_temp: float, end_temp: float) -> float:
        return start_temp - end_temp


@dataclass
class _Cycle:
    start_time: datetime
    start_temp: float
    outdoor_temp: float | None
    is_sunny: bool


@dataclass(frozen=True)
class LearningUpdate:
    zone_id: str
    kind: str
    bucket: str
    rate: float


class VestaLearning:
    """Learning manager for heating and cooling rate per zone and weather bucket."""

    def __init__(self, hass):
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._heating_rates: dict[str, dict[str, float]] = {}
        self._cooling_rates: dict[str, dict[str, float]] = {}
        self._active_heating: dict[str, _Cycle] = {}
        self._active_cooling: dict[str, _Cycle] = {}
        self._observers: list[Callable[[LearningUpdate], Awaitable[None] | None]] = []
        self._heating_learning = _HeatingLearning(
            self,
            rates=self._heating_rates,
            active_cycles=self._active_heating,
            default_rate=DEFAULT_RATE,
            kind="heating",
        )
        self._cooling_learning = _CoolingLearning(
            self,
            rates=self._cooling_rates,
            active_cycles=self._active_cooling,
            default_rate=DEFAULT_COOLING_RATE,
            kind="cooling",
        )

    def add_observer(
        self, observer: Callable[[LearningUpdate], Awaitable[None] | None]
    ) -> None:
        if observer not in self._observers:
            self._observers.append(observer)

    def remove_observer(
        self, observer: Callable[[LearningUpdate], Awaitable[None] | None]
    ) -> None:
        if observer in self._observers:
            self._observers.remove(observer)

    async def _notify_rate_update(self, update: LearningUpdate) -> None:
        if not self._observers:
            return
        for observer in list(self._observers):
            result = observer(update)
            if inspect.isawaitable(result):
                await result

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
        await self._heating_learning.end_cycle(zone_id, end_temp)

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
        await self._cooling_learning.end_cycle(zone_id, end_temp)
