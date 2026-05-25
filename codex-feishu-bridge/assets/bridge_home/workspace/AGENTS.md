# Codex Feishu Bridge Workspace

This is the independent default workspace for the local Codex Feishu bridge.

Rules:
- Use this workspace plus `$CODEX_FEISHU_HOME/shared-memory.md` as the default shared local context.
- Always read `USER_MEMORY.md`, `CODEX_FEISHU_MEMORY.md`, and this `AGENTS.md` before deciding that local context is missing.
- Do not read unrelated project memory files unless the user explicitly asks to work inside that project.
- Do not leak API keys, tokens, passwords, app secrets, or full access keys.
- Scheduled tasks created through the Feishu bot are stored in `$CODEX_FEISHU_HOME/tasks.json` and mirrored into Codex automations.
- When answering through Feishu, emit public execution updates with `执行进展：...` before substantial tool work and after key findings, failures, or verification. These updates should say what is being checked and what was learned, not raw commands, paths, JSON, or generic status.
- Keep tool-call details in the Feishu card's collapsed tool panel. The visible execution process should be readable like Codex desktop commentary.
