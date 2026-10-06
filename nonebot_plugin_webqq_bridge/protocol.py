from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from .config import BridgeConfig
from .mapping import MappingConfig
from .storage import BridgeStorage

bridge_send_context: ContextVar[bool] = ContextVar("webqq_bridge_send", default=False)


def ok(data: Any, echo: Any = None) -> dict[str, Any]:
    result = {"status": "ok", "retcode": 0, "data": data}
    if echo is not None:
        result["echo"] = echo
    return result


def failed(wording: str, echo: Any = None, retcode: int = 1404) -> dict[str, Any]:
    result = {"status": "failed", "retcode": retcode, "message": wording,
              "wording": wording, "data": None}
    if echo is not None:
        result["echo"] = echo
    return result


def synthetic_id(bot_id: str, kind: str, openid: str, official_id: str) -> str:
    digest = hashlib.sha256(f"webqq:{bot_id}:{kind}:{openid}:{official_id}".encode()).digest()
    return str(int.from_bytes(digest[:10], "big"))


def _safe_image_path(raw: str, roots: list[Path]) -> Path:
    if not isinstance(raw, str) or not raw.startswith("file://"):
        raise ValueError("official QQ images must use a local file:// path")
    from urllib.parse import unquote, urlparse
    path = Path(unquote(urlparse(raw).path)).resolve()
    if path.is_symlink() or not path.is_file() or not any(path.is_relative_to(root) for root in roots):
        raise ValueError("image path is outside the bridge allowed roots")
    if path.stat().st_size > 100 * 1024 * 1024:
        raise ValueError("image is larger than 100 MB")
    content_type = mimetypes.guess_type(path.name)[0] or ""
    if not content_type.startswith("image/"):
        raise ValueError("file is not an image")
    return path


def _segment_value(segment: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(segment, dict) or not isinstance(segment.get("data", {}), dict):
        raise ValueError("invalid message segment")
    return str(segment.get("type", "")), segment["data"]


def _event_self_mentions(event: Any) -> bool:
    """True when the raw event's mention list contains a single self-mention."""
    for mention in getattr(event, "mentions", None) or []:
        if getattr(mention, "scope", None) != "single":
            continue
        if getattr(mention, "is_you", False):
            return True
    return False


def _has_at_segment(message: list[dict[str, Any]], bot_id: str) -> bool:
    return any(
        segment.get("type") == "at" and str(segment.get("data", {}).get("qq", "")) == bot_id
        for segment in message
        if isinstance(segment, dict)
    )


_ATTACHMENT_SEGMENT_TYPES = {"audio": "record", "video": "video", "file": "file"}


def _attachment_segment(part: Any) -> dict[str, Any] | None:
    """Map an adapter attachment segment to a WebQQ segment, or None if it has no URL."""
    url = str(part.data.get("url", "") or "").strip()
    if not url:
        return None
    segment_type = _ATTACHMENT_SEGMENT_TYPES.get(str(part.type))
    if segment_type is None:
        return None
    return {"type": segment_type, "data": {"url": url, "file": url}}


class OneBotBridge:
    def __init__(self, config: BridgeConfig, mapping: MappingConfig, storage: BridgeStorage):
        self.config = config
        self.mapping = mapping
        self.storage = storage
        self.websocket: Any = None
        self.bot: Any = None
        self._send_lock = asyncio.Lock()
        self._seq: dict[tuple[str, str], int] = {}

    def set_mapping(self, mapping: MappingConfig) -> None:
        self.mapping = mapping

    def register_bot(self, bot: Any) -> None:
        if not self.config.bot_id or str(getattr(bot, "self_id", "")) == self.config.bot_id:
            self.bot = bot

    async def attach(self, websocket: Any) -> None:
        old = self.websocket
        self.websocket = websocket
        if old is not None and old is not websocket:
            await old.close(code=4001, reason="replaced by a new WebQQ connection")
        await self.replay_queue()

    async def detach(self, websocket: Any) -> None:
        if self.websocket is websocket:
            self.websocket = None

    async def emit(self, event: dict[str, Any]) -> None:
        websocket = self.websocket
        if websocket is None:
            self.storage.queue(event)
            return
        try:
            async with self._send_lock:
                await websocket.send(json.dumps(event, ensure_ascii=False))
        except Exception:
            if self.websocket is websocket:
                self.websocket = None
            self.storage.queue(event)

    async def replay_queue(self) -> None:
        if self.websocket is None:
            return
        for row_id, payload in self.storage.queued():
            try:
                async with self._send_lock:
                    await self.websocket.send(json.dumps(payload, ensure_ascii=False))
            except Exception:
                return
            self.storage.remove_queue(row_id)

    def _conversation(self, chat_id: str) -> tuple[str, str, str] | None:
        if chat_id.startswith("group_") and chat_id[6:].isdigit():
            gid = chat_id[6:]
            return "group", self.mapping.group_openid(gid) or "", gid
        if chat_id.startswith("private_") and chat_id[8:]:
            openid = chat_id[8:]
            return "c2c", openid, openid
        return None

    async def handle_request(self, request: dict[str, Any]) -> dict[str, Any]:
        echo = request.get("echo")
        action = str(request.get("action", ""))
        params = request.get("params") if isinstance(request.get("params"), dict) else {}
        if action == "get_login_info":
            return ok({"user_id": int(self.config.bot_id) if self.config.bot_id.isdigit() else self.config.bot_id,
                       "nickname": "Official QQ Bot"}, echo)
        if action == "get_group_list":
            return ok([{"group_id": int(value), "group_name": self.mapping.label(f"group_{value}", key)}
                       for key, value in self.mapping.groups.items()], echo)
        if action == "get_friend_list":
            return ok([], echo)
        if action == "get_group_member_list":
            gid = str(params.get("group_id", ""))
            if not self.mapping.group_openid(gid):
                return ok([], echo)
            return ok([], echo)
        if action in {"get_group_system_msg", "get_doubt_friends_add_request"}:
            return ok([], echo)
        if action in {"mark_group_msg_as_read", "mark_private_msg_as_read"}:
            return ok({}, echo)
        if action in {"send_group_msg", "send_private_msg"}:
            try:
                result = await self._send(action, params)
                return ok(result, echo)
            except Exception as exc:
                return failed(str(exc), echo, retcode=1400)
        if action == "get_image":
            return failed("official image URLs are already included in message events", echo)
        return failed(f"unsupported action: {action}", echo)

    async def _send(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.bot is None:
            raise RuntimeError("official QQ bot is not connected")
        kind = "group" if action == "send_group_msg" else "c2c"
        numeric = str(params.get("group_id" if kind == "group" else "user_id", ""))
        openid = self.mapping.group_openid(numeric) if kind == "group" else numeric
        if not openid:
            raise ValueError("conversation is not configured")
        message = params.get("message", "")
        if isinstance(message, str):
            segments = [{"type": "text", "data": {"text": message}}]
        elif isinstance(message, list):
            segments = message
        else:
            raise ValueError("message must be text or an array")
        from nonebot.adapters.qq import Message, MessageSegment
        outgoing = Message()
        reply_id = None
        for segment in segments:
            segment_type, data = _segment_value(segment)
            if segment_type == "text":
                outgoing += MessageSegment.text(str(data.get("text", "")))
            elif segment_type == "at":
                openid_member = str(data.get("qq", "")).strip()
                if not openid_member:
                    raise ValueError("mentioned user OpenID is required")
                outgoing += MessageSegment.mention_user(openid_member)
            elif segment_type == "reply":
                reply_id = str(data.get("id", ""))
            elif segment_type == "image":
                path = _safe_image_path(str(data.get("file", data.get("url", ""))), self.config.allowed_file_roots)
                outgoing += MessageSegment.file_image(path)
            else:
                raise ValueError(f"unsupported outgoing segment: {segment_type}")
        ref = self.storage.by_synthetic(reply_id) if reply_id else None
        token = bridge_send_context.set(True)
        try:
            kwargs: dict[str, Any] = {}
            if ref:
                kwargs.update(msg_id=ref["official_id"], msg_ref_id=ref["msg_idx"] or None)
            key = (kind, openid)
            self._seq[key] = self._seq.get(key, 0) + 1
            kwargs["msg_seq"] = self._seq[key]
            if kind == "group":
                result = await self.bot.send_to_group(openid, outgoing, **kwargs)
            else:
                result = await self.bot.send_to_c2c(openid, outgoing, **kwargs)
        finally:
            bridge_send_context.reset(token)
        official_id = str(getattr(result, "id", "") or getattr(result, "message_id", "") or int(time.time() * 1000))
        synthetic = synthetic_id(self.config.bot_id, kind, openid, official_id)
        self.storage.remember_message(synthetic, official_id, self.config.bot_id, kind, openid)
        target_id: int | str = int(numeric) if kind == "group" else openid
        await self.emit({"post_type": "message_sent", "message_type": "group" if kind == "group" else "private",
                         "message_id": int(synthetic), "time": int(time.time()),
                         "message_sent_type": "self", "group_id": int(numeric) if kind == "group" else None,
                         "target_id": target_id, "user_id": target_id,
                         "sender": {"user_id": target_id, "nickname": "You"},
                         "message": segments})
        return {"message_id": int(synthetic)}

    def normalize_event(self, bot: Any, event: Any) -> dict[str, Any] | None:
        self.register_bot(bot)
        name = type(event).__name__
        is_group = name in {"GroupMessageCreateEvent", "GroupAtMessageCreateEvent"} or hasattr(event, "group_openid")
        is_c2c = name == "C2CMessageCreateEvent" or hasattr(getattr(event, "author", None), "user_openid")
        if not (is_group or is_c2c):
            return None
        if is_group:
            conversation_openid = str(getattr(event, "group_openid", ""))
            numeric = self.mapping.group_id(conversation_openid)
            if not numeric:
                return None
            kind, chat_id = "group", f"group_{numeric}"
            user_openid = str(getattr(getattr(event, "author", None), "member_openid", ""))
            user_id = user_openid
            group_id = int(numeric)
            user_name = getattr(getattr(event, "author", None), "username", None) or user_openid
        else:
            user_openid = str(getattr(getattr(event, "author", None), "user_openid", "") or getattr(event, "user_openid", ""))
            if not user_openid:
                return None
            conversation_openid = user_openid
            kind, chat_id, user_id, group_id = "private", f"private_{user_openid}", user_openid, None
            user_name = getattr(getattr(event, "author", None), "username", None) or user_openid
        official_id = str(getattr(event, "id", ""))
        synthetic = synthetic_id(self.config.bot_id, kind, conversation_openid, official_id)
        if self.storage.by_official(self.config.bot_id, official_id):
            return None
        message: list[dict[str, Any]] = []
        try:
            nb_message = event.get_message()
            for part in nb_message:
                if part.type == "text":
                    message.append({"type": "text", "data": {"text": part.data.get("text", "")}})
                elif part.type == "image":
                    message.append({"type": "image", "data": {"url": part.data.get("url", "")}})
                elif part.type == "mention_user":
                    mentioned = str(part.data.get("user_id", ""))
                    message.append({"type": "at", "data": {"qq": mentioned, "name": part.data.get("username", "")}})
                elif part.type in _ATTACHMENT_SEGMENT_TYPES:
                    attachment = _attachment_segment(part)
                    if attachment is not None:
                        message.append(attachment)
                else:
                    message.append({"type": "text", "data": {"text": str(part)}})
        except Exception:
            message = [{"type": "text", "data": {"text": event.get_plaintext()}}]
        # The QQ adapter strips a self-`at` from `event.get_message()` (setting
        # `to_me`), and a reply to the bot also sets `to_me`. Reconstruct an
        # explicit self-mention so downstream `mentionsSelf` detection works;
        # guard against duplicating a self-`at` the adapter already kept.
        if kind == "group":
            bot_id = str(self.config.bot_id or "")
            if not bot_id:
                try:
                    bot_id = str(getattr(bot.self_info, "id", "") or "")
                except Exception:
                    bot_id = ""
            if bot_id and not _has_at_segment(message, bot_id):
                if getattr(event, "to_me", False) or _event_self_mentions(event):
                    message.insert(0, {"type": "at", "data": {"qq": bot_id}})
        now = int(time.time())
        self.storage.remember_message(synthetic, official_id, self.config.bot_id, kind, conversation_openid,
                                      event_id=str(getattr(event, "event_id", "") or ""), timestamp=now)
        return {"post_type": "message", "message_type": kind, "sub_type": "normal",
                "time": now, "self_id": int(self.config.bot_id) if self.config.bot_id.isdigit() else self.config.bot_id,
                "message_id": int(synthetic), "user_id": int(user_id) if str(user_id).isdigit() else user_id,
                "group_id": group_id, "group_name": self.mapping.label(chat_id, conversation_openid) if group_id else "",
                "sender": {"user_id": int(user_id) if str(user_id).isdigit() else user_id,
                           "nickname": user_name, "card": user_name, "role": "member"},
                "message": message, "raw_message": event.get_plaintext(), "font": {},
                "_bridge_official_id": official_id}
