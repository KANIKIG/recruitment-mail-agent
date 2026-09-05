from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from email.utils import parseaddr
from http.cookiejar import CookieJar
import json
import re
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener

from .config import Settings


@dataclass(frozen=True)
class TodoRequest:
    message_id: str
    subject: str
    sender_address: str
    received_at: str
    due_date: date


def _normalize_subject(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def _addresses(value: Any) -> set[str]:
    if isinstance(value, list):
        result: set[str] = set()
        for item in value:
            result.update(_addresses(item))
        return result
    if isinstance(value, dict):
        result = set()
        for key in ("address", "email", "addr"):
            if value.get(key):
                result.add(str(value[key]).lower())
        return result
    address = parseaddr(str(value or ""))[1]
    return {address.lower()} if address else set()


class CoremailTodoClient:
    """Coremail XT 原生邮件待办客户端。

    Coremail 的待办不是 IMAP 标准字段，因此使用其 Webmail 同源接口。密码只
    从本地 Settings 读取，不写日志；会话仅存活于一次同步进程中。
    """

    def __init__(
        self,
        settings: Settings,
        opener_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.settings = settings
        self.base_url = settings.coremail_web_url.rstrip("/")
        self.cookies = CookieJar()
        factory = opener_factory or build_opener
        self.opener = factory(HTTPCookieProcessor(self.cookies))
        self.sid: str | None = None

    def _request(self, request: Request) -> tuple[str, str]:
        with self.opener.open(request, timeout=30) as response:
            return response.read().decode("utf-8", errors="replace"), response.geturl()

    def login(self) -> None:
        user_agent = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
        )
        # Coremail 会在登录页 GET 时初始化会话 Cookie，必须先访问一次。
        self._request(Request(self.base_url + "/", headers={"User-Agent": user_agent}))
        fields = {
            "locale": "zh_CN",
            "nodetect": "false",
            "destURL": "",
            "supportLoginDevice": "true",
            "accessToken": "",
            "timestamp": "",
            "signature": "",
            "nonce": "",
            "device": "",
            "supportDynamicPwd": "true",
            "supportBind2FA": "true",
            "authorizeDevice": "",
            "loginType": "",
            "lookupCallback": "",
            "uid": self.settings.email,
            "password": self.settings.mail_password(),
            "action:login": "",
        }
        body, final_url = self._request(Request(
            f"{self.base_url}/coremail/index.jsp?cus=1",
            data=urlencode(fields).encode(),
            headers={
                "User-Agent": user_agent,
                "Origin": self.base_url,
                "Referer": self.base_url + "/",
            },
        ))
        sid = (parse_qs(urlparse(final_url).query).get("sid") or [None])[0]
        if not sid:
            location_match = re.search(r"[?&]sid=([^&\"'<>\s]+)", body)
            sid = location_match.group(1) if location_match else None
        if not sid:
            match = re.search(
                r"(?:^|[, {])[\"']?(?:sid|s)[\"']?\s*:\s*[\"']([^\"']+)",
                body,
            )
            sid = match.group(1) if match else None
        if not sid:
            code = re.search(
                r"[\"']?loginResultCode[\"']?\s*:\s*[\"']?([^,\n}\"']+)",
                body,
            )
            suffix = f"（{code.group(1)}）" if code and code.group(1) != "null" else ""
            raise RuntimeError(f"Coremail Webmail 登录失败{suffix}")
        self.sid = sid

    @staticmethod
    def _decode_response(raw: str) -> Any:
        raw = re.sub(r"^\s*\)\]\}',?\s*", "", raw)
        data = json.loads(raw)
        code = data.get("code")
        if code and code != "S_OK":
            raise RuntimeError(f"Coremail 接口失败：{code}")
        return data.get("var")

    def _call(self, function: str, payload: str) -> Any:
        if not self.sid:
            self.login()
        raw, _ = self._request(Request(
            f"{self.base_url}/coremail/s/json?func={function}&sid={self.sid}",
            data=payload.encode("utf-8"),
            headers={
                "Accept": "text/x-json",
                "Content-Type": f'text/x-json; tz="{self.settings.timezone}"',
                "User-Agent": "RecruitmentMailAgent/0.2",
            },
        ))
        return self._decode_response(raw)

    def _search(self, fields: dict[str, Any]) -> Any:
        if not self.sid:
            self.login()
        raw, _ = self._request(Request(
            f"{self.base_url}/coremail/XT/jsp/mail.jsp?func=searchMessages&sid={self.sid}",
            data=urlencode(fields).encode("utf-8"),
            headers={
                "Accept": "text/x-json",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "User-Agent": "RecruitmentMailAgent/0.2",
            },
        ))
        return self._decode_response(raw)

    def _list_recent(self) -> list[dict[str, Any]]:
        value = self._search({
            "fid": 1,
            "start": 0,
            "limit": self.settings.coremail_lookup_limit,
            "summaryWindowSize": 0,
            # Coremail 高级搜索的明确日期范围格式为“开始:结束”。
            "receivedDate": f"{self.settings.since_date.isoformat()}:",
        })
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            for key in ("list", "messages", "mail"):
                if isinstance(value.get(key), list):
                    return [item for item in value[key] if isinstance(item, dict)]
        return []

    @staticmethod
    def _find_message(messages: list[dict[str, Any]], todo: TodoRequest) -> dict[str, Any] | None:
        subject = _normalize_subject(todo.subject)
        candidates = [
            item for item in messages
            if _normalize_subject(str(item.get("subject") or "")) == subject
        ]
        if not candidates:
            return None
        sender = todo.sender_address.lower()
        sender_matches = [item for item in candidates if sender in _addresses(item.get("from"))]
        candidates = sender_matches or candidates
        if len(candidates) == 1:
            return candidates[0]

        # 重复主题时选择与 IMAP 收信时间最接近的一封；无法解析日期则保留列表首项。
        try:
            target = datetime.fromisoformat(todo.received_at).timestamp()
        except ValueError:
            return candidates[0]

        def distance(item: dict[str, Any]) -> float:
            value = item.get("receivedDate") or item.get("date")
            if isinstance(value, (int, float)):
                timestamp = float(value) / (1000 if value > 10_000_000_000 else 1)
            else:
                try:
                    timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
                except (TypeError, ValueError):
                    return float("inf")
            return abs(timestamp - target)

        return min(candidates, key=distance)

    def create_todos(self, todos: list[TodoRequest]) -> set[str]:
        if not todos:
            return set()
        messages = self._list_recent()
        completed: set[str] = set()
        for todo in todos:
            message = self._find_message(messages, todo)
            coremail_id = message.get("id") if message else None
            if not coremail_id:
                continue
            due = todo.due_date.isoformat()
            payload = (
                '{"ids":[' + json.dumps(str(coremail_id), ensure_ascii=False) + '],'
                '"attrs":{"flags":{"deferHandle":true},'
                f'"defer":!!date \'{due} 00:00:00\''
                '},"returnOriginalMsgInfos":true}'
            )
            self._call("mbox:updateMessageInfos", payload)
            completed.add(todo.message_id)
        return completed
