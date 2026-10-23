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
            assert [seg.type for seg in message] == ["markdown"]
            assert message["markdown"][0].data["markdown"].content == "hello"
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


def _capture_group_send(bridge, message):
    captured = {}

    class Result:
        id = "official-markdown-outgoing"

    class FakeBot:
        async def send_to_group(self, openid, outgoing, **kwargs):
            captured["openid"] = openid
            captured["message"] = outgoing
            captured["kwargs"] = kwargs
            return Result()

    bridge.bot = FakeBot()
    response = asyncio.run(bridge.handle_request({
        "action": "send_group_msg",
        "params": {"group_id": 123, "message": message},
    }))
    assert response["status"] == "ok"
    return captured


def test_plain_text_is_sent_as_markdown(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path)
    captured = _capture_group_send(bridge, "**bold** and `code`")

    outgoing = captured["message"]
    assert [seg.type for seg in outgoing] == ["markdown"]
    markdown = outgoing["markdown"][0].data["markdown"]
    assert markdown.content == "**bold** and `code`"
    assert markdown.template_id is None
    assert markdown.custom_template_id is None


def test_text_parts_are_joined_into_one_markdown_segment(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path)
    captured = _capture_group_send(bridge, [
        {"type": "text", "data": {"text": "first "}},
        {"type": "text", "data": {"text": "second"}},
    ])

    outgoing = captured["message"]
    assert [seg.type for seg in outgoing] == ["markdown"]
    assert outgoing["markdown"][0].data["markdown"].content == "first second"


def test_markdown_and_mention_coexist_as_parallel_segments(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path)
    captured = _capture_group_send(bridge, [
        {"type": "text", "data": {"text": "hi "}},
        {"type": "at", "data": {"qq": "member-openid"}},
    ])

    outgoing = captured["message"]
    assert [seg.type for seg in outgoing] == ["markdown", "mention_user"]
    assert outgoing["markdown"][0].data["markdown"].content == "hi "
    assert outgoing["mention_user"][0].data["user_id"] == "member-openid"


def test_text_with_image_stays_plain_text(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path)
    image_path = tmp_path / "pic.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    bridge.config.allowed_file_roots = [tmp_path]

    captured = _capture_group_send(bridge, [
        {"type": "text", "data": {"text": "look"}},
        {"type": "image", "data": {"file": image_path.as_uri()}},
    ])

    outgoing = captured["message"]
    assert [seg.type for seg in outgoing] == ["text", "file_image"]
    assert outgoing["text"][0].data["text"] == "look"
    assert outgoing["markdown"] == []


def test_image_only_message_is_unchanged(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path)
    image_path = tmp_path / "pic.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    bridge.config.allowed_file_roots = [tmp_path]

    captured = _capture_group_send(bridge, [
        {"type": "image", "data": {"file": image_path.as_uri()}},
    ])

    assert [seg.type for seg in captured["message"]] == ["file_image"]


def test_unsupported_outgoing_segment_still_fails(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path)

    class FakeBot:
        async def send_to_group(self, openid, outgoing, **kwargs):
            raise AssertionError("should not send")

    bridge.bot = FakeBot()
    response = asyncio.run(bridge.handle_request({
        "action": "send_group_msg",
        "params": {"group_id": 123, "message": [{"type": "video", "data": {}}]},
    }))
    assert response["status"] == "failed"
    assert "unsupported outgoing segment" in response["message"]


_QQ_BOT_ID = "1904967699"
_QQ_BOT_NAME = "乌龟bot"


def _qq_mention(member_id: str, *, is_you: bool, scope: str = "single"):
    return SimpleNamespace(
        id=member_id, is_you=is_you, bot=is_you, scope=scope,
        member_openid=f"openid-{member_id}", username=f"user-{member_id}",
    )


class _FakeQQBot:
    class self_info:
        id = _QQ_BOT_ID
        username = _QQ_BOT_NAME


def _qq_group_event(cls, content: str, mentions):
    from nonebot.adapters.qq.event import GroupMessageCreateEvent
    from nonebot.adapters.qq.bot import _check_at_me

    payload = {
        "id": "qq-message-1",
        "content": content,
        "timestamp": "1",
        "author": {"id": "author", "bot": False, "member_openid": "member-openid",
                   "member_role": "member", "username": "Alice"},
        "group_id": "group-1",
        "group_openid": "group-openid",
    }
    if cls is GroupMessageCreateEvent:
        payload["group_id"] = "group-1"
    event = cls(**payload)
    event.mentions = mentions
    _check_at_me(_FakeQQBot(), event)
    return event


def _at_segments(payload: dict) -> list:
    return [seg for seg in payload["message"] if seg.get("type") == "at"]


def test_self_mention_stripped_by_adapter_is_reconstructed(tmp_path: Path) -> None:
    from nonebot.adapters.qq.event import GroupMessageCreateEvent

    bridge = make_bridge(tmp_path)
    bridge.config.bot_id = _QQ_BOT_ID
    event = _qq_group_event(
        GroupMessageCreateEvent,
        f"<@!{_QQ_BOT_ID}> hello there",
        [_qq_mention(_QQ_BOT_ID, is_you=True)],
    )
    payload = bridge.normalize_event(_FakeQQBot(), event)

    ats = _at_segments(payload)
    assert len(ats) == 1
    assert ats[0]["data"]["qq"] == _QQ_BOT_ID
    assert any(seg["type"] == "text" and "hello there" in seg["data"]["text"] for seg in payload["message"])


def test_at_message_event_does_not_duplicate_self_mention(tmp_path: Path) -> None:
    from nonebot.adapters.qq.event import GroupAtMessageCreateEvent

    bridge = make_bridge(tmp_path)
    bridge.config.bot_id = _QQ_BOT_ID
    event = _qq_group_event(
        GroupAtMessageCreateEvent,
        f"<@!{_QQ_BOT_ID}> hi",
        [_qq_mention(_QQ_BOT_ID, is_you=True)],
    )
    payload = bridge.normalize_event(_FakeQQBot(), event)

    ats = _at_segments(payload)
    assert len(ats) == 1
    assert ats[0]["data"]["qq"] == _QQ_BOT_ID


def test_other_user_mention_is_preserved_without_self_mention(tmp_path: Path) -> None:
    from nonebot.adapters.qq.event import GroupMessageCreateEvent

    bridge = make_bridge(tmp_path)
    bridge.config.bot_id = _QQ_BOT_ID
    event = _qq_group_event(
        GroupMessageCreateEvent,
        "<@!999> hi",
        [_qq_mention("999", is_you=False)],
    )
    payload = bridge.normalize_event(_FakeQQBot(), event)

    ats = _at_segments(payload)
    assert [seg["data"]["qq"] for seg in ats] == ["999"]


def test_reply_to_bot_without_self_at_yields_self_mention(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path)
    bridge.config.bot_id = _QQ_BOT_ID
    event = FakeGroupEvent()
    event.to_me = True
    payload = bridge.normalize_event(SimpleNamespace(self_id=_QQ_BOT_ID), event)

    ats = _at_segments(payload)
    assert len(ats) == 1
    assert ats[0]["data"]["qq"] == _QQ_BOT_ID


def test_plain_group_and_c2c_messages_are_unchanged(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path)
    bridge.config.bot_id = _QQ_BOT_ID
    group_payload = bridge.normalize_event(SimpleNamespace(self_id=_QQ_BOT_ID), FakeGroupEvent())
    assert _at_segments(group_payload) == []

    c2c_payload = bridge.normalize_event(SimpleNamespace(self_id=_QQ_BOT_ID), FakeC2CEvent())
    assert _at_segments(c2c_payload) == []
    assert c2c_payload["user_id"] == "user-openid"


def test_all_mention_does_not_become_self_mention(tmp_path: Path) -> None:
    from nonebot.adapters.qq.event import GroupMessageCreateEvent

    bridge = make_bridge(tmp_path)
    bridge.config.bot_id = _QQ_BOT_ID
    event = _qq_group_event(
        GroupMessageCreateEvent,
        "<@!all> hi",
        [SimpleNamespace(id="all", is_you=True, bot=False, scope="all", member_openid="", username="全体成员")],
    )
    # No single self-mention and no reply: nothing should be reconstructed.
    event.to_me = False
    payload = bridge.normalize_event(_FakeQQBot(), event)
    assert _at_segments(payload) == []


def test_self_mention_falls_back_to_connected_bot_info(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path)
    bridge.config.bot_id = ""
    event = FakeGroupEvent()
    event.to_me = True
    payload = bridge.normalize_event(_FakeQQBot(), event)

    ats = _at_segments(payload)
    assert len(ats) == 1
    assert ats[0]["data"]["qq"] == _QQ_BOT_ID


def test_disconnected_bot_without_configured_id_is_safe(tmp_path: Path) -> None:
    class DisconnectedBot:
        @property
        def self_info(self):
            raise RuntimeError("not connected")

    bridge = make_bridge(tmp_path)
    bridge.config.bot_id = ""
    event = FakeGroupEvent()
    event.to_me = True
    payload = bridge.normalize_event(DisconnectedBot(), event)
    assert _at_segments(payload) == []


def _attachment_group_event(attachments):
    from nonebot.adapters.qq.event import GroupMessageCreateEvent

    return GroupMessageCreateEvent(**{
        "id": "qq-attachment-message",
        "content": "here",
        "timestamp": "1",
        "author": {"id": "author", "bot": False, "member_openid": "member-openid",
                   "member_role": "member", "username": "Alice"},
        "group_id": "group-1",
        "group_openid": "group-openid",
        "attachments": attachments,
    })


def _segments_of(payload, segment_type: str) -> list:
    return [seg for seg in payload["message"] if seg.get("type") == segment_type]


def test_incoming_file_attachment_becomes_file_segment(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path)
    event = _attachment_group_event([
        {"content_type": "application/pdf", "url": "https://multimedia.nt.qq.com.cn/f.pdf",
         "filename": "f.pdf", "size": 20},
    ])
    payload = bridge.normalize_event(SimpleNamespace(self_id="bot"), event)

    files = _segments_of(payload, "file")
    assert len(files) == 1
    assert files[0]["data"]["url"] == "https://multimedia.nt.qq.com.cn/f.pdf"
    assert files[0]["data"]["file"] == "https://multimedia.nt.qq.com.cn/f.pdf"
    # Not degraded to a text placeholder.
    assert not any(
        seg["type"] == "text" and "file" in seg["data"]["text"] for seg in payload["message"]
    )


def test_incoming_audio_and_video_map_to_record_and_video(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path)
    event = _attachment_group_event([
        {"content_type": "audio/silk", "url": "https://multimedia.nt.qq.com.cn/v.silk"},
        {"content_type": "video/mp4", "url": "https://multimedia.nt.qq.com.cn/v.mp4"},
    ])
    payload = bridge.normalize_event(SimpleNamespace(self_id="bot"), event)

    assert [seg["data"]["url"] for seg in _segments_of(payload, "record")] == [
        "https://multimedia.nt.qq.com.cn/v.silk"
    ]
    assert [seg["data"]["url"] for seg in _segments_of(payload, "video")] == [
        "https://multimedia.nt.qq.com.cn/v.mp4"
    ]


def test_incoming_image_attachment_stays_image(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path)
    event = _attachment_group_event([
        {"content_type": "image/png", "url": "https://gchat.qpic.cn/a.png", "filename": "a.png"},
    ])
    payload = bridge.normalize_event(SimpleNamespace(self_id="bot"), event)

    assert [seg["data"]["url"] for seg in _segments_of(payload, "image")] == ["https://gchat.qpic.cn/a.png"]


def test_attachment_without_url_is_skipped(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path)
    event = _attachment_group_event([
        {"content_type": "application/pdf", "url": None},
        {"content_type": "image/png", "url": ""},
    ])
    payload = bridge.normalize_event(SimpleNamespace(self_id="bot"), event)

    assert _segments_of(payload, "file") == []
    assert _segments_of(payload, "image") == []


def test_attachment_helper_skips_missing_url(tmp_path: Path) -> None:
    from nonebot_plugin_webqq_bridge.protocol import _attachment_segment

    assert _attachment_segment(SimpleNamespace(type="file", data={"url": ""})) is None
    assert _attachment_segment(
        SimpleNamespace(type="file", data={"url": "https://multimedia.nt.qq.com.cn/f.pdf"})
    ) == {
        "type": "file",
        "data": {"url": "https://multimedia.nt.qq.com.cn/f.pdf",
                 "file": "https://multimedia.nt.qq.com.cn/f.pdf"},
    }


def test_unknown_segment_still_falls_back_to_text(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path)
    event = FakeGroupEvent()

    class WeirdSegment:
        type = "json"
        data = {"data": "{}"}

        def __str__(self):
            return "[json card]"

    event.get_message = lambda: [WeirdSegment()]
    payload = bridge.normalize_event(SimpleNamespace(self_id="bot"), event)
    assert payload["message"] == [{"type": "text", "data": {"text": "[json card]"}}]


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
