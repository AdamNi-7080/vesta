# Changelog

## Unreleased
- None yet.

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
