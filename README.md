# Home energy & MVHR monitoring and control

Local, cloud-free monitoring for a SigenStor inverter/battery and a Variheat
MVHR/heat pump unit, both over Modbus TCP, plus Open-Meteo weather - and a
first step into control, bridging MQTT occupancy commands to the MVHR over
the same Modbus link. Python collectors poll their respective devices every
few seconds and write to InfluxDB; Grafana visualizes them. Everything runs
in Docker on your own network - nothing leaves the house.

```text/plain
SigenStor (192.168.68.56:502, Modbus TCP)          Open-Meteo (forecast API)          Variheat MVHR (192.168.68.66:502, Modbus TCP)
        |                                                    |                                            |          ^
   collector (Python / pymodbus)          weather-collector (Python / requests)         variheat-collector (Python / pymodbus)
        |                                                    |                                            |          |
        +---------------------> InfluxDB <-------------------+<-------------------------------------------+          |
                                    |                                                                                 |
                                 Grafana (http://localhost:3000)                          variheat-control (Python / pymodbus + MQTT)
                                                                                                             ^
                                                                                                             |
                                                                                          MQTT (mosquitto, existing Node-RED topics)
```

## SigenStor (Modbus)

Registers poll at different rates depending on how fast they actually change -
a single 1-second tick loop in `sigenstor_collector.py` decides what's due each second
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
plant-level essentials; extending `sigenstor-collector/sigenstor_collector.py`'s `REGISTERS`
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

## Variheat MVHR (Modbus)

`variheat-collector/variheat_collector.py` polls a Calorex Variheat AW600
(a pool-hall MVHR/heat pump unit, driven by a Variheat M172 PLC) over Modbus
TCP and writes to a `variheat` measurement in the same InfluxDB bucket,
tagged `source: mvhr`. Same fast/medium tiered-polling structure as the
SigenStor collector, configurable via `VARIHEAT_FAST_POLL_SECONDS` /
`VARIHEAT_MEDIUM_POLL_SECONDS` in `.env`.

| Field | Register | Meaning |
| --- | --: | --- |
| `air_probe_c` / `air_set_point_c` | 16792 / 16790 | Air temperature: measured / setpoint |
| `water_probe_c` / `water_set_point_c` | 16812 / 16810 | Pool water temperature: measured / setpoint |
| `humidity_probe_pct` / `humidity_set_point_pct` | 16780 / 16778 | Relative humidity: measured / setpoint |
| `ambient_probe_c` | 16824 | Outdoor ambient temperature |
| `unit_on`, `occupied`, `standby_switch`, `operation_switch` | 17050, 16928, 16888, 16890 | Overall unit state (`operation_switch` enables/disables air+humidity control, for summer use with pool hall doors open) |
| `compressor`, `air_heating`, `water_heating`, `defrost_active`, `frost_protection_active`, `fans_enable`, `boiler`, `pool_pump`, `reversing_valve` | 16902, 16904, 16906, 16910, 16912, 16946, 16938, 16940, 16944 | Run-state booleans - see below |
| `pressure_fault`, `fire_alarm`, `fan_blockage`, `pool_pump_fault`, `service_due`, `main_fan_alarm`, `clock_needs_setting`, `fault_bms` | 16914, 16916, 16918, 16920, 16922, 16924, 16926, 16942 | Alarms/faults, 0 = clear |

**There's no power register in this map.** The run-state booleans
(compressor/air heating/water heating/defrost/fans/boiler/pool pump) are the
closest proxy for the MVHR's electrical draw, and the reason this is worth
integrating alongside SigenStor rather than as a standalone dashboard: they
let you correlate a grid-import spike in the `sigenstor` measurement
(`grid_power_kw`) with what the MVHR was actually doing at that moment
(e.g. defrost cycles are usually the biggest and shortest spikes).

**Protocol details empirically determined, not in Dantherm's connection
guide** (the PDF documents register addresses only, in IEC61131 1-based
syntax, and how to reach the unit over its RS485 port - this unit is
instead reached over Modbus TCP via its Ethernet port, which the guide
doesn't fully cover):

- Modbus unit/slave id is **255** - not the "Slave Address" shown in the
  PLC's BMS settings menu (that setting is for its RS485 Modbus/RTU port).
- Every register, including nominally read-only ones, is read via **function
  code 3** (read holding registers) - function code 4 (read input registers,
  what SigenStor uses) errors on all of them.
- 32-bit values (REAL, UDINT) decode with **word order "little"** (low word
  first, matching the doc's own note about double-length variables) -
  confirmed by reading the PLC's own live clock registers and checking they
  match wall-clock time.
- Probe *reading* registers (`*_probe_c`, `humidity_probe_pct`) come back
  **pre-scaled x10** versus their setpoint counterparts - e.g. a raw
  humidity reading of 322 against a documented 15-80% setpoint range, so
  322% is impossible but 32.2% isn't. Setpoint registers are the literal
  value with no scaling.

See `variheat-collector/variheat_collector.py`'s module docstring for the
full details. The full register map (see the PDF at the repo root, not
tracked in git) has plenty more - schedule/DST/dance-hall settings, service
dates, per-day occupancy periods, and further write-capable holding
registers for remote control (dampers, etc.) - left out of the collector
for now, but see the next section for the one write-capable register
already in use.

### Node-RED MQTT bridge (retiring BACnet reads)

Separately from Node-RED (`node-red.json` at the repo root - a fuller export
of the user's actual, much larger Node-RED instance, covering lighting,
other rooms, etc. as well as the pool), the MVHR is *also* monitored there
via `node-red-contrib-bacnet`: a 1-second polling loop (tab "Monitoring" +
the "Variheat AW600" subflow) reads ~13 BACnet points and republishes them,
retained, to `variheat/*` MQTT topics that feed the "Pool UI" dashboard
(gauges/LEDs) and the "Circulation Pump" tab's pump-on/off logic. This is
the same BACnet interface with the known firmware bug (see "Occupancy
control bridge" below) - reason enough to be skeptical of leaning on it for
anything beyond what's already there.

`variheat_collector.py` optionally republishes its own Modbus readings to
those same retained topics - set `MQTT_HOST` (reusing the same broker as
the occupancy bridge below) and it's enabled automatically; leave it unset
and this collector behaves exactly as before. Only the topics something in
Node-RED actually subscribes to are covered (checked by grepping
`node-red.json`, not guessed) - `air/probe/out`, `water/probe/out`,
`humidity/probe/out`, `air/heating`, `water/heating`, `occupied`,
`operation/switch`, `pool/pump/fault`, `pool/pump/required`, `service/due`.
Three more topics the BACnet loop publishes (`ambient/probe/out`,
`main/fan/speed/control`, `exhaust/fan/speed`) have no subscriber anywhere
in the flow, so they're intentionally not replicated.

**Status: both this bridge and Node-RED's BACnet loop are live simultaneously
right now** (confirmed by watching the broker - `ambient/probe/out` etc. are
still being updated by BACnet, since nothing publishes those from the Modbus
side). This is intentional and safe - retained MQTT is last-write-wins per
topic, so having two publishers on the same 10 topics doesn't break anything
mid-transition, it just means Pool UI/Circulation Pump are currently seeing
whichever of the two polling loops wrote most recently.

**Cross-check (2026-08-08):** read register 16824 directly via Modbus
(27.8°C) and compared against BACnet's own retained publish on
`variheat/ambient/probe/out` (also 27.8°C, an independent, non-shared
topic) - exact match, confirming Modbus and BACnet are reading the same
underlying values, not just producing plausibly-shaped ones. `pool_pump`
(register 16940) read 0 on both the register and the shared MQTT topic at
the same check, which only confirms agreement while off - it doesn't yet
prove the bridge's *value* is right when the pump actually turns on. Watch
`variheat/pool/pump/required` (or the register directly) the next time the
pump cycles on before fully trusting that path.

**Cadence change:** BACnet's loop polled every ~1-2s; the Modbus bridge
publishes `occupied` and `pool_pump` on `VARIHEAT_FAST_POLL_SECONDS`
(5s). Fine for pump/occupancy logic, but the Pool UI dashboard will feel a
little less snappy once BACnet is retired.

The next step - **not yet done** - is removing the BACnet-Read half of the
"Variheat AW600" subflow and the "Monitoring" tab from the live Node-RED
instance now that the cross-check above confirms agreement. That's a change
to make directly in the Node-RED editor and deploy from there, not
something to apply by hand-editing `node-red.json` and re-importing it - the
exported file is a large, mostly-unrelated snapshot of the whole house
automation setup, and it's safer to delete the specific nodes/tab in the UI
where you can see and undo each change.

**Before deleting the BACnet loop, clear the 3 orphaned topics** it alone
publishes (`variheat/ambient/probe/out`, `variheat/main/fan/speed/control`,
`variheat/exhaust/fan/speed`) - otherwise they freeze at their last BACnet
value forever (retained messages don't expire) and will look like live data
to anyone checking later. Easiest fix: publish an empty retained message to
each (e.g. `mosquitto_pub -r -n -t variheat/ambient/probe/out`), or add
`ambient_probe_c` to `MQTT_TOPICS` in `variheat_collector.py` if you'd
rather it stay a live value instead of going away.

**If deploying under systemd rather than Docker,** remember
`variheat-collector`'s venv needs `pip install -r requirements.txt` re-run
after this change - `paho-mqtt` is a new dependency and the import is
unconditional, so the service will crash on startup without it even if
`MQTT_HOST` is left blank.

**Phase 2 (built, dry-run, not yet armed):** the "Circulation Pump" tab also
writes to the MVHR's `Operation Switch` object directly via `BACnet-Write`
(inverted: schedule-on -> Operation Switch 0, schedule-off -> Operation
Switch 1). Per Dantherm's connection guide, register 16890/BACnet Binary
Value instance 1 "Enables/disables air and humidity control, for use during
summer with pool hall doors open" - a general operating-mode switch, not
specifically a pump control. This deployment's Node-RED flow drives it off
a fixed nightly schedule (`light-scheduler` node "Circulation Pump
Schedule", ~00:00-04:30 and 23:30-24:00 daily) that *also* switches a
separate physical device, a Tasmota smart plug running the pool's actual
circulation pump - i.e. it's being used here as a proxy for "is the
circulation pump running," which is worth confirming in person rather than
assuming matches the documented purpose. `variheat-control/variheat_control.py`
now has a second write path for register 16890 with this same mapping, but
Node-RED needs one small addition first: wire an `mqtt out` node (retained)
off the schedule's existing output, publishing to `pool/control/circulation`
(or whatever you set `VARIHEAT_OPERATION_SWITCH_TOPIC` to) - alongside its
existing wires to the Tasmota switch and BACnet-Write, not replacing them
yet. Leave `VARIHEAT_OPERATION_SWITCH_DRY_RUN=true` (the default,
independent of `VARIHEAT_CONTROL_DRY_RUN`) until `probe_occupancy.py write
operation <0|1>` has confirmed a Modbus write to 16890 has the same effect
BACnet's write did - only then remove the BACnet-Write node and flip the
flag.

## Occupancy control bridge (variheat-control)

Unlike SigenStor, this integration isn't monitoring-only: `variheat-control/variheat_control.py`
writes to the PLC's `Occupancy` register (16950, R/W) to force the MVHR into
occupied or unoccupied mode on demand.

**Why this exists:** the MVHR's BACnet occupancy point has a firmware bug -
it only accepts 2 of its 3 documented states - so occupancy is currently
controlled by a Raspberry Pi (`node-red.json`, tab "Occupancy") that
subscribes to MQTT and drives a relay (Automation HAT) wired into the PLC's
hardwired remote-occupancy input. Modbus talks to the same 3-state register
directly, which should let the Pi and relay be retired once this is
confirmed working. This is also the first step toward the longer-term goal
of programmatic control (e.g. running the MVHR based on solar output/tariff
rather than a fixed schedule) - see "Where this can go next".

**It's a 1:1 behavioral port of the existing Node-RED flow**, not a redesign:
subscribes to every topic in `VARIHEAT_OCCUPIED_TOPICS` (default
`pool/control/occupied,fluvo/control/active`, matching the flow's two `mqtt in`
nodes), ORs their latest known values together (matching the flow's
`combine-logic` node), and writes to the PLC only when the combined result
changes (matching the flow's `rbe` node). Both topics are confirmed to carry
a bare JSON boolean payload with the retain flag set (checked live:
`pool/control/occupied` → `true`, retained) - a fresh subscriber gets the
current desired state immediately on connect, same as the relay already
holds a physical state today.

**This does not start with the rest of the stack.** `docker compose up` will
not bring it up - it's gated behind a Compose profile:

```bash
docker compose --profile control up -d --build variheat-control
```

That's deliberate, and safe to do at any time even before the next step,
because of the following:

**Safety gate: the 0/1/2 mapping for register 16950 isn't documented by
Dantherm.** Their guide says only "UDINT, default 1, min 0, max 2 - controls
if the machine should be forced into occupied, unoccupied, or left to the
NSB schedule" - it doesn't say which raw value means what.
`VARIHEAT_OCCUPIED_VALUE` / `VARIHEAT_UNOCCUPIED_VALUE` have no defaults on
purpose, and `VARIHEAT_CONTROL_DRY_RUN` defaults to `true`: in dry-run mode
the bridge connects to MQTT and Modbus-reads normally, computes what it
would write, and logs it - but never calls `write_registers`. The service
refuses to start at all with `VARIHEAT_CONTROL_DRY_RUN=false` unless both
values are set (checked eagerly at startup, not on first write).

**Mapping confirmed in person, 2026-08-08, against the unit's own display**
(not just register readback) using `probe_occupancy.py`:

| Value | Unit's display shows |
| --: | --- |
| **0** | Occupied |
| **1** | Automatic (NSB/schedule - not used by this bridge, but available) |
| **2** | Unoccupied |

`.env`/`.env.example` are set accordingly (`VARIHEAT_OCCUPIED_VALUE=0`,
`VARIHEAT_UNOCCUPIED_VALUE=2`). Fans staying on for a bit after switching to
unoccupied is expected - the unit has its own overrun setting for this, not
a bug in the bridge. `VARIHEAT_CONTROL_DRY_RUN` is still `true` by default
even with both values set - arming (`false`) is a deliberate, separate step.
If you ever point this at a different Variheat unit, re-verify with
`probe_occupancy.py` rather than assuming this mapping holds - it isn't
documented anywhere Dantherm-side, so there's no guarantee it's consistent
across units/firmware versions.

```bash
# From variheat-control/, or via `docker compose run --rm variheat-control python probe_occupancy.py ...`
python probe_occupancy.py read                # current Occupancy / Operation Switch / Occupied / relay input / fans state
python probe_occupancy.py write occupancy 0   # try each value in turn, watching/listening to the unit
python probe_occupancy.py write operation 1   # Phase 2 - verify a Modbus write to 16890 behaves like BACnet's did
```

Each `write` immediately verifies the write landed (flags loudly if the
readback disagrees - e.g. from security gating), then waits 2s and re-reads
the same block so you can correlate the raw value with the PLC's own
`Occupied` status readback (16928) and the physical relay input (16936,
useful to cross-check against the Pi's current behavior while it's still
wired in).

**Status: armed and live as of 2026-08-08.** In practice the cutover went
the other way round from the original plan above - rather than running the
bridge alongside the Pi and comparing, the Pi was powered off first (to
remove any question of which of the two control paths would win if they
ever disagreed), confirmed via `Remote Occ Unocc` (16936) staying at `0`
with the Pi off, and only then was `VARIHEAT_CONTROL_DRY_RUN` flipped to
`false`. Confirmed live end-to-end: the bridge picked up the retained
`pool/control/occupied` message, wrote `Occupancy = 0`, and the unit's own
display showed "Occupied" with fans running - matching the same behavior
observed during the manual `probe_occupancy.py` testing above. The Pi and
its Node-RED flow (`node-red.json`) are now fully retired; `variheat-control`
is the sole occupancy control path.

## Setup

1. On the SigenStor, confirm Modbus TCP Server is enabled (installer setting).
2. `cp .env.example .env` and edit it - at minimum set a real `INFLUXDB_TOKEN`
   and change the admin passwords.
3. `docker compose up -d --build`
4. Open Grafana at <http://localhost:3000> (login from `.env`). The
   "SigenStor" and "Variheat MVHR" dashboards are auto-provisioned.
5. Open InfluxDB at <http://localhost:8086> if you want to run ad-hoc Flux
   queries against the raw data.

Check collector logs with `docker compose logs -f sigenstor-collector` - it
logs every successful poll and any Modbus connection errors. Check
weather-collector and variheat-collector logs with `docker compose logs -f weather-collector`
/ `docker compose logs -f variheat-collector` similarly.

## Deploying the collector as a systemd service (Ubuntu Server)

InfluxDB and Grafana are left running in Docker Compose (unchanged) - only the
collector moves to a native systemd service. Files for this are in
`deploy/systemd/`. **These haven't been tested on an actual Ubuntu box** (this
was written on macOS) - treat as a solid starting point, not a verified
recipe; check `systemctl status` / `journalctl` carefully after first deploy.

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin sigenstor

sudo mkdir -p /opt/sigenstor-collector /etc/sigenstor-collector
sudo cp sigenstor-collector/sigenstor_collector.py sigenstor-collector/requirements.txt /opt/sigenstor-collector/
sudo python3 -m venv /opt/sigenstor-collector/venv
sudo /opt/sigenstor-collector/venv/bin/pip install -r /opt/sigenstor-collector/requirements.txt

sudo cp deploy/systemd/sigenstor-collector.env.example /etc/sigenstor-collector/collector.env
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
  InfluxDB ever moves elsewhere. `deploy/systemd/sigenstor-collector.env.example`
  sets this explicitly.
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

The Variheat collector deploys the same way, in parallel - swap `sigenstor`
for `variheat` throughout (`deploy/systemd/variheat-collector.service` and
`variheat-collector.env.example` are the equivalent files, and
`variheat-collector/variheat_collector.py` / `requirements.txt` are what get
copied to `/opt/variheat-collector/`). It's an independent systemd unit, so
either collector can be stopped/restarted/redeployed without touching the
other.

`variheat-control` (see "Occupancy control bridge" above) has its own unit
too - `deploy/systemd/variheat-control.service` and `variheat-control.env.example`,
copying `variheat-control/variheat_control.py`, `probe_occupancy.py`, and
`requirements.txt` to `/opt/variheat-control/`. It's fine to `systemctl
enable --now` this one right away, since it stays in dry-run mode (no Modbus
writes at all) until `VARIHEAT_OCCUPIED_VALUE`/`VARIHEAT_UNOCCUPIED_VALUE`
are filled in and `VARIHEAT_CONTROL_DRY_RUN` is explicitly set to `false` in
`control.env` - do that only after confirming the mapping in person.

## Where this can go next

A few more registers were identified but deliberately left out for now (lower
urgency, not "no"): battery SOH (30087) and cell temp/voltage min-max
(30620-30623) for long-term battery health; PCS internal temperature (31003);
and per-phase grid *voltage* (31011-31016), worth watching since UK DNOs
sometimes throttle export due to voltage rise on 3-phase connections. Also
skipped: the 24 "smart load" registers (only relevant if sub-circuit
monitoring is configured in the app) and third-party inverter/EVDC fields
(not installed here).

SigenStor itself stays monitoring-only for now. The same register map
exposes write-capable holding registers (plant-level EMS work mode,
charge/discharge power limits, export limits), so once you've watched
enough real data to know what "the right behaviour" looks like, a
control/strategy layer (e.g. reacting to a dynamic tariff, or holding a
reserve for a peak window) can be added as a separate service that writes
those registers - without needing to change the collector or dashboard.
`variheat-control` (see above) is the first instance of exactly this
pattern, for the MVHR rather than SigenStor.

Variheat's control surface is currently just occupancy. The PLC's Modbus map
has further write-capable registers not used yet - `Damper Switch` (16952,
force dampers min/max/auto) is the next obvious candidate, and the longer-term
goal is choosing when the MVHR runs (and how hard) based on solar output or
tariff rather than a fixed schedule, the same way SigenStor's own
control/strategy layer above would work - likely as logic added to
`variheat-control` once occupancy is proven out, reading `sigenstor`
measurement fields (e.g. `pv_power_kw`) to decide.
