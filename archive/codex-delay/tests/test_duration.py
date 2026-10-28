import unittest

from codex_delay.duration import format_duration, parse_duration


class DurationTests(unittest.TestCase):
    def test_accepts_all_slp_forms(self):
        self.assertEqual(parse_duration("90"), 90)
        self.assertEqual(parse_duration("01:30"), 90)
        self.assertEqual(parse_duration("00:01:30"), 90)

    def test_normalizes_duration(self):
        self.assertEqual(format_duration(parse_duration("90")), "00:01:30")

    def test_rejects_invalid_duration(self):
        for value in ("", "soon", "1:2:3:4", "1::2"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_duration(value)


if __name__ == "__main__":
    unittest.main()
