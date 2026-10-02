from __future__ import annotations

import asyncio
import hmac
import json
from pathlib import Path
from typing import Any

from nonebot import get_driver
from nonebot.adapters import Bot, Event
from nonebot.internal.adapter.bot import Bot as BaseBot
from nonebot.message import event_preprocessor

from .config import BridgeConfig
from .mapping import MappingConfig
from .protocol import OneBotBridge, bridge_send_context, synthetic_id
from .storage import BridgeStorage


class BridgeService:
    def __init__(self) -> None:
        self.config: BridgeConfig | None = None
        self.mapping: MappingConfig | None = None
        self.storage: BridgeStorage | None = None
        self.bridge: OneBotBridge | None = None
        self.server: Any = None
        self.server_task: asyncio.Task[Any] | None = None
        self.mapping_task: asyncio.Task[Any] | None = None
        self.mapping_mtime: int | None = None
        self._registered = False

    def register_hooks(self) -> None:
        if self._registered:
            return
        self._registered = True
        driver = get_driver()
        driver.on_startup(self.startup)
        driver.on_shutdown(self.shutdown)
        event_preprocessor(self._event_preprocessor)
        BaseBot.on_called_api(self._called_api)

    async def startup(self) -> None:
        self.config = BridgeConfig.from_env()
        self.mapping = self._load_mapping(self.config.mapping_file)
        try:
            self.mapping_mtime = self.config.mapping_file.stat().st_mtime_ns
        except OSError:
            self.mapping_mtime = None
        self.storage = BridgeStorage(self.config.state_file)
        self.bridge = OneBotBridge(self.config, self.mapping, self.storage)
        import websockets

        self.server = await websockets.serve(
            self._websocket_handler,
            self.config.host,
            self.config.port,
            max_size=8 * 1024 * 1024,
        )
        self.mapping_task = asyncio.create_task(self._watch_mapping())

    async def shutdown(self) -> None:
        if self.mapping_task is not None:
            self.mapping_task.cancel()
            await asyncio.gather(self.mapping_task, return_exceptions=True)
            self.mapping_task = None
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        if self.storage is not None:
            self.storage.close()
            self.storage = None

    async def _watch_mapping(self) -> None:
        while True:
            await asyncio.sleep(2)
            if self.config is None or not self.config.mapping_file.exists():
                continue
            try:
                mtime = self.config.mapping_file.stat().st_mtime_ns
            except OSError:
                continue
            if mtime == self.mapping_mtime:
                continue
            try:
                await self.reload_mapping()
                self.mapping_mtime = mtime
            except Exception as exc:
                print(f"[webqq-bridge] mapping reload failed: {exc}")

    @staticmethod
    def _load_mapping(path: Path) -> MappingConfig:
        if not path.exists():
            return MappingConfig(groups={}, labels={})
        return MappingConfig.load(path)

    async def reload_mapping(self) -> MappingConfig:
        if self.config is None or self.bridge is None:
            raise RuntimeError("bridge is not started")
        mapping = self._load_mapping(self.config.mapping_file)
        self.mapping = mapping
        self.bridge.set_mapping(mapping)
        try:
            self.mapping_mtime = self.config.mapping_file.stat().st_mtime_ns
        except OSError:
            self.mapping_mtime = None
        if self.bridge.websocket is not None:
            await self.bridge.websocket.close(code=4002, reason="mapping reloaded")
        return mapping

    @staticmethod
    def _headers(websocket: Any) -> Any:
        request = getattr(websocket, "request", None)
        if request is not None and hasattr(request, "headers"):
            return request.headers
        return getattr(websocket, "request_headers", {})

    async def _websocket_handler(self, websocket: Any, *_args: Any) -> None:
        if self.config is None or self.bridge is None:
            await websocket.close(code=1011, reason="bridge is not initialized")
            return
        headers = self._headers(websocket)
        authorization = str(headers.get("Authorization", ""))
        supplied = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        if not hmac.compare_digest(supplied, self.config.token):
            await websocket.close(code=4003, reason="unauthorized")
            return
        await self.bridge.attach(websocket)
        try:
            async for raw in websocket:
                try:
                    request = json.loads(raw)
                    if not isinstance(request, dict):
                        raise ValueError("request must be an object")
                    response = await self.bridge.handle_request(request)
                except Exception as exc:
                    response = {"status": "failed", "retcode": 1400, "message": str(exc),
                                "wording": str(exc), "data": None}
                    if isinstance(raw, str):
                        try:
                            echo = json.loads(raw).get("echo")
                            if echo is not None:
                                response["echo"] = echo
                        except Exception:
                            pass
                await websocket.send(json.dumps(response, ensure_ascii=False))
        finally:
            await self.bridge.detach(websocket)

    async def _event_preprocessor(self, bot: Bot, event: Event) -> None:
        if self.bridge is None:
            return
        payload = self.bridge.normalize_event(bot, event)
        if payload is not None:
            await self.bridge.emit(payload)

    async def _called_api(self, bot: BaseBot, exception: Exception | None, api: str,
                          data: dict[str, Any], result: Any) -> None:
        if exception is not None or self.bridge is None or bridge_send_context.get():
            return
        if api not in {"post_group_messages", "post_c2c_messages"}:
            return
        if self.config and self.config.bot_id and str(getattr(bot, "self_id", "")) != self.config.bot_id:
            return
        self.bridge.register_bot(bot)
        if api == "post_group_messages":
            openid = str(data.get("group_openid", ""))
            numeric = self.bridge.mapping.group_id(openid)
            kind = "group"
        else:
            openid = str(data.get("openid", ""))
            numeric = openid
            kind = "private"
        if not numeric or not openid:
            return
        official_id = str(getattr(result, "id", "") or getattr(result, "message_id", "") or "")
        if not official_id:
            return
        synthetic = synthetic_id(self.config.bot_id, kind, openid, official_id)
        if self.storage and self.storage.by_official(self.config.bot_id, official_id):
            return
        content = str(data.get("content") or "")
        segments: list[dict[str, Any]] = []
        if content:
            segments.append({"type": "text", "data": {"text": content}})
        media = data.get("media")
        media_url = getattr(media, "url", None) or (media.get("url") if isinstance(media, dict) else None)
        if media_url:
            segments.append({"type": "image", "data": {"url": str(media_url)}})
        if self.storage:
            self.storage.remember_message(synthetic, official_id, self.config.bot_id, kind, openid,
                                          msg_idx=str(data.get("msg_seq") or ""))
        await self.bridge.emit({
            "post_type": "message_sent", "message_type": kind, "sub_type": "normal",
            "time": int(__import__("time").time()), "self_id": self.config.bot_id,
            "message_id": int(synthetic), "message_sent_type": "self",
            "group_id": int(numeric) if kind == "group" else None,
            "target_id": int(numeric) if kind == "group" else numeric,
            "user_id": int(numeric) if kind == "group" else numeric,
            "sender": {"user_id": int(numeric) if kind == "group" else numeric,
                        "nickname": "Official Bot"},
            "message": segments,
        })
