"""Domain calculations for Vesta climate control."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

DEFAULT_COMPENSATION_GAIN = 2.0
DEFAULT_COMPENSATION_MIN = 5.0
DEFAULT_COMPENSATION_MAX = 30.0


@dataclass(frozen=True)
class TemperatureCompensation:
    """Result of a temperature compensation calculation."""

    error: float
    compensated_target: float
    clamped_target: float


def calculate_temperature_compensation(
    target_temp: float,
    current_temp: float,
    *,
    gain: float = DEFAULT_COMPENSATION_GAIN,
    min_temp: float = DEFAULT_COMPENSATION_MIN,
    max_temp: float = DEFAULT_COMPENSATION_MAX,
) -> TemperatureCompensation:
    """Calculate a compensated target temperature for TRVs."""
    error = target_temp - current_temp
    compensated = target_temp + (error * gain)
    clamped = max(min_temp, min(max_temp, compensated))
    return TemperatureCompensation(
        error=error,
        compensated_target=compensated,
        clamped_target=clamped,
    )


def compute_preheat_start(
    *,
    current_temp: float | None,
    target_temp: float,
    effective_at: datetime,
    heating_rate: float,
    allow_preheat: bool,
) -> datetime | None:
    """Return when to start preheating to reach target by effective_at."""
    if not allow_preheat:
        return None
    if current_temp is None:
        return None
    if target_temp <= current_temp:
        return None
    if heating_rate <= 0:
        return None
    seconds = ((target_temp - current_temp) / heating_rate) * 3600
    if seconds <= 0:
        return None
    return effective_at - timedelta(seconds=seconds)
