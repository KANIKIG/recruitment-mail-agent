from __future__ import annotations

from pathlib import Path
import json
import sqlite3

from .models import Classification


class StateStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS checkpoint (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS processed_mail (
                message_id TEXT PRIMARY KEY,
                uid INTEGER NOT NULL,
                classification TEXT NOT NULL,
                synced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS agent_cache (
                message_id TEXT PRIMARY KEY,
                result_json TEXT NOT NULL,
                model TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

    def close(self) -> None:
        self.connection.close()

    def reset(self) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM checkpoint")
            self.connection.execute("DELETE FROM processed_mail")
            self.connection.execute("DELETE FROM agent_cache")

    def get_last_uid(self) -> int | None:
        row = self.connection.execute(
            "SELECT value FROM checkpoint WHERE name = 'last_uid'"
        ).fetchone()
        return int(row[0]) if row else None

    def set_last_uid(self, uid: int) -> None:
        self.connection.execute(
            "INSERT INTO checkpoint(name, value) VALUES('last_uid', ?) "
            "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
            (str(uid),),
        )
        self.connection.commit()

    def was_processed(self, message_id: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM processed_mail WHERE message_id = ?", (message_id,)
        ).fetchone() is not None

    def mark_processed(self, message_id: str, uid: int, classification: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO processed_mail(message_id, uid, classification) VALUES(?, ?, ?)",
            (message_id, uid, classification),
        )
        self.connection.commit()

    def get_agent_result(self, message_id: str, model: str) -> Classification | None:
        row = self.connection.execute(
            "SELECT result_json FROM agent_cache WHERE message_id = ? AND model = ?",
            (message_id, model),
        ).fetchone()
        if not row:
            return None
        try:
            return Classification(**json.loads(row[0]))
        except (TypeError, json.JSONDecodeError):
            return None

    def save_agent_result(self, message_id: str, model: str, result: Classification) -> None:
        payload = json.dumps(result.__dict__, ensure_ascii=False)
        self.connection.execute(
            "INSERT INTO agent_cache(message_id, result_json, model) VALUES(?, ?, ?) "
            "ON CONFLICT(message_id) DO UPDATE SET result_json=excluded.result_json, model=excluded.model, created_at=CURRENT_TIMESTAMP",
            (message_id, payload, model),
        )
        self.connection.commit()
