from dataclasses import replace
from datetime import datetime, timezone
import unittest

from autumn_tracker.config import Settings
from autumn_tracker.deepseek_agent import DeepSeekMailAgent
from autumn_tracker.models import MailMessage


class StubAgent(DeepSeekMailAgent):
    def __init__(self, settings: Settings, response: dict):
        super().__init__(settings)
        self.response = response

    def _request(self, payload: dict) -> dict:
        self.payload = payload
        return self.response


class DeepSeekAgentTest(unittest.TestCase):
    def settings(self) -> Settings:
        return replace(
            Settings.from_env(require_targets=False, require_mail=False),
            deepseek_api_key="test-key",
        )

    def message(self) -> MailMessage:
        return MailMessage(
            1,
            "m1",
            "测评通知",
            "示例公司招聘",
            "hr@example.com",
            datetime(2026, 9, 5, tzinfo=timezone.utc),
            "请在 9 月 8 日完成测评。",
        )

    def test_structured_result_and_deadline(self):
        response = {"choices": [{"message": {"content": """{"items":[{"index":0,"is_recruitment":true,"company_name":"示例公司","job_name":"算法工程师","process_status":"测评&AI面","deadline":"2026-09-08T18:00:00+08:00","confidence":0.96,"evidence":"测评通知"}]}"""}}]}
        result = StubAgent(self.settings(), response).classify_batch([self.message()])["m1"]
        self.assertTrue(result.relevant)
        self.assertEqual(result.status, "测评&AI面")
        self.assertEqual(result.deadline, "2026-09-08T18:00+08:00")

    def test_unknown_status_is_downgraded(self):
        response = {"choices": [{"message": {"content": """{"items":[{"index":0,"is_recruitment":true,"company_name":"示例公司","job_name":"算法工程师","process_status":"三面","deadline":null,"confidence":2,"evidence":"通知"}]}"""}}]}
        result = StubAgent(self.settings(), response).classify_batch([self.message()])["m1"]
        self.assertEqual(result.status, "待确认")
        self.assertEqual(result.confidence, 1.0)
        self.assertIsNone(result.deadline)

    def test_missing_index_fails_closed(self):
        response = {"choices": [{"message": {"content": '{"items":[]}'}}]}
        with self.assertRaises(RuntimeError):
            StubAgent(self.settings(), response).classify_batch([self.message()])

    def test_job_recommendation_is_not_an_application(self):
        response = {"choices": [{"message": {"content": """{"items":[{"index":0,"is_recruitment":true,"company_name":"某公司","job_name":"算法工程师","process_status":"投递","deadline":null,"confidence":0.9,"evidence":"推荐"}]}"""}}]}
        message = MailMessage(
            2,
            "m2",
            "尊敬的同学【智联推荐】算法岗位好岗推荐",
            "智联招聘",
            "notice@example.com",
            datetime(2026, 9, 5, tzinfo=timezone.utc),
            "点击查看并投递职位。",
        )
        result = StubAgent(self.settings(), response).classify_batch([message])["m2"]
        self.assertFalse(result.relevant)

    def test_submission_failure_is_not_a_rejection(self):
        response = {"choices": [{"message": {"content": """{"items":[{"index":0,"is_recruitment":true,"company_name":"滴滴","job_name":"校园大使","process_status":"已挂","deadline":null,"confidence":0.9,"evidence":"投递失败"}]}"""}}]}
        message = MailMessage(
            3, "m3", "投递失败2026-08-27", "招聘系统", "notice@example.com",
            datetime(2026, 9, 5, tzinfo=timezone.utc), "岗位提交失败。",
        )
        result = StubAgent(self.settings(), response).classify_batch([message])["m3"]
        self.assertFalse(result.relevant)

    def test_non_process_deadline_is_discarded(self):
        response = {"choices": [{"message": {"content": """{"items":[{"index":0,"is_recruitment":true,"company_name":"汇川技术","job_name":"算法工程师","process_status":"待确认","deadline":"2026-09-08T18:00:00+08:00","confidence":0.9,"evidence":"更新简历"}]}"""}}]}
        result = StubAgent(self.settings(), response).classify_batch([self.message()])["m1"]
        self.assertIsNone(result.deadline)


if __name__ == "__main__":
    unittest.main()
