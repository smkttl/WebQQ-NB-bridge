from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


class BridgeStorage:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                synthetic_id TEXT PRIMARY KEY,
                official_id TEXT NOT NULL,
                bot_id TEXT NOT NULL,
                conversation_kind TEXT NOT NULL,
                conversation_openid TEXT NOT NULL,
                msg_idx TEXT,
                event_id TEXT,
                timestamp REAL NOT NULL,
                UNIQUE(bot_id, official_id)
            );
            CREATE TABLE IF NOT EXISTS queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created REAL NOT NULL,
                payload TEXT NOT NULL
            );
        """)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def remember_message(self, synthetic_id: str, official_id: str, bot_id: str,
                         kind: str, openid: str, msg_idx: str = "", event_id: str = "",
                         timestamp: float | None = None) -> None:
        self.db.execute(
            """INSERT OR REPLACE INTO messages
            (synthetic_id, official_id, bot_id, conversation_kind, conversation_openid,
             msg_idx, event_id, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (synthetic_id, str(official_id), str(bot_id), kind, str(openid), msg_idx,
             event_id, timestamp or time.time()),
        )
        self.db.commit()

    def by_synthetic(self, synthetic_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM messages WHERE synthetic_id = ?", (str(synthetic_id),)).fetchone()

    def by_official(self, bot_id: str, official_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM messages WHERE bot_id = ? AND official_id = ?",
                               (str(bot_id), str(official_id))).fetchone()

    def queue(self, payload: dict[str, Any]) -> None:
        self.db.execute("INSERT INTO queue(created, payload) VALUES (?, ?)",
                        (time.time(), json.dumps(payload, ensure_ascii=False)))
        self.db.execute("DELETE FROM queue WHERE id NOT IN (SELECT id FROM queue ORDER BY id DESC LIMIT 10000)")
        self.db.execute("DELETE FROM queue WHERE created < ?", (time.time() - 7 * 86400,))
        self.db.commit()

    def queued(self) -> list[tuple[int, dict[str, Any]]]:
        rows = self.db.execute("SELECT id, payload FROM queue ORDER BY id").fetchall()
        return [(int(row["id"]), json.loads(row["payload"])) for row in rows]

    def remove_queue(self, row_id: int) -> None:
        self.db.execute("DELETE FROM queue WHERE id = ?", (row_id,))
        self.db.commit()
