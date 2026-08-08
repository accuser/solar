"""
Bridges MQTT occupancy-control messages to the Variheat M172 PLC's Modbus
`Occupancy` register (16950), replacing a Raspberry Pi + relay (Automation
HAT) currently wired into the PLC's hardwired remote-occupancy input. That
relay exists only as a workaround for a firmware bug in the MVHR's BACnet
implementation (its occupancy point only accepts 2 of its 3 documented
states) - Modbus talks to the same 3-state register directly instead, so no
relay is needed once this is confirmed working.

Replicates `node-red.json` (tab "Occupancy") 1:1: subscribes to every topic
in OCCUPIED_TOPICS (default matches the flow's two `mqtt in` nodes -
`pool/control/occupied`, `fluvo/control/active`), ORs their latest known
values together (matching the flow's `combine-logic` node, operator "or",
distinction "topic"), and writes to the PLC only when the combined result
changes (matching the flow's `rbe` node). Both topics carry a bare JSON
boolean payload (confirmed live: `pool/control/occupied` is retained with
payload `true`) - a fresh subscriber gets the current desired state
immediately via MQTT's retained-message delivery, same as the relay would
already be holding a physical state.

See variheat-collector/variheat_collector.py's docstring for the Modbus
quirks this module relies on (unit id 255, function code 3, addr-1,
word_order "little"). The write side's encoding isn't just assumed
symmetric with the read side - it's corroborated by data already collected:
convert_to_registers(1, UINT32, "little") produces [1, 0], and the
collector's own read of Unit On True (17050, also a BOOL-in-UDINT) returned
exactly regs=[1, 0] live. Same layout on both sides.

SAFETY: the mapping from occupied/unoccupied to register 16950's raw values
(documented only as UDINT, default 1, min 0, max 2 - "controls if the
machine should be forced into occupied, unoccupied, or left to the NSB
schedule") is NOT in Dantherm's connection guide. VARIHEAT_OCCUPIED_VALUE /
VARIHEAT_UNOCCUPIED_VALUE have no defaults on purpose - this must be a
deliberate, confirmed choice, not a guess baked into code that could
silently drive a pool hall's climate control wrong. Confirmed in person,
2026-08-08, against the unit's own display (not just register readback):
0=Occupied, 1=Automatic (NSB/schedule - unused here, kept in reserve), and
2=Unoccupied. .env is set accordingly. This is unit/firmware-specific and
undocumented Dantherm-side - re-verify with probe_occupancy.py before
trusting it against a different unit. Regardless of whether the values are
set, VARIHEAT_CONTROL_DRY_RUN defaults to true: the bridge computes and logs
what it would write, but never calls write_registers, until this is
explicitly overridden - arming is a separate deliberate step from having
confirmed values. See probe_occupancy.py for the determination process,
including checking whether register-level security (User Security Enable /
Occ Security) is gating the write in the first place (checked live: it
wasn't - User Security Enable read 0).

A failed write is retried on the next MQTT message that disagrees with the
last successfully-applied state (see on_message: last_written only advances
on a True return from write_occupancy) - but if the topics go quiet with no
further messages, there's no periodic reconciliation loop forcing a retry.
Acceptable for now since this is still dry-run by default; revisit if this
becomes the sole path once the Pi is decommissioned.
"""
import json
import logging
import os

import paho.mqtt.client as mqtt
from pymodbus.client import ModbusTcpClient
from pymodbus.client.mixin import ModbusClientMixin

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("variheat-control")

MQTT_HOST = os.environ["MQTT_HOST"]
MQTT_PORT = int(os.environ.get("MQTT_PORT", 1883))
OCCUPIED_TOPICS = [
    t.strip()
    for t in os.environ.get("VARIHEAT_OCCUPIED_TOPICS", "pool/control/occupied,fluvo/control/active").split(",")
    if t.strip()
]

MODBUS_HOST = os.environ["VARIHEAT_MODBUS_HOST"]
MODBUS_PORT = int(os.environ.get("VARIHEAT_MODBUS_PORT", 502))
UNIT_ID = 255  # see variheat_collector.py

OCCUPANCY_REGISTER = 16950

DRY_RUN = os.environ.get("VARIHEAT_CONTROL_DRY_RUN", "true").strip().lower() not in ("false", "0", "no")

# Required only once actually armed (DRY_RUN=false) - see module docstring.
OCCUPIED_VALUE = os.environ.get("VARIHEAT_OCCUPIED_VALUE")
UNOCCUPIED_VALUE = os.environ.get("VARIHEAT_UNOCCUPIED_VALUE")
if not DRY_RUN:
    if OCCUPIED_VALUE is None or UNOCCUPIED_VALUE is None:
        raise ValueError(
            "VARIHEAT_CONTROL_DRY_RUN=false requires VARIHEAT_OCCUPIED_VALUE and "
            "VARIHEAT_UNOCCUPIED_VALUE to be set - confirm these against the live unit "
            "first (see probe_occupancy.py)"
        )
    OCCUPIED_VALUE = int(OCCUPIED_VALUE)
    UNOCCUPIED_VALUE = int(UNOCCUPIED_VALUE)

DT = ModbusClientMixin.DATATYPE

# topic -> latest known bool. A topic absent here hasn't reported yet and is
# excluded from the OR, same as the Node-RED flow before its first message.
topic_state: dict = {}


def parse_bool(raw: bytes, topic: str) -> bool:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = raw.decode(errors="replace").strip()
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in ("true", "on", "1", "occupied"):
            return True
        if lowered in ("false", "off", "0", "unoccupied"):
            return False
    raise ValueError(f"unrecognized payload on {topic}: {raw!r}")


def write_occupancy(modbus: ModbusTcpClient, occupied: bool) -> bool:
    """Returns True if the desired state was successfully applied (or, in dry
    run, would have been) - callers must only advance their "last written"
    state on a True return, so a failed write gets retried on the next
    message rather than silently sticking."""
    state_name = "occupied" if occupied else "unoccupied"
    if DRY_RUN:
        log.info("[dry run] would write Occupancy (%s) for state=%s", OCCUPANCY_REGISTER, state_name)
        return True

    # Guaranteed set by the module-level check when DRY_RUN is false (see top
    # of file) - asserted here too so a future refactor can't silently drop
    # that invariant and reach a write with an unconfirmed/missing value.
    assert OCCUPIED_VALUE is not None and UNOCCUPIED_VALUE is not None
    value = OCCUPIED_VALUE if occupied else UNOCCUPIED_VALUE
    if not modbus.connected and not modbus.connect():
        log.warning("Could not connect to %s:%s, dropping occupancy write", MODBUS_HOST, MODBUS_PORT)
        return False
    registers = modbus.convert_to_registers(value, data_type=DT.UINT32, word_order="little")
    result = modbus.write_registers(address=OCCUPANCY_REGISTER - 1, values=registers, slave=UNIT_ID)
    if result.isError():
        log.error("Modbus write failed for Occupancy: %s", result)
        modbus.close()
        return False
    log.info("wrote Occupancy (%s) = %s (state=%s)", OCCUPANCY_REGISTER, value, state_name)
    return True


def main() -> None:
    if DRY_RUN:
        log.warning("DRY RUN MODE - no Modbus writes will be made. Set VARIHEAT_CONTROL_DRY_RUN=false "
                    "once VARIHEAT_OCCUPIED_VALUE/VARIHEAT_UNOCCUPIED_VALUE are confirmed against the live unit.")

    modbus = ModbusTcpClient(MODBUS_HOST, port=MODBUS_PORT, timeout=5)
    last_written = None

    def on_connect(client, userdata, flags, reason_code, properties=None):
        log.info("connected to MQTT broker %s:%s (%s)", MQTT_HOST, MQTT_PORT, reason_code)
        for topic in OCCUPIED_TOPICS:
            client.subscribe(topic)
            log.info("subscribed to %s", topic)

    def on_message(client, userdata, msg):
        nonlocal last_written
        try:
            value = parse_bool(msg.payload, msg.topic)
        except ValueError as e:
            log.warning("%s", e)
            return
        topic_state[msg.topic] = value
        occupied = any(topic_state.values())
        log.info("%s -> %s (topics: %s, combined: %s)", msg.topic, value, topic_state, occupied)
        if occupied != last_written:
            if write_occupancy(modbus, occupied):
                last_written = occupied
            else:
                log.warning("occupancy write failed, will retry on the next MQTT message")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_forever()


if __name__ == "__main__":
    main()
