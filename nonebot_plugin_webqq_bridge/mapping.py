from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _numeric_map(value: Any, name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    result: dict[str, str] = {}
    reverse: dict[str, str] = {}
    for key, raw in value.items():
        key = str(key).strip()
        number = str(raw).strip()
        if not key or not number.isdigit() or int(number) <= 0:
            raise ValueError(f"{name} values must be positive decimal IDs")
        if number in reverse:
            raise ValueError(f"{name} maps {number} more than once")
        result[key] = number
        reverse[number] = key
    return result


@dataclass(frozen=True, slots=True)
class MappingConfig:
    groups: dict[str, str]
    labels: dict[str, str]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MappingConfig":
        if not isinstance(raw, dict):
            raise ValueError("mapping configuration must be an object")
        labels = raw.get("labels") or {}
        if not isinstance(labels, dict) or not all(isinstance(k, str) for k in labels):
            raise ValueError("labels must be an object")
        return cls(
            groups=_numeric_map(raw.get("groups"), "groups"),
            labels={str(k): str(v) for k, v in labels.items()},
        )

    @classmethod
    def load(cls, path: Path) -> "MappingConfig":
        with path.open(encoding="utf-8") as handle:
            raw = json.load(handle)
        return cls.from_dict(raw)

    def group_id(self, openid: str) -> str | None:
        return self.groups.get(str(openid))

    def group_openid(self, group_id: str) -> str | None:
        return next((key for key, value in self.groups.items() if value == str(group_id)), None)

    def label(self, chat_id: str, fallback: str) -> str:
        return self.labels.get(chat_id) or fallback
