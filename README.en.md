# codex-buddy-bridge

Route [Codex](https://developers.openai.com/codex) `PermissionRequest` hooks
to the BLE "buddy" hardware ecosystem from
[anthropics/claude-desktop-buddy](https://github.com/anthropics/claude-desktop-buddy).
Press a button on a tiny ESP32 device to approve or deny Codex actions
instead of clicking through prompts in the app.

> **Not affiliated with Anthropic or OpenAI.** This is an independent
> third-party project. "Codex" and "Claude" are trademarks of their
> respective owners.

> 🌏 **中文版**: [README.md](README.md)

## How it works

Codex shipped a stable hooks framework in April 2026, including a
`PermissionRequest` hook that fires whenever the agent is about to ask the
user for an approval (shell command, `apply_patch`, MCP tool call, …).
This project plugs that hook into the existing Claude Desktop Buddy BLE
protocol:

```
Codex (CLI or Desktop) ─▶ PermissionRequest hook (stdin JSON)
                          │
                          ▼  (Unix socket /tmp/codex-buddy.sock)
                       daemon ─▶ on-demand BLE NUS connection
                          │     ▼
                          │   M5 buddy firmware (unchanged)
                          │     ▲  user presses A or B
                          ▼     │
                   {"decision": "allow" | "deny"} ─▶ Codex stdout
```

The daemon does **not** hold the BLE peripheral while idle. BLE is acquired
only during an active approval (~3-5 s) and released immediately, so the
same physical device can keep working with the Claude Desktop app the rest
of the time.

If the buddy is unreachable when an approval fires (Claude has it paired,
device asleep, etc.), the hook returns no decision and Codex falls back to
its native approval prompt. **Codex never hangs because of this bridge.**

## Requirements

- macOS (the daemon depends on `launchd` and Unix domain sockets).
- A `Claude-…` BLE device running the firmware from
  [anthropics/claude-desktop-buddy](https://github.com/anthropics/claude-desktop-buddy)
  — any of the supported boards (M5StickC Plus, M5StickC S3, etc.) works.
  No firmware modification is needed; this bridge speaks the same wire
  protocol Claude does.
- [Codex](https://developers.openai.com/codex) CLI or Desktop, **April 2026
  build or newer** (stable hooks).
- Python 3.9+.

## Install

```bash
git clone https://github.com/Yamiqu/codex-buddy-bridge.git
cd codex-buddy-bridge
./install.sh
```

The installer:

1. Creates `.venv/` and installs `bleak`.
2. Renders the launchd plist with absolute paths and `launchctl load`s it.
   The daemon respawns on crash.
3. Adds `[features]\ncodex_hooks = true` to `~/.codex/config.toml`.
4. Writes `~/.codex/hooks.json` with a `PermissionRequest` entry pointing at
   `hooks/permission_request.py`. Any existing `hooks.json` is backed up.
5. Prints next manual steps.

After install:

- **Restart Codex Desktop and any open Codex CLI sessions** so the
  app-server reloads the hooks config.
- **Approve the macOS Bluetooth prompt** the first time the daemon needs
  the buddy. The prompt targets `.venv/bin/python3`; once granted,
  launchd-spawned runs inherit the permission.

## CLI

The bridge ships a small `codex-buddy` script for everyday control:

| command | what it does |
| --- | --- |
| `codex-buddy status` | is the agent loaded? + last 10 log lines |
| `codex-buddy on` (or `start`) | `launchctl load` the agent |
| `codex-buddy off` (or `stop`) | `launchctl unload`; releases BLE immediately |
| `codex-buddy restart` | unload then load |
| `codex-buddy log` | `tail -F` the daemon log |
| `codex-buddy foreground` | stop the launchd copy and run the daemon in this terminal with `--debug`. Ctrl-C to quit; no respawn |
| `codex-buddy probe` | scan BLE briefly and report what's visible — useful when the log says "No BLE device found" |
| `codex-buddy uninstall` | unload, remove plist, remove `~/.codex/hooks.json` (backed up) |

Tip: drop an alias in your shell rc to make it global:

```bash
alias cbuddy="$HOME/Documents/GitHub/codex-buddy-bridge/codex-buddy"
```

## Verify

```bash
# 1. unit tests
PYTHONPATH=. python3 -m unittest discover -s tests

# 2. daemon log — should print "Daemon ready (on-demand BLE)"
codex-buddy log

# 3. mock a permission round-trip without involving Codex
echo '{"event":"permission_request","payload":{"session_id":"s","turn_id":"t","tool_name":"Bash","tool_input":{"command":"ls","description":"list dir"}}}' \
  | nc -U /tmp/codex-buddy.sock
# Buddy briefly connects, displays "Bash" / "list dir"; press A → nc prints
# {"decision":"allow",...}; daemon disconnects within ~1 s.

# 4. end-to-end: ask Codex to do something needing approval; the buddy lights up.
```

## Daemon flags

`codex-buddy foreground` accepts the daemon's flags:

| flag | default | meaning |
| --- | --- | --- |
| `--device-prefix` | `Claude-` | BLE name prefix to scan for |
| `--address` | (none) | skip scanning, use this BLE address |
| `--socket` | `/tmp/codex-buddy.sock` | Unix socket path |
| `--debug` | off | verbose logging |

## Coexistence with Claude Hardware Buddy

The buddy peripheral is a standard ESP32 BLE peripheral and supports **only
one central at a time**. So at any moment the device talks to *either* the
Claude Desktop app *or* this daemon — not both.

- **Claude is paired right now** → the daemon's BLE scan returns nothing
  (the peripheral stops advertising once connected) → hook returns no
  decision → Codex falls back to its native approval UI. No hang, no error.
- **Nobody is paired** → the daemon connects in a few seconds, shows the
  approval on the buddy, releases the connection after the press, and
  Claude Desktop can re-pair right after.

If you want to silence the daemon entirely while focusing on Claude, run
`codex-buddy off`; `codex-buddy on` re-enables it.

## Troubleshooting

Walk these layers in order — each is independent.

**1. Claude Hardware Buddy stopped working.** Run `codex-buddy off`. That
releases BLE. If Claude still can't see the device, this bridge isn't the
cause; debug Claude / firmware separately.

**2. Codex approval doesn't reach the buddy.** Check the log:
```bash
codex-buddy log
```
You should see `Pending approval c-… for Bash: …` followed by either a
decision or `BLE connect failed` (someone else has the device).

If nothing shows up at all when Codex asks for approval, the hook config is
the culprit:
```bash
grep -A1 features ~/.codex/config.toml      # codex_hooks = true
cat ~/.codex/hooks.json | python3 -m json.tool
```
After config changes, **restart Codex** (the app-server caches the config
at startup).

**3. "No BLE device found" in the log.** Run `codex-buddy probe` — it
scans for a few seconds and reports what's visible, with diagnostics.

**4. Daemon won't start.** `codex-buddy status` to see launchd state, then
`codex-buddy foreground` to watch startup errors live.

**5. Hook script smoke test.**
```bash
echo '{"session_id":"s","turn_id":"t","tool_name":"Bash","tool_input":{"command":"ls"}}' \
  | hooks/permission_request.py
```
Returns within ~110 s with the Codex JSON, or empty if the daemon is down
(in which case Codex falls back to its native prompt — by design). The
script writes diagnostics to stderr and never crashes Codex.

## Architecture

```
codex_buddy_bridge/
├── __main__.py        argparse + asyncio.run(daemon.main(...))
├── daemon.py          on-demand approval flow, request id synthesis
├── ipc.py             async server, sync stdlib client (used by hooks)
├── ble_transport.py   bleak NUS client (one connect per approval)
└── protocol.py        wire JSON: time, owner, snapshot, prompt, decisions
hooks/
└── permission_request.py  IPC client; blocks until daemon answers
scripts/
└── probe.py               ad-hoc BLE scanner used by `codex-buddy probe`
codex-buddy             CLI: status / on / off / log / foreground / probe / uninstall
launchd/
└── com.claudecodebuddy.codex-buddy.plist.template
install.sh
requirements.txt
tests/
```

## Compatibility notes

- **Codex hooks were stable in April 2026.** Earlier builds either don't
  have `PermissionRequest` or use a different schema. Run
  `codex --version` and update if hooks don't fire.
- **Codex Desktop App vs. CLI.** Hooks fire in Codex's app-server, which
  both clients use, so this bridge works for both.
- **macOS only.** The bridge depends on launchd and Unix sockets. The
  protocol layer is portable; a Linux systemd-user unit would be a small
  additional file. PRs welcome.
- **Firmware is unchanged.** The wire format (NUS UUIDs, snapshot fields,
  permission decision shape) follows the
  [REFERENCE.md](https://github.com/anthropics/claude-desktop-buddy/blob/main/REFERENCE.md)
  protocol the Claude Desktop app speaks. The same `Claude-…` device works
  for both hosts.

## Acknowledgments

- [anthropics/claude-desktop-buddy](https://github.com/anthropics/claude-desktop-buddy)
  for the firmware, the wire protocol reference, and the maker-friendly
  attitude that made this bridge possible.
- OpenAI for shipping the `PermissionRequest` hook on a stable surface.

## License

[MIT](LICENSE).

## Friendly Links

[![LINUXDO](https://img.shields.io/badge/%E7%A4%BE%E5%8C%BA-LINUXDO-0086c9?style=for-the-badge&labelColor=555555)](https://linux.do)
