from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autumn_tracker.coremail import TodoRequest
from autumn_tracker.state import StateStore


class TodoQueueTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = TemporaryDirectory()
        self.store = StateStore(Path(self.tempdir.name) / "state.sqlite3")
        self.todo = TodoRequest(
            message_id="m1",
            subject="笔试邀请",
            sender_address="jobs@example.com",
            received_at="2026-09-05T10:00:00+08:00",
            due_date=date(2026, 9, 8),
        )

    def tearDown(self):
        self.store.close()
        self.tempdir.cleanup()

    def test_failed_todo_stays_pending_without_agent_state(self):
        self.store.enqueue_todo(self.todo)
        self.store.mark_todos_failed({"m1"}, "temporary failure")

        self.assertEqual(self.store.pending_todos(), [self.todo])
        row = self.store.connection.execute(
            "SELECT attempts, last_error FROM mail_todo WHERE message_id='m1'"
        ).fetchone()
        self.assertEqual(row, (1, "temporary failure"))

    def test_completed_todo_leaves_pending_queue(self):
        self.store.enqueue_todo(self.todo)
        self.store.mark_todos_done({"m1"})
        self.assertEqual(self.store.pending_todos(), [])

    def test_reenqueue_same_deadline_is_idempotent(self):
        self.store.enqueue_todo(self.todo)
        self.store.mark_todos_done({"m1"})
        self.store.enqueue_todo(self.todo)
        self.assertEqual(self.store.pending_todos(), [])


if __name__ == "__main__":
    unittest.main()
