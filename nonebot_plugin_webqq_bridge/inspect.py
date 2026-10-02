from __future__ import annotations

import os
import re

from nonebot import on_regex
from nonebot.adapters import Event

def _command() -> str:
    return os.environ.get("WEBQQ_BRIDGE_INSPECT_COMMAND", "inspect").strip() or "inspect"


_enabled = os.environ.get("WEBQQ_BRIDGE_INSPECT_ENABLED", "true").lower() not in {"0", "false", "no"}
_pattern = r"^/?" + re.escape(_command()) + r"(?:\s+@[\s\S]+)?\s*$" if _enabled else r"(?!)"
inspect = on_regex(_pattern, priority=1, block=True)


@inspect.handle()
async def handle_inspect(event: Event) -> None:
    text = event.get_plaintext().strip()
    author = getattr(event, "author", None)
    group_openid = getattr(event, "group_openid", None)
    if group_openid:
        member_openid = getattr(author, "member_openid", None) or event.get_user_id()
        lines = [f"Group OpenID: {group_openid}", f"Sender member OpenID: {member_openid}"]
        if "@" in text:
            for segment in event.get_message():
                if segment.type == "mention_user" and not segment.data.get("is_bot"):
                    lines.append(f"Mentioned member OpenID: {segment.data.get('user_id', '')}")
                    break
        await inspect.finish("\n".join(lines))
    user_openid = getattr(author, "user_openid", None) or getattr(event, "user_openid", None)
    if user_openid:
        await inspect.finish(f"User OpenID: {user_openid}")
    await inspect.finish("/inspect can only be used in an official group or C2C chat")
