# codex-buddy-bridge

把 [Codex](https://developers.openai.com/codex) 的 `PermissionRequest` hook
桥接到 [anthropics/claude-desktop-buddy](https://github.com/anthropics/claude-desktop-buddy)
的 BLE "buddy" 硬件生态。在一台小小的 ESP32 设备上按按键来批准或拒绝 Codex
的动作，不必再去应用里点弹窗。

> **本项目与 Anthropic 和 OpenAI 均无任何关联。** 这是一个独立的第三方项目。
> "Codex" 是 OpenAI 的商标，"Claude" 是 Anthropic 的商标。

> 🌏 **English**: [README.en.md](README.en.md)

## 工作原理

Codex 在 2026 年 4 月推出了 stable hooks 框架，其中 `PermissionRequest` hook
会在 agent 即将弹出审批（shell 命令、`apply_patch`、MCP 工具调用等）时触发。
本项目把这个 hook 接到既有的 Claude Desktop Buddy BLE 协议上：

```
Codex (CLI 或 Desktop) ─▶ PermissionRequest hook（stdin JSON）
                          │
                          ▼  (Unix socket /tmp/codex-buddy.sock)
                       daemon ─▶ 按需 BLE NUS 连接
                          │     ▼
                          │   M5 buddy 固件（零改动）
                          │     ▲  用户按 A 或 B
                          ▼     │
                   {"decision": "allow" | "deny"} ─▶ Codex stdout
```

Daemon **平时不占用 BLE**。BLE 仅在审批触发时才临时获取（约 3-5 秒），结束后
立即释放。所以同一台物理设备其余时间可以正常给 Claude Desktop 用。

如果审批触发时 buddy 不可用（Claude 占着、设备睡了等），hook 会返回"无决策"，
Codex 自动 fallback 到原生审批弹窗。**Codex 永远不会因为这个桥而卡死。**

## 环境要求

- macOS（daemon 依赖 `launchd` 和 Unix domain socket）
- 一台 `Claude-…` 名字的 BLE 设备，跑着
  [anthropics/claude-desktop-buddy](https://github.com/anthropics/claude-desktop-buddy)
  的固件 —— 任何官方支持的开发板都行（M5StickC Plus、M5StickC S3 等）。
  无需修改固件；本桥说的是和 Claude 完全相同的 wire protocol。
- [Codex](https://developers.openai.com/codex) CLI 或 Desktop，
  **2026 年 4 月版本或更新**（带 stable hooks）
- Python 3.9+

## 安装

```bash
git clone https://github.com/Yamiqu/codex-buddy-bridge.git
cd codex-buddy-bridge
./install.sh
```

安装脚本会：

1. 创建 `.venv/` 并安装 `bleak`
2. 用绝对路径渲染 launchd plist 并 `launchctl load`，daemon 自动重启
3. 在 `~/.codex/config.toml` 里加 `[features]\ncodex_hooks = true`
4. 把 `PermissionRequest` 配置写进 `~/.codex/hooks.json`，已存在的 hooks.json
   会被备份
5. 打印剩余的手动步骤

安装完成后：

- **重启 Codex Desktop 和已开的 Codex CLI session**，让 app-server 重新加载
  hooks 配置
- 第一次审批触发时，macOS 会弹**蓝牙权限请求**，目标是 `.venv/bin/python3`，
  同意一次即可；之后 launchd 启动的 daemon 会继承这个权限

## CLI

桥附带一个小巧的 `codex-buddy` 脚本管理日常操作：

| 命令 | 作用 |
| --- | --- |
| `codex-buddy status` | agent 是否在跑 + 末尾 10 行日志 |
| `codex-buddy on`（或 `start`） | `launchctl load` 启动 daemon |
| `codex-buddy off`（或 `stop`） | `launchctl unload`，立即释放 BLE |
| `codex-buddy restart` | unload 然后再 load |
| `codex-buddy log` | `tail -F` daemon 日志 |
| `codex-buddy foreground` | 停掉 launchd 那份，在当前终端 `--debug` 跑一份；Ctrl+C 退出后不重启 |
| `codex-buddy probe` | 短暂扫描 BLE 并报告可见设备，日志报"找不到设备"时用 |
| `codex-buddy uninstall` | unload，删 plist，删 `~/.codex/hooks.json`（备份） |

建议在 shell rc 里加一个 alias 全局可用：

```bash
alias cbuddy="$HOME/Documents/GitHub/codex-buddy-bridge/codex-buddy"
```

## 验证

```bash
# 1. 单元测试
PYTHONPATH=. python3 -m unittest discover -s tests

# 2. daemon 日志 —— 应该看到 "Daemon ready (on-demand BLE)"
codex-buddy log

# 3. 不经过 Codex，直接模拟一次审批往返
echo '{"event":"permission_request","payload":{"session_id":"s","turn_id":"t","tool_name":"Bash","tool_input":{"command":"ls","description":"列目录"}}}' \
  | nc -U /tmp/codex-buddy.sock
# buddy 短暂连上、显示 "Bash" / "列目录"；按 A → nc 打印
# {"decision":"allow",...}；daemon 在约 1 秒内断开。

# 4. 端到端：让 Codex 跑一个需要审批的命令；buddy 会亮起。
```

## Daemon 参数

`codex-buddy foreground` 接受 daemon 的命令行参数：

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `--device-prefix` | `Claude-` | 扫描的 BLE 名字前缀 |
| `--address` | (无) | 跳过扫描，直接用此地址 |
| `--socket` | `/tmp/codex-buddy.sock` | Unix socket 路径 |
| `--debug` | 关 | verbose 日志 |

## 与 Claude Hardware Buddy 共存

buddy 是标准 ESP32 BLE peripheral，**一次只能被一个 central 连接**。所以任何
时刻设备**要么和 Claude Desktop 通信，要么和本 daemon 通信**，不能同时。

- **Claude 此刻正连着** → daemon 扫描扫不到任何东西（peripheral 一旦被连上
  就停止广播）→ hook 返回无决策 → Codex 走原生审批 UI。不卡、不报错。
- **没人连着** → daemon 几秒内连上 buddy，把审批显示出来，按完后立即释放，
  Claude Desktop 可以马上重连。

如果你想专心用 Claude、彻底让 daemon 闭嘴，跑 `codex-buddy off` 即可，要用
Codex 时再 `codex-buddy on`。

## 故障排查

按下面的顺序逐层排查，每层独立：

**1. Claude Hardware Buddy 不工作了。** 跑 `codex-buddy off`，BLE 立即释放。
如果 Claude 还是看不到设备，那就不是这个桥的问题，去单独排查 Claude / 固件。

**2. Codex 审批没到 buddy。** 看日志：
```bash
codex-buddy log
```
应当看到 `Pending approval c-… for Bash: …`，紧接着要么是一条决策，要么是
`BLE connect failed`（说明设备被别人占着）。

如果 Codex 提交审批时日志里**什么都没有**，那就是 hook 配置的问题：
```bash
grep -A1 features ~/.codex/config.toml      # 应当有 codex_hooks = true
cat ~/.codex/hooks.json | python3 -m json.tool
```
改完配置后**必须重启 Codex**（app-server 启动时会缓存配置）。

**3. 日志写"No BLE device found"。** 跑 `codex-buddy probe`，它会扫几秒然后
报告周围所有可见的 BLE 设备，带诊断提示。

**4. Daemon 起不来。** `codex-buddy status` 看 launchd 状态，再
`codex-buddy foreground` 在终端实时看启动错误。

**5. Hook 脚本冒烟测试**：
```bash
echo '{"session_id":"s","turn_id":"t","tool_name":"Bash","tool_input":{"command":"ls"}}' \
  | hooks/permission_request.py
```
约 110 秒内会返回 Codex 要的 JSON，daemon 不在时返回空（这种情况下 Codex 会
fallback 到原生审批，是设计预期）。脚本所有诊断都走 stderr，绝不会让 Codex
崩。

## 项目结构

```
codex_buddy_bridge/
├── __main__.py        argparse + asyncio.run(daemon.main(...))
├── daemon.py          按需审批流程，request id 合成
├── ipc.py             async 服务端，hook 用的同步 stdlib 客户端
├── ble_transport.py   bleak NUS 客户端（一次审批一次连接）
└── protocol.py        wire JSON：time / owner / snapshot / prompt / decisions
hooks/
└── permission_request.py  IPC 客户端，阻塞等 daemon 回执
scripts/
└── probe.py               BLE 扫描脚本，被 `codex-buddy probe` 调用
codex-buddy             CLI：status / on / off / log / foreground / probe / uninstall
launchd/
└── com.claudecodebuddy.codex-buddy.plist.template
install.sh
requirements.txt
tests/
```

## 兼容性说明

- **Codex hooks 在 2026 年 4 月才稳定**。更早的版本要么没有
  `PermissionRequest`，要么字段不一样。如果 hook 不触发，先 `codex --version`
  看看版本然后升级。
- **Codex Desktop App 与 CLI 通用**。Hook 在 Codex 的 app-server 触发，两边
  共用，所以这个桥两边都能用。
- **仅 macOS**。Daemon 依赖 launchd 和 Unix socket。协议层是平台无关的；
  Linux 上加一个 systemd-user unit 应该不难，欢迎 PR。
- **固件零改动**。Wire format（NUS UUID、snapshot 字段、审批决策格式）严格
  遵循
  [REFERENCE.md](https://github.com/anthropics/claude-desktop-buddy/blob/main/REFERENCE.md)
  里 Claude Desktop 用的协议。同一台 `Claude-…` 设备给两边轮流用。

## 致谢

- [anthropics/claude-desktop-buddy](https://github.com/anthropics/claude-desktop-buddy)：
  提供了固件、wire protocol 文档，以及鼓励 maker 生态的开放态度，本桥才能做出来。
- OpenAI：把 `PermissionRequest` hook 落到了 stable surface 上。

## 许可

[MIT](LICENSE)。

## Friendly Links

[![LINUXDO](https://img.shields.io/badge/%E7%A4%BE%E5%8C%BA-LINUXDO-0086c9?style=for-the-badge&labelColor=555555)](https://linux.do)
