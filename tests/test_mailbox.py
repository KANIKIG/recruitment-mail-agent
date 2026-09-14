from dataclasses import replace
from datetime import date
from email import policy
from email.parser import BytesParser
import unittest
from unittest.mock import patch

from autumn_tracker.config import Settings
from autumn_tracker.mailbox import ImapMailbox, _message_body


class FakeIMAP:
    instances: list["FakeIMAP"] = []

    def __init__(self, *args, **kwargs):
        self.calls: list[tuple] = []
        self.__class__.instances.append(self)

    def login(self, email, password):
        self.calls.append(("login", email, password))

    def select(self, folder, readonly):
        self.calls.append(("select", folder, readonly))
        return "OK", [b"1"]

    def uid(self, *args):
        self.calls.append(("uid", *args))
        return "OK", [b""]

    def logout(self):
        self.calls.append(("logout",))


class MailboxFlagTest(unittest.TestCase):
    def settings(self) -> Settings:
        return replace(
            Settings.from_env(require_targets=False, require_mail=False),
            email="user@example.com",
            password_value="test-password",
            since_date=date(2026, 8, 20),
        )

    @patch("autumn_tracker.mailbox.imaplib.IMAP4_SSL", FakeIMAP)
    def test_marks_unique_uids_without_changing_seen_flag(self):
        FakeIMAP.instances.clear()
        count = ImapMailbox(self.settings()).mark_flagged([7, 7, 9])
        self.assertEqual(count, 2)
        calls = FakeIMAP.instances[0].calls
        self.assertIn(("select", "INBOX", False), calls)
        self.assertIn(("uid", "store", "7", "+FLAGS.SILENT", r"(\Flagged)"), calls)
        self.assertIn(("uid", "store", "9", "+FLAGS.SILENT", r"(\Flagged)"), calls)

    @patch("autumn_tracker.mailbox.imaplib.IMAP4_SSL", FakeIMAP)
    def test_empty_list_does_not_connect(self):
        FakeIMAP.instances.clear()
        self.assertEqual(ImapMailbox(self.settings()).mark_flagged([]), 0)
        self.assertEqual(FakeIMAP.instances, [])

    @patch("autumn_tracker.mailbox.imaplib.IMAP4_SSL", FakeIMAP)
    def test_flagged_backfill_is_limited_by_start_date(self):
        FakeIMAP.instances.clear()
        self.assertEqual(ImapMailbox(self.settings()).fetch_flagged(), [])
        self.assertIn(
            ("uid", "search", None, "(FLAGGED SINCE 20-Aug-2026)"),
            FakeIMAP.instances[0].calls,
        )

    def test_html_and_calendar_preserve_invite_time_and_real_link(self):
        raw = b"""MIME-Version: 1.0
Content-Type: multipart/mixed; boundary=outer

--outer
Content-Type: multipart/alternative; boundary=inner

--inner
Content-Type: text/plain; charset=utf-8

Please join the interview.
--inner
Content-Type: text/html; charset=utf-8

<p>Please <a href=\"https://meeting.example/join/abc\">join</a>.</p>
--inner--
--outer
Content-Type: text/calendar; charset=utf-8

BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART:20260920T060000Z
DTEND:20260920T070000Z
LOCATION:Room 3
END:VEVENT
END:VCALENDAR
--outer--
"""
        message = BytesParser(policy=policy.default).parsebytes(raw)

        body = _message_body(message)

        self.assertLess(body.index("DTSTART"), body.index("Please"))
        self.assertIn("https://meeting.example/join/abc", body)
        self.assertIn("LOCATION:Room 3", body)


if __name__ == "__main__":
    unittest.main()
