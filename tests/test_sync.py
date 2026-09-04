import unittest

from autumn_tracker.sync import _role_key, should_replace_status


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


if __name__ == "__main__":
    unittest.main()
