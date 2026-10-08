"""Scheduling: natural language parsing, cron, recurrence and persistence."""
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from tests._support import ROOT  # noqa: F401
from ufo.core.scheduler import (Scheduler, next_cron, parse_cron, parse_repeat,
                                parse_when)
from ufo.core.task import TaskPriority
from ufo.errors import SchedulerError


class TestDurations(unittest.TestCase):
    def test_relative_units(self):
        for text, seconds in (("in 30s", 30), ("in 5m", 300), ("in 2h", 7200),
                              ("in 1d", 86400), ("in 1w", 604800)):
            self.assertAlmostEqual(parse_when(text) - time.time(), seconds, delta=5)

    def test_repeat_units(self):
        self.assertEqual(parse_repeat("every 30 seconds")[1], 30)
        self.assertEqual(parse_repeat("every 30 minutes")[1], 1800)
        self.assertEqual(parse_repeat("every hour")[1], 3600, "'every hour' is 1 hour")
        self.assertEqual(parse_repeat("hourly")[0], "hourly")
        self.assertEqual(parse_repeat("daily")[0], "daily")
        self.assertEqual(parse_repeat("every 2h")[1], 7200)
        self.assertEqual(parse_repeat("every 1m")[1], 60)
        with self.assertRaises(SchedulerError):
            parse_repeat("once in a blue moon")


class TestNaturalTime(unittest.TestCase):
    def test_in_relative(self):
        self.assertAlmostEqual(parse_when("in 2 hours"), time.time() + 7200, delta=5)

    def test_tomorrow_with_time(self):
        when = parse_when("tomorrow 08:00")
        self.assertGreater(when, time.time())
        self.assertEqual((datetime.fromtimestamp(when).hour,
                          datetime.fromtimestamp(when).minute), (8, 0))

    def test_today_past_time_rolls_over(self):
        self.assertGreater(parse_when("today 00:01"), time.time(),
                           "a time already past today must land in the future")

    def test_unparseable_is_reported(self):
        with self.assertRaises(SchedulerError):
            parse_when("whenever the mood strikes")

    def test_bad_clock_time_is_reported(self):
        with self.assertRaises(SchedulerError):
            parse_when("tomorrow 99:99")


class TestCron(unittest.TestCase):
    def test_field_expansion(self):
        fields = parse_cron("0 9 * * 1-5")
        self.assertEqual(fields[0], {0})
        self.assertEqual(fields[1], {9})
        self.assertEqual(fields[4], {1, 2, 3, 4, 5})

    def test_step_and_list(self):
        fields = parse_cron("*/15 8,20 * * *")
        self.assertEqual(fields[0], {0, 15, 30, 45})
        self.assertEqual(fields[1], {8, 20})

    def test_named_days_and_months(self):
        fields = parse_cron("0 0 1 jan mon")
        self.assertEqual(fields[3], {1})
        self.assertEqual(fields[4], {1})

    def test_sunday_accepts_0_and_7(self):
        self.assertEqual(parse_cron("0 0 * * 7")[4], {0})

    def test_out_of_range_is_rejected(self):
        with self.assertRaises(SchedulerError):
            parse_cron("99 9 * * *")
        with self.assertRaises(SchedulerError):
            parse_cron("0 25 * * *")

    def test_next_run_honours_minute_and_hour(self):
        now = time.time()
        nxt = next_cron("0 9 * * 1-5", after=now)
        self.assertGreater(nxt, now, "the next match must be in the future")
        moment = datetime.fromtimestamp(nxt)
        self.assertEqual((moment.minute, moment.hour), (0, 9))
        self.assertIn(moment.weekday(), range(0, 5), "must land on a weekday")

    def test_impossible_expression_is_reported(self):
        with self.assertRaises(SchedulerError):
            next_cron("0 0 30 2 *")  # 30 February


class TestScheduleStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.sched = Scheduler(Path(self.dir.name) / "schedules.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_cron_auto_detected(self):
        s = self.sched.add("clear inbox", "0 9 * * 1-5")
        self.assertTrue(s.task_id.startswith("sch_"))
        self.assertEqual(s.schedule_type, "cron")
        self.assertEqual(s.cron, "0 9 * * 1-5")

    def test_absolute_time(self):
        s = self.sched.add("report", "tomorrow 08:00")
        self.assertEqual(s.schedule_type, "absolute")

    def test_recurring(self):
        s = self.sched.add("stretch", "now", repeat="every 30 minutes")
        self.assertEqual(s.schedule_type, "recurring")
        self.assertEqual(s.every_seconds, 1800)

    def test_ids_are_unique_within_one_tick(self):
        ids = {self.sched.add(f"task {i}", "0 9 * * *").task_id for i in range(20)}
        self.assertEqual(len(ids), 20)

    def test_recurrence_advances(self):
        s = self.sched.add("stretch", "now", repeat="every 1m")
        self.assertIn(s.task_id, [d.task_id for d in self.sched.due(now=time.time())])

    def test_due_filters_disabled(self):
        s = self.sched.add("task", "0 9 * * *")
        self.sched.enable(s.task_id, False)
        self.assertEqual(self.sched.due(now=10_000_000), [])

    def test_due_respects_earliest_start(self):
        s = self.sched.add("task", "now", repeat="every 1m")
        s.next_run += 10_000
        self.assertEqual(self.sched.due(now=0), [])

    def test_cron_next_run_is_in_the_future(self):
        s = self.sched.add("clear inbox", "0 9 * * 1-5")
        self.assertGreater(s.next_run, time.time())

    def test_persistence(self):
        s = self.sched.add("persisted", "0 9 * * *", priority="high")
        again = Scheduler(self.sched.path)
        loaded = again.get(s.task_id)
        self.assertEqual(loaded.goal, "persisted")
        self.assertEqual(loaded.priority, TaskPriority.HIGH)

    def test_remove(self):
        s = self.sched.add("task", "0 9 * * *")
        self.assertTrue(self.sched.remove(s.task_id))
        self.assertFalse(self.sched.remove(s.task_id))
        with self.assertRaises(SchedulerError):
            self.sched.get(s.task_id)

    def test_bad_cron_is_rejected(self):
        with self.assertRaises(SchedulerError):
            self.sched.add("task", "99 99 99 99 99")

    def test_unparseable_schedule_is_rejected(self):
        with self.assertRaises(SchedulerError):
            self.sched.add("task", "whenever")

    def test_empty_goal_is_rejected(self):
        with self.assertRaises(SchedulerError):
            self.sched.add("   ", "0 9 * * *")

    def test_explicit_task_id_is_honoured(self):
        s = self.sched.add("task", "0 9 * * *", task_id="sch_fixed")
        self.assertEqual(s.task_id, "sch_fixed")
