# Codex Feishu Bridge Skill

Reusable Codex skill for setting up a local Feishu/Lark bot bridge to Codex.

The bridge treats Feishu as a transport interface. Messages go to the user's local Codex CLI/Desktop runtime, so account/API/model switching, installed skills, MCP tools, local memory, filesystem access, and Codex automation metadata stay on the user's machine.

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

Verify:

```bash
scripts/verify_codex_feishu_bridge.py --home "$HOME/.codex-feishu"
```

## Notes

- The bundled bridge app is intentionally minimal and sanitized.
- Scheduled task output can be routed to one Feishu chat with `CODEX_FEISHU_NOTIFY_CHAT_ID`.
- Feishu-created tasks are mirrored into `$CODEX_HOME/automations` so Codex can see them.
- Send `/new` or `/reset` to the bot to clear only the current Feishu chat context.
- The scheduler is timer-based; the wake window is only a resilience bound.
- No user credentials, chat IDs, logs, conversations, or task stores are included in this repository.
