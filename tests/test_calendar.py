from dataclasses import replace
import json
import unittest

from autumn_tracker.calendar import CalendarEventRequest, LarkCalendar
from autumn_tracker.config import Settings


class StubCalendar(LarkCalendar):
    def __init__(self):
        settings = replace(
            Settings.from_env(require_targets=False, require_mail=False),
            lark_calendar_enabled=True,
        )
        super().__init__(settings)
        self.arguments = None

    def check_connection(self):
        return "primary-calendar-id"

    def _run(self, arguments):
        self.arguments = arguments
        return {"ok": True, "data": {"event": {"event_id": "event-id"}}}


class LarkCalendarTest(unittest.TestCase):
    def test_create_uses_raw_api_without_feishu_vc(self):
        client = StubCalendar()
        request = CalendarEventRequest(
            event_key="key",
            source_message_id="m1",
            summary="技术面｜示例公司｜算法工程师",
            start_at="2099-09-20T14:00+08:00",
            end_at="2099-09-20T15:00+08:00",
            description="邮件自动创建",
        )
        self.assertEqual(client.create_events([request]), {"key": "event-id"})
        data = json.loads(client.arguments[client.arguments.index("--data") + 1])
        params = json.loads(client.arguments[client.arguments.index("--params") + 1])
        self.assertNotIn("vchat", data)
        self.assertEqual(data["reminders"], [{"minutes": 1440}, {"minutes": 30}])
        self.assertEqual(params["calendar_id"], "primary-calendar-id")


if __name__ == "__main__":
    unittest.main()
