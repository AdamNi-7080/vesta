from custom_components.vesta.target_modes import (
    BoostTargetMode,
    EcoTargetMode,
    FailsafeTargetMode,
    SaveTargetMode,
    ScheduledTargetMode,
    TargetContext,
)


def test_scheduled_fallback_with_presence_boost():
    context = TargetContext(
        schedule_target=None,
        off_temp=12.0,
        comfort_temp=20.0,
        eco_temp=16.0,
        has_presence_sensors=True,
        presence_on=True,
    )
    assert ScheduledTargetMode().target(context) == 20.0


def test_eco_mode_uses_eco_temp():
    context = TargetContext(
        schedule_target=21.0,
        off_temp=12.0,
        comfort_temp=20.0,
        eco_temp=16.0,
        has_presence_sensors=False,
        presence_on=False,
    )
    assert EcoTargetMode().target(context) == 16.0


def test_override_modes_return_target():
    context = TargetContext(
        schedule_target=21.0,
        off_temp=12.0,
        comfort_temp=20.0,
        eco_temp=16.0,
        has_presence_sensors=False,
        presence_on=False,
    )
    boost = BoostTargetMode(23.0)
    save = SaveTargetMode(18.5)
    assert boost.target(context) == 23.0
    assert save.target(context) == 18.5
    assert boost.is_override()
    assert save.is_override()


def test_failsafe_mode_returns_target():
    context = TargetContext(
        schedule_target=21.0,
        off_temp=12.0,
        comfort_temp=20.0,
        eco_temp=16.0,
        has_presence_sensors=False,
        presence_on=False,
    )
    assert FailsafeTargetMode(15.0).target(context) == 15.0
