# Codex Feishu Bridge Architecture

## Purpose

The bridge makes Feishu/Lark a messaging interface for local Codex. Feishu receives user messages, the local bridge invokes the user's installed Codex CLI/Desktop runtime, and replies are sent back through the same Feishu bot.

This is not a second AI identity. The actual work should run through local Codex so model selection, API login, installed skills, MCP tools, filesystem access, and Codex automations remain local.

## File Layout

Default install:

```text
$HOME/.codex-feishu/
  .env
  app/
    codex_feishu_bridge.py
    requirements.txt
    start.sh
  conversations/
  logs/
  runtime/venv/
  tasks.json
  shared-memory.md
  workspace/
```

Codex automation mirrors:

```text
$CODEX_HOME/automations/codex-feishu-<task-id>/automation.toml
```

macOS service:

```text
$HOME/Library/LaunchAgents/com.codex.feishu.plist
```

## Data Flow

1. Feishu sends `im.message.receive_v1` to the bridge over websocket.
2. The bridge filters duplicate messages and optional allowlists.
3. The bridge creates a fresh Feishu response card for this single user turn.
4. The bridge builds a prompt from shared memory, recent Feishu chat memory, and the user message.
5. The bridge invokes local Codex with `codex exec`.
6. Codex runs using the user's current `$CODEX_HOME` auth/config, skills, MCP servers, and workspace.
7. The bridge updates that turn's card with the final answer.

## Login and Model Switching

Do not store provider credentials in bridge code. Let Codex own login state:

- ChatGPT login, API login, or custom provider config lives under `$CODEX_HOME`.
- The bridge passes `CODEX_HOME` to the Codex subprocess.
- If the user changes accounts, models, API keys, or providers in Codex, the next bridge turn should pick it up.

If a deployment needs a specific model or profile, set it through `CODEX_FEISHU_CODEX_ARGS`, for example:

```text
CODEX_FEISHU_CODEX_ARGS=exec --skip-git-repo-check -m gpt-5.1
```

## Memory

The template keeps lightweight Feishu memory in `conversations/`. Keep it short. Long Feishu conversations should not be replayed forever because they slow response time and waste tokens.

Use `shared-memory.md` for stable bridge instructions, such as notification routing, preferred language, or identity notes. Avoid copying whole transcripts into shared memory.

## Scheduled Tasks

The template supports simple daily task creation and mirrors the task into Codex automations metadata. The Feishu bridge remains the authoritative runner for Feishu notification delivery because it knows the target chat and bot credentials.

The scheduler is timer-based:

- It computes the next due task.
- It sleeps until the next due time or a configured wake window, whichever is sooner.
- It does not run jobs every 60 seconds; `CODEX_FEISHU_TASK_POLL_SECONDS` is a maximum wake window for resilience.

For richer scheduling, extend the task parser and keep writing automation mirror files so Codex can see the tasks.

## Notification Routing

Use `CODEX_FEISHU_NOTIFY_CHAT_ID` to force all scheduled/notification output into one Feishu chat window. If it is empty, tasks reply to the source chat where they were created.

Always store chat IDs in `.env` or local task metadata, not in published skill files.

## Secret Handling

Never commit:

- `.env`
- Feishu `app_secret`
- tenant/user/app access tokens
- OpenAI/provider API keys
- full Feishu chat IDs from a private installation
- logs, conversations, memories, or task stores from a private installation

Before publishing, scan for real values:

```bash
rg -n "cli_|oc_|ou_|app_secret|tenant_access_token|authorization|sk-|ghp_|github_pat_" .
```
