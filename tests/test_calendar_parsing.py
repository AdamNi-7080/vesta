from datetime import datetime, timezone

from custom_components.vesta.calendar_handler import _event_start, _event_target


class _Config:
    time_zone = "UTC"


class _Hass:
    config = _Config()


def test_event_target_from_summary():
    assert _event_target({"summary": "21"}) == 21.0


def test_event_target_from_description():
    assert _event_target({"description": "Target 21.5C"}) == 21.5


def test_event_target_none():
    assert _event_target({"summary": "Comfort"}) is None


def test_event_start_datetime():
    hass = _Hass()
    event = {"start": {"dateTime": "2026-01-27T08:00:00+00:00"}}
    dt_value = _event_start(hass, event)
    assert dt_value == datetime(2026, 1, 27, 8, 0, tzinfo=timezone.utc)


def test_event_start_date():
    hass = _Hass()
    event = {"start": {"date": "2026-01-27"}}
    dt_value = _event_start(hass, event)
    assert dt_value == datetime(2026, 1, 27, 0, 0, tzinfo=timezone.utc)
