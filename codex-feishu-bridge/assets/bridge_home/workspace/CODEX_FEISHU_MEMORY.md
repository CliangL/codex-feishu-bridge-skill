# Codex Feishu Memory

- This Feishu bridge is an independent local Codex integration.
- Feishu is only the message transport. Actual work must run through the user's local Codex via `codex app-server`.
- Dedicated Feishu bot credentials live in `$CODEX_FEISHU_HOME/.env`; never store them in this file.
- Local shared scheduled tasks live in `$CODEX_FEISHU_HOME/tasks.json` and are mirrored into `$CODEX_HOME/automations`.
- Default workspace is `$CODEX_FEISHU_HOME/workspace` unless a task or command explicitly chooses another workspace.
- The bridge may use a Feishu-specific `codex-home` for model selection while sharing local Codex auth/global state, skills, memories, sessions, plugins, and automations from the main `$CODEX_HOME`.
- Use `/new` or `/reset` to clear only the Feishu chat's lightweight context. Shared memory, installed skills, automations, and Codex login/API configuration remain intact.
- Use `/stop` to terminate the currently running local Codex turn from Feishu.
- Use `/model` and `/model <provider> <model> <reasoning>` to inspect or change the Feishu-side provider/API profile and model without changing the desktop Codex model.

## Public Execution Progress

- The visible Feishu execution process should be public progress, not private chain-of-thought.
- Public progress should explain the concrete execution path: what is being checked, what was found, what failed, what route changed, and what verification passed.
- Raw shell commands, local paths, JSON payloads, code snippets, and large tool results belong in the collapsed tool panel or logs, not the visible progress area.
- Notification-only tasks should send only the final notification body.
