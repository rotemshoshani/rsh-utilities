import json
import os
import unittest

from herdr_stagger import herdr

from stubenv import StubHerdr


class HerdrWrapperTest(unittest.TestCase):
    def setUp(self):
        self.stub = StubHerdr(self)

    def test_agent_list_returns_the_servers_agents(self):
        self.stub.agents([{"pane_id": "wQ:p1", "agent": "codex"}])
        self.assertEqual(herdr.agent_list()[0]["pane_id"], "wQ:p1")

    def test_agent_status_reads_the_lifecycle_state(self):
        self.stub.status("wQ:p1", "blocked")
        self.assertEqual(herdr.agent_status("wQ:p1"), "blocked")

    def test_agent_status_is_none_when_the_agent_is_gone(self):
        """A vanished pane is an outcome, not an exception."""
        self.assertIsNone(herdr.agent_status("wQ:p9"))

    def test_agent_prompt_delivers_the_text(self):
        self.stub.status("wQ:p1", "idle")
        herdr.agent_prompt("wQ:p1", "go ahead")
        self.assertEqual(self.stub.prompts(), [("wQ:p1", "go ahead")])

    def test_a_server_error_carries_its_code(self):
        self.stub.prompt_error("wQ:p1", "agent_prompt_stalled")
        with self.assertRaises(herdr.HerdrError) as caught:
            herdr.agent_prompt("wQ:p1", "go ahead")
        self.assertEqual(caught.exception.code, "agent_prompt_stalled")

    def test_notification_failure_is_swallowed(self):
        """A missing notification must never take down a finished run."""
        self.stub.break_notifications()
        self.assertIsNone(herdr.notification_show("done", "3 sent"))

    def test_never_reaches_for_the_raw_pane_surface(self):
        self.stub.status("wQ:p1", "idle")
        herdr.agent_prompt("wQ:p1", "go ahead")
        self.assertNotIn("pane run", "\n".join(self.stub.calls()))
