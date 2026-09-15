from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autumn_tracker.coremail import TodoRequest
from autumn_tracker.calendar import CalendarEventRequest
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

    def test_calendar_event_queue_is_idempotent(self):
        event = CalendarEventRequest(
            event_key="event-key",
            source_message_id="m1",
            summary="技术面｜示例公司｜算法工程师",
            start_at="2099-09-20T14:00+08:00",
            end_at="2099-09-20T15:00+08:00",
            description="自动创建",
        )
        self.store.enqueue_calendar_event(event)
        self.assertEqual(self.store.pending_calendar_events(), [event])
        self.store.mark_calendar_events_done({"event-key": "event-id"})
        self.store.enqueue_calendar_event(event)
        self.assertEqual(self.store.pending_calendar_events(), [])

    def test_calendar_event_deduplicates_same_message_after_reclassification(self):
        first = CalendarEventRequest(
            event_key="old-key",
            source_message_id="same-message",
            summary="技术面｜公司简称｜算法工程师",
            start_at="2099-09-20T14:00+08:00",
            end_at="2099-09-20T15:00+08:00",
            description="自动创建",
        )
        renamed = CalendarEventRequest(
            event_key="new-key",
            source_message_id="same-message",
            summary="技术面｜公司完整名称｜算法工程师（Algorithm Engineer）",
            start_at=first.start_at,
            end_at=first.end_at,
            description="自动创建",
        )
        self.store.enqueue_calendar_event(first)
        self.store.mark_calendar_events_done({first.event_key: "event-id"})

        self.store.enqueue_calendar_event(renamed)

        count = self.store.connection.execute("SELECT COUNT(*) FROM calendar_event").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(self.store.pending_calendar_events(), [])

    def test_calendar_event_deduplicates_reminder_with_same_meeting_link(self):
        first = CalendarEventRequest(
            event_key="invite-key",
            source_message_id="invite-message",
            summary="技术面｜公司简称｜算法工程师",
            start_at="2099-09-20T14:00+08:00",
            end_at="2099-09-20T15:00+08:00",
            description="自动创建\n- 面试入口：https://meeting.example/join/abc",
        )
        reminder = CalendarEventRequest(
            event_key="reminder-key",
            source_message_id="reminder-message",
            summary="技术面｜公司完整名称｜算法工程师",
            start_at=first.start_at,
            end_at=first.end_at,
            description="面试提醒\n- 面试入口：https://meeting.example/join/abc",
        )
        self.store.enqueue_calendar_event(first)
        self.store.mark_calendar_events_done({first.event_key: "event-id"})

        self.store.enqueue_calendar_event(reminder)

        count = self.store.connection.execute("SELECT COUNT(*) FROM calendar_event").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(self.store.pending_calendar_events(), [])


if __name__ == "__main__":
    unittest.main()
