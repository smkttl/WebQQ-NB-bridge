# NoneBot WebQQ Bridge

This plugin exposes a restricted OneBot v11-compatible WebSocket server from a
NoneBot official QQ bot. WebQQ connects using its existing `ws_url` and
`napcat_token` settings. The bridge is standalone; OpenID private-chat and
textual mention support additionally require the corresponding WebQQ client
parser changes described below.

The bridge only mirrors configured official groups. Group OpenIDs are mapped to
WebQQ's existing numeric group chat IDs. User identities are always kept as
official QQ OpenIDs; C2C chats use `private_<openid>` and need no mapping.

## Install

```bash
pip install git+https://github.com/smkttl/WebQQ-NB-bridge.git
```

Add `nonebot_plugin_webqq_bridge` to the NoneBot plugin list. The official QQ
adapter must be configured separately.

## Configuration

```dotenv
WEBQQ_BRIDGE_HOST=127.0.0.1
WEBQQ_BRIDGE_PORT=49342
WEBQQ_BRIDGE_TOKEN=replace-with-a-random-secret
WEBQQ_BRIDGE_BOT_ID=official-bot-app-id
WEBQQ_BRIDGE_MAPPING_FILE=/absolute/path/webqq-bridge.json
WEBQQ_BRIDGE_ALLOWED_FILE_ROOTS=["/tmp"]
```

Example mapping file:

```json
{
  "groups": {"GROUP_OPENID": "123456789"},
  "labels": {"group_123456789": "Example group"}
}
```

In an official group, send `/inspect` to obtain the group and sender OpenIDs.
`/inspect @member` prints a mentioned member's OpenID. User IDs in events and
mentions remain OpenIDs; the bridge does not attempt to derive numeric QQ IDs.

Point WebQQ at the bridge:

```json
{
  "ws_url": "ws://127.0.0.1:49342/?message_post_format=array",
  "napcat_token": "the-same-bridge-token"
}
```

Version 0.1 supports text, images, OpenID mentions, replies, configured groups,
all C2C conversations, and mirroring of successful text/image responses made by
other NoneBot plugins. NapCat-only actions return explicit unsupported-action
errors.

The official QQ adapter strips a self-mention from `get_message()` and sets
`to_me`. The bridge restores that mention as an `at` segment (also for replies
to the bot) so downstream consumers can detect `mentionsSelf`.

The current WebQQ client validates private chat IDs as numeric. To use the
`private_<openid>` form end to end, WebQQ must accept non-numeric private IDs
and pass the suffix through to the bridge's `send_private_msg` calls.
The bridge accepts structured `at` segments containing an OpenID. WebQQ's
current text shorthand parser only recognizes numeric `@[id]` mentions, so
typing an `@[openid]` shorthand requires the same WebQQ-side parser update.
