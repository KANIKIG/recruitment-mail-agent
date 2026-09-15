import unittest
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from autumn_tracker.config import Settings
from autumn_tracker.lark import LarkRecord
from autumn_tracker.models import Classification, MailMessage
from autumn_tracker.sync import (
    _calendar_event_request,
    _company_key,
    _fields,
    _find_record,
    _record_index,
    _role_key,
    _todo_request,
    repair_flagged_interviews,
    should_replace_status,
)


class StatusPolicyTest(unittest.TestCase):
    def test_never_regress(self):
        self.assertFalse(should_replace_status("技术面", "投递", False))

    def test_advance(self):
        self.assertTrue(should_replace_status("测评&AI面", "技术面", False))

    def test_written_exam_is_separate_stage(self):
        self.assertTrue(should_replace_status("测评&AI面", "笔试", False))

    def test_self_scheduling_becomes_actionable_status(self):
        self.assertTrue(should_replace_status("技术面", "约面", False))

    def test_role_key_ignores_campus_recruitment_prefix(self):
        self.assertIn(
            _role_key("语音大模型算法工程师"),
            _role_key("【27届校招】语音大模型算法工程师（北京/上海）"),
        )

    def test_bilingual_role_matches_existing_short_chinese_role(self):
        classification = Classification(
            relevant=True,
            company="思朗科技",
            role="AI研究员（AI Research Scientist）",
            status="技术面",
            confidence=0.93,
            reason="面试时间已确认",
            source_key="different-key",
            interview_start="2099-09-15T10:00:00+08:00",
        )
        record = LarkRecord("rec1", {
            "公司名称": "思朗科技",
            "岗位名称": "AI研究员",
            "流程状态": ["技术面"],
        })

        matched = _find_record(classification, *_record_index([record]))

        self.assertEqual(_role_key(classification.role), _role_key("AI研究员"))
        self.assertEqual(matched, record)

    def test_full_company_name_matches_short_name(self):
        classification = Classification(
            relevant=True,
            company="智元创新（上海）科技股份有限公司",
            role="具身大模型算法工程师",
            status="技术面",
            confidence=0.95,
            reason="面试时间已确认",
            source_key="different-key",
            interview_start="2099-09-22T10:30:00+08:00",
        )
        record = LarkRecord("rec1", {
            "公司名称": "智元创新",
            "岗位名称": "具身大模型算法工程师",
            "流程状态": ["技术面"],
        })

        matched = _find_record(classification, *_record_index([record]))

        self.assertEqual(_company_key(classification.company), _company_key("智元创新"))
        self.assertEqual(matched, record)

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

    def test_fields_include_company_type(self):
        message = MailMessage(
            uid=1,
            message_id="m1",
            subject="投递成功",
            sender_name="招聘",
            sender_address="jobs@example.com",
            received_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
            body="",
        )
        result = Classification(
            relevant=True,
            company="示例公司",
            role="算法工程师",
            status="投递",
            confidence=0.9,
            reason="投递成功",
            source_key="key",
            company_type="民营企业",
        )
        self.assertEqual(_fields(message, result, timezone.utc)["企业类型"], "民营企业")

    def test_only_confirmed_human_interview_creates_calendar_event(self):
        message = MailMessage(
            uid=1,
            message_id="m1",
            subject="面试时间确认",
            sender_name="招聘",
            sender_address="jobs@example.com",
            received_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
            body="",
        )
        result = Classification(
            relevant=True,
            company="示例公司",
            role="算法工程师",
            status="技术面",
            confidence=0.9,
            reason="已确认面试时间",
            source_key="key",
            interview_start="2099-09-20T14:00:00+08:00",
        )
        event = _calendar_event_request(message, result, timezone.utc)
        self.assertIsNotNone(event)
        self.assertEqual(event.end_at, "2099-09-20T07:00+00:00")
        self.assertIsNone(_calendar_event_request(message, replace(result, status="约面"), timezone.utc))
        self.assertIsNone(_calendar_event_request(message, replace(result, status="测评&AI面"), timezone.utc))

    def test_fixed_written_exam_uses_exam_window(self):
        message = MailMessage(
            uid=1,
            message_id="m1",
            subject="统一笔试通知",
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
            deadline="2099-09-20T19:00:00+08:00",
            confidence=0.9,
            reason="固定笔试",
            source_key="key",
            written_exam_start="2099-09-20T19:00:00+08:00",
            written_exam_end="2099-09-20T21:00:00+08:00",
        )

        event = _calendar_event_request(message, result, timezone.utc)

        self.assertIsNotNone(event)
        self.assertEqual(event.summary, "笔试｜示例公司｜算法工程师")
        self.assertEqual(event.start_at, "2099-09-20T11:00+00:00")
        self.assertEqual(event.end_at, "2099-09-20T13:00+00:00")

    def test_flexible_written_exam_uses_deadline(self):
        message = MailMessage(
            uid=1,
            message_id="m1",
            subject="在线笔试邀请",
            sender_name="招聘",
            sender_address="jobs@example.com",
            received_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
            body="请在截止前自行完成",
        )
        result = Classification(
            relevant=True,
            company="示例公司",
            role="算法工程师",
            status="笔试",
            deadline="2099-09-20T22:00:00+08:00",
            confidence=0.9,
            reason="自行完成",
            source_key="key",
        )

        event = _calendar_event_request(message, result, timezone.utc)

        self.assertIsNotNone(event)
        self.assertEqual(event.summary, "笔试截止｜示例公司｜算法工程师")
        self.assertEqual(event.start_at, "2099-09-20T14:00+00:00")
        self.assertEqual(event.end_at, "2099-09-20T14:30+00:00")

    def test_repair_enqueues_calendar_even_when_table_record_is_unmatched(self):
        message = MailMessage(
            uid=1,
            message_id="m1",
            subject="视频面试邀约",
            sender_name="招聘",
            sender_address="jobs@example.com",
            received_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
            body="面试时间已确认",
        )
        result = Classification(
            relevant=True,
            company="示例公司",
            role="全新岗位",
            status="技术面",
            confidence=0.95,
            reason="面试时间已确认",
            source_key="unmatched-key",
            deadline="2099-09-20T14:00:00+08:00",
            interview_start="2099-09-20T14:00:00+08:00",
        )
        with TemporaryDirectory() as directory:
            settings = replace(
                Settings.from_env(require_targets=False, require_mail=False),
                email="user@example.com",
                password_value="test-password",
                deepseek_api_key="test-key",
                lark_base_token="test-base",
                lark_table_id="test-table",
                lark_calendar_enabled=True,
                coremail_todo_enabled=False,
                database_path=Path(directory) / "state.sqlite3",
            )
            captured = []

            def sync_calendar(_, state):
                captured.extend(state.pending_calendar_events())
                return len(captured), 0

            with (
                patch("autumn_tracker.sync.ImapMailbox") as mailbox_class,
                patch("autumn_tracker.sync.DeepSeekMailAgent") as agent_class,
                patch("autumn_tracker.sync.LarkBase") as lark_class,
                patch("autumn_tracker.sync.print"),
                patch("autumn_tracker.sync._sync_pending_todos", return_value=(0, 0)),
                patch("autumn_tracker.sync._sync_pending_calendar_events", side_effect=sync_calendar),
            ):
                mailbox_class.return_value.fetch_flagged.return_value = [message]
                agent_class.return_value.classify_batch.return_value = {message.message_id: result}
                lark_class.return_value.list_records.return_value = []

                stats = repair_flagged_interviews(settings)

        self.assertEqual(stats["unmatched_candidates"], 1)
        self.assertEqual(stats["calendar_eligible_unmatched"], 1)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].source_message_id, message.message_id)


if __name__ == "__main__":
    unittest.main()
