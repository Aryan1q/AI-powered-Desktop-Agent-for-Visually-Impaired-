"""Tests for the persona layer.

The persona is the only place where wording is rewritten, so the tests here are
mostly about not breaking things: an instruction must survive untouched, and
the phrase slots must not collide with the sentence around them.
"""
import unittest

from ufo.voice.persona import PERSONAS, Persona, _tidy, get_persona


class PersonaBasicsTest(unittest.TestCase):
    def test_none_is_a_pass_through(self):
        p = Persona("none", address="sir")
        self.assertFalse(p.enabled)
        self.assertEqual(p.repeat("open notepad"), "You said: Open notepad.")
        self.assertEqual(p.wake(), ".")
        self.assertEqual(p.status("done"), "Done.")

    def test_unknown_persona_degrades_to_none(self):
        self.assertEqual(Persona("wat").name, "none")

    def test_every_persona_name_resolves(self):
        for name in PERSONAS:
            self.assertEqual(Persona(name).name, name)


class PersonaSpellingTest(unittest.TestCase):
    def test_closes_with_a_space(self):
        """A closer is a suffix; gluing it on produced 'donewith your permission'."""
        for _ in range(60):
            line = Persona("jarvis", address="sir").status("the task is done")
            self.assertNotIn("donewith", line)
            for closer in ("as you require", "if that is your wish",
                           "with your permission"):
                if closer in line:
                    self.assertIn("done " + closer, line)

    def test_hedge_precedes_the_address(self):
        """A hedge after ', sir' produced 'shall I proceed, sir if I may?'."""
        for _ in range(60):
            line = Persona("jarvis", address="sir").confirm("shall I proceed")
            self.assertIn(", sir?", line)
            self.assertNotIn("sir if", line)
            self.assertNotIn("sir, if", line)

    def test_possessive_idiom_survives_tidying(self):
        """'I have it' must not become 'I've it'."""
        self.assertEqual(_tidy("I have it"), "I have it")
        self.assertEqual(_tidy("I have them"), "I have them")
        self.assertEqual(_tidy("I have completed the task"), "I've completed the task")


class PersonaRepeatTest(unittest.TestCase):
    def setUp(self):
        self.p = Persona("jarvis", address="sir")

    def test_repeat_keeps_the_instruction_verbatim(self):
        """The user approves what they hear, so the words must not change."""
        for _ in range(60):
            line = self.p.repeat("open notepad and type hello")
            self.assertIn("Open notepad and type hello", line)

    def test_repeat_of_empty_is_empty(self):
        self.assertEqual(self.p.repeat("   "), "")

    def test_repeat_is_spoken_not_questioned(self):
        for _ in range(60):
            self.assertNotIn("?", self.p.repeat("open notepad"))

    def test_repeat_does_not_rename_the_command(self):
        """A rewrite of a command would be a rewrite of what was approved.

        This is the case that motivated the pass-through: tidying prose used to
        eat the space before a period, so 'every .txt' became 'every.txt'.
        """
        spoken = self.p.repeat("rename every .txt in Downloads to .md")
        self.assertIn("Rename every .txt in Downloads to .md", spoken)
        self.assertNotIn("every.txt", spoken)
        self.assertNotIn("to.md", spoken)

    def test_repeat_preserves_interior_spacing(self):
        spoken = self.p.repeat("open   a.txt   and  b.md")
        self.assertIn("Open a.txt and b.md", spoken)


class PersonaLineTest(unittest.TestCase):
    def test_blocked_states_the_reason(self):
        p = Persona("jarvis", address="sir")
        line = p.blocked("that window is not on the allowlist")
        self.assertIn("allowlist", line)
        self.assertTrue(line.startswith("I'm afraid I can't"))

    def test_status_handles_empty_input(self):
        self.assertEqual(Persona("jarvis").status(""), "")

    def test_cadence_changes_the_refusal(self):
        dry = Persona("jarvis", address="sir", cadence="dry").blocked("no")
        warm = Persona("jarvis", address="sir", cadence="warm").blocked("no")
        self.assertIn("I'm afraid", dry)
        self.assertIn("I'm sorry", warm)

    def test_terse_adds_nothing(self):
        p = Persona("terse", address="sir")
        self.assertEqual(p.status("the task is done"), "The task is done, sir.")
        self.assertEqual(p.done("finished"), "Finished, sir.")

    def test_terse_addresses_because_the_user_asked_for_it(self):
        """A terse persona has no openers, but still honours the address."""
        self.assertEqual(Persona("terse", address="sir").wake(), ", sir.")


class PersonaSpacingTest(unittest.TestCase):
    def test_closer_and_address_read_together(self):
        for _ in range(60):
            line = Persona("jarvis", address="sir").status("the task is done")
            self.assertNotIn("done, sir with", line)
            self.assertNotIn("donewith", line)
            self.assertTrue(line.endswith("."))


class PersonaPromptTest(unittest.TestCase):
    def test_disabled_persona_adds_no_prompt(self):
        self.assertEqual(Persona("none").system_prompt_addition(), "")

    def test_enabled_persona_asks_for_prose_only(self):
        """A persona may colour thoughts but must not touch the action set."""
        addition = Persona("jarvis").system_prompt_addition()
        self.assertIn("thought", addition.lower())
        for banned in ("action", "you may now", "ignore the allowlist"):
            self.assertNotIn(banned, addition.lower())


class GetPersonaTest(unittest.TestCase):
    def test_from_config(self):
        class V:
            persona = "jarvis"
            address = "sir"
            cadence = "dry"
            chime = True

        class C:
            voice = V()

        p = get_persona(C())
        self.assertEqual(p.name, "jarvis")
        self.assertEqual(p.address, "sir")

    def test_missing_config_is_silent(self):
        self.assertEqual(get_persona(object()).name, "none")
        self.assertEqual(get_persona(None).name, "none")


if __name__ == "__main__":
    unittest.main()
