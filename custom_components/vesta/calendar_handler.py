"""Calendar polling and parsing helpers for Vesta."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import re

from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class CalendarDecision:
    target: float
    start: dt_util.dt.datetime
    is_active: bool


class CalendarHandler:
    """Handle calendar polling and decision making."""

    def __init__(self, hass, calendar_entity: str) -> None:
        self._hass = hass
        self._calendar_entity = calendar_entity
        self._last_signature: tuple[dt_util.dt.datetime, float] | None = None
        self._suppressed_signature: tuple[dt_util.dt.datetime, float] | None = None

    def suppress_last_event(self) -> None:
        if self._last_signature is None:
            return
        self._suppressed_signature = self._last_signature

    async def async_poll(
        self, now: dt_util.dt.datetime | None = None
    ) -> CalendarDecision | None:
        if not self._calendar_entity:
            return None
        if self._hass.states.get(self._calendar_entity) is None:
            _LOGGER.debug(
                "Calendar entity %s not ready yet", self._calendar_entity
            )
            return None
        if now is None:
            now = dt_util.utcnow()
        start_search = now - timedelta(hours=24)
        end = now + timedelta(days=7)
        events = await _fetch_calendar_events(
            self._hass, self._calendar_entity, start_search, end
        )
        _LOGGER.debug(
            "Calendar fetch: %s events found between %s and %s",
            len(events),
            start_search,
            end,
        )
        if not events:
            return None
        next_event, is_active = _next_calendar_event(self._hass, now, events)
        if not next_event:
            return None
        start = _event_start(self._hass, next_event)
        if start is None:
            return None

        if is_active:
            target = _event_target(next_event)
            if target is None:
                return None
            return CalendarDecision(
                target=target, start=start, is_active=True
            )

        if start <= now:
            return None
        target = _event_target(next_event)
        if target is None:
            return None

        signature = (start, target)
        if signature == self._suppressed_signature:
            return None
        if signature == self._last_signature:
            return None

        self._last_signature = signature
        return CalendarDecision(target=target, start=start, is_active=False)


async def _fetch_calendar_events(
    hass,
    calendar_entity: str,
    start: dt_util.dt.datetime,
    end: dt_util.dt.datetime,
) -> list[dict]:
    try:
        response = await hass.services.async_call(
            "calendar",
            "get_events",
            {
                "entity_id": calendar_entity,
                "start_date_time": start.isoformat(),
                "end_date_time": end.isoformat(),
            },
            blocking=True,
            return_response=True,
        )
    except Exception as err:  # pragma: no cover - defensive
        _LOGGER.warning("Calendar poll failed: %s", err)
        return []
    return _extract_calendar_events(response, calendar_entity)


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
