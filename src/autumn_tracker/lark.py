from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import time
from typing import Any


STATUS_OPTIONS = [
    {"name": "待确认", "hue": "Gray", "lightness": "Lighter"},
    {"name": "投递", "hue": "Blue", "lightness": "Lighter"},
    {"name": "测评&AI面", "hue": "Wathet", "lightness": "Light"},
    {"name": "笔试", "hue": "Purple", "lightness": "Lighter"},
    {"name": "技术面", "hue": "Orange", "lightness": "Light"},
    {"name": "HR面", "hue": "Turquoise", "lightness": "Light"},
    {"name": "主管面", "hue": "Carmine", "lightness": "Light"},
    {"name": "Offer", "hue": "Green", "lightness": "Standard"},
    {"name": "已挂", "hue": "Red", "lightness": "Light"},
]

FIELD_SPECS = [
    {"name": "公司名称", "type": "text"},
    {"name": "岗位名称", "type": "text"},
    {"name": "流程状态", "type": "select", "options": STATUS_OPTIONS},
    {"name": "更新时间", "type": "datetime", "style": {"format": "yyyy/MM/dd HH:mm"}},
    {"name": "投递时间", "type": "datetime", "style": {"format": "yyyy/MM/dd HH:mm"}},
    {"name": "截止时间", "type": "datetime", "style": {"format": "yyyy/MM/dd HH:mm"}}
]


class LarkCliError(RuntimeError):
    pass


@dataclass(frozen=True)
class LarkRecord:
    record_id: str
    fields: dict[str, Any]


class LarkBase:
    def __init__(self, cli: str, app_token: str | None = None, table_id: str | None = None):
        self.cli = cli
        self.app_token = app_token
        self.table_id = table_id

    def _run_command(self, arguments: list[str]) -> dict:
        executable = str(Path(self.cli)) if "/" in self.cli else self.cli
        command = [executable, *arguments]
        completed = None
        for attempt in range(5):
            completed = subprocess.run(command, capture_output=True, text=True)
            combined = completed.stderr + completed.stdout
            rate_limited = "limited" in combined.lower() or "800004135" in combined
            if not rate_limited:
                break
            time.sleep(1.5 * (attempt + 1))
        assert completed is not None
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise LarkCliError(f"lark-cli 调用失败：{detail}")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise LarkCliError(f"lark-cli 未返回 JSON：{completed.stdout[:500]}") from exc
        if isinstance(payload, dict) and payload.get("code") not in (None, 0):
            raise LarkCliError(f"飞书 API 错误：{payload.get('msg', payload)}")
        if isinstance(payload, dict) and payload.get("ok") is False:
            error = payload.get("error", {})
            detail = error.get("message") if isinstance(error, dict) else error
            raise LarkCliError(f"lark-cli 错误：{detail or payload}")
        return payload

    @staticmethod
    def _find(payload: Any, key: str) -> Any:
        if isinstance(payload, dict):
            if key in payload:
                return payload[key]
            for value in payload.values():
                found = LarkBase._find(value, key)
                if found is not None:
                    return found
        elif isinstance(payload, list):
            for value in payload:
                found = LarkBase._find(value, key)
                if found is not None:
                    return found
        return None

    def create_base(self, name: str) -> tuple[str, str]:
        payload = self._run_command([
            "base", "+base-create",
            "--name", name,
            "--table-name", "秋招投递进展",
            "--fields", json.dumps(FIELD_SPECS, ensure_ascii=False),
            "--time-zone", "Asia/Shanghai",
            "--as", "user",
            "--format", "json",
        ])
        base_token = self._find(payload, "base_token") or self._find(payload, "app_token")
        table_id = self._find(payload, "table_id")
        if not table_id:
            table = self._find(payload, "table")
            table_id = table.get("id") if isinstance(table, dict) else None
        if not base_token or not table_id:
            raise LarkCliError(f"创建智能表格后未取得 base_token/table_id：{payload}")
        return str(base_token), str(table_id)

    def create_default_views(self) -> None:
        app_token, table_id = self._require_target()
        common = ["--base-token", app_token, "--table-id", table_id, "--as", "user", "--format", "json"]
        listed = self._run_command(["base", "+view-list", *common])
        views = self._find(listed, "views") or []
        view_ids = {
            str(item.get("name")): str(item.get("id"))
            for item in views
            if isinstance(item, dict) and item.get("name") and item.get("id")
        }
        kanban_id = view_ids.get("进展看板")
        if not kanban_id:
            created = self._run_command(["base", "+view-create", *common, "--json", '{"name":"进展看板","type":"kanban"}'])
            kanban_id = self._find(created, "view_id") or self._find(created, "id")
        if kanban_id:
            self._run_command([
                "base", "+view-set-group", *common, "--view-id", str(kanban_id),
                "--json", '{"group_config":[{"field":"流程状态","desc":false}]}',
            ])
        for view in views:
            if isinstance(view, dict) and view.get("id") and view.get("type") in {"grid", "kanban"}:
                self.set_view_sort(str(view["id"]), "更新时间", descending=True)

    def list_views(self) -> list[dict[str, Any]]:
        app_token, table_id = self._require_target()
        payload = self._run_command([
            "base", "+view-list", "--base-token", app_token, "--table-id", table_id,
            "--as", "user", "--format", "json",
        ])
        views = self._find(payload, "views") or []
        return [item for item in views if isinstance(item, dict)]

    def list_fields(self) -> list[dict[str, Any]]:
        app_token, table_id = self._require_target()
        payload = self._run_command([
            "base", "+field-list", "--base-token", app_token, "--table-id", table_id,
            "--as", "user", "--format", "json",
        ])
        fields = self._find(payload, "fields") or []
        return [item for item in fields if isinstance(item, dict)]

    def update_field(self, field_id: str, definition: dict[str, Any]) -> None:
        app_token, table_id = self._require_target()
        self._run_command([
            "base", "+field-update", "--base-token", app_token, "--table-id", table_id,
            "--field-id", field_id, "--json", json.dumps(definition, ensure_ascii=False),
            "--as", "user", "--yes", "--format", "json",
        ])

    def create_fields(self, definitions: list[dict[str, Any]]) -> None:
        app_token, table_id = self._require_target()
        self._run_command([
            "base", "+field-create", "--base-token", app_token, "--table-id", table_id,
            "--json", json.dumps(definitions, ensure_ascii=False), "--as", "user", "--format", "json",
        ])

    def delete_field(self, field_id: str) -> None:
        app_token, table_id = self._require_target()
        self._run_command([
            "base", "+field-delete", "--base-token", app_token, "--table-id", table_id,
            "--field-id", field_id, "--as", "user", "--yes", "--format", "json",
        ])

    def delete_view(self, view_id: str) -> None:
        app_token, table_id = self._require_target()
        self._run_command([
            "base", "+view-delete", "--base-token", app_token, "--table-id", table_id,
            "--view-id", view_id, "--as", "user", "--yes", "--format", "json",
        ])

    def set_view_sort(self, view_id: str, field: str, descending: bool = True) -> None:
        app_token, table_id = self._require_target()
        self._run_command([
            "base", "+view-set-sort", "--base-token", app_token, "--table-id", table_id,
            "--view-id", view_id,
            "--json", json.dumps({"sort_config": [{"field": field, "desc": descending}]}, ensure_ascii=False),
            "--as", "user", "--format", "json",
        ])

    def _require_target(self) -> tuple[str, str]:
        if not self.app_token or not self.table_id:
            raise LarkCliError("尚未配置飞书 app_token/table_id")
        return self.app_token, self.table_id

    def list_records(self) -> list[LarkRecord]:
        app_token, table_id = self._require_target()
        payload = self._run_command([
            "base", "+record-list",
            "--base-token", app_token,
            "--table-id", table_id,
            "--limit", "200",
            "--as", "user",
            "--format", "json",
        ])
        records: list[LarkRecord] = []
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        matrix = data.get("data") if isinstance(data, dict) else None
        names = data.get("fields") if isinstance(data, dict) else None
        record_ids = data.get("record_id_list") if isinstance(data, dict) else None
        if isinstance(matrix, list) and isinstance(names, list) and isinstance(record_ids, list):
            for record_id, row in zip(record_ids, matrix):
                records.append(LarkRecord(str(record_id), dict(zip(names, row))))
            return records
        items = self._find(payload, "items") or self._find(payload, "records") or []
        for item in items if isinstance(items, list) else []:
            record_id = item.get("record_id") or item.get("id")
            fields = item.get("fields", item)
            if record_id:
                records.append(LarkRecord(str(record_id), fields))
        return records

    def create_record(self, fields: dict[str, Any]) -> str | None:
        app_token, table_id = self._require_target()
        payload = self._run_command([
            "base", "+record-upsert",
            "--base-token", app_token,
            "--table-id", table_id,
            "--json", json.dumps(self._typed_fields(fields), ensure_ascii=False),
            "--as", "user",
            "--format", "json",
        ])
        record_id = self._find(payload, "record_id")
        if record_id is None:
            record_ids = self._find(payload, "record_id_list")
            record_id = record_ids[0] if isinstance(record_ids, list) and record_ids else None
        return str(record_id) if record_id else None

    def delete_records(self, record_ids: list[str]) -> None:
        if not record_ids:
            return
        app_token, table_id = self._require_target()
        for offset in range(0, len(record_ids), 100):
            chunk = record_ids[offset : offset + 100]
            arguments = [
                "base", "+record-delete", "--base-token", app_token, "--table-id", table_id,
                "--as", "user", "--yes", "--format", "json",
            ]
            for record_id in chunk:
                arguments.extend(["--record-id", record_id])
            self._run_command(arguments)

    def update_record(self, record_id: str, fields: dict[str, Any]) -> None:
        app_token, table_id = self._require_target()
        self._run_command([
            "base", "+record-upsert",
            "--base-token", app_token,
            "--table-id", table_id,
            "--record-id", record_id,
            "--json", json.dumps(self._typed_fields(fields), ensure_ascii=False),
            "--as", "user",
            "--format", "json",
        ])

    def batch_update_records(self, updates: dict[str, dict[str, Any]]) -> None:
        if not updates:
            return
        app_token, table_id = self._require_target()
        items = list(updates.items())
        for offset in range(0, len(items), 200):
            batch = {record_id: self._typed_fields(fields) for record_id, fields in items[offset : offset + 200]}
            self._run_command([
                "base", "+record-batch-update", "--base-token", app_token, "--table-id", table_id,
                "--json", json.dumps({"update_records": batch}, ensure_ascii=False),
                "--as", "user", "--format", "json",
            ])

    @staticmethod
    def _typed_fields(fields: dict[str, Any]) -> dict[str, Any]:
        result = dict(fields)
        for status_field in ("当前进展", "流程状态"):
            if status_field in result and isinstance(result[status_field], str):
                result[status_field] = [result[status_field]]
        return result
