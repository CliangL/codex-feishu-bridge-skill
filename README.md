# Codex Feishu Bridge Skill

Reusable Codex skill for setting up a local Feishu/Lark bot bridge to Codex.

The bridge treats Feishu as a transport interface. Messages go to the user's local Codex CLI/Desktop runtime, so account/API switching, installed skills, MCP tools, local memory, filesystem access, and Codex automation metadata stay on the user's machine.

This repository now packages the same bridge shape used in the local production install:

- Feishu talks to the local Codex app-server bridge, not a separate cloud AI.
- Feishu keeps a lightweight conversation memory layer, but shares the user's local Codex memory, skills, and automations.
- Feishu-created scheduled tasks are mirrored into Codex automations.
- Feishu can use a fixed model/runtime independent from desktop model switching through `/model`.
- Notification chats can receive notification-only正文 without footer/tool/progress noise.

## Install The Skill

Copy `codex-feishu-bridge/` into a Codex skills directory, for example:

```bash
mkdir -p "$HOME/.codex/skills"
cp -R codex-feishu-bridge "$HOME/.codex/skills/"
```

Then ask Codex to use the `codex-feishu-bridge` skill.

## Local Bridge Install

From the skill folder:

```bash
scripts/install_codex_feishu_bridge.py \
  --app-id FEISHU_APP_ID \
  --app-secret FEISHU_APP_SECRET \
  --notify-chat-id FEISHU_NOTIFY_CHAT_ID \
  --install-launch-agent
```

Use your own Feishu app values. Do not commit `.env`.

By default this installs to `~/.codex-feishu` and creates:

- `~/.codex-feishu/app`
- `~/.codex-feishu/runtime/src`
- `~/.codex-feishu/workspace`
- `~/.codex-feishu/codex-home`
- `~/.codex-feishu/conversations`
- `~/.codex-feishu/tasks.json`

The install keeps Feishu model/runtime state under `~/.codex-feishu/codex-home`, while linking shared Codex assets such as `skills`, `automations`, `memories`, `sessions`, `plugins`, and auth/global state from the main `~/.codex`.

Verify:

```bash
scripts/verify_codex_feishu_bridge.py --home "$HOME/.codex-feishu"
```

## Notes

- The bundled bridge app is sanitized, but it is not a toy example; it is meant to preserve the real local Codex-to-Feishu workflow.
- Scheduled task output can be routed to one Feishu chat with `CODEX_FEISHU_NOTIFY_CHAT_ID`.
- Feishu-created tasks are mirrored into `$CODEX_HOME/automations` so Codex can see them.
- Send `/new` or `/reset` to the bot to clear only the current Feishu chat context.
- Send `/stop` to terminate the current running Codex turn from Feishu.
- Send `/model` to view the current Feishu model and supported models.
- Send `/model <name>` to switch the Feishu-side model without affecting the desktop Codex model.
- Mid-run follow-up messages are classified: progress questions reply immediately without interrupting, clear corrections interrupt, and ordinary supplemental messages are queued for the next turn.
- The scheduler is timer-based; the wake window is only a resilience bound.
- No user credentials, chat IDs, logs, conversations, or task stores are included in this repository.
