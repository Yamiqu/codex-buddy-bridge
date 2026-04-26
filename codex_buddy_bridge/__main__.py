from __future__ import annotations

import argparse
import asyncio
import logging

from .daemon import DaemonConfig, main as daemon_main
from .ipc import DEFAULT_SOCKET_PATH


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="codex_buddy_bridge",
        description="Daemon bridging Codex hooks to ClaudeCodeBuddy BLE hardware.",
    )
    parser.add_argument("--device-prefix", default="Claude-", help="BLE device name prefix to scan for.")
    parser.add_argument("--address", help="BLE address/identifier (skips scan).")
    parser.add_argument("--socket", default=DEFAULT_SOCKET_PATH, help="Unix socket path for hook IPC.")
    parser.add_argument("--debug", action="store_true", help="Enable verbose logging.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = DaemonConfig(
        socket_path=args.socket,
        device_prefix=args.device_prefix,
        address=args.address,
    )
    asyncio.run(daemon_main(config))


if __name__ == "__main__":
    main()
