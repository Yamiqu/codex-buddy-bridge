#!/usr/bin/env python3
"""Diagnose why the daemon couldn't find the buddy.

Runs a short BLE scan independent of the daemon (the daemon doesn't hold
BLE while idle, so they don't interfere) and prints every discovered
device, highlighting any that match the configured Claude- prefix.

Use this when the daemon log shows 'No BLE device found with prefix' to
figure out whether:
  - Claude Desktop has the buddy paired (peripheral stops advertising
    once a central connects),
  - the buddy is asleep / off,
  - or something else is going on.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

try:
    from bleak import BleakScanner
except ImportError:
    print("bleak not installed; run install.sh first or activate the venv", file=sys.stderr)
    sys.exit(2)


async def main(prefix: str, timeout: float) -> int:
    print(f"Scanning for {timeout:.0f}s for any BLE device (prefix of interest: {prefix!r})\n")
    discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)

    if not discovered:
        print("No BLE devices seen at all.")
        print("Macs with Bluetooth turned off, or the venv Python missing")
        print("Bluetooth permission, both look like this. Check System")
        print("Settings → Privacy & Security → Bluetooth.")
        return 1

    print(f"{'name':<32} {'address':<22} rssi")
    print("-" * 60)
    found_target = False
    for addr, (dev, adv) in sorted(discovered.items(), key=lambda kv: -(kv[1][1].rssi or -127)):
        name = (dev.name or adv.local_name or "<no name>")[:31]
        match = name.startswith(prefix)
        marker = "  ← MATCH" if match else ""
        print(f"{name:<32} {addr:<22} {adv.rssi}{marker}")
        if match:
            found_target = True

    print()
    if found_target:
        print(f"✓ Found a {prefix!r} device — daemon should be able to connect to it next time.")
        return 0

    print(f"✗ No device starting with {prefix!r} seen. Likely causes:")
    print("  1. Claude Desktop has the buddy paired right now → it stops")
    print("     advertising once connected. Close Claude's Hardware Buddy")
    print("     window (or quit Claude Desktop) and probe again.")
    print("  2. The device is asleep / powered off → press a button or")
    print("     plug it in.")
    print("  3. Bluetooth permission for the venv Python isn't granted →")
    print("     System Settings → Privacy & Security → Bluetooth → enable")
    print("     the entry pointing at .venv/bin/python3.")
    return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", default="Claude-")
    parser.add_argument("--timeout", type=float, default=8.0)
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.prefix, args.timeout)))
