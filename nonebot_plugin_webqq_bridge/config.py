from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _json_list(value: str, default: list[str]) -> list[str]:
    if not value:
        return default
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("WEBQQ_BRIDGE_ALLOWED_FILE_ROOTS must be a JSON array") from exc
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError("WEBQQ_BRIDGE_ALLOWED_FILE_ROOTS must be a string array")
    return parsed


@dataclass(slots=True)
class BridgeConfig:
    host: str = "127.0.0.1"
    port: int = 49342
    token: str = ""
    bot_id: str = ""
    mapping_file: Path = Path("webqq-bridge.json")
    state_file: Path = Path(".webqq-bridge/state.sqlite3")
    allowed_file_roots: list[Path] = field(default_factory=lambda: [Path("/tmp")])
    inspect_enabled: bool = True
    inspect_command: str = "inspect"

    @classmethod
    def from_env(cls) -> "BridgeConfig":
        port = int(os.environ.get("WEBQQ_BRIDGE_PORT", "49342"))
        if not 1 <= port <= 65535:
            raise ValueError("WEBQQ_BRIDGE_PORT must be between 1 and 65535")
        token = os.environ.get("WEBQQ_BRIDGE_TOKEN", "")
        if not token:
            raise ValueError("WEBQQ_BRIDGE_TOKEN is required")
        command = os.environ.get("WEBQQ_BRIDGE_INSPECT_COMMAND", "inspect").strip()
        if not command or any(ch.isspace() for ch in command):
            raise ValueError("WEBQQ_BRIDGE_INSPECT_COMMAND must be one word")
        roots = [Path(item).expanduser().resolve() for item in _json_list(
            os.environ.get("WEBQQ_BRIDGE_ALLOWED_FILE_ROOTS", ""), ["/tmp"]
        )]
        return cls(
            host=os.environ.get("WEBQQ_BRIDGE_HOST", "127.0.0.1"),
            port=port,
            token=token,
            bot_id=os.environ.get("WEBQQ_BRIDGE_BOT_ID", "").strip(),
            mapping_file=Path(os.environ.get("WEBQQ_BRIDGE_MAPPING_FILE", "webqq-bridge.json")).expanduser(),
            state_file=Path(os.environ.get("WEBQQ_BRIDGE_STATE_FILE", ".webqq-bridge/state.sqlite3")).expanduser(),
            allowed_file_roots=roots,
            inspect_enabled=os.environ.get("WEBQQ_BRIDGE_INSPECT_ENABLED", "true").lower() not in {"0", "false", "no"},
            inspect_command=command,
        )
