"""Category 37/38/39/42/47: the trigger, alert, context, recipe and self-management gaps.

Every test here corresponds to a spec item that was verified broken by calling
the handler, not by reading it.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ufo import config as config_mod
from ufo.actions import auto, triggers
from ufo.config import SafetyConfig
from ufo.safety.policy import ActionPolicy


class _Home(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        cfg = config_mod.Config()
        try:
            cfg.storage.home = str(self.tmp)
        except Exception:
            pass
        self._previous = config_mod.active()
        config_mod.set_active(cfg)
        self.addCleanup(config_mod.set_active, self._previous)


class TestContextTriggers(_Home):
    """Category 39. Every one of the eight spec conditions used to be rejected."""

    def test_every_spec_condition_is_accepted_as_two_arguments(self):
        # The catalog declares targets=condition, args=what to do. The old body
        # joined the two and demanded a literal " then ", so no pair of real
        # arguments could ever be accepted.
        for condition in ("before my meeting", "after I close this",
                          "when I get home", "on wi-fi home", "at night",
                          "while I'm away", "charger is plugged in",
                          "screen is locked"):
            with self.subTest(condition=condition):
                out = auto.context_trigger(condition, "start music")
                self.assertIn("rule saved", out)
                self.assertNotIn("say what to do", out)

    def test_the_action_is_stored_not_swallowed(self):
        auto.context_trigger("at night", "start music")
        self.assertEqual(auto._load()["rules"][-1]["goal"], "start music")

    def test_one_sentence_still_works(self):
        out = auto.context_trigger("at night then mute the volume", "")
        self.assertIn("rule saved", out)
        self.assertEqual(auto._load()["rules"][-1]["goal"], "mute the volume")

    def test_missing_action_asks_for_one(self):
        self.assertIn("say what to do", auto.context_trigger("at night", ""))

    def test_empty_asks_for_a_rule(self):
        self.assertIn("give me a rule", auto.context_trigger("", ""))

    def test_get_home_is_understood(self):
        self.assertEqual(triggers.parse_condition("i get home")[0], "arrived_home")

    def test_get_home_fires_on_joining_a_network(self):
        ctx = triggers.Context(on_wifi="HomeNet")
        self.assertTrue(triggers.evaluate("i get home", ctx))
        self.assertFalse(triggers.evaluate("i get home", triggers.Context()))

    def test_get_home_respects_a_named_network(self):
        self.assertTrue(triggers.evaluate(
            "get home on homenet", triggers.Context(on_wifi="HomeNet")))
        self.assertFalse(triggers.evaluate(
            "get home on homenet", triggers.Context(on_wifi="Cafe")))

    def test_on_charge_is_charger_plugged(self):
        self.assertEqual(
            triggers.parse_condition("on charge")[0], "charger_plugged")

    def test_bare_locked_is_the_screen(self):
        self.assertEqual(
            triggers.parse_condition("locked")[0], "screen_locked")

    def test_unrecognised_condition_is_refused_not_guessed(self):
        out = auto.context_trigger("when the mood is right", "start music")
        self.assertIn("do not understand", out)
        self.assertEqual(auto._load().get("rules", []), [])


class TestAlerts(_Home):
    """Category 38. Two of the eight spec alert kinds were rejected."""

    def test_every_spec_alert_kind_is_accepted(self):
        for alert in ("when my download finishes", "when the battery is low",
                      "when the website changes", "when email arrives from bob",
                      "when a file appears", "when cpu is high",
                      "when my meeting starts"):
            with self.subTest(alert=alert):
                out = auto.alert(alert, "tell me")
                self.assertIn("alert set", out, out)

    def test_the_message_does_not_end_up_in_the_condition(self):
        # Joining the two slots put "tell me" at the end of every stored
        # condition, which then had to be parsed around.
        auto.alert("when the battery is low", "tell me")
        self.assertEqual(auto._load()["rules"][-1]["when"], "the battery is low")
        self.assertEqual(auto._load()["rules"][-1]["goal"], "alert: tell me")

    def test_website_change_condition_parses(self):
        self.assertEqual(
            triggers.parse_condition("the website changes")[0], "website_changed")

    def test_email_from_picks_up_the_sender(self):
        kind, arg = triggers.parse_condition("email arrives from bob")
        self.assertEqual(kind, "email_from")
        self.assertEqual(arg, "bob")

    def test_email_alert_never_claims_to_have_checked_mail(self):
        # There is no mail check in the tick, so it must stay false.
        self.assertFalse(
            triggers.evaluate("email arrives from bob", triggers.Context()))

    def test_a_bare_message_is_spoken_not_stored(self):
        with mock.patch("ufo.voice.notify") as notify:
            out = auto.alert("", "the build is done")
        self.assertIn("told you", out)
        self.assertTrue(notify.called)
        self.assertEqual(auto._load().get("rules", []), [])

    def test_alert_list_shows_them(self):
        auto.alert("when the battery is low", "tell me")
        self.assertIn("battery is low", auto.list_alerts())


class TestTriggerOnEvent(_Home):
    """Category 37. `auto_trigger` was advertised as setting a rule but only queried."""

    def test_condition_plus_action_stores_a_rule(self):
        out = auto.trigger("when the battery is low", "run backup")
        self.assertIn("rule saved", out)
        self.assertEqual(auto._load()["rules"][-1]["goal"], "run backup")

    def test_the_spec_event_kinds_parse(self):
        for event in ("when a file is created", "on window close",
                      "on battery low", "when the network connects"):
            with self.subTest(event=event):
                self.assertIn("rule saved", auto.trigger(event, "run backup"))

    def test_network_reconnects_fires_on_a_network(self):
        self.assertTrue(triggers.evaluate(
            "when the network connects", triggers.Context(on_wifi="HomeNet")))
        self.assertFalse(triggers.evaluate(
            "when the network connects", triggers.Context()))

    def test_now_still_runs_the_tick(self):
        auto.context_trigger("at night", "start music")
        with mock.patch.object(triggers, "check_now", return_value=[]):
            self.assertIn("no rule matched", auto.trigger("now", ""))

    def test_unknown_rule_says_how_to_make_one(self):
        out = auto.trigger("no such thing", "")
        self.assertIn("no rule called", out)
        self.assertIn("trigger_context", out)


class TestVagueRouting(_Home):
    """Category 42. 'fix my wi-fi' found nothing because the hyphen is a space."""

    def test_the_hyphenated_spelling_routes(self):
        out = auto.interpret("fix my wi-fi", "")
        self.assertNotIn("does not route", out)
        self.assertIn("net_fix", out)

    def test_both_spellings_reach_the_same_recipe(self):
        hyphen = auto.interpret("fix my wi-fi", "")
        plain = auto.interpret("fix my wifi", "")
        self.assertEqual(hyphen.split("->")[1].split("(")[0],
                         plain.split("->")[1].split("(")[0])

    def test_every_spec_vague_form_routes(self):
        for phrase in ("make my laptop faster", "clean up my desktop",
                       "get me ready for my meeting", "fix my wi-fi",
                       "organize my life", "find that thing i saw last week",
                       "do what i did yesterday"):
            with self.subTest(phrase=phrase):
                self.assertNotIn("does not route", auto.interpret(phrase, ""))

    def test_spelling_fold_does_not_break_other_phrases(self):
        self.assertEqual(
            __import__("ufo.capabilities.catalog", fromlist=["x"])
            .normalise_phrase("Power-Point deck"), "powerpoint deck")


class TestSelfManagement(_Home):

    def test_self_update_reports_an_update_path_or_says_there_is_none(self):
        out = auto.self_update("", "")
        self.assertIn("running", out)
        self.assertIn("2.0.0", out)

    def test_self_update_does_not_act_without_being_asked(self):
        # It must not shell out on a bare "how am I doing" question.
        with mock.patch("subprocess.run") as run:
            auto.self_update("", "")
        self.assertFalse(run.called)

    def test_self_update_needs_the_word_install(self):
        with mock.patch("subprocess.run") as run:
            out = auto.self_update("yes please", "")
        self.assertFalse(run.called)
        self.assertIn("install", out)

    def test_permissions_start_empty_and_say_so(self):
        self.assertIn("no per-category permissions", auto.permissions("", ""))

    def test_permissions_by_number_and_by_name_agree(self):
        auto.permissions("31", "confirm")
        self.assertEqual(config_mod.active().safety.category_permissions["31"],
                         "confirm")
        auto.permissions("finance", "block")
        self.assertEqual(config_mod.active().safety.category_permissions["31"],
                         "block")

    def test_permissions_accepts_the_equals_form(self):
        auto.permissions("31=confirm", "")
        self.assertEqual(config_mod.active().safety.category_permissions["31"],
                         "confirm")

    def test_unknown_category_is_refused(self):
        self.assertIn("not a category", auto.permissions("99", "block"))
        self.assertEqual(
            config_mod.active().safety.category_permissions, {})

    def test_bad_mode_is_refused(self):
        self.assertIn("allow, confirm or block", auto.permissions("31", "maybe"))
        self.assertEqual(
            config_mod.active().safety.category_permissions, {})

    def test_permissions_lists_what_is_set(self):
        auto.permissions("31", "confirm")
        listed = auto.permissions("", "")
        self.assertIn("31", listed)
        self.assertIn("confirm", listed)
        self.assertIn("Finance", listed)


class TestCategoryGateIsEnforced(unittest.TestCase):
    """The gate has to be real, not a preference nobody reads."""

    def test_block_refuses_the_whole_category(self):
        policy = ActionPolicy.from_config(
            SafetyConfig(category_permissions={"31": "block"}))
        decision = policy.evaluate("fin_pay", "electric bill", "", kind="fs")
        self.assertFalse(decision.allowed)
        self.assertIn("block", decision.reason)

    def test_confirm_forces_confirmation(self):
        policy = ActionPolicy.from_config(
            SafetyConfig(category_permissions={"31": "confirm"}))
        decision = policy.evaluate("fin_pay", "electric bill", "", kind="fs")
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.needs_confirmation)

    def test_another_category_is_untouched(self):
        policy = ActionPolicy.from_config(
            SafetyConfig(category_permissions={"31": "block"}))
        self.assertTrue(policy.evaluate("file_find", "a.pdf", "", kind="fs").allowed)

    def test_nothing_set_means_everything_is_allowed(self):
        policy = ActionPolicy.from_config(SafetyConfig())
        self.assertTrue(policy.evaluate("fin_pay", "bill", "", kind="fs").allowed)

    def test_an_unknown_action_is_not_gated(self):
        policy = ActionPolicy.from_config(
            SafetyConfig(category_permissions={"31": "block"}))
        self.assertTrue(policy.evaluate("not_a_capability", "", "").allowed)

    def test_a_nonsense_setting_is_ignored_rather_than_enforced(self):
        policy = ActionPolicy.from_config(
            SafetyConfig(category_permissions={"31": "nonsense"}))
        self.assertTrue(policy.evaluate("fin_pay", "bill", "", kind="fs").allowed)


if __name__ == "__main__":
    unittest.main()