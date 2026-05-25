# Codex Feishu Bridge Architecture

## Purpose

The bridge makes Feishu/Lark a messaging interface for local Codex. Feishu receives user messages, the local bridge invokes the user's installed Codex CLI/Desktop runtime, and replies are sent back through the same Feishu bot.

This is not a second AI identity. The actual work should run through local Codex so model selection, API login, installed skills, MCP tools, filesystem access, and Codex automations remain local.

## File Layout

Default install:

```text
$HOME/.codex-feishu/
  .env
  feishu-model.json
  app/
    codex_feishu_app.py
    start.sh
    conversation_memory.py
    shared_memory.py
    tasks.py
  codex-home/
    config.toml
  conversations/
  logs/
  runtime/
    src/
    venv/
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
5. The bridge invokes local Codex through `codex app-server`.
6. Codex runs using the Feishu-side `codex-home` config plus shared local Codex auth/state, skills, MCP servers, automations, and workspace.
7. The bridge updates that turn's card with progress, tool summaries, and the final answer.

The progress area is driven first by model-authored public execution updates.
The model emits lines such as `执行进展：我先查 Home Assistant 的实体来源，再确认卫生间人体传感器的状态`.
The bridge displays those lines in the Feishu card and removes them from the
final answer. Low-level command/tool/file-change events remain visible in the
collapsed tool panel; they are only used as visible progress fallback when no
model-authored public progress has appeared yet.

If a new Feishu message arrives while a turn is still running, the bridge should classify it:

- Progress or status questions: answer from the current execution state without interrupting the turn.
- Explicit corrections or direction changes: interrupt the active turn and start a new one with the new instruction.
- Supplemental or unrelated messages: optionally queue them for follow-up after the current turn finishes.

## Login and Model Switching

Do not store provider credentials in bridge code. Let Codex own login state:

- ChatGPT login, API login, or custom provider config lives under `$CODEX_HOME`.
- The bridge keeps a Feishu-specific `codex-home` under `$HOME/.codex-feishu/codex-home`.
- That Feishu `codex-home` links back to the main local Codex auth/global state and shared directories, so account/API switching, skills, memories, sessions, plugins, and automations continue to be shared.
- Desktop model switching does not have to affect Feishu. The bridge can keep its own model in `codex-home/config.toml` and `feishu-model.json`.
- `/model` shows the current Feishu model and supported options.
- `/model <name>` updates only the Feishu-side model/runtime, not the desktop Codex model.

If a deployment wants a fixed default Feishu model, seed it into the Feishu-side `codex-home/config.toml` during install and keep `/model` available for later switching.

## Memory

The template keeps lightweight Feishu memory in `conversations/`. Keep it short. Long Feishu conversations should not be replayed forever because they slow response time and waste tokens.

Send `/new`, `/reset`, `新对话`, or `开启新对话` to clear only the current Feishu chat's lightweight context. This does not delete shared memory, installed skills, Codex automations, Codex auth, or model/API configuration.

Send `/stop`, `stop`, `停止`, `终止`, or `中止` to terminate the currently running Codex turn for that Feishu chat.

During a running task:

- progress questions should return current execution status without interrupting;
- explicit corrections should interrupt and switch direction;
- ordinary supplemental messages may be queued and handled after the current turn.

Use `shared-memory.md` for stable bridge instructions, such as notification routing, preferred language, or identity notes. Avoid copying whole transcripts into shared memory.

The default workspace also loads:

- `USER_MEMORY.md`: non-secret durable user facts, infrastructure aliases, Home Assistant/NAS/server access order, and response preferences.
- `CODEX_FEISHU_MEMORY.md`: bridge-level operating rules.
- `AGENTS.md`: workspace rules for Feishu turns.

Use these files to keep local memory stable across Codex account/API/model
switches. They do not make different cloud Codex threads share a remote thread
id; they provide local continuity for Feishu turns.

## Scheduled Tasks

The template supports simple daily task creation and mirrors the task into Codex automations metadata. The Feishu bridge remains the authoritative runner for Feishu notification delivery because it knows the target chat and bot credentials.

The scheduler is timer-based:

- It computes the next due task.
- It sleeps until the next due time or a configured wake window, whichever is sooner.
- It does not run jobs every 60 seconds; `CODEX_FEISHU_TASK_POLL_SECONDS` is a maximum wake window for resilience.

For richer scheduling, extend the task parser and keep writing automation mirror files so Codex can see the tasks. The bridge remains the authoritative runner for Feishu notification delivery.

## Notification Routing

Use `CODEX_FEISHU_NOTIFY_CHAT_ID` to force all scheduled/notification output into one Feishu chat window. If it is empty, tasks reply to the source chat where they were created.

Notification-only output should contain only the final正文. Do not append runtime footer, tool call summary, or execution-progress sections in that notification window.

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
