# Changelog

## Unreleased
- None yet.

## 0.2.3
- Avoid redundant TRV commands when targets are already applied.

## 0.2.2
- Fix Home Assistant startup listener to run on the main loop.

## 0.2.1
- Make sensor-driven updates lazy to reduce boiler recalculation churn.
- Clear window detection history after holds to prevent repeat triggers.
- Treat UNKNOWN/UNAVAILABLE master switch states as "on" for safety.

## 0.2.0
- Refactor thermostat math into a domain layer and split calendar/presence/window logic into dedicated modules.
- Add strategy/command/observer patterns for boiler drivers, valve control, and manager notifications.
- Introduce boiler state machine, circuit breaker, demand batching, and exponential retry backoff.
- Replace bucketed learning with linear regression rates (slope/intercept) and expose new attributes.
- Add vesta_include label support with auto-created labels for include/ignore discovery control.
- Expand integration logging for tracing demand, state transitions, and safety events.
- Add comprehensive coordinator/manager/integration test coverage and pytest config defaults.

## 0.1.14
- Fix startup boiler retry scheduling when last_off is unknown.
- Prevent double-unsubscribe on Home Assistant start.

## 0.1.13
- Expose preheat status and learned rates in climate attributes.

## 0.1.12
- Add device triggers for preheat, window, and failure events.

## 0.1.11
- Add configurable comfort temperature and use it as schedule fallback.
- Safe calendar polling and default schedule/eco values on startup.

## 0.1.10
- Expand calendar fetch window to include recent past events.

## 0.1.9
- Fix home/away detection for zone.home numeric state.

## 0.1.8
- Disable presence and proximity discovery (temporary).
- Apply active calendar events immediately after restart.

## 0.1.7
- Add UI strings for config/options flows.
- Filter noisy diagnostic sensors from generic presence discovery.
- Expose calendar entity attribute and log calendar discovery.
- Fallback to TRV temperature if external sensors are unavailable.

## 0.1.6
- Fix area discovery to use device registry area assignments.

## 0.1.5
- Fix startup ordering to avoid climate service errors.
- Fix options flow day selector values for frontend compatibility.

## 0.1.4
- Fix options flow time default serialization for HA frontend.

## 0.1.3
- Fix options flow creation to use Home Assistant's current handler pattern.

## 0.1.2
- Fix options flow initialization for Home Assistant.

## 0.1.1
- Fix temperature unit import for newer Home Assistant versions.

## 0.1.0
- Initial release.
- Area discovery for TRVs, sensors, and calendars with `vesta_ignore` label support.
- UI config flow + options flow (edit settings post-install, including maintenance schedule).
- Adaptive pre-heating with learned heating rates.
- Cooling-rate learning with solar-aware buckets and fallback.
- System health monitoring attributes (boiler failure/runaway detection).
- Sensor fusion for room temperature + humidity aggregation.
- Presence detection (binary, distance, and generic string sensors).
- Battery failsafe with safety temperature lockout.
- Valve maintenance routine (user-configurable day/time).
- Compensated TRV setpoints to reduce premature valve close.
- Reachable-TRV filtering to avoid zombie devices.
