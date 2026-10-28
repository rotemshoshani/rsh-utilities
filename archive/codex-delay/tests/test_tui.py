import unittest

from codex_delay.tui import (
    DELAY_FOCUS,
    PROMPT_FOCUS,
    _Choice,
    _Dashboard,
    _build_request,
    _prompt_preview,
)


class DashboardTests(unittest.TestCase):
    def test_builds_fresh_session_request_from_dashboard(self):
        state = _Dashboard(
            choices=[_Choice(None)],
            prompt="fix this button layout\nwithout changing behavior",
            duration="01:30",
        )

        request = _build_request(state)

        self.assertIsNotNone(request)
        self.assertEqual(request.duration, "00:01:30")
        self.assertEqual(request.prompt, "fix this button layout\nwithout changing behavior")
        self.assertIsNone(request.session_id)

    def test_missing_prompt_returns_focus_to_prompt(self):
        state = _Dashboard(choices=[_Choice(None)], prompt="", duration="01:30", focus=DELAY_FOCUS)

        self.assertIsNone(_build_request(state))
        self.assertEqual(state.focus, PROMPT_FOCUS)

    def test_invalid_delay_returns_focus_to_delay(self):
        state = _Dashboard(choices=[_Choice(None)], prompt="do the work", duration="later")

        self.assertIsNone(_build_request(state))
        self.assertEqual(state.focus, DELAY_FOCUS)

    def test_prompt_preview_wraps_and_limits_lines(self):
        preview = _prompt_preview("one two three four five six seven", width=10, limit=2)

        self.assertEqual(len(preview), 2)
        self.assertTrue(preview[-1].endswith("…"))


if __name__ == "__main__":
    unittest.main()
