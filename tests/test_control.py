"""Category 44: pause, resume, undo, redo, forget — the agent driving verbs."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ufo.actions import auto
from ufo.capabilities import executor
from ufo.core import control


def _fresh():
    tmp = Path(tempfile.mkdtemp())
    return control.reset_for_tests(tmp / "control.json")


class TestPause(unittest.TestCase):

    def setUp(self):
        self.control = _fresh()

    def tearDown(self):
        self.control.resume()

    def test_pause_actually_pauses(self):
        auto.pause("phone call")
        self.assertTrue(self.control.paused)
        self.assertEqual(self.control.pause_reason, "phone call")

    def test_paused_blocks_ordinary_steps(self):
        auto.pause("on a call")
        reason = executor._control_halt()
        self.assertIn("paused", reason)
        self.assertIn("resume", reason)

    def test_not_paused_blocks_nothing(self):
        self.assertEqual(executor._control_halt(), "")

    def test_control_verbs_stay_available_while_paused(self):
        # Otherwise nothing could ever resume.
        for name in ("ctl_resume", "ctl_status", "ctl_pause"):
            self.assertTrue(executor._is_control_verb(name), name)
        for name in ("hw_battery", "file_delete", "routine"):
            self.assertFalse(executor._is_control_verb(name), name)

    def test_resume_clears_the_reason(self):
        auto.pause("lunch")
        auto.resume()
        self.assertFalse(self.control.paused)
        self.assertEqual(self.control.pause_reason, "")

    def test_resume_when_not_paused(self):
        self.assertEqual(auto.resume(), "not paused")

    def test_pause_survives_a_restart(self):
        auto.pause("before the demo")
        reloaded = control.Control(self.control.path)
        self.assertTrue(reloaded.paused)
        self.assertEqual(reloaded.pause_reason, "before the demo")

    def test_resume_does_not_clear_the_emergency_switch(self):
        from ufo.safety import emergency

        auto.pause("x")
        with mock.patch.object(type(emergency), "tripped", True):
            out = auto.resume()
        self.assertIn("emergency stop is still tripped", out)
        self.assertFalse(self.control.paused)


class TestReversals(unittest.TestCase):

    def test_exact_toggles_are_reversible(self):
        self.assertEqual(executor.reversal_for("set_do_not_disturb", "on"),
                         "turn do not disturb off")
        self.assertEqual(executor.reversal_for("set_do_not_disturb", "off"),
                         "turn do not disturb on")
        self.assertEqual(executor.reversal_for("set_mute", "mute"), "unmute")

    def test_a_level_is_not_reversible_by_unmuting(self):
        # Un-muting does not restore the level. Claiming otherwise would tell
        # the user their volume is back when it is not.
        self.assertEqual(executor.reversal_for("set_volume", "30"), "")

    def test_deletion_is_not_reversible(self):
        self.assertEqual(executor.reversal_for("file_delete", "x.txt"), "")

    def test_unknown_capability_is_not_reversible(self):
        self.assertEqual(executor.reversal_for("net_flush_dns", ""), "")


class TestUndoJournal(unittest.TestCase):

    def setUp(self):
        self.control = _fresh()

    def test_nothing_to_undo_when_empty(self):
        self.assertEqual(auto.undo(), "nothing to undo")
        self.assertEqual(auto.redo(), "nothing to redo")

    def test_read_only_steps_are_not_journalled(self):
        classifier = executor.RiskClassifier()
        self.assertFalse(classifier.changes_machine("hw_battery", "system", ""))
        self.assertFalse(classifier.changes_machine("ctl_status", "system", ""))

    def test_irreversible_change_is_still_recorded(self):
        # Dropping it would make undo reach past the deletion and reverse
        # whatever came before it.
        self.control.record("file_delete", "x.txt", "", undo="", reversible=False)
        out = auto.undo()
        self.assertIn("cannot undo file_delete", out)
        self.assertIn("no reversal recorded", out)

    def test_undo_does_not_reach_past_an_irreversible_step(self):
        self.control.record("set_do_not_disturb", "", "on",
                            undo="turn do not disturb off")
        self.control.record("file_delete", "x.txt", "", undo="", reversible=False)
        out = auto.undo()
        self.assertIn("cannot undo file_delete", out)
        self.assertNotIn("turn do not disturb off", out)

    def test_undo_marks_the_irreversible_step_in_the_listing(self):
        self.control.record("file_delete", "x.txt", "", undo="", reversible=False)
        listing = auto.undo_list()
        self.assertIn("file_delete", listing)
        self.assertIn("!!", listing)

    def test_undo_runs_the_reversal_and_reports_failure_honestly(self):
        self.control.record("set_do_not_disturb", "", "on",
                            undo="turn do not disturb off")
        with mock.patch("ufo.actions.triggers.run_goal",
                        return_value="[ok ] turned it off"):
            out = auto.undo()
        self.assertIn("undid set_do_not_disturb", out)

    def test_failed_reversal_is_reported_not_claimed_as_done(self):
        self.control.record("set_do_not_disturb", "", "on",
                            undo="turn do not disturb off")
        with mock.patch("ufo.actions.triggers.run_goal",
                        return_value="[FAIL] no such setting"):
            out = auto.undo()
        self.assertIn("failed", out)
        self.assertNotIn("undid", out)

    def test_redo_puts_the_step_back(self):
        self.control.record("set_do_not_disturb", "", "on",
                            undo="turn do not disturb off")
        self.control.pop_undo()
        with mock.patch("ufo.actions.triggers.run_goal", return_value="ok"):
            out = auto.redo()
        self.assertIn("redid set_do_not_disturb", out)

    def test_undo_then_redo_restores_the_stack(self):
        self.control.record("set_do_not_disturb", "", "on",
                            undo="turn do not disturb off")
        self.control.pop_undo()
        self.control.pop_redo()
        self.assertEqual(self.control.undo_depth, 1)
        self.assertEqual(self.control.redo_depth, 0)

    def test_journal_survives_a_restart(self):
        self.control.record("set_do_not_disturb", "", "on",
                            undo="turn do not disturb off")
        reloaded = control.Control(self.control.path)
        self.assertEqual(reloaded.undo_depth, 1)

    def test_journal_is_bounded(self):
        for i in range(control.JOURNAL_LIMIT + 25):
            self.control.record(f"cap{i}", "", "", undo="x")
        self.assertEqual(self.control.undo_depth, control.JOURNAL_LIMIT)

    def test_corrupt_journal_does_not_crash(self):
        self.control.path.write_text("{not json", encoding="utf-8")
        fresh = control.Control(self.control.path)
        self.assertEqual(fresh.undo_depth, 0)
        self.assertFalse(fresh.paused)


class TestReadOnlyCoversWindowState(unittest.TestCase):

    def test_minimising_counts_as_a_change(self):
        # A user in read-only mode has not agreed to their desktop moving.
        classifier = executor.RiskClassifier()
        for action in ("window_manage", "window_minimize"):
            self.assertTrue(
                classifier.changes_machine(action, "window", "minimize_all"),
                action)
        self.assertTrue(
            classifier.changes_machine("window_snap", "ui", "snap_left"))


class TestForget(unittest.TestCase):

    def setUp(self):
        _fresh()

    def test_remember_then_forget(self):
        from ufo.core.memory import Memory

        with mock.patch("ufo.core.memory.get_memory") as get_memory:
            memory = get_memory.return_value
            memory.facts.return_value = {}
            memory.recall.return_value = ""
            auto.remember("my wifi", "the loft one")
            self.assertTrue(memory.remember.called)

            memory.forget.return_value = True
            out = auto.remember("my wifi", "forget")
            self.assertIn("forgotten", out)
            self.assertTrue(memory.forget.called)

    def test_forgetting_what_it_does_not_have(self):
        from ufo.core.memory import Memory

        with mock.patch("ufo.core.memory.get_memory") as get_memory:
            get_memory.return_value.forget.return_value = False
            out = auto.remember("nothing", "forget")
            self.assertIn("not holding anything", out)

    def test_remember_with_no_subject_lists_what_it_has(self):
        with mock.patch("ufo.core.memory.get_memory") as get_memory:
            get_memory.return_value.facts.return_value = {"a": "1", "b": "2"}
            out = auto.remember()
            self.assertIn("what should I remember", out)
            self.assertIn("a = 1", out)


if __name__ == "__main__":
    unittest.main()
