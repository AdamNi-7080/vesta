"""Target mode strategies for Vesta temperature selection."""

from __future__ import annotations

from dataclasses import dataclass

MIN_TARGET_TEMP = 5.0
MAX_TARGET_TEMP = 30.0


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

    def calculate_final_target(self, context: TargetContext) -> float | None:
        raw = self._get_raw_target(context)
        if raw is None:
            return None
        if self._should_apply_presence_boost(context):
            raw = _apply_presence_boost(raw, context)
        return _clamp_target(raw)

    def target(self, context: TargetContext) -> float | None:
        return self.calculate_final_target(context)

    def _get_raw_target(
        self, context: TargetContext
    ) -> float | None:  # pragma: no cover - interface
        raise NotImplementedError

    def _should_apply_presence_boost(self, context: TargetContext) -> bool:
        return True

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


def _clamp_target(target: float) -> float:
    return max(MIN_TARGET_TEMP, min(MAX_TARGET_TEMP, target))


class ScheduledTargetMode(TargetMode):
    name = "scheduled"

    def _get_raw_target(self, context: TargetContext) -> float | None:
        return (
            context.schedule_target
            if context.schedule_target is not None
            else context.off_temp
        )


class EcoTargetMode(TargetMode):
    name = "eco"

    def _get_raw_target(self, context: TargetContext) -> float | None:
        return context.eco_temp


@dataclass(frozen=True)
class BoostTargetMode(TargetMode):
    target_temp: float
    name = "boost"

    def _get_raw_target(self, context: TargetContext) -> float | None:
        return self.target_temp

    def _should_apply_presence_boost(self, context: TargetContext) -> bool:
        return False

    def is_override(self) -> bool:
        return True


@dataclass(frozen=True)
class SaveTargetMode(TargetMode):
    target_temp: float
    name = "save"

    def _get_raw_target(self, context: TargetContext) -> float | None:
        return self.target_temp

    def _should_apply_presence_boost(self, context: TargetContext) -> bool:
        return False

    def is_override(self) -> bool:
        return True


@dataclass(frozen=True)
class PreheatTargetMode(TargetMode):
    target_temp: float
    name = "preheat"

    def _get_raw_target(self, context: TargetContext) -> float | None:
        return self.target_temp

    def _should_apply_presence_boost(self, context: TargetContext) -> bool:
        return False


@dataclass(frozen=True)
class FailsafeTargetMode(TargetMode):
    target_temp: float
    name = "failsafe"

    def _get_raw_target(self, context: TargetContext) -> float | None:
        return self.target_temp

    def _should_apply_presence_boost(self, context: TargetContext) -> bool:
        return False
