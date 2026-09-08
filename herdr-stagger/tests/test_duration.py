import unittest

from herdr_stagger.duration import DurationError, format_duration, parse_duration


class ParseDurationTest(unittest.TestCase):
    def test_accepts_bare_seconds(self):
        self.assertEqual(parse_duration("90"), 90)

    def test_accepts_unit_suffixes(self):
        self.assertEqual(parse_duration("90s"), 90)
        self.assertEqual(parse_duration("10m"), 600)
        self.assertEqual(parse_duration("2h"), 7200)

    def test_accepts_compound_suffixes(self):
        self.assertEqual(parse_duration("1h30m"), 5400)
        self.assertEqual(parse_duration("1h30m10s"), 5410)

    def test_accepts_slp_clock_forms(self):
        self.assertEqual(parse_duration("1:30"), 90)
        self.assertEqual(parse_duration("00:01:30"), 90)

    def test_rejects_garbage(self):
        for bad in ["", "   ", "abc", "10x", "-5", "1:70:00", "1:99"]:
            with self.assertRaises(DurationError, msg=bad):
                parse_duration(bad)

    def test_rejects_zero_and_the_ceiling(self):
        with self.assertRaises(DurationError):
            parse_duration("0")
        with self.assertRaises(DurationError):
            parse_duration("25h")


class FormatDurationTest(unittest.TestCase):
    def test_scales_units(self):
        self.assertEqual(format_duration(45), "45s")
        self.assertEqual(format_duration(150), "2m30s")
        self.assertEqual(format_duration(3912), "1h05m12s")


if __name__ == "__main__":
    unittest.main()


class AllowZeroTest(unittest.TestCase):
    def test_zero_is_rejected_by_default(self):
        with self.assertRaises(DurationError):
            parse_duration("0")

    def test_zero_is_allowed_when_a_delay_may_be_skipped(self):
        """`--start 0` means fire the first pane now."""
        self.assertEqual(parse_duration("0", allow_zero=True), 0)

    def test_zero_seconds_is_allowed_in_the_same_way(self):
        self.assertEqual(parse_duration("0s", allow_zero=True), 0)

    def test_negatives_are_still_impossible(self):
        with self.assertRaises(DurationError):
            parse_duration("-5m", allow_zero=True)
