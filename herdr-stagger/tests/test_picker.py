import unittest

from herdr_stagger import picker

AGENTS = [
    {"pane_id": "wQ:p6M", "tab_id": "wQ:tH", "workspace_id": "wQ", "agent": "codex",
     "agent_status": "idle", "terminal_title_stripped": "Document template tags guide"},
    {"pane_id": "wQ:p64", "tab_id": "wQ:tC", "workspace_id": "wQ", "agent": "codex",
     "agent_status": "idle", "terminal_title_stripped": "Resume Priority 2 recovery"},
    {"pane_id": "wN:pV", "tab_id": "wN:tN", "workspace_id": "wN", "agent": "claude",
     "agent_status": "working", "terminal_title_stripped": "stagger runner"},
    {"pane_id": "wQ:p6S", "tab_id": "wQ:tM", "workspace_id": "wQ", "agent": "codex",
     "agent_status": "blocked", "terminal_title_stripped": "Add company meeting recording"},
]


class CandidatesTest(unittest.TestCase):
    def test_sorted_by_workspace_then_tab_then_pane(self):
        found = picker.candidates(AGENTS)
        self.assertEqual(
            [c["pane_id"] for c in found], ["wN:pV", "wQ:p64", "wQ:p6M", "wQ:p6S"]
        )

    def test_marks_the_calling_pane(self):
        found = picker.candidates(AGENTS, self_pane="wN:pV")
        marked = [c for c in found if c["is_self"]]
        self.assertEqual([c["pane_id"] for c in marked], ["wN:pV"])

    def test_workspace_filter_drops_other_workspaces(self):
        found = picker.candidates(AGENTS, workspace="wQ")
        self.assertEqual(
            [c["pane_id"] for c in found], ["wQ:p64", "wQ:p6M", "wQ:p6S"]
        )

    def test_title_falls_back_to_the_pane_id(self):
        found = picker.candidates([{"pane_id": "wQ:p1", "agent": "codex"}])
        self.assertEqual(found[0]["title"], "wQ:p1")


class ParseSelectionTest(unittest.TestCase):
    def setUp(self):
        self.found = picker.candidates(AGENTS, self_pane="wN:pV")

    def test_numbers_are_taken_in_fire_order_not_listed_order(self):
        picked = picker.parse_selection("4 2 3", self.found)
        self.assertEqual(
            [p["pane_id"] for p in picked], ["wQ:p6S", "wQ:p64", "wQ:p6M"]
        )

    def test_commas_separate_as_well_as_spaces(self):
        picked = picker.parse_selection("2,3", self.found)
        self.assertEqual([p["pane_id"] for p in picked], ["wQ:p64", "wQ:p6M"])

    def test_rejects_a_repeated_pane(self):
        with self.assertRaises(picker.PickerError):
            picker.parse_selection("2 3 2", self.found)

    def test_rejects_a_number_outside_the_table(self):
        with self.assertRaises(picker.PickerError):
            picker.parse_selection("2 9", self.found)

    def test_rejects_zero_because_the_table_is_one_based(self):
        with self.assertRaises(picker.PickerError):
            picker.parse_selection("0", self.found)

    def test_rejects_words(self):
        with self.assertRaises(picker.PickerError):
            picker.parse_selection("two", self.found)

    def test_rejects_an_empty_selection(self):
        with self.assertRaises(picker.PickerError):
            picker.parse_selection("   ", self.found)

    def test_rejects_the_calling_pane(self):
        """Prompting yourself mid-run is never the intent."""
        with self.assertRaises(picker.PickerError):
            picker.parse_selection("1", self.found)


class ParseGapsTest(unittest.TestCase):
    def test_one_value_fans_out_to_every_gap(self):
        self.assertEqual(picker.parse_gaps("20m", 3), [1200, 1200, 1200])

    def test_exact_count_maps_positionally(self):
        self.assertEqual(picker.parse_gaps("20m 45m", 2), [1200, 2700])

    def test_commas_separate_as_well_as_spaces(self):
        self.assertEqual(picker.parse_gaps("20m, 45m", 2), [1200, 2700])

    def test_rejects_a_count_that_is_neither_one_nor_exact(self):
        with self.assertRaises(picker.PickerError):
            picker.parse_gaps("20m 45m", 3)

    def test_says_what_it_expected_without_saying_it_twice(self):
        with self.assertRaises(picker.PickerError) as caught:
            picker.parse_gaps("20m 45m", 1)
        self.assertEqual(str(caught.exception), "expected 1 gap, got 2")

    def test_rejects_a_bad_duration(self):
        with self.assertRaises(picker.PickerError):
            picker.parse_gaps("soon", 2)

    def test_zero_means_back_to_back(self):
        """The operator may want two panes released together; that is their call."""
        self.assertEqual(picker.parse_gaps("0", 2), [0, 0])

    def test_a_single_pane_needs_no_gaps(self):
        self.assertEqual(picker.parse_gaps("20m", 0), [])

    def test_a_single_pane_accepts_an_empty_answer(self):
        self.assertEqual(picker.parse_gaps("", 0), [])


class BuildPlanTest(unittest.TestCase):
    def setUp(self):
        self.picked = picker.parse_selection("2 3 4", picker.candidates(AGENTS))

    def test_first_step_waits_the_start_delay(self):
        steps = picker.build_plan(self.picked, 300, [1200, 1200], "go")
        self.assertEqual(steps[0].after_seconds, 300)

    def test_later_steps_carry_their_own_gap(self):
        steps = picker.build_plan(self.picked, 300, [1200, 2700], "go")
        self.assertEqual([s.after_seconds for s in steps], [300, 1200, 2700])

    def test_every_step_carries_the_prompt_and_its_title(self):
        steps = picker.build_plan(self.picked, 300, [1200, 1200], "go ahead")
        self.assertEqual([s.prompt for s in steps], ["go ahead"] * 3)
        self.assertEqual(steps[0].title, "Resume Priority 2 recovery")

    def test_rejects_a_gap_count_that_does_not_match(self):
        with self.assertRaises(picker.PickerError):
            picker.build_plan(self.picked, 300, [1200], "go")
