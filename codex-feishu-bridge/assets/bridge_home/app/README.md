# Codex Feishu Bridge

Standalone local Feishu/Lark bot bridge for Codex. It has its own Feishu app credentials, runtime, workspace, memory, task store, logs, and LaunchAgent under `~/.codex-feishu`.

## Paths

- App runtime: `~/.codex-feishu/app`
- Python/runtime source: `~/.codex-feishu/runtime`
- Credentials: `~/.codex-feishu/.env`
- Default workspace: `~/.codex-feishu/workspace`
- Feishu-specific Codex home: `~/.codex-feishu/codex-home`
- Shared memory: `~/.codex-feishu/shared-memory.md`
- Per-conversation lightweight memory: `~/.codex-feishu/conversations`
- Scheduled tasks: `~/.codex-feishu/tasks.json`
- Codex automation mirrors: `~/.codex/automations/codex-feishu-*/automation.toml`
- Logs: `~/.codex-feishu/logs/launchd.err.log`
- LaunchAgent: `~/Library/LaunchAgents/com.codex.feishu.plist`

## Commands

```bash
~/.codex-feishu/app/start.sh --check
~/.codex-feishu/app/start.sh --connect-check --connect-check-seconds 2
launchctl print gui/$(id -u)/com.codex.feishu
launchctl kickstart -k gui/$(id -u)/com.codex.feishu
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.codex.feishu.plist
```

## Feishu Task Syntax

```text
/task daily 08:30 整理今天日程和待办
/task every 30m 检查下载状态
/task at 2026-05-25T09:00 提醒我更新日报
/task weekly mon,wed@08:30 生成项目周报
```

## Notes

- The bridge replies through the same dedicated Feishu bot that receives the message.
- Feishu keeps its own provider/API profile and model selection in `~/.codex-feishu/codex-home/config.toml` and `~/.codex-feishu/feishu-model.json`, so desktop model switches do not affect the bot. Use `/model` to inspect it and `/model <provider> <model> <reasoning>` to switch precisely.
- Configure fallback models with `CODEX_FEISHU_FALLBACK_MODELS` or from Feishu with `/fallback-model`. The list is generic: any provider/model/reasoning profile already present in the Feishu `codex-home` can be used. If the active model fails with a retryable provider error such as insufficient balance, quota, rate limit, upstream 5xx, or timeout, the bridge switches to the next fallback model and retries the current turn.
- Runtime fallback commands: `/fallback-model` shows the list, `/fallback-model set <provider> <model> <reasoning>` replaces it, `/fallback-model add <provider> <model> <reasoning>` appends one candidate, and `/fallback-model clear` disables automatic fallback.
- Codex account login and API login do not share the same cloud thread; this bridge shares local memory, lightweight Feishu conversation summaries, and scheduled tasks through `~/.codex-feishu`.
- Normal Feishu chat uses a fresh local Codex turn per message and only injects the rolling summary plus recent turns, so long Feishu history does not keep inflating one app-server session.
- Feishu-created scheduled tasks are authoritative in `~/.codex-feishu/tasks.json` and mirrored into Codex automations for visibility. `com.codex.feishu` sleeps until the next due time, and Feishu task changes wake the scheduler to recalculate; tasks do not execute every 60 seconds.
- Skills created or installed from Feishu must go into Codex-visible skill paths such as `~/.codex/skills` or `~/.agents/skills`.
- Do not print app secrets, access tokens, API keys, or complete access keys in logs or chat.
