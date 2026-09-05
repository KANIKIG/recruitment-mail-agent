from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import sqlite3

from .coremail import TodoRequest
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
            CREATE TABLE IF NOT EXISTS mail_todo (
                message_id TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                sender_address TEXT NOT NULL,
                received_at TEXT NOT NULL,
                due_date TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                completed_at TEXT
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
            self.connection.execute("DELETE FROM mail_todo")

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

    def enqueue_todo(self, todo: TodoRequest) -> None:
        self.connection.execute(
            "INSERT INTO mail_todo(message_id, subject, sender_address, received_at, due_date) "
            "VALUES(?, ?, ?, ?, ?) ON CONFLICT(message_id) DO UPDATE SET "
            "subject=excluded.subject, sender_address=excluded.sender_address, "
            "received_at=excluded.received_at, due_date=excluded.due_date, completed_at=NULL",
            (
                todo.message_id,
                todo.subject,
                todo.sender_address,
                todo.received_at,
                todo.due_date.isoformat(),
            ),
        )
        self.connection.commit()

    def pending_todos(self) -> list[TodoRequest]:
        rows = self.connection.execute(
            "SELECT message_id, subject, sender_address, received_at, due_date "
            "FROM mail_todo WHERE completed_at IS NULL ORDER BY due_date, message_id"
        ).fetchall()
        return [TodoRequest(*row[:4], due_date=date.fromisoformat(row[4])) for row in rows]

    def mark_todos_done(self, message_ids: set[str]) -> None:
        if not message_ids:
            return
        with self.connection:
            self.connection.executemany(
                "UPDATE mail_todo SET completed_at=CURRENT_TIMESTAMP, last_error=NULL "
                "WHERE message_id=?",
                [(message_id,) for message_id in message_ids],
            )

    def mark_todos_failed(self, message_ids: set[str], error: str) -> None:
        if not message_ids:
            return
        with self.connection:
            self.connection.executemany(
                "UPDATE mail_todo SET attempts=attempts+1, last_error=? WHERE message_id=?",
                [(error[:500], message_id) for message_id in message_ids],
            )
