"""
Interactive helper for verifying Modbus writes on the live Variheat unit
before arming variheat_control.py's write paths.

Usage, in person at the unit (or anywhere on the same network):

    python probe_occupancy.py read
        Prints Occupancy and Operation Switch (the two write targets),
        Occupied (the PLC's own status readback), Remote Occ Unocc (the
        hardwired relay input the Pi used to drive - useful to compare
        against while it's still connected), Fans Enable (a quick "is it
        actually doing something differently" signal), and two security
        flags (User Security Enable, Occ Security) - if either is on, the
        PLC's BMS settings say it can gate/reject writes to the occupied
        force switch, which would otherwise look identical to "this value
        does nothing." Check these before concluding anything about what a
        write did or didn't do.

    python probe_occupancy.py write occupancy <0|1|2>
        Register 16950. Dantherm's connection guide documents it only as
        "UDINT, default 1, min 0, max 2 - controls if the machine should be
        forced into occupied, unoccupied, or left to the NSB schedule",
        with no mapping from those meanings to the raw values - hence
        probing all three.

    python probe_occupancy.py write operation <0|1>
        Register 16890 (Phase 2). Unlike Occupancy, the mapping here isn't
        a mystery - node-red.json's own BACnet-Write already uses 0=on,
        1=off successfully - what this verifies is whether a *Modbus*
        write to this register has the same physical effect BACnet's write
        did (it's never been written via Modbus before now).

    Both write commands immediately read back the target register and flag
    loudly if the readback doesn't match what was written (a silent write
    rejection, e.g. from security gating, would otherwise look identical to
    "this value has no effect" - the two are very different problems). Then
    each waits 2s for the PLC to react and prints the same block read
    above so you can see what changed. Watch/listen to the unit itself too
    - the registers only tell you the PLC's own interpretation, not whether
    it's physically correct.

Once verified: for Occupancy, set VARIHEAT_OCCUPIED_VALUE /
VARIHEAT_UNOCCUPIED_VALUE and flip VARIHEAT_CONTROL_DRY_RUN=false. For
Operation Switch, flip VARIHEAT_OPERATION_SWITCH_DRY_RUN=false (the mapping
is already fixed in code, see variheat_control.py).
"""
import os
import sys
import time

from pymodbus.client import ModbusTcpClient
from pymodbus.client.mixin import ModbusClientMixin

MODBUS_HOST = os.environ.get("VARIHEAT_MODBUS_HOST", "192.168.68.66")
MODBUS_PORT = int(os.environ.get("VARIHEAT_MODBUS_PORT", 502))
UNIT_ID = 255

DT = ModbusClientMixin.DATATYPE

# doc address -> (label, dtype). Most booleans in this map are BOOL-in-UDINT
# (2 registers) but the security flags are BOOL-in-INT (1 register, per the
# doc's separate "Booleans" table) - reading them as UINT32 silently pulls in
# the *next* register as a bogus high word (confirmed live: read User
# Security Enable as UINT32 and it came back 65536, because register 16397
# "Priority Security" = 1 landed in what convert_from_registers treated as
# the high word). Get the dtype/count wrong here and this whole script's
# purpose - telling you whether a write was actually applied - breaks
# silently.
REGISTERS = [
    (16950, "Occupancy (write target)", DT.UINT32),
    (16890, "Operation Switch (write target, Phase 2)", DT.UINT32),
    (16928, "Occupied (PLC status readback)", DT.UINT32),
    (16936, "Remote Occ Unocc (hardwired relay input)", DT.UINT32),
    (16946, "Fans Enable", DT.UINT32),
    (16396, "User Security Enable (if on, writes may be gated)", DT.UINT16),
    (16392, "Occ Security (if on, occupied force switch may be gated)", DT.UINT16),
]


def read_all(client: ModbusTcpClient) -> None:
    for addr, label, dtype in REGISTERS:
        count = 2 if dtype == DT.UINT32 else 1
        result = client.read_holding_registers(address=addr - 1, count=count, slave=UNIT_ID)
        if result.isError():
            print(f"  {label} @ {addr}: ERROR {result}")
            continue
        value = client.convert_from_registers(result.registers, data_type=dtype, word_order="little")
        print(f"  {label} @ {addr}: {value}")


def write_register(client: ModbusTcpClient, addr: int, label: str, value: int) -> None:
    """UDINT (2-register) writes only - both current callers (Occupancy
    16950, Operation Switch 16890) are BOOL-in-UDINT per the doc's Booleans
    table. Don't point this at a BOOL-in-INT register (1 register, e.g. the
    security flags in REGISTERS below) without adding a dtype/count param -
    that exact single-vs-double-register mismatch already produced garbage
    readbacks once in this file (see module history)."""
    registers = client.convert_to_registers(value, data_type=DT.UINT32, word_order="little")
    result = client.write_registers(address=addr - 1, values=registers, slave=UNIT_ID)
    if result.isError():
        print(f"Write failed: {result}")
        sys.exit(1)
    print(f"Wrote {label} = {value}")

    readback = client.read_holding_registers(address=addr - 1, count=2, slave=UNIT_ID)
    if readback.isError():
        print(f"  WARNING: could not read back {label} to verify: {readback}")
        return
    readback_value = client.convert_from_registers(readback.registers, data_type=DT.UINT32, word_order="little")
    if readback_value != value:
        print(f"  *** WARNING: wrote {value} but {label} now reads {readback_value} - the write was "
              f"silently rejected or overridden. Check User Security Enable (16396) / Occ Security (16392) "
              f"below before concluding anything about what value {value} means. ***")


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in ("read", "write"):
        print(__doc__)
        sys.exit(1)

    client = ModbusTcpClient(MODBUS_HOST, port=MODBUS_PORT, timeout=5)
    if not client.connect():
        print(f"Could not connect to {MODBUS_HOST}:{MODBUS_PORT}")
        sys.exit(1)

    if sys.argv[1] == "read":
        read_all(client)
    elif sys.argv[1] == "write":
        usage = "usage: python probe_occupancy.py write occupancy <0|1|2> | write operation <0|1>"
        if len(sys.argv) != 4 or sys.argv[2] not in ("occupancy", "operation"):
            print(usage)
            sys.exit(1)
        target, raw_value = sys.argv[2], sys.argv[3]
        if target == "occupancy":
            if raw_value not in ("0", "1", "2"):
                print(usage)
                sys.exit(1)
            write_register(client, 16950, "Occupancy", int(raw_value))
        else:
            if raw_value not in ("0", "1"):
                print(usage)
                sys.exit(1)
            write_register(client, 16890, "Operation Switch", int(raw_value))
        print("waiting 2s for the PLC to react...")
        time.sleep(2)
        read_all(client)

    client.close()


if __name__ == "__main__":
    main()
