"""Adaptive heating learning storage for Vesta."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import inspect
import math
from typing import Awaitable, Callable

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import STORAGE_KEY, STORAGE_VERSION

DEFAULT_RATE = 1.5
DEFAULT_COOLING_RATE = 0.5
MIN_CYCLE_HOURS = 0.25
MIN_HISTORY_POINTS = 5
MAX_HISTORY_POINTS = 50
RATE_MIN = 0.1
RATE_MAX = 5.0


class _ThermalLearningBase:
    """Template for cycle learning logic."""

    def __init__(
        self,
        parent: "VestaLearning",
        *,
        history: dict[str, list[dict[str, float]]],
        active_cycles: dict[str, _Cycle],
        kind: str,
    ) -> None:
        self._parent = parent
        self._history = history
        self._active_cycles = active_cycles
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
        if cycle.outdoor_temp is None or not math.isfinite(cycle.outdoor_temp):
            return
        observed_rate = delta / hours
        history = self._history.setdefault(zone_id, [])
        history.append(
            {
                "outdoor": float(cycle.outdoor_temp),
                "rate": round(observed_rate, 3),
            }
        )
        self._parent._prune_history(history)
        await self._parent.async_save()
        await self._parent._notify_rate_update(
            LearningUpdate(
                zone_id=zone_id,
                kind=self._kind,
                outdoor=float(cycle.outdoor_temp),
                rate=round(observed_rate, 3),
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
    outdoor: float
    rate: float


class VestaLearning:
    """Learning manager for heating and cooling rates per zone."""

    def __init__(self, hass):
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._heating_history: dict[str, list[dict[str, float]]] = {}
        self._cooling_history: dict[str, list[dict[str, float]]] = {}
        self._active_heating: dict[str, _Cycle] = {}
        self._active_cooling: dict[str, _Cycle] = {}
        self._observers: list[Callable[[LearningUpdate], Awaitable[None] | None]] = []
        self._heating_learning = _HeatingLearning(
            self,
            history=self._heating_history,
            active_cycles=self._active_heating,
            kind="heating",
        )
        self._cooling_learning = _CoolingLearning(
            self,
            history=self._cooling_history,
            active_cycles=self._active_cooling,
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
            if (
                "zone_heating_history" in loaded
                or "zone_cooling_history" in loaded
            ):
                self._heating_history = (
                    loaded.get("zone_heating_history", {}) or {}
                )
                self._cooling_history = (
                    loaded.get("zone_cooling_history", {}) or {}
                )
                for history in self._heating_history.values():
                    self._prune_history(history)
                for history in self._cooling_history.values():
                    self._prune_history(history)
            else:
                self._heating_history = {}
                self._cooling_history = {}

    async def async_save(self) -> None:
        await self._store.async_save(
            {
                "zone_heating_history": self._heating_history,
                "zone_cooling_history": self._cooling_history,
            }
        )

    def _prune_history(self, history: list[dict[str, float]]) -> None:
        while len(history) > MAX_HISTORY_POINTS:
            history.pop(0)

    def get_rate(
        self, zone_id: str, outdoor_temp: float | None, is_sunny: bool = False
    ) -> float:
        if outdoor_temp is None or not math.isfinite(outdoor_temp):
            return DEFAULT_RATE
        history = self._heating_history.get(zone_id, [])
        rate = self._predict_rate(history, outdoor_temp)
        if rate is not None:
            return rate
        return DEFAULT_RATE

    def get_heating_regression(
        self, zone_id: str
    ) -> tuple[float | None, float | None]:
        history = self._heating_history.get(zone_id, [])
        return self._regression_from_history(history)

    def get_cooling_rate(
        self, zone_id: str, outdoor_temp: float | None, is_sunny: bool = False
    ) -> float:
        if outdoor_temp is None or not math.isfinite(outdoor_temp):
            return DEFAULT_COOLING_RATE
        history = self._cooling_history.get(zone_id, [])
        rate = self._predict_rate(history, outdoor_temp)
        if rate is not None:
            return rate
        return DEFAULT_COOLING_RATE

    def get_cooling_regression(
        self, zone_id: str
    ) -> tuple[float | None, float | None]:
        history = self._cooling_history.get(zone_id, [])
        return self._regression_from_history(history)

    def _predict_rate(
        self, history: list[dict[str, float]], outdoor_temp: float
    ) -> float | None:
        points = self._history_points(history)
        if len(points) < MIN_HISTORY_POINTS:
            return None
        slope, intercept = self._linear_regression(points)
        if slope is None or intercept is None:
            return None
        predicted = (slope * outdoor_temp) + intercept
        return max(RATE_MIN, min(RATE_MAX, predicted))

    def _regression_from_history(
        self, history: list[dict[str, float]]
    ) -> tuple[float | None, float | None]:
        points = self._history_points(history)
        if len(points) < MIN_HISTORY_POINTS:
            return None, None
        return self._linear_regression(points)

    def _history_points(
        self, history: list[dict[str, float]]
    ) -> list[tuple[float, float]]:
        points: list[tuple[float, float]] = []
        for point in history:
            if not isinstance(point, dict):
                continue
            outdoor = point.get("outdoor")
            rate = point.get("rate")
            if outdoor is None or rate is None:
                continue
            try:
                outdoor_value = float(outdoor)
                rate_value = float(rate)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(outdoor_value) or not math.isfinite(rate_value):
                continue
            points.append((outdoor_value, rate_value))
        return points

    def _linear_regression(
        self, points: list[tuple[float, float]]
    ) -> tuple[float | None, float | None]:
        n = len(points)
        if n == 0:
            return None, None
        sum_x = sum(point[0] for point in points)
        sum_y = sum(point[1] for point in points)
        sum_xy = sum(point[0] * point[1] for point in points)
        sum_x2 = sum(point[0] ** 2 for point in points)
        denominator = (n * sum_x2) - (sum_x**2)
        if denominator == 0:
            return None, None
        slope = ((n * sum_xy) - (sum_x * sum_y)) / denominator
        intercept = (sum_y - (slope * sum_x)) / n
        return slope, intercept

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
