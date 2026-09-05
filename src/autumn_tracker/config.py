from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
import os


ROOT = Path(__file__).resolve().parents[2]


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
    deepseek_api_key: str | None
    deepseek_base_url: str
    deepseek_model: str
    deepseek_batch_size: int
    lark_base_token: str | None
    lark_table_id: str | None
    lark_cli: str
    since_date: date
    sync_interval_seconds: int
    max_messages: int
    min_confidence: float
    timezone: str
    database_path: Path

    @classmethod
    def from_env(cls, require_targets: bool = False, require_mail: bool = True) -> "Settings":
        load_dotenv(ROOT / ".env")
        email = os.getenv("IMAP_EMAIL", "").strip()
        if require_mail and not email:
            raise ValueError("缺少 IMAP_EMAIL；请先复制 .env.example 为 .env 并填写邮箱地址")
        result = cls(
            email=email,
            imap_host=os.getenv("IMAP_HOST", ""),
            imap_port=int(os.getenv("IMAP_PORT", "993")),
            imap_folder=os.getenv("IMAP_FOLDER", "INBOX"),
            password_value=os.getenv("IMAP_PASSWORD") or None,
            coremail_todo_enabled=os.getenv("COREMAIL_TODO_ENABLED", "false").lower() in {"1", "true", "yes", "on"},
            coremail_web_url=os.getenv("COREMAIL_WEB_URL", "").rstrip("/"),
            coremail_lookup_limit=max(20, min(500, int(os.getenv("COREMAIL_LOOKUP_LIMIT", "200")))),
            deepseek_api_key=os.getenv("DEEPSEEK_API_KEY") or None,
            deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
            deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            deepseek_batch_size=max(1, min(20, int(os.getenv("DEEPSEEK_BATCH_SIZE", "8")))),
            lark_base_token=os.getenv("LARK_BASE_TOKEN") or os.getenv("LARK_APP_TOKEN") or None,
            lark_table_id=os.getenv("LARK_TABLE_ID") or None,
            lark_cli=os.getenv("LARK_CLI", "./node_modules/.bin/lark-cli"),
            since_date=date.fromisoformat(os.getenv("SYNC_SINCE_DATE", "2026-08-20")),
            sync_interval_seconds=max(60, int(os.getenv("SYNC_INTERVAL_SECONDS", "300"))),
            max_messages=int(os.getenv("SYNC_MAX_MESSAGES", "300")),
            min_confidence=float(os.getenv("SYNC_MIN_CONFIDENCE", "0.55")),
            timezone=os.getenv("SYNC_TIMEZONE", "Asia/Shanghai"),
            database_path=ROOT / "data" / "tracker.sqlite3",
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
