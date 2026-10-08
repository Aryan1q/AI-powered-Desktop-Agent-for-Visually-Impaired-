"""Triggers: the "when this happens, do that" engine.

The behaviour that matters is not the parsing, it is the two properties that
keep a trigger from doing damage: an unknown condition never fires, and a
settled-file check never fires twice for the same file.
"""
import tempfile
import time
import unittest
from pathlib import Path

from tests._support import ROOT  # noqa: F401
from ufo import config as config_mod
from ufo.actions import auto, triggers
from ufo.config import Config


class TestParsing(unittest.TestCase):
    def test_known_conditions_are_recognised(self):
        cases = {
            "the battery is low": "battery_low",
            "on battery": "on_battery",
            "the charger is plugged in": "charger_plugged",
            "the screen is locked": "screen_locked",
            "at night": "at_night",
            "it is the weekend": "weekend",
            "cpu is high": "cpu_high",
            "a download finishes": "download_finished",
            "while i'm away": "while_away",
            "always": "always",
        }
        for text, want in cases.items():
            self.assertEqual(triggers.parse_condition(text)[0], want, text)

    def test_unknown_condition_is_not_guessed(self):
        """A trigger that fires on the wrong thing is worse than one that never fires."""
        for text in ("when the flux capacitor charges", "the moon is full",
                     "banana", ""):
            self.assertEqual(triggers.parse_condition(text)[0], "unknown", text)

    def test_a_folder_named_downloads_is_not_a_download(self):
        """`C:/Users/x/Downloads` names a folder. Matching it as a download event
        watches the wrong thing."""
        kind, arg = triggers.parse_condition(
            "a file appears in C:/Users/x/Downloads")
        self.assertEqual(kind, "file_appeared")
        self.assertIn("downloads", arg)

    def test_arguments_are_extracted(self):
        self.assertEqual(triggers.parse_condition("notepad is open")[1], "notepad")
        self.assertEqual(triggers.parse_condition("connected to HomeWifi")[1],
                         "homewifi")
        self.assertEqual(triggers.parse_condition("email from boss@x.com")[1],
                         "boss@x.com")

    def test_describe_is_human_readable(self):
        self.assertIn("battery", triggers.describe_condition("the battery is low"))


class TestEvaluation(unittest.TestCase):
    def setUp(self):
        night = triggers.time.mktime((2026, 3, 3, 23, 30, 0, 0, 0, -1))
        self.ctx = triggers.Context(now=night)
        self.ctx.battery_percent = 18
        self.ctx.battery_charging = False
        self.ctx.on_mains = False
        self.ctx.screen_locked = True
        self.ctx.cpu_percent = 91
        self.ctx.on_wifi = "HomeWifi"
        self.ctx.window_titles = ["Notepad - notes.txt"]
        self.ctx.foreground = "Notepad - notes.txt"

    def test_true_conditions(self):
        for cond in ("the battery is low", "on battery", "the screen is locked",
                     "at night", "cpu is high", "connected to HomeWifi",
                     "notepad is open", "always"):
            self.assertTrue(triggers.evaluate(cond, self.ctx), cond)

    def test_false_conditions(self):
        for cond in ("the charger is plugged in", "while i'm away"):
            self.assertFalse(triggers.evaluate(cond, self.ctx), cond)

    def test_unknown_condition_never_fires(self):
        self.assertFalse(
            triggers.evaluate("when the flux capacitor charges", self.ctx))

    def test_charging_suppresses_the_low_battery_alert(self):
        """Telling someone to charge a battery that is charging is noise."""
        self.ctx.battery_charging = True
        self.assertFalse(triggers.evaluate("the battery is low", self.ctx))


class TestFolderWatch(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ufo-trig-")
        self.cfg = Config()
        self.cfg.storage.home = self.home
        self._previous = config_mod._ACTIVE
        config_mod.set_active(self.cfg)
        self.folder = Path(self.home) / "inbox"
        self.folder.mkdir()
        self.ctx = triggers.Context(now=time.time())
        self.ctx.battery_percent = 95
        self.ctx.on_mains = True

    def tearDown(self):
        config_mod.set_active(self._previous)

    def fired(self) -> list[str]:
        return [f.reason for f in triggers.check_now(self.ctx, run=False)]

    def test_an_empty_folder_can_still_fire(self):
        """An empty folder must not stay in the 'first run' state forever."""
        auto.watch_folder(str(self.folder), "note it")
        self.assertEqual(self.fired(), [])
        (self.folder / "invoice.pdf").write_text("x", encoding="utf-8")
        self.assertEqual(len(self.fired()), 1)

    def test_pre_existing_files_do_not_fire(self):
        (self.folder / "old.txt").write_text("x", encoding="utf-8")
        auto.watch_folder(str(self.folder), "note it")
        self.assertEqual(self.fired(), [])

    def test_each_file_fires_once(self):
        auto.watch_folder(str(self.folder), "note it")
        self.fired()
        (self.folder / "a.pdf").write_text("x", encoding="utf-8")
        self.assertEqual(len(self.fired()), 1)
        self.assertEqual(self.fired(), [])

    def test_a_partial_download_does_not_fire(self):
        auto.watch_folder(str(self.folder), "note it")
        self.fired()
        (self.folder / "movie.iso.crdownload").write_text("part", encoding="utf-8")
        self.assertEqual(self.fired(), [])
        (self.folder / "movie.iso").write_text("done", encoding="utf-8")
        self.assertEqual(len(self.fired()), 1)

    def test_unwatch_stops_the_firing(self):
        auto.watch_folder(str(self.folder), "note it")
        self.fired()
        auto.unwatch(str(self.folder), "")
        (self.folder / "later.pdf").write_text("x", encoding="utf-8")
        self.assertEqual(self.fired(), [])

    def test_a_missing_folder_is_not_an_error(self):
        auto.watch_folder(str(self.folder), "note it")
        self.fired()
        import shutil

        shutil.rmtree(self.folder)
        self.assertEqual(self.fired(), [])


class TestRuleStorage(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ufo-rules-")
        self.cfg = Config()
        self.cfg.storage.home = self.home
        self._previous = config_mod._ACTIVE
        config_mod.set_active(self.cfg)

    def tearDown(self):
        config_mod.set_active(self._previous)

    def test_an_unreadable_condition_is_refused_not_stored(self):
        message = auto.add_rule("if the flux capacitor charges", "then fly")
        self.assertIn("do not understand", message)
        self.assertEqual(auto._load().get("rules", []), [])

    def test_a_readable_condition_is_stored(self):
        message = auto.add_rule("if battery is low", "then tell me")
        self.assertIn("rule saved", message)
        self.assertEqual(len(auto._load()["rules"]), 1)

    def test_rule_list_describes_the_condition(self):
        auto.add_rule("if battery is low", "then tell me")
        self.assertIn("battery is low", auto.rule_list("", ""))


if __name__ == "__main__":
    unittest.main()