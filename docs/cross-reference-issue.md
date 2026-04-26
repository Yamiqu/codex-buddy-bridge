# Cross-reference issue draft

Paste **one** of the two versions below into a new issue at
<https://github.com/anthropics/claude-desktop-buddy/issues/new>. Don't paste
both — pick whichever language fits the upstream's discussion style (the
upstream README and existing issues are English; English version recommended).

The goal is discoverability, not approval. You're not asking them to merge
anything; you're telling their audience that a third-party project exists.
Tone is "FYI for makers", not "please feature me". If they later choose to
add a link from their README, that's a bonus.

---

## English version (recommended)

**Title:** Third-party project: Codex Desktop bridge using PermissionRequest hooks

**Body:**

Hi! 👋 Sharing a project some `claude-desktop-buddy` users might find
useful: <https://github.com/Yamiqu/codex-buddy-bridge>

It routes [Codex](https://developers.openai.com/codex)'s `PermissionRequest`
hook (stable since April 2026) to the same BLE buddy hardware this repo
documents — so a single `Claude-…` device can show approvals from both
Claude Desktop and Codex Desktop / CLI. The firmware is unchanged; the
bridge is a small macOS daemon that speaks your `REFERENCE.md` wire
protocol.

### Design notes worth flagging

- **No firmware fork.** The bridge speaks the protocol documented in
  `REFERENCE.md` verbatim (NUS UUIDs, snapshot fields, permission decision
  shape). The same buddy device works for both ecosystems.
- **On-demand BLE.** Since BLE peripherals are 1:1, the daemon never holds
  the connection while idle — it acquires BLE only during an active Codex
  approval (~3-5 s), then releases it so the Claude Desktop app stays
  fully functional.
- **Graceful fallback.** If the buddy is unreachable when an approval
  fires (Claude has it paired, device asleep, etc.), the hook returns no
  decision and Codex falls back to its native approval prompt. Codex
  never hangs because of the bridge.

Independent third-party project, MIT-licensed, not affiliated with
Anthropic or OpenAI. Just thought maker folks here might be interested
that the protocol works equally well for OpenAI's agent.

Happy to take feedback on the protocol consumption side if you spot
anything I got wrong.

— @Yamiqu

---

## 中文版（备选；如果上游 issue 区有中文讨论传统再用）

**标题：** 第三方项目：基于 PermissionRequest hook 的 Codex Desktop 桥

**正文：**

各位好 👋 分享一个 `claude-desktop-buddy` 用户可能会感兴趣的项目：
<https://github.com/Yamiqu/codex-buddy-bridge>

它把 [Codex](https://developers.openai.com/codex) 的 `PermissionRequest`
hook（2026 年 4 月起 stable）接到这个仓库定义的 BLE buddy 硬件协议上 ——
同一台 `Claude-…` 设备可以同时给 Claude Desktop 和 Codex Desktop / CLI
显示审批。固件零改动，桥本身是一个 macOS 上的小 daemon，说的是你们
`REFERENCE.md` 里的那套 wire protocol。

### 几个值得说的设计点

- **不 fork 固件。** 桥严格遵循 `REFERENCE.md`（NUS UUID、snapshot 字段、
  permission decision 格式），同一台 buddy 给两边轮流用。
- **按需 BLE。** BLE peripheral 是 1:1，所以 daemon 平时**完全不占** BLE，
  仅在 Codex 审批触发时临时获取（约 3-5 秒），用完立即释放，Claude Desktop
  其余时间不受影响。
- **优雅降级。** 如果审批触发时 buddy 不可用（Claude 占着、设备睡了等），
  hook 返回"无决策"，Codex 自动 fallback 到原生审批弹窗，绝不卡死。

独立的第三方项目，MIT 协议，与 Anthropic 和 OpenAI 均无关联。觉得这个
maker 社区可能对"同一套协议两边都能用"这件事感兴趣，特来分享一下。

如果你们看到我对协议的理解有不对的地方欢迎指出。

— @Yamiqu
