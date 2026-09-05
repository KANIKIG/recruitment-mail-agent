from dataclasses import replace
from datetime import date
import unittest
from unittest.mock import patch

from autumn_tracker.config import Settings
from autumn_tracker.mailbox import ImapMailbox


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


if __name__ == "__main__":
    unittest.main()
