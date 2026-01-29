from datetime import datetime, timedelta, timezone

from custom_components.vesta.domain.climate import (
    calculate_temperature_compensation,
    compute_preheat_start,
)


def test_temperature_compensation_basic():
    result = calculate_temperature_compensation(20.0, 18.0)
    assert result.error == 2.0
    assert result.compensated_target == 24.0
    assert result.clamped_target == 24.0


def test_temperature_compensation_clamped_min():
    result = calculate_temperature_compensation(5.0, 30.0)
    assert result.clamped_target == 5.0


def test_temperature_compensation_clamped_max():
    result = calculate_temperature_compensation(30.0, 5.0)
    assert result.clamped_target == 30.0


def test_preheat_start_disallowed():
    effective_at = datetime(2026, 1, 27, 8, 0, tzinfo=timezone.utc)
    assert (
        compute_preheat_start(
            current_temp=18.0,
            target_temp=21.0,
            effective_at=effective_at,
            heating_rate=1.5,
            allow_preheat=False,
        )
        is None
    )


def test_preheat_start_valid():
    effective_at = datetime(2026, 1, 27, 8, 0, tzinfo=timezone.utc)
    start_at = compute_preheat_start(
        current_temp=18.0,
        target_temp=21.0,
        effective_at=effective_at,
        heating_rate=1.5,
        allow_preheat=True,
    )
    assert start_at == effective_at - timedelta(hours=2)
