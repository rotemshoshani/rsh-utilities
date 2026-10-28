import unittest

from codex_delay.launcher import ScheduleRequest, codex_arguments


class LauncherTests(unittest.TestCase):
    def test_resumes_selected_session_with_prompt(self):
        request = ScheduleRequest("00:01:30", "fix this button layout", "thread-id", "ui tweaks")
        self.assertEqual(
            codex_arguments(request),
            ["cdx", "resume", "thread-id", "fix this button layout"],
        )

    def test_starts_fresh_session_with_prompt(self):
        request = ScheduleRequest("00:01:30", "fix this button layout", None, "Fresh session")
        self.assertEqual(codex_arguments(request), ["cdx", "fix this button layout"])


if __name__ == "__main__":
    unittest.main()
