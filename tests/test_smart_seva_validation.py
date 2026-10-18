import math
import unittest
from datetime import date

from smart_seva_validation import bounded_integer, finite_number, hhmm_time, iso_date


class FiniteNumberTests(unittest.TestCase):
    def test_accepts_valid_boundary_values(self):
        self.assertEqual(finite_number("0", minimum=0), 0.0)
        self.assertEqual(finite_number("10000", maximum=10000), 10000.0)

    def test_rejects_nan_and_infinity(self):
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    finite_number(value)

    def test_rejects_out_of_range_values(self):
        with self.assertRaises(ValueError):
            finite_number("-1", minimum=0)
        with self.assertRaises(ValueError):
            finite_number("100000000", maximum=10000)


class BoundedIntegerTests(unittest.TestCase):
    def test_accepts_valid_integer(self):
        self.assertEqual(bounded_integer("1000", minimum=0, maximum=1000), 1000)

    def test_rejects_decimal_and_huge_values(self):
        for value in ("1.5", "1001", "NaN", ""):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    bounded_integer(value, minimum=0, maximum=1000)


class DateAndTimeTests(unittest.TestCase):
    def test_accepts_iso_date_and_hhmm(self):
        self.assertEqual(iso_date("2026-10-17"), date(2026, 10, 17))
        self.assertEqual(hhmm_time("09:30").strftime("%H:%M"), "09:30")

    def test_rejects_invalid_dates_and_times(self):
        for value in ("2026-02-30", "17-10-2026", ""):
            with self.subTest(date=value):
                with self.assertRaises(ValueError):
                    iso_date(value)
        for value in ("25:00", "9:30", "noon", ""):
            with self.subTest(time=value):
                with self.assertRaises(ValueError):
                    hhmm_time(value)


if __name__ == "__main__":
    unittest.main()
