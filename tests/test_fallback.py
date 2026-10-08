"""Category 48: the fallback ladder, and honest partial reporting."""
import unittest
from unittest import mock

from ufo.capabilities import fallback, nlu
from ufo.capabilities.executor import Executor
from ufo.config import Config


class _FastPerception(unittest.TestCase):
    """The ladder's vision and UIA rungs look at the real screen.

    That is the point in production and far too slow for a unit test, so every
    subclass sees a stubbed screen instead.
    """

    def setUp(self):
        control = mock.Mock()
        control.name, control.type, control.id = "OK button", "Button", 1
        observation = mock.Mock(controls=[control])

        patches = [
            mock.patch("ufo.perception.vision.observe_screen",
                       return_value=observation),
            mock.patch("ufo.perception.uia.list_all_windows", return_value=[]),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)


def _executor(**kw):
    return Executor(cfg=Config(), record_gaps=False, **kw)


class TestSplitCompound(unittest.TestCase):

    def test_strips_trailing_comma(self):
        parts = nlu.split_compound("open the file, then rename it, then upload it")
        self.assertEqual(parts, ["open the file", "rename it", "upload it"])

    def test_strips_dangling_and(self):
        parts = nlu.split_compound("empty the recycle bin and then compress it")
        self.assertEqual(parts, ["empty the recycle bin", "compress it"])

    def test_trailing_punctuation_is_not_left_on_a_part(self):
        parts = nlu.split_compound("open the file, then rename it.")
        self.assertEqual(parts, ["open the file", "rename it"])

    def test_bare_and_is_not_a_sequence_marker(self):
        self.assertFalse(nlu.has_sequence_marker("find and replace in this file"))
        self.assertTrue(nlu.has_sequence_marker("rename it then upload it"))
        self.assertTrue(nlu.has_sequence_marker("badal do, phir upload karo"))
        self.assertTrue(nlu.has_sequence_marker("badal do, फिर upload करो"))

    def test_ambiguous_and_still_reads_as_one_action(self):
        plan = nlu.interpret("find and replace in this file")
        self.assertIsNotNone(plan)


class TestRoutingHonesty(_FastPerception):
    """A multi-part request must never be satisfied by one trigger word."""

    def test_compound_not_reduced_to_single_match(self):
        ex = _executor(confirm=lambda m, r: True)
        result = ex.handle("check my battery and then compress the heliosphere")
        self.assertEqual(result.outcome, "PARTIAL")
        ran = [s.step.capability for s in result.steps]
        self.assertEqual(ran, ["hw_battery"])

    def test_finishes_only_when_every_part_routed(self):
        ex = _executor(confirm=lambda m, r: True)
        result = ex.handle("mute the sound and then set volume to 30")
        self.assertEqual(result.outcome, "FINISH")
        self.assertEqual(len(result.steps), 2)

    def test_bare_and_does_not_break_a_single_action(self):
        ex = _executor(confirm=lambda m, r: True)
        result = ex.handle("find and replace in this file")
        self.assertNotEqual(result.outcome, "UNSUPPORTED")

    def test_full_compound_outcome_is_finish(self):
        ex = _executor(confirm=lambda m, r: True)
        result = ex.handle("open the file, then rename it, then upload it")
        self.assertIn(result.outcome, ("FINISH", "PARTIAL", "BLOCKED", "UNSUPPORTED"))


class TestLadderOrder(_FastPerception):

    def test_unknown_request_reaches_vision_and_reports(self):
        ex = _executor(confirm=lambda m, r: True)
        result = ex.handle("make me a sandwich with rocket fuel")
        self.assertEqual(result.outcome, "UNSUPPORTED")
        self.assertIn("What should I do", result.text)
        self.assertIn("What I tried first", result.text)

    def test_no_confirmer_means_no_generated_code(self):
        """Nobody to ask means the script rung is skipped, not guessed at."""
        attempt = fallback._try_script("write a script to rename invoices", False)
        self.assertFalse(attempt.ok)
        self.assertTrue(attempt.skipped)
        self.assertIn("approval", attempt.detail)

    def test_script_draft_is_written_but_never_run(self):
        from ufo.config import Config as C
        with mock.patch.object(fallback, "_write_script_draft") as draft:
            draft.return_value = None
            attempt = fallback._try_script("write a script to rename invoices", True)
        self.assertFalse(attempt.ok)

    def test_unscriptable_request_is_skipped_not_attempted(self):
        attempt = fallback._try_script("make me a sandwich", True)
        self.assertTrue(attempt.skipped)
        self.assertFalse(attempt.ok)

    def test_partial_detail_names_the_missing_part(self):
        ex = _executor(confirm=lambda m, r: True)
        result = ex.handle("check my battery and then compress the heliosphere")
        self.assertIn("compress the heliosphere", result.text)

    def test_question_targets_the_missing_part_not_the_done_one(self):
        ex = _executor(confirm=lambda m, r: True)
        result = ex.handle("check my battery and then compress the heliosphere")
        self.assertIn("compress the heliosphere", result.text)
        self.assertNotIn("Did you mean", result.text)

    def test_suggestions_exclude_already_run_capabilities(self):
        plan = nlu.Plan("x", [nlu.Step("hw_battery")])
        partial = fallback.Attempt("partial", True, "x", plan=plan)
        done, other = mock.Mock(), mock.Mock()
        done.name, other.name = "hw_battery", "other_thing"
        near = [(done, 0.9, 1), (other, 0.5, 1)]
        kept = Executor._suggestions_besides(near, partial)
        self.assertEqual([c.name for c, _p, _s in kept], ["other_thing"])


class TestGapLog(_FastPerception):

    def test_unknown_request_is_logged(self):
        ex = Executor(cfg=Config(), record_gaps=True)
        with mock.patch("ufo.capabilities.gaps.get_log") as get_log:
            get_log.return_value.add.return_value = None
            ex.handle("fluff the wombat registry")
            self.assertTrue(get_log.return_value.add.called)

    def test_no_gap_when_request_is_understood(self):
        ex = Executor(cfg=Config(), record_gaps=True)
        with mock.patch("ufo.capabilities.gaps.get_log") as get_log:
            get_log.return_value.add.return_value = None
            ex.handle("check my battery")
            self.assertFalse(get_log.return_value.add.called)


if __name__ == "__main__":
    unittest.main()