"""
Polls the Variheat M172 PLC (a Calorex Variheat AW600 MVHR) over Modbus TCP
and writes to InfluxDB.

Register addresses are from Dantherm's "Variheat M172 PLC BACnet and Modbus
v1" connection guide (28/02/2018), which lists them in IEC61131 syntax
(1-based) - so every address here is (doc address - 1) for pymodbus's
0-based addressing. Four things the doc doesn't say, found by probing a
live unit on 2026-08-08:

- The Modbus unit/slave id is 255, not the "Slave Address" shown in the
  PLC's BMS settings menu (that setting is for the RS485 Modbus/RTU port;
  this unit is reached over Modbus TCP via the Ethernet port instead, and
  255 is what it actually answers to).
- Every register - including the R/O ones - answers to function code 3
  (read holding registers). Function code 4 (read input registers) errors
  on all of them.
- Double-length values (REAL, UDINT) need pymodbus's word_order="little"
  (low word first) to decode - confirmed via the live clock block
  (Year/Month/Day/Hours/Minute at 16874-16882 read back 26/8/8/11/15 on
  2026-08-08 ~11:15 local with this order; word_order="big" instead gives
  denormal floats around 1e-41).
- Probe reading REAL registers (the *_probe_c / *_probe_pct fields below)
  are pre-scaled x10 relative to their setpoint counterparts: Humidity
  Probe Reading read back 322 against a documented setpoint range of
  15-80%, and 322% is physically impossible while 32.2% isn't. This also
  matches the doc's stated ±3277 range for probe readings, the signature
  of an INT16-precision value at 0.1 resolution boxed in a REAL. Setpoint
  registers (*_set_point) are the literal value with no such scaling.

There's no register in this map for the MVHR's actual power draw. The
compressor / air-heating / water-heating / defrost / fans-enable / boiler /
pool-pump booleans stand in for it instead - useful for correlating against
SigenStor's grid import (the "sigenstor" bucket measurement, `grid_power_kw`)
when the MVHR is what's driving a load spike.

Optionally also republishes a subset of readings to MQTT, retained, on the
same `variheat/*` topics that a Node-RED instance (see node-red.json at the
repo root) currently populates via a 1-second BACnet polling loop - this
lets that dashboard and its Circulation Pump control logic keep working
unchanged while the BACnet read loop itself gets retired in favour of the
Modbus reads this module already does. Only enabled if MQTT_HOST is set;
otherwise this module behaves exactly as before (Modbus -> InfluxDB only).
Topic list and field mapping are in MQTT_TOPICS below - it's deliberately
just the topics something in Node-RED actually subscribes to (checked
against node-red.json directly), not the full register set.
"""
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import paho.mqtt.client as mqtt
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS
from pymodbus.client import ModbusTcpClient
from pymodbus.client.mixin import ModbusClientMixin

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("variheat-collector")

MODBUS_HOST = os.environ["VARIHEAT_MODBUS_HOST"]
MODBUS_PORT = int(os.environ.get("VARIHEAT_MODBUS_PORT", 502))
# Fixed - see module docstring. Not the BMS-settings "Slave Address".
UNIT_ID = 255

# FAST is for the live probe readings and the run-state booleans that stand
# in for power draw; MEDIUM is for setpoints (only change when someone
# adjusts them) and alarms/faults (0 almost all the time).
FAST_POLL_SECONDS = int(os.environ.get("VARIHEAT_FAST_POLL_SECONDS", 5))
MEDIUM_POLL_SECONDS = int(os.environ.get("VARIHEAT_MEDIUM_POLL_SECONDS", 30))
TICK_SECONDS = 1

if min(FAST_POLL_SECONDS, MEDIUM_POLL_SECONDS) < TICK_SECONDS:
    raise ValueError("poll intervals must be >= TICK_SECONDS")

INFLUXDB_URL = os.environ.get("INFLUXDB_URL", "http://influxdb:8086")
INFLUXDB_TOKEN = os.environ["INFLUXDB_TOKEN"]
INFLUXDB_ORG = os.environ["INFLUXDB_ORG"]
INFLUXDB_BUCKET = os.environ["INFLUXDB_BUCKET"]

# Optional - see module docstring. Empty/unset disables the MQTT bridge entirely.
MQTT_HOST = os.environ.get("MQTT_HOST", "")
MQTT_PORT = int(os.environ.get("MQTT_PORT", 1883))

DT = ModbusClientMixin.DATATYPE


@dataclass(frozen=True)
class Register:
    field: str
    address: int  # doc (1-based) address; read_register converts to 0-based
    count: int
    dtype: Any
    scale: float
    interval_seconds: int


REGISTERS = [
    # --- live probe readings and run state (fast) ---
    Register("air_probe_c", 16792, 2, DT.FLOAT32, 0.1, FAST_POLL_SECONDS),
    Register("water_probe_c", 16812, 2, DT.FLOAT32, 0.1, FAST_POLL_SECONDS),
    Register("humidity_probe_pct", 16780, 2, DT.FLOAT32, 0.1, FAST_POLL_SECONDS),
    Register("ambient_probe_c", 16824, 2, DT.FLOAT32, 0.1, FAST_POLL_SECONDS),
    # Run-state booleans - the closest thing this register map has to a power
    # signal. All BOOL-in-UDINT (32-bit, but only bit 0 is meaningful).
    Register("unit_on", 17050, 2, DT.UINT32, 1, FAST_POLL_SECONDS),
    Register("occupied", 16928, 2, DT.UINT32, 1, FAST_POLL_SECONDS),
    Register("compressor", 16902, 2, DT.UINT32, 1, FAST_POLL_SECONDS),
    Register("air_heating", 16904, 2, DT.UINT32, 1, FAST_POLL_SECONDS),
    Register("water_heating", 16906, 2, DT.UINT32, 1, FAST_POLL_SECONDS),
    Register("defrost_active", 16910, 2, DT.UINT32, 1, FAST_POLL_SECONDS),
    Register("fans_enable", 16946, 2, DT.UINT32, 1, FAST_POLL_SECONDS),
    Register("boiler", 16938, 2, DT.UINT32, 1, FAST_POLL_SECONDS),
    Register("pool_pump", 16940, 2, DT.UINT32, 1, FAST_POLL_SECONDS),
    # --- setpoints and alarms/faults (medium) ---
    Register("air_set_point_c", 16790, 2, DT.FLOAT32, 1, MEDIUM_POLL_SECONDS),
    Register("water_set_point_c", 16810, 2, DT.FLOAT32, 1, MEDIUM_POLL_SECONDS),
    Register("humidity_set_point_pct", 16778, 2, DT.FLOAT32, 1, MEDIUM_POLL_SECONDS),
    Register("standby_switch", 16888, 2, DT.UINT32, 1, MEDIUM_POLL_SECONDS),
    # Enables/disables air+humidity control (for summer use with pool hall
    # doors open) - read here only for the MQTT bridge/dashboard below; not
    # yet written by anything in this project (Node-RED's Circulation Pump
    # tab still owns that write, via BACnet - see module docstring).
    Register("operation_switch", 16890, 2, DT.UINT32, 1, MEDIUM_POLL_SECONDS),
    Register("frost_protection_active", 16912, 2, DT.UINT32, 1, MEDIUM_POLL_SECONDS),
    Register("pressure_fault", 16914, 2, DT.UINT32, 1, MEDIUM_POLL_SECONDS),
    Register("fire_alarm", 16916, 2, DT.UINT32, 1, MEDIUM_POLL_SECONDS),
    Register("fan_blockage", 16918, 2, DT.UINT32, 1, MEDIUM_POLL_SECONDS),
    Register("pool_pump_fault", 16920, 2, DT.UINT32, 1, MEDIUM_POLL_SECONDS),
    Register("service_due", 16922, 2, DT.UINT32, 1, MEDIUM_POLL_SECONDS),
    Register("main_fan_alarm", 16924, 2, DT.UINT32, 1, MEDIUM_POLL_SECONDS),
    Register("clock_needs_setting", 16926, 2, DT.UINT32, 1, MEDIUM_POLL_SECONDS),
    Register("fault_bms", 16942, 2, DT.UINT32, 1, MEDIUM_POLL_SECONDS),
    Register("reversing_valve", 16944, 2, DT.UINT32, 1, MEDIUM_POLL_SECONDS),
]

# field -> MQTT topic, retained, JSON-encoded value - only the topics
# node-red.json's Pool UI / Circulation Pump tabs actually subscribe to
# (verified by grepping the exported flow, not guessed). Payload shapes
# already match what those nodes expect without any extra work: numbers
# stay numbers (gauges), and 0/1 ints work directly with both the ui_switch/
# ui_led "num" comparisons and the combine-logic OR node's truthiness check.
MQTT_TOPICS = {
    "air_probe_c": "variheat/air/probe/out",
    "water_probe_c": "variheat/water/probe/out",
    "humidity_probe_pct": "variheat/humidity/probe/out",
    "air_heating": "variheat/air/heating",
    "water_heating": "variheat/water/heating",
    "occupied": "variheat/occupied",
    "pool_pump_fault": "variheat/pool/pump/fault",
    # BACnet's "Pool Pump Required" == Modbus's "Pool Pump" (16940, "indicates
    # if the pool pump is being called for") - same point, different name.
    "pool_pump": "variheat/pool/pump/required",
    "service_due": "variheat/service/due",
    "operation_switch": "variheat/operation/switch",
}


def read_register(client: ModbusTcpClient, reg: Register) -> float:
    result = client.read_holding_registers(address=reg.address - 1, count=reg.count, slave=UNIT_ID)
    if result.isError():
        raise IOError(f"Modbus error reading {reg.field} @ {reg.address}: {result}")
    raw = client.convert_from_registers(result.registers, data_type=reg.dtype, word_order="little")
    if not isinstance(raw, (int, float)):
        raise TypeError(f"{reg.field} @ {reg.address}: expected a numeric value, got {type(raw).__name__}")
    # InfluxDB locks a field's type (int vs float) to its first write, so registers
    # with scale 1 (booleans/statuses) must stay int - never coerce to float here.
    return raw * reg.scale if reg.scale != 1 else raw


def read_metrics(client: ModbusTcpClient, registers: list) -> dict:
    """Read every given register. Raises on the first Modbus failure."""
    return {reg.field: read_register(client, reg) for reg in registers}


def write_metrics(write_api, metrics: dict) -> None:
    point = Point("variheat").tag("source", "mvhr")
    for field, value in metrics.items():
        point = point.field(field, value)
    point = point.time(time.time_ns(), WritePrecision.NS)
    write_api.write(bucket=INFLUXDB_BUCKET, record=point)


def publish_mqtt(mqtt_client: mqtt.Client, metrics: dict) -> None:
    for field, value in metrics.items():
        topic = MQTT_TOPICS.get(field)
        if topic is None:
            continue
        mqtt_client.publish(topic, json.dumps(value), retain=True)


def main() -> None:
    influx = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
    write_api = influx.write_api(write_options=SYNCHRONOUS)

    modbus = ModbusTcpClient(MODBUS_HOST, port=MODBUS_PORT, timeout=5)

    mqtt_client = None
    if MQTT_HOST:
        mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        mqtt_client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
        mqtt_client.loop_start()
        log.info("MQTT bridge enabled -> %s:%s, %d topics", MQTT_HOST, MQTT_PORT, len(MQTT_TOPICS))
    else:
        log.info("MQTT bridge disabled (MQTT_HOST not set)")

    while True:
        tick_started_at = time.monotonic()
        try:
            now = int(time.time())

            if not modbus.connected and not modbus.connect():
                log.warning("Could not connect to %s:%s, retrying in %ss", MODBUS_HOST, MODBUS_PORT, TICK_SECONDS)
                continue

            due = [reg for reg in REGISTERS if now % reg.interval_seconds == 0]
            if not due:
                continue

            try:
                metrics = read_metrics(modbus, due)
            except Exception:
                # A Modbus-side failure - the connection may be in a bad state,
                # so close it and let the next tick reconnect from scratch.
                log.exception("modbus read failed, reconnecting")
                modbus.close()
                continue

            try:
                write_metrics(write_api, metrics)
                log.info("wrote %s", metrics)
            except Exception:
                # An InfluxDB-side failure - Modbus is fine, so leave it
                # connected and just drop this tick's write.
                log.exception("influxdb write failed, will retry next tick")

            if mqtt_client is not None:
                try:
                    publish_mqtt(mqtt_client, metrics)
                except Exception:
                    # An MQTT-side failure - independent of both Modbus and
                    # InfluxDB, so leave those alone and just drop this
                    # tick's publish.
                    log.exception("mqtt publish failed, will retry next tick")

        finally:
            elapsed = time.monotonic() - tick_started_at
            time.sleep(max(0.0, TICK_SECONDS - elapsed))


if __name__ == "__main__":
    main()
