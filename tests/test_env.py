"""Tests for the .env loader.

The loader is the one place a secret enters the process, so the tests care
about three things: it never overwrites a deliberate setting, it never lets an
arbitrary key through, and it never puts a value anywhere it could be read back.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ufo import env


class ParseTest(unittest.TestCase):
    def test_basic_assignment(self):
        self.assertEqual(env.parse("GEMINI_API_KEY=abc123"),
                         {"GEMINI_API_KEY": "abc123"})

    def test_export_prefix_is_accepted(self):
        self.assertEqual(env.parse("export GEMINI_API_KEY=abc"),
                         {"GEMINI_API_KEY": "abc"})

    def test_quotes_are_stripped(self):
        self.assertEqual(env.parse('GEMINI_API_KEY="a b c"'),
                         {"GEMINI_API_KEY": "a b c"})
        self.assertEqual(env.parse("OPENAI_API_KEY='a b c'"),
                         {"OPENAI_API_KEY": "a b c"})

    def test_other_quote_inside_is_kept(self):
        self.assertEqual(env.parse('GEMINI_API_KEY="a\'b"'),
                         {"GEMINI_API_KEY": "a'b"})

    def test_comments_and_blanks_are_skipped(self):
        text = "# a comment\n\n   \nGEMINI_API_KEY=abc\n"
        self.assertEqual(env.parse(text), {"GEMINI_API_KEY": "abc"})

    def test_trailing_comment_is_stripped_from_a_value(self):
        """'KEY=abc  # note' must send 'abc', not the comment as part of the key."""
        for text in ("GEMINI_API_KEY=abc  # rotated",
                     "GEMINI_API_KEY=abc # rotated",
                     "GEMINI_API_KEY=abc\t# rotated"):
            self.assertEqual(env.parse(text), {"GEMINI_API_KEY": "abc"}, text)

    def test_hash_inside_quotes_is_kept(self):
        self.assertEqual(env.parse('GEMINI_API_KEY="abc#def"'),
                         {"GEMINI_API_KEY": "abc#def"})

    def test_bare_hash_in_value_is_kept(self):
        """Without a space before it, '#' is part of the value, not a comment."""
        self.assertEqual(env.parse("GEMINI_API_KEY=abc#def"),
                         {"GEMINI_API_KEY": "abc#def"})

    def test_malformed_lines_do_not_raise(self):
        text = "no equals sign here\nGEMINI_API_KEY=abc\n=\n=novalue\n"
        self.assertEqual(env.parse(text), {"GEMINI_API_KEY": "abc"})

    def test_empty_value_is_ignored(self):
        self.assertEqual(env.parse('GEMINI_API_KEY=""'), {})

    def test_only_allowlisted_keys_are_read(self):
        """A .env may hold anything; only known key names may reach os.environ."""
        text = ("PATH=C:\\evil\nGEMINI_API_KEY=abc\n"
                "UFO_SAFETY_DRY_RUN=0\nHOME=C:\\other\n")
        self.assertEqual(env.parse(text), {"GEMINI_API_KEY": "abc"})

    def test_a_safety_switch_cannot_be_set_from_env_file(self):
        """Otherwise a stray line could quietly disable the dry-run guard."""
        self.assertEqual(env.parse("UFO_SAFETY_DRY_RUN=1"), {})

    def test_value_with_equals_signs_survives(self):
        self.assertEqual(env.parse("GEMINI_API_KEY=a=b=c"),
                         {"GEMINI_API_KEY": "a=b=c"})


class LoadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / ".env"
        self.path.write_text("GEMINI_API_KEY=from-file\n", encoding="utf-8")

    def _load(self, **kwargs):
        """Load with a clean slate, and report what os.environ looks like *inside*
        the patch. Asserting outside the `with` would read the restored
        environment, not the one the loader produced.
        """
        with mock.patch.object(env, "candidate_paths", return_value=[self.path]), \
             mock.patch.dict(os.environ, {}, clear=False):
            for key in env.ALLOWED:
                os.environ.pop(key, None)
            applied = env.load_env(**kwargs)
            return applied, dict(os.environ)

    def test_sets_missing_key(self):
        applied, environ = self._load()
        self.assertEqual(applied, {"GEMINI_API_KEY": "from-file"})
        self.assertEqual(environ["GEMINI_API_KEY"], "from-file")

    def test_existing_env_var_wins(self):
        """A per-run override must not need the file edited."""
        with mock.patch.object(env, "candidate_paths", return_value=[self.path]), \
             mock.patch.dict(os.environ, {"GEMINI_API_KEY": "from-shell"}):
            for key in env.ALLOWED:
                if key != "GEMINI_API_KEY":
                    os.environ.pop(key, None)
            applied = env.load_env()
            self.assertEqual(applied, {})
            self.assertEqual(os.environ["GEMINI_API_KEY"], "from-shell")

    def test_missing_file_is_not_an_error(self):
        with mock.patch.object(env, "candidate_paths",
                               return_value=[Path(self.tmp.name) / "nope"]):
            self.assertEqual(env.load_env(), {})

    def test_unreadable_file_is_not_an_error(self):
        bad = Path(self.tmp.name) / "sub" / "dir" / ".env"
        with mock.patch.object(env, "candidate_paths", return_value=[bad]):
            self.assertEqual(env.load_env(), {})

    def test_ufo_no_dotenv_opt_out(self):
        with mock.patch.object(env, "candidate_paths", return_value=[self.path]), \
             mock.patch.dict(os.environ, {"UFO_NO_DOTENV": "1"}):
            for key in env.ALLOWED:
                os.environ.pop(key, None)
            self.assertEqual(env.load_env(), {})
            self.assertNotIn("GEMINI_API_KEY", os.environ)

    def test_force_bypasses_the_opt_out(self):
        with mock.patch.object(env, "candidate_paths", return_value=[self.path]), \
             mock.patch.dict(os.environ, {"UFO_NO_DOTENV": "1"}):
            for key in env.ALLOWED:
                os.environ.pop(key, None)
            self.assertEqual(env.load_env(force=True),
                             {"GEMINI_API_KEY": "from-file"})
            self.assertEqual(os.environ["GEMINI_API_KEY"], "from-file")

    def test_project_wins_over_home(self):
        """candidate_paths is lowest priority first; the first hit is used."""
        paths = env.candidate_paths()
        self.assertEqual(len(paths), 2)
        self.assertTrue(paths[0].name == ".env")
        self.assertIn(".ufo", str(paths[1]))


class StatusTest(unittest.TestCase):
    def test_reports_presence_not_value(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "secret-value"}):
            status = env.env_status()
        self.assertTrue(status["GEMINI_API_KEY"])
        self.assertNotIn("secret-value", repr(status))
        for value in status.values():
            self.assertIsInstance(value, bool)


class WriteEnvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / ".env"

    def test_writes_and_rereads(self):
        env.write_env({"GEMINI_API_KEY": "written"}, self.path)
        self.assertEqual(env.parse(self.path.read_text(encoding="utf-8")),
                         {"GEMINI_API_KEY": "written"})

    def test_preserves_existing_keys(self):
        self.path.write_text("OPENAI_API_KEY=keep-me\n", encoding="utf-8")
        env.write_env({"GEMINI_API_KEY": "new"}, self.path)
        parsed = env.parse(self.path.read_text(encoding="utf-8"))
        self.assertEqual(parsed, {"OPENAI_API_KEY": "keep-me",
                                  "GEMINI_API_KEY": "new"})

    def test_refuses_unknown_key(self):
        with self.assertRaises(ValueError):
            env.write_env({"PATH": "C:\\evil"}, self.path)
        self.assertFalse(self.path.exists())

    def test_empty_value_is_not_written(self):
        env.write_env({"GEMINI_API_KEY": ""}, self.path)
        self.assertEqual(env.parse(self.path.read_text(encoding="utf-8")), {})


class ProjectFileTest(unittest.TestCase):
    """The real .env in this repository."""

    def test_example_and_real_file_are_gitignored(self):
        ignore = (Path(__file__).resolve().parent.parent / ".gitignore")
        if not ignore.exists():
            self.skipTest("no .gitignore")
        lines = [ln.strip() for ln in ignore.read_text(encoding="utf-8").splitlines()]
        self.assertIn(".env", lines)

    def test_example_file_contains_no_real_key(self):
        example = Path(__file__).resolve().parent.parent / ".env.example"
        if not example.exists():
            self.skipTest("no .env.example")
        text = example.read_text(encoding="utf-8")
        for value in env.parse(text).values():
            self.assertEqual(value, "", ".env.example must not ship a real key")

    def test_no_committed_file_contains_a_key(self):
        """No real key may appear outside the gitignored .env.

        The scan matches the actual values read from .env at run time rather
        than key *prefixes*. A prefix match flags the synthetic fixtures that
        every redaction test necessarily contains ('sk-ant-abcdef0123...'),
        and a test that cries wolf gets deleted rather than fixed.

        An empty .env is a valid state — this install runs fully local on
        Ollama — so the test must not demand that a key be present. It still
        refuses to pass silently: with no key to compare against it also scans
        for the high-entropy shapes real provider keys have.
        """
        root = Path(__file__).resolve().parent.parent
        this = Path(__file__).resolve()
        env_file = root / ".env"
        if not env_file.exists():
            self.skipTest("no .env to compare against")

        secrets = [v for v in env.parse(env_file.read_text(encoding="utf-8")).values()
                   if len(v) >= 16]
        skip_dirs = {".venv", "__pycache__", ".git", "node_modules", "logs", "runs"}

        offenders = []
        scanned = 0
        for path in root.rglob("*"):
            if not path.is_file() or path.resolve() in (this, env_file):
                continue
            if skip_dirs & set(path.parts):
                continue
            try:
                if path.stat().st_size > 5_000_000:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            scanned += 1
            for value in secrets:
                if value in text:
                    offenders.append(str(path.relative_to(root)))
        self.assertEqual(offenders, [],
                         "a live key is committed outside .env: "
                         f"{sorted(set(offenders))}")

    def test_committed_files_hold_no_key_shaped_string(self):
        """Runs whether or not .env has a key in it.

        With no key loaded there is nothing to compare against, so this scans
        committed text for the shapes provider keys actually have. The
        synthetic fixtures in the redaction tests are excluded by an explicit
        allowlist of exact values, not by pattern, so a real key pasted into a
        test still fails.
        """
        import re

        root = Path(__file__).resolve().parent.parent
        skip_dirs = {".venv", "__pycache__", ".git", "node_modules", "logs", "runs"}
        known_fixtures = {
            "sk-ant-abcdef0123456789ABCDEF",
            "sk-ant-abcdef0123456789abcdef0123456789",
            "sk-openai-abcdef0123456789abcdef0123456789",
        }

        patterns = {
            "google/gemini": re.compile(r"\bAQ\.[A-Za-z0-9_-]{30,}"),
            "anthropic": re.compile(r"\bsk-ant-[A-Za-z0-9]{20,}"),
            "openai": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}"),
        }

        offenders = []
        scanned = 0
        for path in root.rglob("*"):
            if not path.is_file() or path.name == ".env":
                continue
            if skip_dirs & set(path.parts):
                continue
            try:
                if path.stat().st_size > 5_000_000:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            scanned += 1
            for name, pattern in patterns.items():
                for match in pattern.findall(text):
                    if match in known_fixtures:
                        continue
                    offenders.append(f"{path.relative_to(root)} ({name})")

        self.assertGreater(scanned, 10, "the scan barely looked at anything")
        self.assertEqual(offenders, [],
                         "a key-shaped string is committed: "
                         f"{sorted(set(offenders))}")


if __name__ == "__main__":
    unittest.main()
