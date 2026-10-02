from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from nonebot_plugin_webqq_bridge.config import BridgeConfig
from nonebot_plugin_webqq_bridge.mapping import MappingConfig
from nonebot_plugin_webqq_bridge.protocol import OneBotBridge, synthetic_id
from nonebot_plugin_webqq_bridge.service import BridgeService
from nonebot_plugin_webqq_bridge.storage import BridgeStorage


def test_mapping_validation_and_reverse_lookup() -> None:
    mapping = MappingConfig.from_dict({
        "groups": {"g": 123},
        "labels": {"group_123": "Group"},
    })
    assert mapping.group_id("g") == "123"
    assert mapping.group_openid("123") == "g"
    assert mapping.label("group_123", "fallback") == "Group"


def test_mapping_rejects_duplicate_numeric_values() -> None:
    with pytest.raises(ValueError, match="maps 1 more than once"):
        MappingConfig.from_dict({"groups": {"a": 1, "b": "1"}})


def test_synthetic_ids_are_decimal_and_stable() -> None:
    first = synthetic_id("bot", "group", "openid", "message")
    assert first == synthetic_id("bot", "group", "openid", "message")
    assert first.isdigit()
    assert first != synthetic_id("bot", "group", "openid", "other")


def test_storage_round_trip(tmp_path: Path) -> None:
    storage = BridgeStorage(tmp_path / "state.sqlite3")
    storage.remember_message("100", "official", "bot", "group", "g", msg_idx="4")
    assert storage.by_synthetic("100")["official_id"] == "official"
    storage.queue({"post_type": "message", "message_id": 100})
    assert storage.queued()[0][1]["message_id"] == 100
    storage.close()


def make_bridge(tmp_path: Path) -> OneBotBridge:
    config = BridgeConfig(token="secret", bot_id="bot", state_file=tmp_path / "state.sqlite3")
    mapping = MappingConfig.from_dict({
        "groups": {"group-openid": "123"},
        "labels": {},
    })
    return OneBotBridge(config, mapping, BridgeStorage(config.state_file))


def test_onebot_discovery_actions(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path)
    groups = asyncio.run(bridge.handle_request({"action": "get_group_list", "echo": 1}))
    friends = asyncio.run(bridge.handle_request({"action": "get_friend_list", "echo": 2}))
    assert groups["status"] == friends["status"] == "ok"
    assert groups["data"][0]["group_id"] == 123
    assert friends["data"] == []
    assert groups["echo"] == 1
    unsupported = asyncio.run(bridge.handle_request({"action": "delete_msg", "echo": 3}))
    assert unsupported["status"] == "failed"
    assert unsupported["echo"] == 3


def test_outgoing_group_send_uses_reverse_mapping(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path)

    class Result:
        id = "official-outgoing-1"

    class FakeBot:
        async def send_to_group(self, openid, message, **kwargs):
            assert openid == "group-openid"
            assert message.extract_plain_text() == "hello"
            assert kwargs["msg_seq"] == 1
            return Result()

    bridge.bot = FakeBot()
    response = asyncio.run(bridge.handle_request({
        "action": "send_group_msg",
        "params": {"group_id": 123, "message": "hello"},
        "echo": "send",
    }))
    assert response["status"] == "ok"
    assert response["data"]["message_id"] > 0


class FakePart:
    def __init__(self, part_type: str, data: dict[str, object], text: str = "") -> None:
        self.type = part_type
        self.data = data
        self._text = text

    def __str__(self) -> str:
        return self._text or str(self.data.get("text", ""))


class FakeGroupEvent:
    id = "official-message-1"
    event_id = "event-1"
    group_openid = "group-openid"
    author = SimpleNamespace(member_openid="member-openid", username="Alice")

    def get_message(self):
        return [FakePart("text", {"text": "hello"}, "hello")]

    def get_plaintext(self):
        return "hello"


def test_group_event_normalizes_to_existing_webqq_shape(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path)
    bot = SimpleNamespace(self_id="bot")
    payload = bridge.normalize_event(bot, FakeGroupEvent())
    assert payload["message_type"] == "group"
    assert payload["group_id"] == 123
    assert payload["user_id"] == "member-openid"
    assert payload["message"][0]["data"]["text"] == "hello"
    assert str(payload["message_id"]).isdigit()
    assert bridge.normalize_event(bot, FakeGroupEvent()) is None


def test_mapping_file_can_be_loaded(tmp_path: Path) -> None:
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps({"groups": {"g": "1"}}), encoding="utf-8")
    loaded = MappingConfig.load(path)
    assert loaded.group_id("g") == "1"


class FakeC2CEvent:
    id = "official-c2c-message-1"
    event_id = "event-c2c-1"
    author = SimpleNamespace(user_openid="user-openid", username="Alice")

    def get_message(self):
        return [FakePart("text", {"text": "hello"}, "hello")]

    def get_plaintext(self):
        return "hello"


def test_c2c_event_uses_openid_without_a_user_mapping(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path)
    payload = bridge.normalize_event(SimpleNamespace(self_id="bot"), FakeC2CEvent())
    assert payload["message_type"] == "private"
    assert payload["user_id"] == "user-openid"
    assert payload["sender"]["user_id"] == "user-openid"
    assert payload["group_id"] is None


def test_private_send_and_mentions_use_openids(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path)
    captured = {}

    class Result:
        id = "official-private-outgoing-1"

    class FakeBot:
        async def send_to_c2c(self, openid, message, **kwargs):
            captured["openid"] = openid
            captured["message"] = message
            return Result()

    bridge.bot = FakeBot()
    response = asyncio.run(bridge.handle_request({
        "action": "send_private_msg",
        "params": {"user_id": "user-openid", "message": "hello"},
    }))
    assert response["status"] == "ok"
    assert captured["openid"] == "user-openid"

    class GroupBot:
        async def send_to_group(self, openid, message, **kwargs):
            captured["group_openid"] = openid
            captured["message"] = message
            return Result()

    bridge.bot = GroupBot()
    asyncio.run(bridge.handle_request({
        "action": "send_group_msg",
        "params": {"group_id": 123, "message": [{"type": "at", "data": {"qq": "member-openid"}}]},
    }))
    assert captured["group_openid"] == "group-openid"
    assert captured["message"][0].type == "mention_user"
    assert captured["message"][0].data["user_id"] == "member-openid"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _start_live_bridge(tmp_path: Path):
    websockets = pytest.importorskip("websockets")
    config = BridgeConfig(
        host="127.0.0.1",
        port=_free_port(),
        token="secret-token",
        bot_id="bot",
        mapping_file=tmp_path / "mapping.json",
        state_file=tmp_path / "state.sqlite3",
    )
    config.mapping_file.write_text(json.dumps({"groups": {"group-openid": 123}}), encoding="utf-8")
    service = BridgeService()
    service.config = config
    service.mapping = MappingConfig.load(config.mapping_file)
    service.storage = BridgeStorage(config.state_file)
    service.bridge = OneBotBridge(config, service.mapping, service.storage)
    server = await websockets.serve(service._websocket_handler, config.host, config.port)
    return service, server


def test_live_loopback_round_trip(tmp_path: Path) -> None:
    asyncio.run(_live_loopback_round_trip(tmp_path))


async def _live_loopback_round_trip(tmp_path: Path) -> None:
    websockets = pytest.importorskip("websockets")
    service, server = await _start_live_bridge(tmp_path)
    config = service.config
    bridge = service.bridge
    url = f"ws://{config.host}:{config.port}/"

    captured: dict[str, object] = {}

    class Result:
        id = "official-outgoing-1"

    class FakeBot:
        async def send_to_c2c(self, openid, message, **kwargs):
            captured["c2c"] = (openid, message)
            return Result()

        async def send_to_group(self, openid, message, **kwargs):
            captured["group"] = (openid, message)
            return Result()

    async def request(client, payload):
        await client.send(json.dumps(payload))
        while True:
            message = json.loads(await asyncio.wait_for(client.recv(), timeout=5))
            if message.get("echo") == payload["echo"]:
                return message

    bridge.bot = FakeBot()
    try:
        async with websockets.connect(url) as bogus:
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await asyncio.wait_for(bogus.recv(), timeout=5)

        async with websockets.connect(
            url, additional_headers={"Authorization": "Bearer secret-token"}
        ) as client:
            await bridge.emit({"post_type": "message", "message_type": "group",
                               "group_id": 123, "user_id": "member-openid",
                               "message": [{"type": "text", "data": {"text": "hi"}}]})
            group_event = json.loads(await asyncio.wait_for(client.recv(), timeout=5))
            assert group_event["group_id"] == 123
            assert group_event["user_id"] == "member-openid"

            await bridge.emit({"post_type": "message", "message_type": "private",
                               "user_id": "user-openid",
                               "message": [{"type": "text", "data": {"text": "dm"}}]})
            c2c_event = json.loads(await asyncio.wait_for(client.recv(), timeout=5))
            assert c2c_event["message_type"] == "private"
            assert c2c_event["user_id"] == "user-openid"

            response = await request(client, {
                "action": "send_private_msg", "echo": "e1",
                "params": {"user_id": "user-openid", "message": "reply"},
            })
            assert response["status"] == "ok"
            assert response["echo"] == "e1"
            assert captured["c2c"][0] == "user-openid"

            response = await request(client, {
                "action": "send_group_msg", "echo": "e2",
                "params": {"group_id": 123,
                           "message": [{"type": "at", "data": {"qq": "member-openid"}},
                                       {"type": "text", "data": {"text": " ping"}}]},
            })
            assert response["status"] == "ok"
            assert captured["group"][0] == "group-openid"
            assert captured["group"][1][0].type == "mention_user"
            assert captured["group"][1][0].data["user_id"] == "member-openid"
    finally:
        server.close()
        await server.wait_closed()
        service.storage.close()
