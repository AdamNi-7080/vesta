# Vesta
The Zero-Config Heating Engine for Home Assistant.

![GitHub Release](https://img.shields.io/github/v/release/AdamNi-7080/vesta)
![License](https://img.shields.io/github/license/AdamNi-7080/vesta)
![Maintained](https://img.shields.io/badge/Maintained%3F-yes-brightgreen.svg)

[![Open in Home Assistant](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=AdamNi-7080&repository=vesta&category=integration)

## Elevator Pitch
Vesta brings **Tado-level intelligence** to affordable Zigbee TRVs—**without the cloud**. It automatically discovers your Home Assistant Areas, builds smart heating zones, and learns how your home behaves so it can pre-heat efficiently and stay comfortable with minimal setup.

## Feature Highlights
- 🧠 **Adaptive Learning**: Physics-based pre-heating that learns your home’s thermal properties.
- 🛡️ **Boiler Guard**: Anti-cycle protection and safe fail-states.
- 🔋 **Battery Failsafe**: Prevents frozen pipes when TRVs die.
- ☀️ **Solar Awareness**: Smarter heating on sunny days.
- 👻 **Zombie Management**: Ignores dead/unreachable devices automatically.

## Installation
### HACS (Recommended)
1. Open HACS in Home Assistant.
2. Click the three dots (top right) → **Custom repositories**.
3. Paste your GitHub URL (e.g. `https://github.com/AdamNi-7080/vesta`).
4. Type: **Integration** → **Add**.
5. Download **Vesta**, then restart Home Assistant.
6. Add the **Vesta** integration from **Settings → Devices & Services**.

### Manual (for development)
1. Copy `custom_components/vesta` into your Home Assistant `config/custom_components/`.
2. Restart Home Assistant.
3. Add the **Vesta** integration from **Settings → Devices & Services**.

## Quick Start
1. Install.
2. Restart.
3. Add the integration.
4. Done. Vesta auto-discovers zones from your Areas.

## Configuration Reference
Vesta uses a UI config flow (no YAML required). All optional settings can be edited later via the integration options.

| Option             | Required | Default  | Description                                              |
|--------------------|----------|----------|----------------------------------------------------------|
| Boiler Entity      | ✅        | —        | The boiler controller (climate/switch/input_boolean).    |
| Weather Entity     | ✅        | —        | Used for learning buckets and solar awareness.           |
| Boost Temperature  | ❌        | 25°C     | Manual boost target temperature.                         |
| Minimum Cycle Time | ❌        | 5 min    | Anti-cycle protection window.                            |
| Valve Maintenance  | ❌        | On       | Weekly valve exercise routine.                           |
| Maintenance Day    | ❌        | Thursday | Day of week for valve exercise.                          |
| Maintenance Time   | ❌        | 11:00    | Time for valve exercise (local time).                    |
| Bermuda Threshold  | ❌        | 2.5 m    | Distance presence threshold (if using distance sensors). |

## How It Works
- Each Area with one or more real `climate.*` entities becomes a Vesta zone.
- Vesta creates a virtual thermostat: `climate.<area>_vesta`.
- Schedules target `number.<area>_schedule_target` (Scheduler Card friendly).

### Manual Override Logic
- **Boost**: setting a temperature higher than the schedule starts a 90‑minute timer.
- **Save**: setting a temperature lower than the schedule holds indefinitely.
- **Resume**: setting to the schedule cancels any active override.

### Pre-Heating (Predictive Start)
Vesta can start heating early using learned rates to hit a target at a specific time.

#### Zero‑Config Calendar Discovery
If an Area contains a `calendar.*` entity, Vesta will poll it every 15 minutes and use the **next event**:
- **Event title or description** should be a numeric temperature (e.g. `21` or `21.0`)
- **Event start time** becomes the target’s effective time

#### Advanced: Service Hook
You can also call `vesta.set_schedule` to set a future target programmatically:
```yaml
service: vesta.set_schedule
data:
  area_name: "Living Room"
  target: 21
  effective_at: "2026-01-27T08:00:00+00:00"
```

## Notes
- If no window sensors exist, a rapid temperature drop triggers a 15‑minute heating pause.
- If no presence sensors exist, presence-based boost is ignored.
- If the configured weather entity is missing, learning defaults to the “cool” bucket.
- If all TRVs in an area are unreachable, Vesta skips set commands and logs a warning.
- If a battery failsafe is active, calendar and schedule updates are ignored until batteries recover.
- To exclude a specific device (for example, a rogue temperature sensor or a specific TRV) from Vesta's control, create a Label in Home Assistant named `vesta_ignore` and assign it to the device or entity. Vesta will skip it during discovery.

## Logging & Debugging
Vesta logs are namespaced under `custom_components.vesta` (and submodules like `custom_components.vesta.coordinator`). For deep troubleshooting, enable debug logs for the integration:

```yaml
logger:
  default: info
  logs:
    custom_components.vesta: debug
```

Helpful log signals include:
- Boiler state transitions (idle, firing, anti-cycle, failsafe).
- Demand changes and debounce timing.
- Window hold triggers and clears.
- Schedule updates and calendar-derived targets.

## File Structure
```
vesta/
├── .github/
│   └── workflows/
│       └── validate.yml
├── custom_components/
│   └── vesta/
│       ├── __init__.py
│       ├── manifest.json
│       ├── const.py
│       ├── config_flow.py
│       ├── climate.py
│       ├── number.py
│       ├── switch.py
│       ├── coordinator.py
│       ├── learning.py
│       └── services.yaml
├── hacs.json
├── LICENSE
├── README.md
└── tests/
    └── test_calendar_parsing.py
```
