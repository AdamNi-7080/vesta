# Changelog

## Unreleased
- None yet.

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
