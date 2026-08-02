# SigenStor monitoring

Local, cloud-free monitoring for a SigenStor inverter/battery over Modbus TCP.
A Python collector polls the plant-level registers every few seconds and writes
them to InfluxDB; Grafana visualizes them. Everything runs in Docker on your
own network - nothing leaves the house.

```text/plain
SigenStor (192.168.68.56:502, Modbus TCP)          Open-Meteo (forecast API)
        |                                                    |
   collector (Python / pymodbus)          weather-collector (Python / requests)
        |                                                    |
        +---------------------> InfluxDB <-------------------+
                                    |
                                 Grafana (http://localhost:3000)
```

## What's collected

Registers poll at different rates depending on how fast they actually change -
a single 1-second tick loop in `collector.py` decides what's due each second
via `now % register.interval_seconds == 0`, so a 5s register and a 300s
register share one loop without the slow one holding up the fast one. Three
tiers, each configurable in `.env`:

- **`FAST_POLL_SECONDS`** (default 5s) - live power flow: plant/PV/battery/grid
  power, SOC, PV string voltage/current.
- **`MEDIUM_POLL_SECONDS`** (default 30s) - state that changes occasionally:
  EMS work mode, on/off-grid status, alarms, gateway connection.
- **`SLOW_POLL_SECONDS`** (default 300s) - slow-moving cumulative counters:
  daily/lifetime load, imported/exported energy.

All registers, read via function code 4 (read input registers):

| Field | Register | Meaning |
| --- | --: | --- |
| `pv_power_kw` | 30035 | PV generation (total, all strings) |
| `battery_power_kw` | 30037 | Battery power: **>0 charging, <0 discharging** |
| `grid_power_kw` | 30005 | Grid power: **>0 import (buy), <0 export (sell)** |
| `plant_active_power_kw` | 30031 | Total plant active power |
| `battery_soc_pct` | 30014 | Battery state of charge |
| `running_state` | 30051 | Plant running state code |
| `grid_power_phase_a_kw` | 30052 | Grid power on phase A (same sign convention) |
| `grid_power_phase_b_kw` | 30054 | Grid power on phase B |
| `grid_power_phase_c_kw` | 30056 | Grid power on phase C |
| `grid_sensor_connected` | 30004 | Whether the Gateway/CT sensor is online |
| `pv1_voltage_v`, `pv1_current_a` | 31027/31028 | PV string 1 (inverter-level, slave id `INVERTER_SLAVE_ID`) |
| `pv2_voltage_v`, `pv2_current_a` | 31029/31030 | PV string 2 |
| `ems_work_mode` | 30003 | Active strategy: self-consumption / AI / TOU / full feed-in / remote EMS / custom |
| `on_off_grid_status` | 30009 | 0 on grid, 1 off grid (auto), 2 off grid (manual) |
| `general_alarm1`..`general_alarm5` | 30027-30030, 30072 | Fault/alarm codes, 0 = clear |
| `total_load_daily_kwh` | 30092 | Household consumption, resets daily |
| `total_load_kwh` | 30094 | Household consumption, lifetime |
| `total_imported_energy_kwh` | 30216 | Grid import, lifetime |
| `total_exported_energy_kwh` | 30220 | Grid export, lifetime |

**On the Sigen Gateway (C60-2):** it has no Modbus registers of its own - it's
the CT-clamp/meter hardware sitting behind the `[Grid Sensor]` registers
above (grid power, per-phase grid power). `grid_sensor_connected` (30004) is
its online/offline status. So its data was already being collected; nothing
extra was needed once that mapping was clear.

**On the 2 PV strings:** these live in a separate register table (section
5.3 of the protocol) that's only readable via the *inverter's* own Modbus
slave id, not the plant address 247 - `INVERTER_SLAVE_ID` in `.env` (default
`1`). This SigenStor actually reports 3 available PV string inputs (MPPT
count 3), of which only PV1/PV2 read non-zero here - PV3 is present in the
register map but unwired, so it's left out of collection.

Addresses, scale factors, and sign conventions above are from Sigenergy's
Modbus Protocol V2.7 (2025-05-23), section 5.1, and were verified against a
live device before this was built. The plant-level totals (`grid_power_kw`,
`plant_active_power_kw`, `pv_power_kw`, `battery_power_kw`) already sum
across all phases, so a 3-phase installation doesn't change those - but the
protocol separately exposes true per-phase registers (`Plant phase A/B/C
active power` at 30015/30017/30019, and `[Grid sensor] Phase A/B/C active
power` at 30052/30054/30056), which only exist because this is a 3-phase
system. The grid ones are collected here since they're the most actionable:
useful for spotting phase imbalance, which matters if your DNO enforces a
per-phase export limit rather than a total one, or if a single-phase load
(EV charger, immersion) is skewing one phase.

The full register map has many more fields (per-inverter detail, energy
counters, alarms, per-load consumption for up to 24 "smart loads", and the
write-capable holding registers for remote control). This starts with the
plant-level essentials; extending `collector/collector.py`'s `REGISTERS`
list is straightforward - see Sigenergy's Modbus Protocol PDF (ask
Sigenergy support/installer for the latest version) or the community
register tables in
[TypQxQ/Sigenergy-Local-Modbus](https://github.com/TypQxQ/Sigenergy-Local-Modbus)
and [seud0nym/sigenergy2mqtt](https://github.com/seud0nym/sigenergy2mqtt).

## Weather (Open-Meteo)

`weather-collector/weather_collector.py` polls Open-Meteo's free forecast
API (no key needed) hourly and writes to a separate `weather` measurement in
the same InfluxDB bucket, tagged `source: open-meteo`:

| Field | Meaning |
| --- | --- |
| `cloud_cover_pct` | Total cloud cover |
| `shortwave_radiation_wm2` | Global horizontal irradiance - the main PV driver |
| `direct_radiation_wm2` | Direct beam component |
| `diffuse_radiation_wm2` | Diffuse (scattered) component |
| `temperature_c` | 2m air temperature |

Each poll fetches `WEATHER_PAST_DAYS` (default 92, Open-Meteo's cap) of
recent history plus `WEATHER_FORECAST_DAYS` (default 16) ahead, and writes
every hourly point keyed by its own timestamp - so re-polling naturally
refines past hours toward reanalysis and future hours toward a shorter,
more accurate forecast, since InfluxDB takes the last write per timestamp.
Set `WEATHER_LATITUDE`/`WEATHER_LONGITUDE` in `.env` to the site's
coordinates.

This exists to pair with PV history for forecasting work - see
`forecasting/`.

## Setup

1. On the SigenStor, confirm Modbus TCP Server is enabled (installer setting).
2. `cp .env.example .env` and edit it - at minimum set a real `INFLUXDB_TOKEN`
   and change the admin passwords.
3. `docker compose up -d --build`
4. Open Grafana at <http://localhost:3000> (login from `.env`). The "SigenStor"
   dashboard is auto-provisioned.
5. Open InfluxDB at <http://localhost:8086> if you want to run ad-hoc Flux
   queries against the raw data.

Check collector logs with `docker compose logs -f collector` - it logs every
successful poll and any Modbus connection errors. Check weather-collector
logs with `docker compose logs -f weather-collector` similarly.

## Deploying the collector as a systemd service (Ubuntu Server)

InfluxDB and Grafana are left running in Docker Compose (unchanged) - only the
collector moves to a native systemd service. Files for this are in
`deploy/systemd/`. **These haven't been tested on an actual Ubuntu box** (this
was written on macOS) - treat as a solid starting point, not a verified
recipe; check `systemctl status` / `journalctl` carefully after first deploy.

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin sigenstor

sudo mkdir -p /opt/sigenstor-collector /etc/sigenstor-collector
sudo cp collector/collector.py collector/requirements.txt /opt/sigenstor-collector/
sudo python3 -m venv /opt/sigenstor-collector/venv
sudo /opt/sigenstor-collector/venv/bin/pip install -r /opt/sigenstor-collector/requirements.txt

sudo cp deploy/systemd/collector.env.example /etc/sigenstor-collector/collector.env
sudo $EDITOR /etc/sigenstor-collector/collector.env   # fill in INFLUXDB_TOKEN, confirm INFLUXDB_URL
sudo chown sigenstor:sigenstor /etc/sigenstor-collector/collector.env
sudo chmod 600 /etc/sigenstor-collector/collector.env
sudo chown -R sigenstor:sigenstor /opt/sigenstor-collector

sudo cp deploy/systemd/sigenstor-collector.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sigenstor-collector
journalctl -u sigenstor-collector -f
```

Things specific to this move, beyond the obvious "write a unit file":

- **`INFLUXDB_URL` changes.** Docker Compose's internal DNS name
  (`http://influxdb:8086`) only resolves inside the Compose network. Once the
  collector runs outside Docker, it needs `http://localhost:8086` (InfluxDB's
  container still publishes that port on this host) or a LAN IP/hostname if
  InfluxDB ever moves elsewhere. `deploy/systemd/collector.env.example` sets
  this explicitly.
- **Modbus vs InfluxDB failures are now handled separately in the code** - a
  failed Modbus read closes and reconnects the Modbus session; a failed
  InfluxDB write just logs and retries next tick, leaving Modbus alone. This
  matters more once this is unattended, always-on infrastructure rather than
  something running in a terminal you're watching.
- **PEP 668 on 26.04**: `pip install` outside a venv will refuse to run
  ("externally-managed-environment") - the steps above use a venv, so this
  shouldn't bite, but it's the first thing to check if install fails.
- **No local persistence for dropped writes.** If InfluxDB is briefly
  unreachable, that tick's data is just lost (by design, for simplicity) -
  worth revisiting if gapless history matters more once this is unattended.
- The unit ships with fairly aggressive sandboxing (`ProtectSystem=strict`,
  no filesystem writes, etc.) since the collector's only job is outbound
  network I/O and it holds an InfluxDB token in its environment.

## Where this can go next

A few more registers were identified but deliberately left out for now (lower
urgency, not "no"): battery SOH (30087) and cell temp/voltage min-max
(30620-30623) for long-term battery health; PCS internal temperature (31003);
and per-phase grid *voltage* (31011-31016), worth watching since UK DNOs
sometimes throttle export due to voltage rise on 3-phase connections. Also
skipped: the 24 "smart load" registers (only relevant if sub-circuit
monitoring is configured in the app) and third-party inverter/EVDC fields
(not installed here).

This stack is deliberately monitoring-only. The same register map exposes
write-capable holding registers (plant-level EMS work mode, charge/discharge
power limits, export limits), so once you've watched enough real data to
know what "the right behaviour" looks like, a control/strategy layer (e.g.
reacting to a dynamic tariff, or holding a reserve for a peak window) can be
added as a separate service that writes those registers - without needing to
change the collector or dashboard.
