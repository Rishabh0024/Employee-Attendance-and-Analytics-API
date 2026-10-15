"""Database-free regression tests for deterministic attendance rules."""
import unittest
from datetime import date, datetime, timedelta, timezone

from app.main import (
    UTC, EmployeeIn, PunchInIn, app, attendance_date_for,
    build_department_trend_pipeline, build_late_leaderboard_pipeline,
    compute_half_day, compute_late_minutes, compute_overtime,
    compute_work_hours, epoch_ms_to_datetime,
)


class TimeRuleTests(unittest.TestCase):
    def test_late_grace_boundary(self):
        day = date(2026, 7, 6)
        boundary = datetime(2026, 7, 6, 4, 10, tzinfo=UTC)
        self.assertEqual(compute_late_minutes(boundary, "09:30", day), 0)
        self.assertEqual(compute_late_minutes(boundary + timedelta(seconds=1), "09:30", day), 10)

    def test_late_minutes_from_shift_start(self):
        day = date(2026, 7, 6)
        punch = datetime(2026, 7, 6, 4, 45, 59, tzinfo=UTC)
        self.assertEqual(compute_late_minutes(punch, "09:30", day), 45)

    def test_half_up_work_hour_rounding(self):
        start = datetime(2026, 7, 6, 0, 0, tzinfo=UTC)
        self.assertEqual(compute_work_hours(start, start + timedelta(hours=9, minutes=7, seconds=30)), 9.13)

    def test_half_day_threshold(self):
        self.assertTrue(compute_half_day(4.49))
        self.assertFalse(compute_half_day(4.50))
        self.assertFalse(compute_half_day(None))

    def test_overtime_threshold(self):
        employee = {"shift_start": "09:30", "shift_end": "18:30"}
        day = date(2026, 7, 6)
        before = datetime(2026, 7, 6, 13, 29, 59, tzinfo=UTC)
        self.assertEqual(compute_overtime(before, employee, day), 0)
        self.assertEqual(compute_overtime(before + timedelta(seconds=1), employee, day), 30)

    def test_overnight_shift(self):
        employee = {"shift_start": "22:00", "shift_end": "06:00"}
        punch_in = datetime(2026, 7, 7, 0, 15, tzinfo=UTC)
        self.assertEqual(attendance_date_for(punch_in, employee), date(2026, 7, 6))
        punch_out = datetime(2026, 7, 7, 1, 0, tzinfo=UTC)
        self.assertEqual(compute_overtime(punch_out, employee, date(2026, 7, 6)), 30)

    def test_epoch_milliseconds_truncated_to_seconds(self):
        ms = int(datetime(2026, 7, 6, 4, 10, 0, 900000, tzinfo=UTC).timestamp() * 1000)
        result = epoch_ms_to_datetime(ms)
        self.assertEqual(result.microsecond, 0)
        self.assertEqual(result, datetime(2026, 7, 6, 4, 10, tzinfo=UTC))


class ValidationTests(unittest.TestCase):
    def test_shift_times_must_differ(self):
        with self.assertRaises(Exception):
            EmployeeIn(emp_code="EMP1234", name="Test User", email="test@example.com",
                       department="Engineering", shift_start="09:30", shift_end="09:30",
                       joined_on="2026-07-01")

    def test_joined_on_requires_date_string(self):
        with self.assertRaises(Exception):
            EmployeeIn(emp_code="EMP1234", name="Test User", email="test@example.com",
                       department="Engineering", joined_on=1783312500)

    def test_punch_timestamp_is_strict_epoch_ms(self):
        with self.assertRaises(Exception):
            PunchInIn(emp_code="EMP1234", punched_at=1783312500.0)
        with self.assertRaises(Exception):
            PunchInIn(emp_code="EMP1234", punched_at=None)


class PipelineTests(unittest.TestCase):
    def test_leaderboard_ranks_before_cutoff(self):
        pipeline = build_late_leaderboard_pipeline("2026-07", limit=2)
        names = [next(iter(stage)) for stage in pipeline]
        rank_index = names.index("$setWindowFields")
        self.assertLess(rank_index, names.index("$match", rank_index))
        rank_stage = next(s["$setWindowFields"] for s in pipeline if "$setWindowFields" in s)
        self.assertIn("$rank", rank_stage["output"]["rank"])

    def test_trend_uses_database_gap_fill_and_window(self):
        pipeline = build_department_trend_pipeline("Engineering", date(2026, 7, 1), date(2026, 7, 7))
        names = [next(iter(stage)) for stage in pipeline]
        self.assertIn("$densify", names)
        self.assertIn("$setWindowFields", names)
        self.assertIn("$lookup", names)


class ContractTests(unittest.TestCase):
    def test_all_twelve_operations_are_registered(self):
        schema = app.openapi()
        operations = {(method, path) for path, methods in schema["paths"].items() for method in methods}
        expected = {
            ("get", "/health"), ("post", "/employees"), ("get", "/employees"),
            ("post", "/attendance/punch-in"), ("get", "/attendance"),
            ("post", "/attendance/punch-out"), ("patch", "/attendance/{emp_code}/{date}"),
            ("get", "/analytics/employees/{emp_code}/monthly"),
            ("get", "/analytics/departments/summary"),
            ("get", "/analytics/leaderboard/late"),
            ("get", "/analytics/departments/{department}/trend"),
            ("get", "/admin/explain/{endpoint}"),
        }
        self.assertEqual(operations, expected)
        self.assertEqual(schema["openapi"], "3.0.3")


if __name__ == "__main__":
    unittest.main()
