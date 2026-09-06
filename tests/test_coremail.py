from dataclasses import replace
from datetime import date
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import Request

from autumn_tracker.config import Settings
from autumn_tracker.coremail import CoremailTodoClient, TodoRequest


class StubCoremail(CoremailTodoClient):
    def __init__(self, messages):
        settings = replace(
            Settings.from_env(require_targets=False, require_mail=False),
            coremail_todo_enabled=True,
            coremail_web_url="https://mail.example.com",
        )
        super().__init__(settings)
        self.messages = messages
        self.calls: list[tuple[str, str]] = []

    def _list_recent(self):
        return self.messages

    def _call(self, function: str, payload: str):
        self.calls.append((function, payload))
        return []


class CoremailTodoTest(unittest.TestCase):
    def todo(self) -> TodoRequest:
        return TodoRequest(
            message_id="m1",
            subject=" 在线  笔试邀请 ",
            sender_address="jobs@example.com",
            received_at="2026-09-05T10:00:00+08:00",
            due_date=date(2026, 9, 8),
        )

    def test_creates_native_defer_on_matching_message(self):
        client = StubCoremail([{
            "id": "coremail-id",
            "subject": "在线 笔试邀请",
            "from": [{"address": "jobs@example.com"}],
        }])
        self.assertEqual(client.create_todos([self.todo()]), {"m1"})
        function, payload = client.calls[0]
        self.assertEqual(function, "mbox:updateMessageInfos")
        self.assertIn('"deferHandle":true', payload)
        self.assertIn("!!date '2026-09-08 00:00:00'", payload)
        self.assertIn('"coremail-id"', payload)

    def test_does_not_update_when_source_message_is_missing(self):
        client = StubCoremail([])
        self.assertEqual(client.create_todos([self.todo()]), set())
        self.assertEqual(client.calls, [])

    def test_sender_disambiguates_duplicate_subjects(self):
        messages = [
            {"id": "wrong", "subject": "在线 笔试邀请", "from": "other@example.com"},
            {"id": "right", "subject": "在线 笔试邀请", "from": "jobs@example.com"},
        ]
        match = CoremailTodoClient._find_message(messages, self.todo())
        self.assertEqual(match["id"], "right")

    def test_transient_network_error_is_retried(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"ok"

            def geturl(self):
                return "https://mail.example.com/ok"

        class Opener:
            def __init__(self):
                self.calls = 0

            def open(self, request, timeout):
                self.calls += 1
                if self.calls < 3:
                    raise URLError("temporary EOF")
                return Response()

        opener = Opener()
        client = StubCoremail([])
        client.opener = opener
        with patch("autumn_tracker.coremail.time.sleep"):
            body, url = client._request(Request("https://mail.example.com"))
        self.assertEqual((body, url), ("ok", "https://mail.example.com/ok"))
        self.assertEqual(opener.calls, 3)


if __name__ == "__main__":
    unittest.main()
