"""The shipped config.example.json must actually load.

A starter file that trips the strict unknown-key check, or that silently
overrides your paths with `Path("")`, is worse than no starter file at all.
"""
import json
import unittest
from pathlib import Path

from tests._support import ROOT
from ufo.config import Config

EXAMPLE = ROOT / "config.example.json"


class TestExampleConfig(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))

    def test_is_valid_json(self):
        self.assertIsInstance(self.raw, dict)

    def test_loads_without_error(self):
        cfg = Config.load(EXAMPLE)
        self.assertEqual(cfg.safety.emergency_hotkey, "ctrl+alt+q")

    def test_has_no_comment_keys(self):
        """The loader rejects unknown keys, so comments cannot live in here."""
        for section, body in self.raw.items():
            for key in body:
                self.assertFalse(key.startswith("_"),
                                 f"{section}.{key} looks like a comment")

    def test_paths_are_not_blanked_out(self):
        """An empty string would override the real default with Path('')."""
        cfg = Config.load(EXAMPLE)
        for field in ("home", "log_root", "memory_path", "history_path",
                      "tasks_path", "schedule_path", "state_path", "secrets_path"):
            value = getattr(cfg.storage, field, None)
            if value is not None and str(value):
                self.assertNotEqual(str(value), ".",
                                    f"storage.{field} resolved to the cwd")

    def test_fail_closed_by_default(self):
        self.assertEqual(Config().safety.allowlist, [])

    def test_example_allowlist_is_intentionally_narrow(self):
        cfg = Config.load(EXAMPLE)
        for entry in cfg.safety.allowlist:
            self.assertNotIn(".*", entry,
                             "the example must not ship a wildcard allowlist")

    def test_every_section_matches_a_dataclass(self):
        cfg = Config()
        for section, body in self.raw.items():
            self.assertTrue(hasattr(cfg, section), f"unknown section {section}")
            for key in body:
                self.assertTrue(hasattr(getattr(cfg, section), key),
                                f"unknown key {section}.{key}")


if __name__ == "__main__":
    unittest.main()
