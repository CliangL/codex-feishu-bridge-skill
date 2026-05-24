---
name: codex-feishu-bridge
description: Set up, customize, or troubleshoot a local Codex-to-Feishu bridge. Use when Codex needs to connect a dedicated Feishu/Lark bot to the user's local Codex CLI/Desktop runtime, route bot messages into local Codex, send Codex replies back to Feishu, keep lightweight Feishu conversation memory, expose Feishu-created scheduled tasks in Codex automations, configure notification chat routing, verify LaunchAgent/service logs, or package a sanitized bridge setup for another user.
---

# Codex Feishu Bridge

Use this skill to make Feishu a thin interface to the user's local Codex, not a separate AI service. The bridge should inherit the current local Codex account/API/model from `CODEX_HOME` and should never hard-code secrets, chat IDs, API keys, or user-specific paths.

## Workflow

1. Read `references/architecture.md` before changing bridge behavior or explaining how the pieces fit.
2. Read `references/feishu_setup.md` when creating a new Feishu app or checking scopes/events.
3. For a fresh install, run `scripts/install_codex_feishu_bridge.py --help`, then install with user-provided credentials or create only `.env.example` for manual filling.
4. After installing or editing, run `scripts/verify_codex_feishu_bridge.py --home "$HOME/.codex-feishu"`.
5. On macOS LaunchAgent installs, reload with `launchctl bootout ...` and `launchctl bootstrap ...`; use `launchctl print` and recent logs to confirm the running environment.
6. Before publishing or sharing, scan the whole repo for secrets and personal IDs. Use placeholders such as `FEISHU_APP_ID`, `FEISHU_APP_SECRET`, `FEISHU_NOTIFY_CHAT_ID`, `$HOME/.codex-feishu`, and `$CODEX_HOME`.

## Implementation Rules

- Keep Feishu app credentials in `$HOME/.codex-feishu/.env` or another ignored local env file.
- Keep bridge memory outside the skill repo, normally under `$HOME/.codex-feishu/conversations` and `$HOME/.codex-feishu/shared-memory.md`.
- Invoke local Codex through the installed `codex` binary. Do not call OpenAI or model-provider APIs directly from the bridge.
- Let login/API/model switching flow through `$CODEX_HOME/config.toml` and Codex auth files. The bridge should not maintain a second model config except optional command-line overrides.
- Create a fresh Feishu response card/message for every inbound user turn. Updating that per-turn card is fine; reusing one global card for all turns is not.
- Mirror Feishu-created scheduled jobs into `$CODEX_HOME/automations/<id>/automation.toml` with metadata marking the bridge as the authoritative Feishu runner.
- Route scheduled/notification output to `CODEX_FEISHU_NOTIFY_CHAT_ID` when set; otherwise use the source Feishu chat.
- Treat the scheduler as timer-based. A wake window or fallback wait is acceptable, but do not describe it as executing every N seconds.
- Redact `app_secret`, tokens, API keys, authorization headers, and full access keys from logs and user-facing output.

## Bundled Resources

- `scripts/install_codex_feishu_bridge.py`: copies the reusable bridge template, creates a venv, writes local config examples, and optionally installs a macOS LaunchAgent.
- `scripts/verify_codex_feishu_bridge.py`: checks configuration, dependencies, LaunchAgent status, logs, and Codex automation mirrors without printing secrets.
- `references/architecture.md`: bridge layout, data flow, memory and automation behavior.
- `references/feishu_setup.md`: Feishu developer console checklist, scopes, events, and callback modes.
- `assets/bridge_app/`: sanitized runnable bridge template.
