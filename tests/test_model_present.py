"""A model that is not installed must fail before the user speaks, not after.

The failure this replaces: the phone takes an instruction, the narrator reads it
back, the user confirms it, and only then does a 404 arrive — followed by four
more identical attempts, because nothing in the loop knew a missing model is not
a temporary condition. The user had already done the hard part (walked to the
phone, held the button, spoken) when the answer was that nothing was wrong.
"""
from __future__ import annotations

import argparse
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock


def _tags(*names: str) -> str:
    import json
    return json.dumps({"models": [{"name": n, "model": n} for n in names]})


class TestModelPresenceCheck(unittest.TestCase):
    def setUp(self):
        from ufo.interface import cli
        self.cli = cli
        self.urlopen = mock.MagicMock()
        self.urlopen.return_value.__enter__.return_value.read.return_value = \
            _tags("gemma3:4b").encode("utf-8")
        self.patcher = mock.patch("urllib.request.urlopen", self.urlopen)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def _check(self, model: str):
        return self.cli._model_missing_locally(model)

    def test_an_installed_model_is_reported_present(self):
        self.assertIsNone(self._check("gemma3:4b"))

    def test_a_model_that_is_not_installed_is_reported_missing(self):
        result = self._check("llama3.2-vision")
        self.assertIsNotNone(result, "a model that is not there must be caught")
        what, fix = result
        self.assertIn("llama3.2-vision", what)
        self.assertIn("ollama pull llama3.2-vision", fix)

    def test_the_fix_lists_what_is_actually_installed(self):
        """The user has to choose, so the command alone is not enough."""
        _, fix = self._check("llama3.2-vision")
        self.assertIn("gemma3:4b", fix)

    def test_a_bare_family_name_matches_any_of_its_tags(self):
        """Ollama accepts `gemma3` for `gemma3:4b`, so must this."""
        self.assertIsNone(self._check("gemma3"))

    def test_an_uninstalled_tag_is_not_hidden_by_a_family_match(self):
        """`gemma3:99b` is a different model and is not installed.

        Matching only the part before the colon would call this present, and the
        user would sit through the same 404 the check exists to prevent.
        """
        self.assertIsNotNone(self._check("gemma3:99b"))

    def test_a_server_that_is_down_is_reported_with_its_own_fix(self):
        """Down and wrong-name are different problems with different fixes.

        Reporting "not answering" as "pull the model" sends the user off to
        pull a model they already have, against a server that is not running.
        """
        self.urlopen.side_effect = OSError("connection refused")
        result = self._check("gemma3:4b")
        self.assertIsNotNone(result, "an unreachable server must be reported")
        what, fix = result
        self.assertIn("not answering", what)
        self.assertIn("ollama serve", fix)
        self.assertNotIn("ollama pull", fix,
                         "pulling cannot fix a server that is not running")

    def test_an_unparseable_reply_is_reported_rather_than_assumed_fine(self):
        """Anything that is not a real answer is a problem worth naming."""
        self.urlopen.return_value.__enter__.return_value.read.return_value = \
            b"<html>not json</html>"
        self.assertIsNotNone(self._check("gemma3:4b"))

    def test_no_configured_model_is_not_a_startup_blocker(self):
        """With nothing pinned the client discovers one; nothing to report."""
        self.assertIsNone(self._check(""))

    def test_no_installed_models_names_the_pull_command(self):
        """An empty server is answerable: "you have nothing, pull one"."""
        self.urlopen.return_value.__enter__.return_value.read.return_value = \
            _tags().encode("utf-8")
        result = self._check("gemma3:4b")
        self.assertIsNotNone(result)
        self.assertIn("ollama pull gemma3:4b", result[1])


class TestMissingModelStopsTheRun(unittest.TestCase):
    """Once the model is known missing, the run must stop rather than retry."""

    def test_a_fatal_llm_error_is_not_retried(self):
        from ufo.errors import ModelNotAvailable

        self.assertTrue(ModelNotAvailable("x").fatal)

    def test_a_transient_llm_error_is_still_retryable(self):
        from ufo.errors import LLMError, LLMTimeout

        self.assertFalse(LLMError("x").fatal)
        self.assertFalse(LLMTimeout("x").fatal)

    def test_a_404_about_a_model_becomes_the_fatal_error(self):
        from ufo.agent.llm import _status_error
        from ufo.errors import ModelNotAvailable

        err = _status_error(
            RuntimeError("x"), 404,
            '{"message": "model \'gemma3:4b\' not found", "code": "not_found"}')
        self.assertIsInstance(err, ModelNotAvailable)
        self.assertIn("gemma3:4b", str(err))
        self.assertIn("ollama pull", str(err))

    def test_a_404_about_something_else_is_not_treated_as_a_missing_model(self):
        """A wrong 404 must not claim a model is missing when it is not."""
        from ufo.agent.llm import _status_error
        from ufo.errors import ModelNotAvailable

        err = _status_error(RuntimeError("x"), 404, '{"detail": "Not Found"}')
        self.assertNotIsInstance(err, ModelNotAvailable)
        self.assertFalse(getattr(err, "fatal", False))


class TestStartupChecksTheModel(unittest.TestCase):
    """The preflight must consult the server, once the other checks pass."""

    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self._prev = os.environ.get("UFO_HOME")
        os.environ["UFO_HOME"] = self._home.name
        self.addCleanup(self._home.cleanup)
        if self._prev is None:
            self.addCleanup(lambda: os.environ.pop("UFO_HOME", None))
        else:
            self.addCleanup(lambda: os.environ.__setitem__("UFO_HOME", self._prev))

    def _run(self, model: str):
        from ufo.config import Config
        from ufo.interface import cli

        cfg = Config()
        cfg.llm.provider = "ollama"
        cfg.llm.model = model
        cfg.llm.offline = True
        cfg.safety.allowlist = ["Notepad"]
        cfg.phone.transcription = "browser"

        args = argparse.Namespace(
            host=None, port=None, auto=False, yes=False, allow=[], deny=[],
            allow_all=False, home=None, provider=None, model=None,
            max_steps=None, max_cost=None, voice=False, no_voice=False,
            dry_run=False, mode="computer", task="", input=False,
            confirm_all=False, title=None, exe=None)

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "cmd_listen", return_value=0) as listen, \
                mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = \
                _tags("gemma3:4b").encode("utf-8")
            with redirect_stdout(out), redirect_stderr(err):
                rc = cli.cmd_default(args, cfg)
        return rc, out.getvalue() + err.getvalue(), listen

    def test_a_missing_model_stops_before_the_port_is_bound(self):
        rc, text, listen = self._run("llama3.2-vision")
        self.assertEqual(rc, 2)
        self.assertFalse(listen.called,
                         "must not open a port for a model that cannot answer")
        self.assertIn("ollama pull llama3.2-vision", text)

    def test_an_installed_model_starts_normally(self):
        rc, _, listen = self._run("gemma3:4b")
        self.assertEqual(rc, 0)
        self.assertTrue(listen.called)


class TestOllamaModelDiscovery(unittest.TestCase):
    """No model pinned means ask the server, not guess a name."""

    def _tags(self, *entries):
        import json
        return json.dumps({"models": list(entries)})

    def _with_tags(self, payload):
        m = mock.MagicMock()
        m.return_value.__enter__.return_value.read.return_value = payload
        return mock.patch("urllib.request.urlopen", m)

    def test_a_vision_model_is_preferred(self):
        """The agent reads a screenshot every step; a text model guesses."""
        from ufo.agent.llm import discover_ollama_model

        payload = self._tags({"name": "llama3.2:1b", "capabilities": ["completion"]},
                             {"name": "gemma3:4b", "capabilities": ["completion", "vision"]})
        with self._with_tags(payload.encode()):
            self.assertEqual(discover_ollama_model(), "gemma3:4b")

    def test_a_text_only_install_still_runs(self):
        """Degraded is better than refusing; the request will report the limit."""
        from ufo.agent.llm import discover_ollama_model

        payload = self._tags({"name": "llama3.2:1b", "capabilities": ["completion"]})
        with self._with_tags(payload.encode()):
            self.assertEqual(discover_ollama_model(), "llama3.2:1b")

    def test_a_server_that_cannot_be_asked_returns_nothing(self):
        """Never guess: "" makes the caller explain, a wrong name makes a 404."""
        from ufo.agent.llm import discover_ollama_model

        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            self.assertEqual(discover_ollama_model(), "")

    def test_an_empty_install_returns_nothing(self):
        from ufo.agent.llm import discover_ollama_model

        with self._with_tags(self._tags().encode()):
            self.assertEqual(discover_ollama_model(), "")

    def test_an_unpinned_ollama_client_takes_the_discovered_model(self):
        """The whole point: a fresh install must not need a model name typed."""
        from ufo.agent.llm import Client
        from ufo.config import LLMConfig

        payload = self._tags({"name": "gemma3:4b",
                              "capabilities": ["completion", "vision"]})
        with self._with_tags(payload.encode()):
            client = Client(LLMConfig(provider="ollama", model=""))
        self.assertEqual(client.model, "gemma3:4b")

    def test_an_explicit_model_is_never_overridden(self):
        from ufo.agent.llm import Client
        from ufo.config import LLMConfig

        payload = self._tags({"name": "gemma3:4b", "capabilities": ["vision"]})
        with self._with_tags(payload.encode()):
            client = Client(LLMConfig(provider="ollama", model="qwen2.5vl:7b"))
        self.assertEqual(client.model, "qwen2.5vl:7b")


if __name__ == "__main__":
    unittest.main()
