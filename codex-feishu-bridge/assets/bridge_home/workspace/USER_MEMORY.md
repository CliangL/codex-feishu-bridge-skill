# User Memory

This file stores stable user preferences and long-lived facts for the local
Codex Feishu bridge. It is loaded into every new Feishu turn as part of the
shared local memory.

Update only with information that is durable and useful across future turns.
Do not put API keys, tokens, passwords, app secrets, private keys, or full
access keys here.

## Confirmed Preferences

- The user is interacting through the Feishu bot, but execution must always use
  the current local Codex on this machine.
- Final replies should stay concise and include what was done, verification
  result, and any important limitations.
- If a skill must be created, installed, or updated, write it to a Codex-visible
  path such as `$CODEX_HOME/skills` or `$HOME/.agents/skills`.
- Feishu notification tasks should use `$CODEX_FEISHU_HOME/app/tasks.py` for
  add/pause/resume/delete instead of editing task JSON directly.

## Infrastructure Access Memory

Store only non-secret connection facts here: host aliases, usernames,
IPs/hostnames, jump paths, ports, service roles, and preferred access order.

Examples to customize:
- Home Assistant URL: `http://HOME_ASSISTANT_HOST:8123`
- Home network jump host alias: `JUMP_HOST_ALIAS`
- NAS SSH alias: `NAS_SSH_ALIAS`
- Router SSH alias or host: `ROUTER_HOST`
- PVE/Proxmox SSH alias or host: `PVE_HOST`
- Remote server SSH aliases: `US_SERVER_ALIAS`, `CLOUD_SERVER_ALIAS`

For Home Assistant or smart-home state queries:
- Treat Home Assistant as the source of truth for device state.
- Read only the user's own device state exposed by Home Assistant.
- It is acceptable to report lights, switches, doors, temperature/humidity,
  power, motion, human-presence, and occupancy sensor states.
- For human-presence, motion, occupancy, and room sensors, report only the
  entity state, friendly name, timestamps, and attributes that Home Assistant
  exposes.
- Do not access cameras, microphones, audio feeds, or third-party/private
  surveillance data.
