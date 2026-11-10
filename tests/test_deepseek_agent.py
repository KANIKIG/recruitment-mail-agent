from dataclasses import replace
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from autumn_tracker.config import Settings
from autumn_tracker.deepseek_agent import OpenAICompatibleMailAgent
from autumn_tracker.models import Classification, MailMessage


class StubAgent(OpenAICompatibleMailAgent):
    def __init__(self, settings: Settings, response: dict):
        super().__init__(settings)
        self.response = response

    def _request(self, payload: dict) -> dict:
        self.payload = payload
        return self.response


class FakeHttpResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return b'{"choices":[]}'


class OpenAICompatibleAgentTest(unittest.TestCase):
    def settings(self) -> Settings:
        return replace(
            Settings.from_env(require_targets=False, require_mail=False),
            llm_api_key="test-key",
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
        response = {"choices": [{"message": {"content": """{"items":[{"index":0,"is_recruitment":true,"company_name":"示例公司","job_name":"算法工程师","enterprise_type":"央国企","process_status":"测评&AI面","deadline":"2026-09-08T18:00:00+08:00","confidence":0.96,"evidence":"测评通知"}]}"""}}]}
        result = StubAgent(self.settings(), response).classify_batch([self.message()])["m1"]
        self.assertTrue(result.relevant)
        self.assertEqual(result.status, "测评&AI面")
        self.assertEqual(result.deadline, "2026-09-08T18:00+08:00")
        self.assertEqual(result.company_type, "央国企")

    def test_generic_provider_payload_has_no_deepseek_only_fields(self):
        response = {"choices": [{"message": {"content": """{"items":[{"index":0,"is_recruitment":true,"company_name":"示例公司","job_name":"算法工程师","process_status":"投递","deadline":null,"confidence":0.9,"evidence":"申请成功"}]}"""}}]}
        settings = replace(
            self.settings(),
            llm_base_url="https://llm.example.com/v1",
            llm_chat_completions_url=None,
            llm_extra_body={},
            llm_json_mode=False,
            llm_system_role="developer",
            llm_max_tokens_param="max_completion_tokens",
        )
        agent = StubAgent(settings, response)

        agent.classify_batch([self.message()])

        self.assertEqual(agent.payload["model"], settings.llm_model)
        self.assertEqual(agent.payload["messages"][0]["role"], "developer")
        self.assertIn("max_completion_tokens", agent.payload)
        self.assertNotIn("max_tokens", agent.payload)
        self.assertNotIn("response_format", agent.payload)
        self.assertNotIn("thinking", agent.payload)

    def test_request_uses_configured_openai_compatible_endpoint_and_headers(self):
        settings = replace(
            self.settings(),
            llm_api_key="provider-key",
            llm_base_url="https://llm.example.com/v1",
            llm_chat_completions_url="https://proxy.example.com/custom/chat/completions",
            llm_extra_headers={"X-Provider": "example"},
            llm_timeout_seconds=45,
        )
        agent = OpenAICompatibleMailAgent(settings)

        with patch("autumn_tracker.deepseek_agent.urlopen", return_value=FakeHttpResponse()) as mocked:
            agent._request({"model": "provider-model", "messages": []})

        request = mocked.call_args.args[0]
        self.assertEqual(request.full_url, "https://proxy.example.com/custom/chat/completions")
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers["authorization"], "Bearer provider-key")
        self.assertEqual(headers["x-provider"], "example")
        self.assertEqual(mocked.call_args.kwargs["timeout"], 45)

    def test_company_type_batch_is_strict(self):
        response = {"choices": [{"message": {"content": """{"items":[{"index":0,"enterprise_type":"民营企业"},{"index":1,"enterprise_type":"外企"}]}"""}}]}
        result = StubAgent(self.settings(), response).classify_company_types(["甲公司", "乙公司"])
        self.assertEqual(result, {"甲公司": "民营企业", "乙公司": "外企"})

    def test_invalid_company_type_is_not_written(self):
        response = {"choices": [{"message": {"content": """{"items":[{"index":0,"is_recruitment":true,"company_name":"示例公司","job_name":"算法工程师","enterprise_type":"未知类型","process_status":"投递","deadline":null,"confidence":0.9,"evidence":"投递成功"}]}"""}}]}
        result = StubAgent(self.settings(), response).classify_batch([self.message()])["m1"]
        self.assertIsNone(result.company_type)

    def test_self_scheduling_cannot_become_confirmed_interview(self):
        response = {"choices": [{"message": {"content": """{"items":[{"index":0,"is_recruitment":true,"company_name":"示例公司","job_name":"算法工程师","enterprise_type":"民营企业","process_status":"约面","deadline":"2026-09-18T18:00:00+08:00","interview_start":"2026-09-20T14:00:00+08:00","interview_end":"2026-09-20T15:00:00+08:00","meeting_link":"https://example.com/booking","interview_location":null,"confidence":0.98,"evidence":"请自行选择面试时间"}]}"""}}]}
        result = StubAgent(self.settings(), response).classify_batch([self.message()])["m1"]
        self.assertEqual(result.status, "约面")
        self.assertEqual(result.deadline, "2026-09-18T18:00+08:00")
        self.assertIsNone(result.interview_start)
        self.assertIsNone(result.meeting_link)

    def test_ai_interview_cannot_become_calendar_interview(self):
        response = {"choices": [{"message": {"content": """{"items":[{"index":0,"is_recruitment":true,"company_name":"示例公司","job_name":"算法工程师","enterprise_type":"民营企业","process_status":"测评&AI面","deadline":"2026-09-18T18:00:00+08:00","interview_start":"2026-09-18T18:00:00+08:00","interview_end":null,"meeting_link":"https://example.com/ai","interview_location":null,"confidence":0.98,"evidence":"AI面试"}]}"""}}]}
        result = StubAgent(self.settings(), response).classify_batch([self.message()])["m1"]
        self.assertEqual(result.status, "测评&AI面")
        self.assertIsNone(result.interview_start)

    def test_confirmed_interview_keeps_calendar_fields(self):
        response = {"choices": [{"message": {"content": """{"items":[{"index":0,"is_recruitment":true,"company_name":"示例公司","job_name":"算法工程师","enterprise_type":"民营企业","process_status":"技术面","deadline":null,"interview_start":"2099-09-20T14:00:00+08:00","interview_end":"2099-09-20T14:45:00+08:00","meeting_link":"https://meeting.example.com/join","interview_location":null,"confidence":0.98,"evidence":"面试时间已确认"}]}"""}}]}
        result = StubAgent(self.settings(), response).classify_batch([self.message()])["m1"]
        self.assertEqual(result.interview_start, "2099-09-20T14:00+08:00")
        self.assertEqual(result.interview_end, "2099-09-20T14:45+08:00")
        self.assertEqual(result.deadline, result.interview_start)

    def test_fixed_written_exam_keeps_exam_window(self):
        response = {"choices": [{"message": {"content": """{"items":[{"index":0,"is_recruitment":true,"company_name":"示例公司","job_name":"算法工程师","enterprise_type":"民营企业","process_status":"笔试","deadline":"2099-09-20T22:00:00+08:00","written_exam_start":"2099-09-20T19:00:00+08:00","written_exam_end":"2099-09-20T21:00:00+08:00","confidence":0.98,"evidence":"统一笔试时间"}]}"""}}]}
        result = StubAgent(self.settings(), response).classify_batch([self.message()])["m1"]
        self.assertEqual(result.written_exam_start, "2099-09-20T19:00+08:00")
        self.assertEqual(result.written_exam_end, "2099-09-20T21:00+08:00")
        self.assertEqual(result.deadline, result.written_exam_start)

    def test_flexible_written_exam_uses_only_deadline(self):
        response = {"choices": [{"message": {"content": """{"items":[{"index":0,"is_recruitment":true,"company_name":"示例公司","job_name":"算法工程师","enterprise_type":"民营企业","process_status":"笔试","deadline":"2099-09-20T22:00:00+08:00","written_exam_start":null,"written_exam_end":"2099-09-20T21:00:00+08:00","confidence":0.98,"evidence":"截止前自行完成"}]}"""}}]}
        result = StubAgent(self.settings(), response).classify_batch([self.message()])["m1"]
        self.assertEqual(result.deadline, "2099-09-20T22:00+08:00")
        self.assertIsNone(result.written_exam_start)
        self.assertIsNone(result.written_exam_end)

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

    def test_json_code_fence_is_tolerated(self):
        response = {"choices": [{"message": {"content": """```json
{"items":[{"index":0,"is_recruitment":true,"company_name":"示例公司","job_name":"算法工程师","process_status":"投递","deadline":null,"confidence":0.9,"evidence":"投递成功"}]}
```"""}}]}
        result = StubAgent(self.settings(), response).classify_batch([self.message()])["m1"]
        self.assertEqual(result.status, "投递")

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

    def test_campus_recruitment_webinar_is_not_an_application(self):
        response = {"choices": [{"message": {"content": """{"items":[{"index":0,"is_recruitment":true,"company_name":"示例银行","job_name":"待确认岗位","process_status":"投递","deadline":null,"confidence":0.9,"evidence":"校园招聘"}]}"""}}]}
        message = MailMessage(
            4,
            "m4",
            "【空宣邀请函】示例银行校园招聘",
            "招聘系统",
            "notice@example.com",
            datetime(2026, 9, 5, tzinfo=timezone.utc),
            "欢迎参加线上宣讲会。",
        )

        result = StubAgent(self.settings(), response).classify_batch([message])["m4"]

        self.assertFalse(result.relevant)

    def test_interview_satisfaction_survey_is_not_a_new_process_event(self):
        response = {"choices": [{"message": {"content": """{"items":[{"index":0,"is_recruitment":true,"company_name":"示例公司","job_name":"待确认岗位","process_status":"技术面","deadline":null,"confidence":0.9,"evidence":"面试后问卷"}]}"""}}]}
        message = MailMessage(
            5,
            "m5",
            "示例公司面试满意度问卷",
            "招聘系统",
            "notice@example.com",
            datetime(2026, 9, 5, tzinfo=timezone.utc),
            "感谢参加面试，请评价面试体验。",
        )

        result = StubAgent(self.settings(), response).classify_batch([message])["m5"]

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

    def test_relative_hours_are_repaired_from_received_time(self):
        message = self.message()
        message = replace(message, body="请务必认真作答，并在72小时内完成。")
        classification = Classification(
            relevant=True,
            company="示例公司",
            role="算法工程师",
            status="测评&AI面",
            confidence=0.9,
            reason="测评邀请",
            source_key="key",
        )
        repaired = OpenAICompatibleMailAgent(self.settings()).repair_missing_deadline(message, classification)
        self.assertEqual(repaired.deadline, "2026-09-08T08:00+08:00")

    def test_link_validity_days_are_repaired(self):
        message = self.message()
        message = replace(message, body="首次进入后开始考试，链接有效期 7天。")
        classification = Classification(
            relevant=True,
            company="示例公司",
            role="算法工程师",
            status="测评&AI面",
            confidence=0.9,
            reason="AI 面试邀请",
            source_key="key",
        )
        repaired = OpenAICompatibleMailAgent(self.settings()).repair_missing_deadline(message, classification)
        self.assertEqual(repaired.deadline, "2026-09-12T08:00+08:00")

    def test_english_assessment_deadlines_use_earliest_relative_limit(self):
        message = replace(
            self.message(),
            received_at=datetime(2026, 9, 26, 12, 25, tzinfo=timezone.utc),
            subject="P&G - Invitation to Online Assessment",
            body=(
                "Please complete your online assessment within 2 days. "
                "Please note that the assessment link will expire in 14 days. "
                "Finish the online assessment by whichever of these dates is earlier."
            ),
        )
        classification = Classification(
            relevant=True,
            company="宝洁",
            role="Product Supply Manager",
            status="测评&AI面",
            confidence=0.95,
            reason="Online assessment invitation",
            source_key="key",
        )

        repaired = OpenAICompatibleMailAgent(self.settings()).repair_missing_deadline(message, classification)

        self.assertEqual(repaired.deadline, "2026-09-28T20:25+08:00")

    def test_unrelated_english_response_sla_is_not_a_deadline(self):
        message = replace(
            self.message(),
            body="For questions, customer service usually responds within 1 day.",
        )
        classification = Classification(
            relevant=True,
            company="示例公司",
            role="算法工程师",
            status="测评&AI面",
            confidence=0.9,
            reason="Online assessment invitation",
            source_key="key",
        )

        repaired = OpenAICompatibleMailAgent(self.settings()).repair_missing_deadline(message, classification)

        self.assertIsNone(repaired.deadline)


if __name__ == "__main__":
    unittest.main()
