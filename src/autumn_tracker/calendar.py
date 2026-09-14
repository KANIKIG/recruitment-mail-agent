from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
from typing import Any

from .config import Settings


@dataclass(frozen=True)
class CalendarEventRequest:
    event_key: str
    source_message_id: str
    summary: str
    start_at: str
    end_at: str
    description: str
    location: str | None = None


class LarkCalendar:
    """通过官方 lark-cli 在用户主日历创建面试日程。"""

    def __init__(self, settings: Settings):
        self.settings = settings

    def _run(self, arguments: list[str]) -> dict[str, Any]:
        executable = str(Path(self.settings.lark_cli)) if "/" in self.settings.lark_cli else self.settings.lark_cli
        environment = os.environ.copy()
        environment["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
        environment["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
        completed: subprocess.CompletedProcess[str] | None = None
        for attempt in range(4):
            completed = subprocess.run(
                [executable, *arguments],
                capture_output=True,
                text=True,
                env=environment,
            )
            combined = completed.stdout + completed.stderr
            rate_limited = "800004135" in combined or "rate limit" in combined.lower()
            if completed.returncode == 0 or not rate_limited:
                break
            time.sleep(1.5 * (attempt + 1))
        assert completed is not None
        raw = completed.stdout.strip() or completed.stderr.strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"lark-cli 日历命令未返回 JSON：{raw[:500]}") from exc
        if completed.returncode != 0 or payload.get("ok") is False:
            error = payload.get("error", {})
            detail = error.get("message") if isinstance(error, dict) else error
            raise RuntimeError(f"飞书日历调用失败：{detail or raw[:500]}")
        return payload

    @staticmethod
    def _find(value: Any, key: str) -> Any:
        if isinstance(value, dict):
            if key in value:
                return value[key]
            for child in value.values():
                found = LarkCalendar._find(child, key)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = LarkCalendar._find(child, key)
                if found is not None:
                    return found
        return None

    def check_connection(self) -> str:
        payload = self._run([
            "calendar", "calendars", "primary", "--as", "user", "--format", "json",
        ])
        calendar_id = self._find(payload, "calendar_id") or self._find(payload, "id")
        if not calendar_id:
            raise RuntimeError("飞书主日历响应中缺少 calendar_id")
        return str(calendar_id)

    def create_events(self, requests: list[CalendarEventRequest]) -> dict[str, str]:
        if not requests:
            return {}
        calendar_id = self.check_connection()
        created: dict[str, str] = {}
        for event in requests:
            start = datetime.fromisoformat(event.start_at)
            end = datetime.fromisoformat(event.end_at)
            data: dict[str, Any] = {
                "summary": event.summary,
                "description_rich": event.description,
                "start_time": {
                    "timestamp": str(int(start.timestamp())),
                    "timezone": self.settings.timezone,
                },
                "end_time": {
                    "timestamp": str(int(end.timestamp())),
                    "timezone": self.settings.timezone,
                },
                "free_busy_status": "busy",
                "need_notification": False,
                "reminders": [{"minutes": 1440}, {"minutes": 30}],
            }
            if event.location:
                data["location"] = {"name": event.location, "address": event.location}
            params = {
                "calendar_id": calendar_id,
                "idempotency_key": str(uuid.uuid5(uuid.NAMESPACE_URL, event.event_key)),
            }
            payload = self._run([
                "calendar", "events", "create",
                "--params", json.dumps(params, ensure_ascii=False),
                "--data", json.dumps(data, ensure_ascii=False),
                "--as", "user", "--format", "json",
            ])
            event_id = self._find(payload, "event_id") or self._find(payload, "id")
            if not event_id:
                raise RuntimeError("创建飞书日程后未返回 event_id")
            created[event.event_key] = str(event_id)
        return created
