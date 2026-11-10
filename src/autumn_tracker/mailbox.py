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


IMAP_FETCH_BATCH_SIZE = 50


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeError):
        return value


def _strip_html(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(
        r'''(?is)<a\b[^>]*?href=["'](https?://[^"']+)["'][^>]*>(.*?)</a>''',
        lambda match: f"{re.sub(r'<[^>]+>', ' ', match.group(2))} {match.group(1)}",
        value,
    )
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    value = value.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", value).strip()


def _message_body(message: Message) -> str:
    plain: list[str] = []
    html: list[str] = []
    calendar: list[str] = []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html", "text/calendar"}:
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeError):
            payload = part.get_payload(decode=True) or b""
            content = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if content_type == "text/calendar":
            calendar.append(str(content))
        elif content_type == "text/html":
            html.append(str(content))
        else:
            plain.append(str(content))

    # HTML 中通常才有真实 href；优先使用清洗后的 HTML，确保会议入口不会
    # 因纯文本替代件只保留“点击进入”而丢失。ICS 放在最前面，避免 4,000 字符
    # 的 Agent 输入上限截掉 DTSTART/DTEND/LOCATION 等关键字段。
    content_body = _strip_html("\n".join(html)) if html else "\n".join(plain)
    sections = (["[日历邀请]\n" + "\n".join(calendar)] if calendar else []) + [content_body]
    body = "\n".join(section for section in sections if section)
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
            for offset in range(0, len(uids), IMAP_FETCH_BATCH_SIZE):
                requested = uids[offset : offset + IMAP_FETCH_BATCH_SIZE]
                uid_set = ",".join(str(uid) for uid in requested)
                status, payload = client.uid("fetch", uid_set, "(UID BODY.PEEK[])")
                if status != "OK":
                    raise RuntimeError(f"IMAP 批量读取失败：UID {requested[0]}–{requested[-1]}")
                raw_by_uid: dict[int, bytes] = {}
                for item in payload or []:
                    if not isinstance(item, tuple) or len(item) < 2:
                        continue
                    metadata, raw = item[0], item[1]
                    if not isinstance(metadata, bytes) or not isinstance(raw, bytes):
                        continue
                    match = re.search(rb"\bUID\s+(\d+)\b", metadata)
                    if match:
                        raw_by_uid[int(match.group(1))] = raw
                for uid in requested:
                    raw = raw_by_uid.get(uid)
                    if raw is None:
                        continue
                    parsed = BytesParser(policy=policy.default).parsebytes(raw)
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
                if requested:
                    max_seen = max(requested[-1], max_seen or 0)
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
