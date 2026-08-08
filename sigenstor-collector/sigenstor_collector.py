"""
Polls SigenStor Modbus registers and writes them to InfluxDB.

Register addresses, scale factors, and slave ids are from Sigenergy's Modbus
Protocol V2.7 (2025-05-23). Plant-level registers (section 5.1) live under
slave id 247; PV-string-level registers (section 5.3) live under the
inverter's own slave id. All verified live against a real SigenStor.

Registers poll at different rates - a single 1-second tick loop decides
what's due each second via `now % register.interval_seconds == 0`, so a
5s register and a 300s register can share the same loop without the slow
one holding up the fast one (or vice versa).
"""
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS
from pymodbus.client import ModbusTcpClient
from pymodbus.client.mixin import ModbusClientMixin

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sigenstor-collector")

MODBUS_HOST = os.environ["MODBUS_HOST"]
MODBUS_PORT = int(os.environ.get("MODBUS_PORT", 502))
PLANT_SLAVE_ID = int(os.environ.get("PLANT_SLAVE_ID", 247))
# The hybrid inverter's own slave id (distinct from the fixed plant address 247).
# Needed for PV-string-level detail. Default 1 is Sigenergy's typical single-inverter default.
INVERTER_SLAVE_ID = int(os.environ.get("INVERTER_SLAVE_ID", 1))

# Poll tiers, in seconds. FAST is for live power-flow numbers people want smooth
# graphs of; MEDIUM for state that changes occasionally (mode, alarms); SLOW for
# slow-moving cumulative counters. Each is a multiple of the ones below it, so
# their due-ticks line up and coalesce into a single Influx write when they do.
FAST_POLL_SECONDS = int(os.environ.get("FAST_POLL_SECONDS", 5))
MEDIUM_POLL_SECONDS = int(os.environ.get("MEDIUM_POLL_SECONDS", 30))
SLOW_POLL_SECONDS = int(os.environ.get("SLOW_POLL_SECONDS", 300))
TICK_SECONDS = 1

if min(FAST_POLL_SECONDS, MEDIUM_POLL_SECONDS, SLOW_POLL_SECONDS) < TICK_SECONDS:
    raise ValueError("poll intervals must be >= TICK_SECONDS")

INFLUXDB_URL = os.environ.get("INFLUXDB_URL", "http://influxdb:8086")
INFLUXDB_TOKEN = os.environ["INFLUXDB_TOKEN"]
INFLUXDB_ORG = os.environ["INFLUXDB_ORG"]
INFLUXDB_BUCKET = os.environ["INFLUXDB_BUCKET"]

DT = ModbusClientMixin.DATATYPE


@dataclass(frozen=True)
class Register:
    field: str
    address: int
    count: int
    dtype: Any
    scale: float
    slave: int
    interval_seconds: int


# battery_power_kw: >0 charging, <0 discharging. grid_power_kw*: >0 import, <0 export.
REGISTERS = [
    # --- live power flow (fast) ---
    Register("plant_active_power_kw", 30031, 2, DT.INT32, 0.001, PLANT_SLAVE_ID, FAST_POLL_SECONDS),
    Register("pv_power_kw", 30035, 2, DT.INT32, 0.001, PLANT_SLAVE_ID, FAST_POLL_SECONDS),
    Register("battery_power_kw", 30037, 2, DT.INT32, 0.001, PLANT_SLAVE_ID, FAST_POLL_SECONDS),
    Register("grid_power_kw", 30005, 2, DT.INT32, 0.001, PLANT_SLAVE_ID, FAST_POLL_SECONDS),
    Register("battery_soc_pct", 30014, 1, DT.UINT16, 0.1, PLANT_SLAVE_ID, FAST_POLL_SECONDS),
    Register("running_state", 30051, 1, DT.UINT16, 1, PLANT_SLAVE_ID, FAST_POLL_SECONDS),
    # Per-phase grid power - a 3-phase-only breakdown, useful for spotting phase
    # imbalance (e.g. an EV charger or immersion skewing one phase).
    Register("grid_power_phase_a_kw", 30052, 2, DT.INT32, 0.001, PLANT_SLAVE_ID, FAST_POLL_SECONDS),
    Register("grid_power_phase_b_kw", 30054, 2, DT.INT32, 0.001, PLANT_SLAVE_ID, FAST_POLL_SECONDS),
    Register("grid_power_phase_c_kw", 30056, 2, DT.INT32, 0.001, PLANT_SLAVE_ID, FAST_POLL_SECONDS),
    # Section 5.3 of the protocol - PV strings, only via the inverter's own slave
    # id. This SigenStor reports 3 available string inputs (MPPT count 3); only
    # 2 are physically wired here, so PV3 is omitted.
    Register("pv1_voltage_v", 31027, 1, DT.INT16, 0.1, INVERTER_SLAVE_ID, FAST_POLL_SECONDS),
    Register("pv1_current_a", 31028, 1, DT.INT16, 0.01, INVERTER_SLAVE_ID, FAST_POLL_SECONDS),
    Register("pv2_voltage_v", 31029, 1, DT.INT16, 0.1, INVERTER_SLAVE_ID, FAST_POLL_SECONDS),
    Register("pv2_current_a", 31030, 1, DT.INT16, 0.01, INVERTER_SLAVE_ID, FAST_POLL_SECONDS),
    # --- state that changes occasionally (medium) ---
    # The Sigen Gateway (C60-2) has no registers of its own - it's the CT/meter
    # device behind the "[Grid Sensor]" registers above. This just confirms it's
    # online (0: not connected, 1: connected).
    Register("grid_sensor_connected", 30004, 1, DT.UINT16, 1, PLANT_SLAVE_ID, MEDIUM_POLL_SECONDS),
    # EMS work mode: 0 Max self consumption, 1 AI Mode, 2 TOU, 5 Full Feed-in to
    # Grid, 7 Remote EMS mode, 9 Custom - i.e. which strategy is currently active.
    Register("ems_work_mode", 30003, 1, DT.UINT16, 1, PLANT_SLAVE_ID, MEDIUM_POLL_SECONDS),
    # On/off-grid status: 0 on grid, 1 off grid (auto), 2 off grid (manual).
    Register("on_off_grid_status", 30009, 1, DT.UINT16, 1, PLANT_SLAVE_ID, MEDIUM_POLL_SECONDS),
    # General alarms - 0 means clear. Codes decode per Appendices 2-5 and 11 of
    # the protocol doc; collected here just as a "something's wrong" signal.
    Register("general_alarm1", 30027, 1, DT.UINT16, 1, PLANT_SLAVE_ID, MEDIUM_POLL_SECONDS),
    Register("general_alarm2", 30028, 1, DT.UINT16, 1, PLANT_SLAVE_ID, MEDIUM_POLL_SECONDS),
    Register("general_alarm3", 30029, 1, DT.UINT16, 1, PLANT_SLAVE_ID, MEDIUM_POLL_SECONDS),
    Register("general_alarm4", 30030, 1, DT.UINT16, 1, PLANT_SLAVE_ID, MEDIUM_POLL_SECONDS),
    Register("general_alarm5", 30072, 1, DT.UINT16, 1, PLANT_SLAVE_ID, MEDIUM_POLL_SECONDS),
    # --- slow-moving cumulative counters (slow) ---
    Register("total_load_daily_kwh", 30092, 2, DT.UINT32, 0.01, PLANT_SLAVE_ID, SLOW_POLL_SECONDS),
    Register("total_load_kwh", 30094, 4, DT.UINT64, 0.01, PLANT_SLAVE_ID, SLOW_POLL_SECONDS),
    Register("total_imported_energy_kwh", 30216, 4, DT.UINT64, 0.01, PLANT_SLAVE_ID, SLOW_POLL_SECONDS),
    Register("total_exported_energy_kwh", 30220, 4, DT.UINT64, 0.01, PLANT_SLAVE_ID, SLOW_POLL_SECONDS),
]


def read_register(client: ModbusTcpClient, reg: Register) -> float:
    result = client.read_input_registers(address=reg.address, count=reg.count, slave=reg.slave)
    if result.isError():
        raise IOError(f"Modbus error reading {reg.field} @ {reg.address} (slave {reg.slave}): {result}")
    raw = client.convert_from_registers(result.registers, data_type=reg.dtype, word_order="big")
    if not isinstance(raw, (int, float)):
        raise TypeError(f"{reg.field} @ {reg.address}: expected a numeric value, got {type(raw).__name__}")
    # InfluxDB locks a field's type (int vs float) to its first write, so registers
    # with scale 1 (counts/codes/statuses) must stay int - never coerce to float here.
    return raw * reg.scale if reg.scale != 1 else raw


def read_metrics(client: ModbusTcpClient, registers: list) -> dict:
    """Read every given register. Raises on the first Modbus failure."""
    return {reg.field: read_register(client, reg) for reg in registers}


def write_metrics(write_api, metrics: dict) -> None:
    point = Point("sigenstor").tag("source", "plant")
    for field, value in metrics.items():
        point = point.field(field, value)
    point = point.time(time.time_ns(), WritePrecision.NS)
    write_api.write(bucket=INFLUXDB_BUCKET, record=point)


def main() -> None:
    influx = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
    write_api = influx.write_api(write_options=SYNCHRONOUS)

    modbus = ModbusTcpClient(MODBUS_HOST, port=MODBUS_PORT, timeout=5)

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

        finally:
            elapsed = time.monotonic() - tick_started_at
            time.sleep(max(0.0, TICK_SECONDS - elapsed))


if __name__ == "__main__":
    main()
