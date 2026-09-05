import unittest
from datetime import datetime, timezone
from dataclasses import replace

from autumn_tracker.config import Settings
from autumn_tracker.models import Classification, MailMessage
from autumn_tracker.sync import _role_key, _todo_request, should_replace_status


class StatusPolicyTest(unittest.TestCase):
    def test_never_regress(self):
        self.assertFalse(should_replace_status("技术面", "投递", False))

    def test_advance(self):
        self.assertTrue(should_replace_status("测评&AI面", "技术面", False))

    def test_written_exam_is_separate_stage(self):
        self.assertTrue(should_replace_status("测评&AI面", "笔试", False))

    def test_role_key_ignores_campus_recruitment_prefix(self):
        self.assertIn(
            _role_key("语音大模型算法工程师"),
            _role_key("【27届校招】语音大模型算法工程师（北京/上海）"),
        )

    def test_manual_lock(self):
        self.assertFalse(should_replace_status("投递", "Offer", True))

    def test_terminal_is_stable(self):
        self.assertFalse(should_replace_status("已挂", "技术面", False))

    def test_todo_requires_deadline_stage_and_future_date(self):
        message = MailMessage(
            uid=1,
            message_id="m1",
            subject="笔试邀请",
            sender_name="招聘",
            sender_address="jobs@example.com",
            received_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
            body="",
        )
        result = Classification(
            relevant=True,
            company="示例公司",
            role="算法工程师",
            status="笔试",
            deadline="2099-09-08T19:00:00+08:00",
            confidence=0.9,
            reason="笔试通知",
            source_key="示例公司|算法工程师",
        )
        todo = _todo_request(message, result, timezone.utc)
        self.assertEqual(todo.due_date.isoformat(), "2099-09-08")
        no_deadline = replace(result, deadline=None)
        self.assertIsNone(_todo_request(message, no_deadline, timezone.utc))
        application = replace(result, status="投递")
        self.assertIsNone(_todo_request(message, application, timezone.utc))


if __name__ == "__main__":
    unittest.main()
