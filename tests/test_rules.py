"""Category 46: corrections that change behaviour, not just notes."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ufo.actions import auto
from ufo.capabilities import executor
from ufo.core import rules as rulesmod


def _fresh():
    tmp = Path(tempfile.mkdtemp())
    return rulesmod.reset_for_tests(tmp / "rules.json")


class TestTokens(unittest.TestCase):

    def test_filler_and_negations_are_not_the_subject(self):
        rule = rulesmod.Rule("never", "never rename anything in Downloads")
        self.assertEqual(rule.tokens(), {"rename", "downloads"})

    def test_underscores_are_split_so_plain_words_match_capabilities(self):
        # "never delete" has to be able to veto `file_delete`.
        rule = rulesmod.Rule("never", "delete")
        self.assertEqual(rule.tokens(), {"delete"})

    def test_a_rule_with_no_distinctive_word_has_no_tokens(self):
        self.assertEqual(rulesmod.Rule("never", "never do it").tokens(), set())


class TestVeto(unittest.TestCase):

    def setUp(self):
        self.rules = _fresh()

    def test_matching_rule_vetoes(self):
        self.rules.add("never", "rename anything in Downloads")
        self.assertTrue(self.rules.veto_for("file_rename", "Downloads/notes.txt"))

    def test_non_matching_path_is_allowed(self):
        self.rules.add("never", "rename anything in Downloads")
        self.assertEqual(
            self.rules.veto_for("file_rename", "Desktop/notes.txt"), "")

    def test_unrelated_capability_is_allowed(self):
        self.rules.add("never", "rename anything in Downloads")
        self.assertEqual(self.rules.veto_for("hw_battery", ""), "")

    def test_plain_word_rule_matches_the_capability(self):
        self.rules.add("never", "delete")
        self.assertTrue(self.rules.veto_for("file_delete", "a.txt"))

    def test_no_substring_matching(self):
        # "deleted from my clipboard" must not trigger a rule about deleting.
        self.rules.add("never", "empty")
        self.assertEqual(
            self.rules.veto_for("clip_paste", "deleted from my clipboard"), "")

    def test_a_vacuous_rule_is_refused(self):
        # Otherwise it would veto every action that mentions an "it".
        with self.assertRaises(ValueError):
            self.rules.add("never", "never do it")

    def test_only_never_vetoes(self):
        self.rules.add("always", "file_backup")
        self.assertEqual(self.rules.veto_for("file_backup"), "")


class TestPrecedence(unittest.TestCase):

    def setUp(self):
        self.rules = _fresh()

    def test_always_ranks_higher(self):
        self.rules.add("always", "file_backup")
        self.assertEqual(self.rules.rank_for("file_backup"), 2)
        self.assertEqual(self.rules.rank_for("file_delete"), 0)

    def test_prefer_ranks_below_always(self):
        self.rules.add("always", "file_backup")
        self.rules.add("prefer", "file_backup")
        self.assertEqual(self.rules.rank_for("file_backup"), 2)

    def test_always_does_not_veto_or_permit(self):
        self.rules.add("always", "file_backup")
        self.assertEqual(self.rules.veto_for("file_backup"), "")
        self.assertTrue(self.rules.vetoed_capabilities() == set())


class TestExecutorHonoursVeto(unittest.TestCase):

    def setUp(self):
        self.rules = _fresh()

    def tearDown(self):
        self.rules.clear()

    def test_veto_short_circuits_before_the_guard(self):
        self.rules.add("never", "hw_battery")
        self.assertTrue(executor._rule_veto("hw_battery", "", ""))

    def test_no_rule_no_veto(self):
        self.assertEqual(executor._rule_veto("hw_battery", "", ""), "")

    def test_veto_reason_names_the_rule(self):
        self.rules.add("never", "hw_battery")
        self.assertIn("hw_battery", executor._rule_veto("hw_battery"))


class TestParse(unittest.TestCase):

    def setUp(self):
        self.rules = _fresh()

    def test_negations_become_vetoes(self):
        for text in ("never rename files", "don't rename files",
                     "do not rename files", "no more renaming"):
            self.rules.clear()
            self.rules.parse(text)
            self.assertEqual(self.rules.all()[0].kind, "never", text)

    def test_always_and_prefer(self):
        self.assertEqual(self.rules.parse("always back up first").kind, "always")
        self.rules.clear()
        self.assertEqual(self.rules.parse("prefer dark mode").kind, "prefer")

    def test_a_sentence_is_refused_rather_than_stored_pointlessly(self):
        with self.assertRaises(ValueError):
            self.rules.parse("I do not like the tone of the report")

    def test_negation_is_stripped_from_the_subject(self):
        rule = self.rules.parse("never rename anything in Downloads")
        self.assertEqual(rule.tokens(), {"rename", "downloads"})
        self.assertNotIn("never", rule.subject.lower())


class TestFeedbackHandler(unittest.TestCase):

    def setUp(self):
        self.rules = _fresh()

    def test_a_correction_becomes_a_rule(self):
        out = auto.feedback("never rename anything in Downloads")
        self.assertIn("rule saved", out)
        self.assertTrue(self.rules.veto_for("file_rename", "Downloads/a.txt"))

    def test_always_is_reported_as_a_preference_not_a_promise(self):
        out = auto.feedback("always run the backup first")
        self.assertIn("offer that first", out)
        self.assertEqual(self.rules.veto_for("file_backup"), "")

    def test_non_rule_is_filed_not_lost(self):
        with mock.patch("ufo.core.memory.get_memory") as get_memory:
            out = auto.feedback("I do not like the tone")
            self.assertIn("written it to memory", out)
            self.assertTrue(get_memory.return_value.remember.called)

    def test_empty_request_asks_for_an_example(self):
        out = auto.feedback()
        self.assertIn("never rename", out)

    def test_list_and_forget(self):
        auto.feedback("never rename anything in Downloads")
        self.assertIn("rename", auto.feedback("list"))
        out = auto.feedback("forget rule rename anything in Downloads")
        self.assertIn("removed", out)
        self.assertEqual(self.rules.all(), [])

    def test_clear_all(self):
        auto.feedback("never rename anything in Downloads")
        out = auto.feedback("clear")
        self.assertIn("cleared 1", out)
        self.assertEqual(self.rules.all(), [])

    def test_forgetting_a_rule_that_is_not_there(self):
        self.assertIn("no rule about", auto.feedback("forget rule nothing here"))


class TestPersistence(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.rules = rulesmod.reset_for_tests(self.tmp / "rules.json")

    def test_rules_survive_a_restart(self):
        self.rules.add("never", "file_delete")
        reloaded = rulesmod.Rules(self.tmp / "rules.json")
        self.assertTrue(reloaded.veto_for("file_delete", "a.txt"))

    def test_duplicate_rule_is_updated_not_duplicated(self):
        self.rules.add("never", "file_delete", "first reason")
        self.rules.add("never", "file_delete", "second reason")
        self.assertEqual(len(self.rules.all()), 1)
        self.assertEqual(self.rules.all()[0].note, "second reason")

    def test_missing_file_is_not_an_error(self):
        fresh = rulesmod.Rules(self.tmp / "nope.json")
        self.assertEqual(fresh.all(), [])

    def test_corrupt_file_is_not_an_error(self):
        path = self.tmp / "bad.json"
        path.write_text("{oops", encoding="utf-8")
        self.assertEqual(rulesmod.Rules(path).all(), [])


if __name__ == "__main__":
    unittest.main()