# Feishu Setup Checklist

## Create the App

1. Open the Feishu/Lark developer console.
2. Create a custom app for this bridge.
3. Enable bot capability.
4. Copy the app ID and app secret into local `.env`.
5. Publish or install the app to the tenant so it can receive messages.

Use a dedicated bot for Codex. Do not reuse another production assistant bot unless that is intentional.

## Required Bot Events

Subscribe to:

- `im.message.receive_v1`

Optional events for richer UX:

- `im.message.reaction.created_v1`
- `im.message.reaction.deleted_v1`
- `card.action.trigger`

The bundled template uses websocket mode, which avoids exposing a public HTTP callback URL. If using webhook mode in a custom implementation, configure and verify the event callback URL, verification token, and encrypt key.

## Required Permissions

Minimum practical permissions vary by tenant policy, but these are commonly needed:

- receive messages sent to the bot
- send messages as the bot
- read message content
- obtain basic bot information

For group chats, add the bot to the group and either mention it or disable mention requirement with:

```text
CODEX_FEISHU_REQUIRE_MENTION=false
```

For direct messages, users must be able to open a bot DM and send messages.

## IDs

Common Feishu IDs:

- `cli_...`: app ID
- `oc_...`: chat ID
- `ou_...`: open ID

Use placeholders in docs and public repos. Keep real IDs in local `.env`, `tasks.json`, or private deployment notes.

## Local Install Summary

From the skill folder:

```bash
scripts/install_codex_feishu_bridge.py \
  --app-id FEISHU_APP_ID \
  --app-secret FEISHU_APP_SECRET \
  --notify-chat-id FEISHU_NOTIFY_CHAT_ID \
  --install-launch-agent
```

Then reload on macOS:

```bash
launchctl bootout gui/$(id -u) "$HOME/Library/LaunchAgents/com.codex.feishu.plist" 2>/dev/null || true
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.codex.feishu.plist"
```

Verify:

```bash
scripts/verify_codex_feishu_bridge.py --home "$HOME/.codex-feishu"
```

## Troubleshooting

If the bot does not receive messages:

- Confirm the app is installed to the tenant.
- Confirm `im.message.receive_v1` is subscribed.
- Confirm websocket mode is enabled for the app.
- Confirm no second bridge process is using the same app credentials.

If Codex does not answer:

- Run `codex doctor`.
- Check `CODEX_HOME`.
- Check that `codex exec --skip-git-repo-check "hello"` works locally.
- Check `$HOME/.codex-feishu/logs/bridge.log`.

If scheduled tasks are not visible in Codex:

- Check `$CODEX_HOME/automations`.
- Check `CODEX_FEISHU_AUTOMATIONS=true`.
- Check `tasks.json` and the bridge log for task creation.
