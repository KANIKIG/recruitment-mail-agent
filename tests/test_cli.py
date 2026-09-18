import unittest

from autumn_tracker.cli import AUTOSTART_RUNTIME_ROOT, LAUNCH_AGENT_LABEL, _launch_agent_definition


class LaunchAgentTest(unittest.TestCase):
    def test_launch_agent_only_starts_project_watcher(self):
        definition = _launch_agent_definition(300)

        self.assertEqual(definition["Label"], LAUNCH_AGENT_LABEL)
        arguments = definition["ProgramArguments"]
        self.assertTrue(str(arguments[0]).endswith("python3"))
        self.assertEqual(arguments[1:], ["-m", "autumn_tracker.cli", "watch", "--interval", "300"])
        self.assertEqual(
            definition["EnvironmentVariables"]["PYTHONPATH"],
            str(AUTOSTART_RUNTIME_ROOT / "src"),
        )
        self.assertEqual(
            definition["EnvironmentVariables"]["LARK_CLI"],
            str(AUTOSTART_RUNTIME_ROOT / "node_modules" / ".bin" / "lark-cli"),
        )
        self.assertIn("/usr/bin", definition["EnvironmentVariables"]["PATH"].split(":"))
        self.assertNotIn("WorkingDirectory", definition)
        self.assertTrue(definition["RunAtLoad"])
        self.assertEqual(definition["KeepAlive"], {"SuccessfulExit": False})
        serialized = repr(definition)
        self.assertNotIn("IMAP_PASSWORD", serialized)
        self.assertNotIn("DEEPSEEK_API_KEY", serialized)


if __name__ == "__main__":
    unittest.main()
