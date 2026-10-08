"""`python main.py` with no arguments must start the app, or say why not.

The failure this protects against is specific: binding a port and printing a
URL, and only then telling the user that no transcription engine exists — after
they have walked to the phone, held the button and spoken. So every thing that
blocks startup is checked first, and the port is only opened once the answer is
yes.
"""
from __future__ import annotations

import argparse
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


class TestBareMainStartsTheApp(unittest.TestCase):
    """`cmd_default` is what a bare `python main.py` runs."""

    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self._prev = os.environ.get("UFO_HOME")
        os.environ["UFO_HOME"] = self._home.name
        from ufo.config import Config
        from ufo.interface import cli
        from ufo.voice import phone

        self.Config = Config
        self.cli = cli
        self.phone = phone
        # Bound to a port by cmd_listen; the real one is stubbed everywhere.
        self.listen = mock.Mock(return_value=0)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("UFO_HOME", None)
        else:
            os.environ["UFO_HOME"] = self._prev
        self._home.cleanup()

    def _cfg(self, **kw):
        cfg = self.Config()
        cfg.llm.provider = kw.get("provider", "ollama")
        cfg.llm.offline = kw.get("offline", True)
        cfg.safety.allowlist = list(kw.get("allowlist", ["Notepad"]))
        cfg.phone.transcription = kw.get("transcription", "local")
        return cfg

    def _args(self):
        return argparse.Namespace(
            host=None, port=None, auto=False, yes=False, allow=[], deny=[],
            allow_all=False, home=None, provider=None, model=None,
            max_steps=None, max_cost=None, voice=False, no_voice=False,
            dry_run=False, mode="computer", task="", input=False,
            confirm_all=False, title=None, exe=None)

    def _run(self, *, whisper=True, **cfg_kw):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(self.phone, "local_transcription_available",
                               return_value=whisper), \
                mock.patch.object(self.cli, "cmd_listen", self.listen), \
                redirect_stdout(out), redirect_stderr(err):
            rc = self.cli.cmd_default(self._args(), self._cfg(**cfg_kw))
        return rc, out.getvalue() + err.getvalue()

    # -- the happy path --
    def test_a_ready_config_starts_listening(self):
        rc, _ = self._run()
        self.assertEqual(rc, 0)
        self.assertTrue(self.listen.called,
                        "a ready config must actually start the app")

    def test_it_prints_the_url_before_waiting(self):
        """cmd_listen prints the URL; the handoff must not swallow it."""
        _, text = self._run()
        self.assertIn("Ctrl+C", text,
                      "the user needs to know how to stop it")

    # -- blockers --
    def test_a_missing_transcription_engine_is_caught_before_binding(self):
        """The cost of missing this is walking to the phone and speaking."""
        rc, text = self._run(whisper=False)
        self.assertEqual(rc, 2)
        self.assertFalse(self.listen.called,
                         "must not open a port it cannot serve")
        self.assertIn("faster-whisper", text)
        self.assertIn("pip install faster-whisper", text)

    def test_an_empty_allowlist_is_caught_before_binding(self):
        rc, text = self._run(allowlist=())
        self.assertEqual(rc, 2)
        self.assertFalse(self.listen.called)
        self.assertIn("allowlist", text)
        self.assertIn("ufo config allow", text)

    def test_a_cloud_provider_with_offline_off_is_caught(self):
        rc, text = self._run(provider="gemini", offline=False)
        self.assertEqual(rc, 2)
        self.assertFalse(self.listen.called)
        self.assertIn("ollama", text)

    def test_several_blockers_are_all_listed_at_once(self):
        """Fixing one problem per run is a bad first experience."""
        rc, text = self._run(provider="gemini", offline=False,
                             allowlist=(), whisper=False)
        self.assertEqual(rc, 2)
        self.assertFalse(self.listen.called)
        self.assertIn("faster-whisper", text)
        self.assertIn("allowlist", text)
        self.assertIn("ollama", text)

    # -- what it must NOT block --
    def test_a_blocked_provider_while_offline_is_on_still_starts(self):
        """Offline already refuses it at request time.

        Stopping here would be redundant and would report a config that works.
        The agent is built lazily, so a provider that is never reached is not a
        startup failure.
        """
        rc, _ = self._run(provider="gemini", offline=True)
        self.assertEqual(rc, 0)
        self.assertTrue(self.listen.called)

    def test_browser_transcription_needs_no_local_engine(self):
        """The phone does the work, so a missing engine is not a blocker."""
        rc, _ = self._run(transcription="browser", whisper=False)
        self.assertEqual(rc, 0)
        self.assertTrue(self.listen.called)


class TestNoSubcommandResolvesToTheDefault(unittest.TestCase):
    """argparse attaches no `func` when no subcommand is named."""

    def test_bare_invocation_runs_the_preflight_not_cmd_run(self):
        """Without this, a bare `python main.py` would try to run an empty task."""
        from ufo.interface import cli

        with mock.patch.object(cli, "cmd_default", return_value=0) as default, \
                mock.patch.object(cli, "cmd_run", return_value=99) as run, \
                mock.patch.object(cli, "load_config") as load, \
                mock.patch.object(cli, "configure"), \
                mock.patch.object(cli.win32, "dpi_awareness_set"):
            from ufo.config import Config
            load.return_value = Config()
            rc = cli.main([])

        self.assertEqual(rc, 0)
        self.assertTrue(default.called, "bare invocation must run cmd_default")
        self.assertFalse(run.called,
                         "cmd_run would demand a task and exit 2")

    def test_an_explicit_subcommand_is_untouched(self):
        """`doctor` and the rest must not be rerouted through the preflight."""
        from ufo.interface import cli

        with mock.patch.object(cli, "cmd_doctor", return_value=0) as doctor, \
                mock.patch.object(cli, "cmd_default", return_value=77) as default, \
                mock.patch.object(cli, "load_config") as load, \
                mock.patch.object(cli, "configure"), \
                mock.patch.object(cli.win32, "dpi_awareness_set"):
            from ufo.config import Config
            load.return_value = Config()
            rc = cli.main(["doctor"])

        self.assertEqual(rc, 0)
        self.assertTrue(doctor.called)
        self.assertFalse(default.called)


if __name__ == "__main__":
    unittest.main()
