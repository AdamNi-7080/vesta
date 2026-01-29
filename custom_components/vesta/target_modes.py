"""Target mode strategies for Vesta temperature selection."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TargetContext:
    schedule_target: float | None
    off_temp: float
    comfort_temp: float
    eco_temp: float
    has_presence_sensors: bool
    presence_on: bool


class TargetMode:
    """Base class for target selection strategies."""

    name = "base"

    def target(self, context: TargetContext) -> float | None:  # pragma: no cover - interface
        raise NotImplementedError

    def is_override(self) -> bool:
        return False


def _apply_presence_boost(target: float, context: TargetContext) -> float:
    if (
        context.has_presence_sensors
        and context.presence_on
        and target <= context.off_temp
    ):
        return max(target, context.comfort_temp)
    return target


class ScheduledTargetMode(TargetMode):
    name = "scheduled"

    def target(self, context: TargetContext) -> float | None:
        target = (
            context.schedule_target
            if context.schedule_target is not None
            else context.off_temp
        )
        return _apply_presence_boost(target, context)


class EcoTargetMode(TargetMode):
    name = "eco"

    def target(self, context: TargetContext) -> float | None:
        return _apply_presence_boost(context.eco_temp, context)


@dataclass(frozen=True)
class BoostTargetMode(TargetMode):
    target_temp: float
    name = "boost"

    def target(self, context: TargetContext) -> float | None:
        return self.target_temp

    def is_override(self) -> bool:
        return True


@dataclass(frozen=True)
class SaveTargetMode(TargetMode):
    target_temp: float
    name = "save"

    def target(self, context: TargetContext) -> float | None:
        return self.target_temp

    def is_override(self) -> bool:
        return True


@dataclass(frozen=True)
class PreheatTargetMode(TargetMode):
    target_temp: float
    name = "preheat"

    def target(self, context: TargetContext) -> float | None:
        return self.target_temp


@dataclass(frozen=True)
class FailsafeTargetMode(TargetMode):
    target_temp: float
    name = "failsafe"

    def target(self, context: TargetContext) -> float | None:
        return self.target_temp
