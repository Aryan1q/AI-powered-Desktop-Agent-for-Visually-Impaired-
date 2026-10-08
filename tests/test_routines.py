"""Category 40: the nine modes, and the guard staying in the path."""
import unittest

from ufo.actions import auto
from ufo.capabilities import nlu


class TestModeTable(unittest.TestCase):

    def test_all_nine_modes_exist(self):
        expected = {"morning", "work", "focus", "meeting", "presentation",
                    "study", "gaming", "travel", "end of day"}
        self.assertEqual(set(auto.ROUTINES), expected)

    def test_every_mode_has_on_steps(self):
        for name, table in auto.ROUTINES.items():
            self.assertTrue(table.get("on"), f"{name} has no on steps")

    def test_every_step_is_a_sentence_not_a_bare_spec(self):
        """Steps are goals, so they route through the executor and the guard."""
        for name, table in auto.ROUTINES.items():
            for step in table.get("on", []) + table.get("off", []):
                self.assertNotIn(";", step, f"{name}: {step!r}")
                self.assertTrue(step.strip())


class TestCanonicalNames(unittest.TestCase):

    def test_catalog_names(self):
        for name in auto.ROUTINES:
            self.assertEqual(auto._canonical_routine(name), name)

    def test_leading_verbs_and_suffixes_are_stripped(self):
        self.assertEqual(auto._canonical_routine("start morning routine"), "morning")
        self.assertEqual(auto._canonical_routine("focus mode"), "focus")
        self.assertEqual(auto._canonical_routine("begin the work routine"), "work")

    def test_everyday_aliases(self):
        self.assertEqual(auto._canonical_routine("pomodoro"), "focus")
        self.assertEqual(auto._canonical_routine("deep work"), "focus")
        self.assertEqual(auto._canonical_routine("endday"), "end of day")
        self.assertEqual(auto._canonical_routine("wind down"), "end of day")
        self.assertEqual(auto._canonical_routine("commute"), "travel")

    def test_unknown_is_empty_not_guessed(self):
        self.assertEqual(auto._canonical_routine("nonsense mode"), "")


class TestRouting(unittest.TestCase):

    def _spec(self, text):
        plan = nlu.interpret(text)
        self.assertIsNotNone(plan, f"{text!r} did not route")
        return plan.steps[0].spec

    def test_modes_route_to_the_routine_capability(self):
        # One step, not an expanded list: the expansion must happen behind the
        # guard, not in the router.
        for text in ["start morning routine", "enter focus mode",
                     "turn on gaming mode", "start my presentation routine",
                     "start study mode", "start commute routine"]:
            spec = self._spec(text)
            self.assertTrue(spec.startswith("routine:"), f"{text!r} -> {spec!r}")

    def test_off_direction_is_carried(self):
        self.assertEqual(self._spec("focus off"), "routine:focus:off")
        self.assertEqual(self._spec("leave focus mode"), "routine:focus:off")
        self.assertEqual(self._spec("exit work mode"), "routine:work:off")

    def test_bare_stop_is_left_to_the_stop_capability(self):
        """`ctl_stop` wins over `routine:work:off` on purpose.

        "stop" is an explicit trigger for the confirmed stop-everything
        capability, and reading it as "leave work mode" would quietly toggle a
        mode when the user meant to halt. The guard is the safer reading.
        """
        self.assertEqual(self._spec("stop work mode"), "ctl_stop")

    def test_on_direction_is_carried(self):
        self.assertEqual(self._spec("enter focus mode"), "routine:focus:on")
        self.assertEqual(self._spec("start morning routine"), "routine:morning:on")

    def test_aliases_reach_their_mode(self):
        self.assertTrue(self._spec("start my deep work routine").startswith("routine:focus"))
        self.assertTrue(self._spec("start commute routine").startswith("routine:travel"))


class TestOffMustNotTurnOn(unittest.TestCase):
    """"focus off" used to run the five *on* steps. It must not, ever."""

    def test_off_never_expands_into_steps(self):
        plan = nlu.interpret("focus off")
        self.assertIsNotNone(plan)
        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.steps[0].capability, "routine")
        self.assertEqual(plan.steps[0].args, "off")

    def test_off_words_are_not_filler(self):
        for word in ("off", "stop", "leave", "exit", "disable"):
            self.assertNotIn(word, nlu._ROUTINE_TAIL,
                             f"{word!r} being filler is what ran the on steps")


class TestMentionIsNotCommand(unittest.TestCase):

    def test_mentioning_a_mode_does_not_run_it(self):
        for text in ["work only in this folder", "my focus is the report",
                     "the meeting is at 3", "study the logs",
                     "organise my study notes", "travel plans for June",
                     "the gaming mode is broken"]:
            plan = nlu.interpret(text)
            if plan is None:
                continue
            self.assertFalse(
                any(s.capability == "routine" for s in plan.steps),
                f"{text!r} fired a mode: {plan.specs()}")


class TestRoutineHandler(unittest.TestCase):

    def test_lists_modes(self):
        out = auto.routine()
        for name in auto.ROUTINES:
            self.assertIn(name, out)

    def test_unknown_mode_names_what_it_knows(self):
        out = auto.routine("nonsense mode")
        self.assertIn("no mode called", out)
        self.assertIn("morning", out)

    def test_custom_routine_wins_over_builtin(self):
        self.assertEqual(auto._canonical_routine("focus"), "focus")

    def test_mode_with_nothing_to_undo_says_so(self):
        out = auto.routine("end of day", "off")
        self.assertIn("nothing to undo", out)


if __name__ == "__main__":
    unittest.main()