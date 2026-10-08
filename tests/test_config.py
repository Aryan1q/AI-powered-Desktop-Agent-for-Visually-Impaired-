"""Config layering: defaults, file, env, overrides — and the failure modes."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._support import ROOT  # noqa: F401  (path bootstrap)
from ufo.config import Config
from ufo.errors import ConfigError


class TestPersistenceAcrossProcesses(unittest.TestCase):
    """`ufo config allow X` is only meaningful if the next command sees it.

    `load()` used to read a file only when a path was passed explicitly, so
    every save went to `~/.ufo/config.json` and every later process started
    from pure defaults: the allowlist appeared to be set and then was not.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {"UFO_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("UFO_CONFIG", None)

    def test_saved_allowlist_is_read_back(self):
        Config().save()
        first = Config()
        first.safety.allowlist = ["Notepad"]
        first.save()

        self.assertEqual(Config.load().safety.allowlist, ["Notepad"])

    def test_config_path_is_recorded(self):
        Config.load().save()
        self.assertEqual(Config.load().config_path, self.home / "config.json")

    def test_absent_default_file_is_not_an_error(self):
        """A fresh install has no config.json and must not complain."""
        self.assertFalse((self.home / "config.json").exists())
        cfg = Config.load()
        self.assertEqual(cfg.safety.allowlist, [])

    def test_every_saved_section_survives(self):
        cfg = Config()
        cfg.safety.allowlist = ["Notepad"]
        cfg.voice.enabled = True
        cfg.phone.port = 9999
        cfg.save()
        back = Config.load()
        self.assertEqual(back.safety.allowlist, ["Notepad"])
        self.assertTrue(back.voice.enabled)
        self.assertEqual(back.phone.port, 9999)

    def test_explicit_path_still_wins(self):
        other = self.home / "other.json"
        other.write_text(json.dumps({"safety": {"allowlist": ["Excel"]}}),
                         encoding="utf-8")
        (self.home / "config.json").write_text(
            json.dumps({"safety": {"allowlist": ["Notepad"]}}), encoding="utf-8")
        self.assertEqual(Config.load(str(other)).safety.allowlist, ["Excel"])

    def test_named_missing_file_is_still_an_error(self):
        with self.assertRaises(ConfigError):
            Config.load(str(self.home / "nope.json"))


class TestDefaults(unittest.TestCase):
    def test_fails_closed(self):
        cfg = Config()
        self.assertEqual(cfg.safety.allowlist, [],
                         "an empty allowlist must allow nothing")
        self.assertFalse(cfg.safety.dry_run)
        self.assertEqual(cfg.safety.emergency_hotkey, "ctrl+alt+q")
        self.assertEqual(cfg.automation.max_steps, 30)

    def test_to_dict_is_json_serialisable(self):
        json.dumps(Config().to_dict(), default=str)


class TestFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "config.json"

    def tearDown(self):
        self.dir.cleanup()

    def write(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8")
        return self.path

    def test_nested_sections_merge_over_defaults(self):
        self.write({"safety": {"dry_run": True},
                    "automation": {"max_steps": 5}})
        cfg = Config.load(self.path)
        self.assertTrue(cfg.safety.dry_run)
        self.assertEqual(cfg.automation.max_steps, 5)
        self.assertEqual(cfg.automation.step_delay,
                         Config().automation.step_delay, "untouched keys keep defaults")

    def test_unknown_section_is_rejected(self):
        self.write({"nope": {}})
        with self.assertRaises(ConfigError) as ctx:
            Config.load(self.path)
        self.assertIn("nope", str(ctx.exception))

    def test_unknown_key_is_rejected(self):
        self.write({"safety": {"dry_ryn": True}})
        with self.assertRaises(ConfigError) as ctx:
            Config.load(self.path)
        self.assertIn("dry_ryn", str(ctx.exception))

    def test_invalid_json_is_reported_clearly(self):
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ConfigError):
            Config.load(self.path)

    def test_missing_file_raises(self):
        with self.assertRaises(ConfigError):
            Config.load(self.path)

    def test_round_trip(self):
        cfg = Config()
        cfg.safety.allowlist = ["Notepad"]
        cfg.automation.max_steps = 7
        saved = cfg.save(self.path)
        again = Config.load(saved)
        self.assertEqual(again.safety.allowlist, ["Notepad"])
        self.assertEqual(again.automation.max_steps, 7)


class TestEnv(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def test_env_overrides_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            path.write_text(json.dumps({"automation": {"max_steps": 5}}),
                            encoding="utf-8")
            os.environ["UFO_AUTOMATION_MAX_STEPS"] = "11"
            os.environ["UFO_SAFETY_DRY_RUN"] = "true"
            cfg = Config.load(path)
        self.assertEqual(cfg.automation.max_steps, 11)
        self.assertTrue(cfg.safety.dry_run)

    def test_env_coercion(self):
        os.environ["UFO_SAFETY_ALLOWLIST"] = "Notepad, Calculator"
        os.environ["UFO_LLM_TEMPERATURE"] = "0.4"
        os.environ["UFO_SERVER_PORT"] = "9000"
        cfg = Config()
        cfg._merge_env()
        self.assertEqual(cfg.safety.allowlist, ["Notepad", "Calculator"])
        self.assertAlmostEqual(cfg.llm.temperature, 0.4)
        self.assertEqual(cfg.server.port, 9000)

    def test_bad_env_value_raises(self):
        os.environ["UFO_AUTOMATION_MAX_STEPS"] = "not-a-number"
        with self.assertRaises(ConfigError):
            Config()._merge_env()


class TestTypeCoercion(unittest.TestCase):
    """A safety switch that accepts the *string* 'false' is a switch that is
    still on. Every source funnels through the same coercion."""

    def test_string_false_does_not_leave_dry_run_on(self):
        for raw in ("false", "False", "FALSE", "no", "off", "0", ""):
            cfg = Config().apply_overrides({"safety.dry_run": raw})
            self.assertIs(cfg.safety.dry_run, False, f"dry_run={raw!r}")

    def test_string_true_turns_dry_run_on(self):
        for raw in ("true", "True", "yes", "on", "1"):
            cfg = Config().apply_overrides({"safety.dry_run": raw})
            self.assertIs(cfg.safety.dry_run, True, f"dry_run={raw!r}")

    def test_numbers_and_lists(self):
        cfg = Config().apply_overrides({"automation.max_steps": "12",
                                        "safety.allowlist": "Notepad, Calc"})
        self.assertEqual(cfg.automation.max_steps, 12)
        self.assertEqual(cfg.safety.allowlist, ["Notepad", "Calc"])

    def test_zero_is_not_swallowed(self):
        cfg = Config().apply_overrides({"llm.temperature": 0,
                                        "automation.max_steps": 0})
        self.assertEqual(cfg.llm.temperature, 0.0)
        self.assertEqual(cfg.automation.max_steps, 0)

    def test_real_booleans_still_work(self):
        cfg = Config().apply_overrides({"safety.dry_run": True})
        self.assertIs(cfg.safety.dry_run, True)

    def test_uncoercible_value_is_reported(self):
        with self.assertRaises(ConfigError) as ctx:
            Config().apply_overrides({"automation.max_steps": "lots"})
        self.assertIn("max_steps", str(ctx.exception))

    def test_file_values_are_coerced_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            path.write_text(json.dumps({"safety": {"dry_run": "false"},
                                        "automation": {"max_steps": "7"}}),
                            encoding="utf-8")
            cfg = Config.load(path)
        self.assertIs(cfg.safety.dry_run, False)
        self.assertEqual(cfg.automation.max_steps, 7)


class TestOverrides(unittest.TestCase):
    def test_dotted_and_nested_forms(self):
        a = Config().apply_overrides({"safety.dry_run": True,
                                      "automation": {"max_steps": 3}})
        self.assertTrue(a.safety.dry_run)
        self.assertEqual(a.automation.max_steps, 3)

    def test_none_means_no_override(self):
        cfg = Config()
        before = cfg.automation.max_steps
        cfg.apply_overrides({"automation.max_steps": None})
        self.assertEqual(cfg.automation.max_steps, before)

    def test_unknown_override_raises(self):
        with self.assertRaises(ConfigError):
            Config().apply_overrides({"safety.nope": 1})
        with self.assertRaises(ConfigError):
            Config().apply_overrides({"nosection.x": 1})


if __name__ == "__main__":
    unittest.main()
