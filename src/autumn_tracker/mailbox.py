from __future__ import annotations

from datetime import datetime, timezone
from email import policy
from email.header import decode_header, make_header
from email.message import Message
from email.parser import BytesParser
from email.utils import parsedate_to_datetime, parseaddr
import imaplib
import re
from zoneinfo import ZoneInfo

from .config import Settings
from .models import MailMessage


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeError):
        return value


def _strip_html(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    value = value.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", value).strip()


def _message_body(message: Message) -> str:
    plain: list[str] = []
    html: list[str] = []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeError):
            payload = part.get_payload(decode=True) or b""
            content = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        (plain if content_type == "text/plain" else html).append(str(content))
    body = "\n".join(plain) if plain else _strip_html("\n".join(html))
    return re.sub(r"\s+", " ", body).strip()[:30000]


class ImapMailbox:
    def __init__(self, settings: Settings):
        self.settings = settings

    def check_connection(self) -> int:
        client = imaplib.IMAP4_SSL(
            self.settings.imap_host,
            self.settings.imap_port,
            timeout=30,
        )
        try:
            client.login(self.settings.email, self.settings.mail_password())
            status, data = client.select(self.settings.imap_folder, readonly=True)
            if status != "OK":
                raise RuntimeError(f"无法只读打开邮箱目录 {self.settings.imap_folder}")
            return int(data[0]) if data and data[0] else 0
        finally:
            try:
                client.logout()
            except imaplib.IMAP4.error:
                pass

    def fetch(self, after_uid: int | None = None) -> tuple[list[MailMessage], int | None]:
        criterion = (
            f"UID {after_uid + 1}:*"
            if after_uid
            else f'SINCE {self.settings.since_date.strftime("%d-%b-%Y")}'
        )
        return self._fetch_matching(criterion, after_uid)

    def fetch_flagged(self) -> list[MailMessage]:
        """读取起始日期后的已标记邮件，用于一次性补建截止待办。"""
        criterion = f'(FLAGGED SINCE {self.settings.since_date.strftime("%d-%b-%Y")})'
        messages, _ = self._fetch_matching(criterion)
        return messages

    def _fetch_matching(
        self,
        criterion: str,
        after_uid: int | None = None,
    ) -> tuple[list[MailMessage], int | None]:
        since = datetime.combine(
            self.settings.since_date,
            datetime.min.time(),
            tzinfo=ZoneInfo(self.settings.timezone),
        )
        client = imaplib.IMAP4_SSL(
            self.settings.imap_host,
            self.settings.imap_port,
            timeout=30,
        )
        try:
            client.login(self.settings.email, self.settings.mail_password())
            status, _ = client.select(self.settings.imap_folder, readonly=True)
            if status != "OK":
                raise RuntimeError(f"无法只读打开邮箱目录 {self.settings.imap_folder}")
            status, data = client.uid("search", None, criterion)
            if status != "OK":
                raise RuntimeError("IMAP 搜索失败")
            uids = [int(item) for item in (data[0] or b"").split()]
            # 部分 IMAP 服务在 `UID n:*` 的 n 超过当前最大 UID 时，仍会返回
            # 最后一封邮件。必须本地再做严格大于过滤，确保不重复抓取旧正文。
            if after_uid is not None:
                uids = [uid for uid in uids if uid > after_uid]
            # 从最早一批开始，避免首次同步邮件过多时跳过旧 UID。
            uids = uids[: self.settings.max_messages]
            messages: list[MailMessage] = []
            max_seen = after_uid
            for uid in uids:
                status, payload = client.uid("fetch", str(uid), "(BODY.PEEK[])")
                if status != "OK" or not payload or not isinstance(payload[0], tuple):
                    continue
                parsed = BytesParser(policy=policy.default).parsebytes(payload[0][1])
                sender_name, sender_address = parseaddr(_decode(parsed.get("From")))
                try:
                    received_at = parsedate_to_datetime(parsed.get("Date"))
                    if received_at.tzinfo is None:
                        received_at = received_at.replace(tzinfo=timezone.utc)
                except (TypeError, ValueError, OverflowError):
                    received_at = datetime.now(timezone.utc)
                parsed_message = MailMessage(
                    uid=uid,
                    message_id=str(parsed.get("Message-ID") or f"imap:{uid}"),
                    subject=_decode(parsed.get("Subject")),
                    sender_name=_decode(sender_name),
                    sender_address=sender_address.lower(),
                    received_at=received_at,
                    body=_message_body(parsed),
                )
                if parsed_message.received_at.astimezone(since.tzinfo) >= since:
                    messages.append(parsed_message)
                max_seen = max(uid, max_seen or 0)
            return messages, max_seen
        finally:
            try:
                client.logout()
            except imaplib.IMAP4.error:
                pass

    def mark_flagged(self, uids: list[int]) -> int:
        """给已成功同步的后续流程邮件加 IMAP 星标。"""
        unique_uids = sorted(set(uids))
        if not unique_uids:
            return 0
        client = imaplib.IMAP4_SSL(
            self.settings.imap_host,
            self.settings.imap_port,
            timeout=30,
        )
        try:
            client.login(self.settings.email, self.settings.mail_password())
            status, _ = client.select(self.settings.imap_folder, readonly=False)
            if status != "OK":
                raise RuntimeError(f"无法以可写方式打开邮箱目录 {self.settings.imap_folder}")
            for uid in unique_uids:
                status, _ = client.uid("store", str(uid), "+FLAGS.SILENT", r"(\Flagged)")
                if status != "OK":
                    raise RuntimeError(f"无法给邮件 UID {uid} 添加星标")
            return len(unique_uids)
        finally:
            try:
                client.logout()
            except imaplib.IMAP4.error:
                pass
