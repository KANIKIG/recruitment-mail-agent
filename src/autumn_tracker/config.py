from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
import os
from typing import Any


ROOT = Path(__file__).resolve().parents[2]


def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return default


def _json_object_env(name: str) -> dict[str, Any]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} 必须是合法 JSON 对象") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{name} 必须是 JSON 对象")
    return value


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    email: str
    imap_host: str
    imap_port: int
    imap_folder: str
    password_value: str | None
    coremail_todo_enabled: bool
    coremail_web_url: str
    coremail_lookup_limit: int
    llm_api_key: str | None
    llm_base_url: str
    llm_chat_completions_url: str | None
    llm_model: str
    llm_batch_size: int
    llm_json_mode: bool
    llm_system_role: str
    llm_max_tokens_param: str
    llm_extra_body: dict[str, Any]
    llm_extra_headers: dict[str, str]
    llm_timeout_seconds: int
    lark_base_token: str | None
    lark_table_id: str | None
    lark_cli: str
    lark_calendar_enabled: bool
    since_date: date
    sync_interval_seconds: int
    max_messages: int
    min_confidence: float
    timezone: str
    database_path: Path

    @property
    def llm_endpoint(self) -> str:
        if self.llm_chat_completions_url:
            return self.llm_chat_completions_url
        if self.llm_base_url.endswith("/chat/completions"):
            return self.llm_base_url
        return self.llm_base_url.rstrip("/") + "/chat/completions"

    @property
    def llm_cache_key(self) -> str:
        endpoint_hash = hashlib.sha256(self.llm_endpoint.encode("utf-8")).hexdigest()[:12]
        return f"{self.llm_model}@{endpoint_hash}"

    @classmethod
    def from_env(cls, require_targets: bool = False, require_mail: bool = True) -> "Settings":
        load_dotenv(ROOT / ".env")
        email = os.getenv("IMAP_EMAIL", "").strip()
        if require_mail and not email:
            raise ValueError("缺少 IMAP_EMAIL；请先复制 .env.example 为 .env 并填写邮箱地址")
        llm_base_url = _first_env(
            "LLM_BASE_URL",
            "OPENAI_BASE_URL",
            "DEEPSEEK_BASE_URL",
            default="https://api.deepseek.com",
        ).rstrip("/")
        llm_extra_body = _json_object_env("LLM_EXTRA_BODY_JSON")
        if not llm_extra_body and llm_base_url == "https://api.deepseek.com":
            # 保持旧版 DeepSeek 部署默认关闭 thinking；其他兼容服务不携带此专属字段。
            llm_extra_body = {"thinking": {"type": "disabled"}}
        extra_headers_raw = _json_object_env("LLM_EXTRA_HEADERS_JSON")
        llm_extra_headers = {str(key): str(value) for key, value in extra_headers_raw.items()}
        llm_system_role = os.getenv("LLM_SYSTEM_ROLE", "system").strip().lower()
        if llm_system_role not in {"system", "developer"}:
            raise ValueError("LLM_SYSTEM_ROLE 只能是 system 或 developer")
        llm_max_tokens_param = os.getenv("LLM_MAX_TOKENS_PARAM", "max_tokens").strip()
        if llm_max_tokens_param not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("LLM_MAX_TOKENS_PARAM 只能是 max_tokens 或 max_completion_tokens")
        result = cls(
            email=email,
            imap_host=os.getenv("IMAP_HOST", ""),
            imap_port=int(os.getenv("IMAP_PORT", "993")),
            imap_folder=os.getenv("IMAP_FOLDER", "INBOX"),
            password_value=os.getenv("IMAP_PASSWORD") or None,
            coremail_todo_enabled=os.getenv("COREMAIL_TODO_ENABLED", "false").lower() in {"1", "true", "yes", "on"},
            coremail_web_url=os.getenv("COREMAIL_WEB_URL", "").rstrip("/"),
            coremail_lookup_limit=max(20, min(500, int(os.getenv("COREMAIL_LOOKUP_LIMIT", "200")))),
            llm_api_key=_first_env("LLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY") or None,
            llm_base_url=llm_base_url,
            llm_chat_completions_url=os.getenv("LLM_CHAT_COMPLETIONS_URL", "").strip().rstrip("/") or None,
            llm_model=_first_env("LLM_MODEL", "OPENAI_MODEL", "DEEPSEEK_MODEL", default="deepseek-v4-flash"),
            llm_batch_size=max(1, min(20, int(_first_env("LLM_BATCH_SIZE", "DEEPSEEK_BATCH_SIZE", default="8")))),
            llm_json_mode=os.getenv("LLM_JSON_MODE", "true").lower() in {"1", "true", "yes", "on"},
            llm_system_role=llm_system_role,
            llm_max_tokens_param=llm_max_tokens_param,
            llm_extra_body=llm_extra_body,
            llm_extra_headers=llm_extra_headers,
            llm_timeout_seconds=max(10, min(600, int(os.getenv("LLM_TIMEOUT_SECONDS", "90")))),
            lark_base_token=os.getenv("LARK_BASE_TOKEN") or os.getenv("LARK_APP_TOKEN") or None,
            lark_table_id=os.getenv("LARK_TABLE_ID") or None,
            lark_cli=os.getenv("LARK_CLI", "./node_modules/.bin/lark-cli"),
            lark_calendar_enabled=os.getenv("LARK_CALENDAR_ENABLED", "false").lower() in {"1", "true", "yes", "on"},
            since_date=date.fromisoformat(os.getenv("SYNC_SINCE_DATE", "2026-08-20")),
            sync_interval_seconds=max(60, int(os.getenv("SYNC_INTERVAL_SECONDS", "300"))),
            max_messages=int(os.getenv("SYNC_MAX_MESSAGES", "300")),
            min_confidence=float(os.getenv("SYNC_MIN_CONFIDENCE", "0.55")),
            timezone=os.getenv("SYNC_TIMEZONE", "Asia/Shanghai"),
            database_path=Path(
                os.getenv("TRACKER_DATABASE_PATH", str(ROOT / "data" / "tracker.sqlite3"))
            ).expanduser(),
        )
        if require_targets and (not result.lark_base_token or not result.lark_table_id):
            raise ValueError("缺少 LARK_BASE_TOKEN/LARK_TABLE_ID；请先执行 ./tracker init-base")
        if result.coremail_todo_enabled and not result.coremail_web_url:
            raise ValueError("启用邮件待办时必须配置 COREMAIL_WEB_URL")
        return result

    def mail_password(self) -> str:
        if self.password_value:
            return self.password_value
        raise ValueError("未在 .env 中配置 IMAP_PASSWORD")
