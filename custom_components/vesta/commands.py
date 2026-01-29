"""Command primitives for Vesta service calls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from homeassistant.components.climate.const import (
    ATTR_HVAC_MODES,
    HVACMode,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_TEMPERATURE,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.util import dt as dt_util

@dataclass(frozen=True)
class CommandResult:
    success: bool
    data: dict[str, Any] | None = None
    error: str | None = None


class Command(Protocol):
    async def execute(self, hass) -> CommandResult: ...

    def summary(self) -> str: ...


@dataclass(frozen=True)
class CommandRecord:
    when: dt_util.dt.datetime
    name: str
    status: str
    detail: str | None = None


class CommandExecutor:
    def __init__(self, hass, *, history_size: int = 50) -> None:
        self._hass = hass
        self._history: list[CommandRecord] = []
        self._history_size = history_size

    @property
    def history(self) -> list[CommandRecord]:
        return list(self._history)

    async def execute(self, command: Command, *, propagate: bool = False) -> CommandResult:
        self._record(command, "queued")
        try:
            result = await command.execute(self._hass)
        except Exception as err:  # pragma: no cover - defensive
            detail = str(err)
            self._record(command, "failed", detail)
            if propagate:
                raise
            return CommandResult(success=False, error=detail)
        detail = result.error if result.error else None
        status = "executed" if result.success else "failed"
        self._record(command, status, detail)
        return result

    def _record(self, command: Command, status: str, detail: str | None = None) -> None:
        name = command.__class__.__name__
        if hasattr(command, "summary"):
            summary = command.summary()
            if summary:
                name = f"{name}({summary})"
        record = CommandRecord(
            when=dt_util.utcnow(),
            name=name,
            status=status,
            detail=detail,
        )
        self._history.append(record)
        if len(self._history) > self._history_size:
            self._history.pop(0)


class BoilerDriver(Protocol):
    entity_id: str
    boost_temp: float
    off_temp: float

    async def turn_on(self, hass) -> CommandResult: ...

    async def turn_off(self, hass) -> CommandResult: ...


@dataclass(frozen=True)
class ClimateBoilerDriver:
    entity_id: str
    boost_temp: float
    off_temp: float

    async def turn_on(self, hass) -> CommandResult:
        state = hass.states.get(self.entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return CommandResult(False, error="entity unavailable")

        current_temp = state.attributes.get(ATTR_TEMPERATURE)
        try:
            current_temp = float(current_temp)
        except (TypeError, ValueError):
            current_temp = None
        if (
            state.state == HVACMode.HEAT
            and current_temp is not None
            and abs(current_temp - self.boost_temp) < 0.1
        ):
            return CommandResult(True)
        if not hass.services.has_service("climate", SERVICE_SET_TEMPERATURE):
            return CommandResult(False, error="set_temperature unavailable")
        if hass.services.has_service("climate", SERVICE_SET_HVAC_MODE):
            await hass.services.async_call(
                "climate",
                SERVICE_SET_HVAC_MODE,
                {ATTR_ENTITY_ID: self.entity_id, "hvac_mode": HVACMode.HEAT},
                blocking=True,
            )
        await hass.services.async_call(
            "climate",
            SERVICE_SET_TEMPERATURE,
            {ATTR_ENTITY_ID: self.entity_id, ATTR_TEMPERATURE: self.boost_temp},
            blocking=True,
        )
        return CommandResult(True)

    async def turn_off(self, hass) -> CommandResult:
        state = hass.states.get(self.entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return CommandResult(False, error="entity unavailable")

        was_on = state.state == HVACMode.HEAT
        if not hass.services.has_service("climate", SERVICE_SET_TEMPERATURE):
            return CommandResult(False, error="set_temperature unavailable")
        state = hass.states.get(self.entity_id)
        hvac_modes = []
        if state is not None:
            hvac_modes = state.attributes.get(ATTR_HVAC_MODES, [])
        if HVACMode.OFF in hvac_modes:
            if hass.services.has_service("climate", SERVICE_SET_HVAC_MODE):
                await hass.services.async_call(
                    "climate",
                    SERVICE_SET_HVAC_MODE,
                    {ATTR_ENTITY_ID: self.entity_id, "hvac_mode": HVACMode.OFF},
                    blocking=True,
                )
        await hass.services.async_call(
            "climate",
            SERVICE_SET_TEMPERATURE,
            {ATTR_ENTITY_ID: self.entity_id, ATTR_TEMPERATURE: self.off_temp},
            blocking=True,
        )
        return CommandResult(True, data={"was_on": was_on})


@dataclass(frozen=True)
class DomainBoilerDriver:
    entity_id: str
    boost_temp: float
    off_temp: float
    domain: str

    async def turn_on(self, hass) -> CommandResult:
        state = hass.states.get(self.entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return CommandResult(False, error="entity unavailable")
        if state.state == STATE_ON:
            return CommandResult(True)
        await hass.services.async_call(
            self.domain,
            "turn_on",
            {ATTR_ENTITY_ID: self.entity_id},
            blocking=True,
        )
        return CommandResult(True)

    async def turn_off(self, hass) -> CommandResult:
        state = hass.states.get(self.entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return CommandResult(False, error="entity unavailable")
        was_on = state.state == STATE_ON
        await hass.services.async_call(
            self.domain,
            "turn_off",
            {ATTR_ENTITY_ID: self.entity_id},
            blocking=True,
        )
        return CommandResult(True, data={"was_on": was_on})


def build_boiler_driver(
    entity_id: str, boost_temp: float, off_temp: float
) -> BoilerDriver:
    domain = entity_id.split(".", 1)[0]
    if domain == "climate":
        return ClimateBoilerDriver(entity_id, boost_temp, off_temp)
    return DomainBoilerDriver(entity_id, boost_temp, off_temp, domain)


@dataclass(frozen=True)
class TurnBoilerOnCommand:
    driver: BoilerDriver

    def summary(self) -> str:
        return f"{self.driver.entity_id} -> {self.driver.boost_temp}"

    async def execute(self, hass) -> CommandResult:
        return await self.driver.turn_on(hass)


@dataclass(frozen=True)
class TurnBoilerOffCommand:
    driver: BoilerDriver

    def summary(self) -> str:
        return f"{self.driver.entity_id} -> {self.driver.off_temp}"

    async def execute(self, hass) -> CommandResult:
        return await self.driver.turn_off(hass)


@dataclass(frozen=True)
class SetTrvModeAndTempCommand:
    entity_ids: list[str]
    hvac_mode: HVACMode
    temperature: float

    def summary(self) -> str:
        return f"{len(self.entity_ids)} -> {self.temperature}"

    async def execute(self, hass) -> CommandResult:
        if not self.entity_ids:
            return CommandResult(True)
        await hass.services.async_call(
            "climate",
            SERVICE_SET_HVAC_MODE,
            {ATTR_ENTITY_ID: self.entity_ids, "hvac_mode": self.hvac_mode},
            blocking=True,
        )
        await hass.services.async_call(
            "climate",
            SERVICE_SET_TEMPERATURE,
            {ATTR_ENTITY_ID: self.entity_ids, ATTR_TEMPERATURE: self.temperature},
            blocking=True,
        )
        return CommandResult(True)
