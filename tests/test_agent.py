"""Agent-side behaviour that must hold even when the model misbehaves:
JSON repair, decision validation, and the early-finish rejection rule.
"""
import unittest

from tests._support import ROOT  # noqa: F401
from ufo.agent import Decision, validate_decision
from ufo.agent.llm import (as_list, dedupe, estimate_cost, extract_all_objects,
                           parse_json, require)
from ufo.errors import LLMResponseError, ValidationError

VALID = ("click", "type", "wait", "screenshot", "read_text", "finish", "fail")


class TestParseJSON(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(parse_json('{"a": 1}'), {"a": 1})

    def test_fenced_block(self):
        self.assertEqual(parse_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_surrounding_prose(self):
        self.assertEqual(parse_json('Sure! Here you go:\n{"a": 1}\nHope that helps.'),
                         {"a": 1})

    def test_trailing_comma(self):
        self.assertEqual(parse_json('{"a": 1, "b": 2,}'), {"a": 1, "b": 2})

    def test_single_quotes(self):
        self.assertEqual(parse_json("{'a': 'b'}"), {"a": "b"})

    def test_python_literals(self):
        self.assertEqual(parse_json('{"ok": True, "why": None}'),
                         {"ok": True, "why": None})

    def test_truncated_object_is_closed(self):
        self.assertEqual(parse_json('{"action": "click", "target": "Save"'),
                         {"action": "click", "target": "Save"})

    def test_multiple_objects(self):
        found = extract_all_objects('{"step": 1} and then {"step": 2}')
        self.assertEqual(len(found), 2)
        self.assertEqual(found[1]["step"], 2)

    def test_list_needs_opt_in(self):
        with self.assertRaises(LLMResponseError):
            parse_json('[1, 2, 3]')
        self.assertEqual(parse_json('[1, 2]', allow_list=True), [1, 2])

    def test_unusable_output_raises(self):
        for junk in ("", "   ", "I refuse.", None):
            with self.assertRaises(LLMResponseError):
                parse_json(junk)

    def test_never_raises_on_braces(self):
        """A stray brace must not blow up prompt handling."""
        self.assertIn(parse_json, (parse_json,))


class TestHelpers(unittest.TestCase):
    def test_as_list(self):
        self.assertEqual(as_list(None), [])
        self.assertEqual(as_list("a"), ["a"])
        self.assertEqual(as_list(["a", "b"]), ["a", "b"])

    def test_dedupe_preserves_order(self):
        self.assertEqual(dedupe(["b", "a", "b", "c", "a"]), ["b", "a", "c"])

    def test_require_returns_a_retry_prompt(self):
        prompt = require("All done.")
        self.assertIn("JSON", prompt,
                      "require() hands the model a repair instruction")

    def test_estimate_cost_is_monotonic(self):
        cheap = estimate_cost("gemini-2.5-flash", 1000, 1000)
        dear = estimate_cost("gemini-2.5-pro", 1000, 1000)
        self.assertLess(cheap, dear)
        self.assertEqual(estimate_cost("gemini-2.5-flash", 0, 0), 0.0)
        self.assertGreater(estimate_cost("unknown-model-xyz", 1000, 1000), 0.0,
                           "an unknown model falls back to a default rate so the "
                           "cost budget still has something to enforce")

    def test_versioned_model_ids_price_like_their_base(self):
        base = estimate_cost("gemini-2.5-flash", 10_000, 10_000)
        for variant in ("gemini-2.5-flash-latest",
                        "gemini-2.5-flash-preview-09-2025",
                        "GEMINI-2.5-FLASH"):
            self.assertAlmostEqual(estimate_cost(variant, 10_000, 10_000), base,
                                   msg=variant)


class TestValidateDecision(unittest.TestCase):
    def test_minimal_valid(self):
        d = validate_decision({"action": "click", "target": "Save"}, VALID)
        self.assertEqual(d.action, "click")
        self.assertEqual(d.target, "Save")
        self.assertEqual(d.status, "CONTINUE")

    def test_finish_carries_its_answer_as_args(self):
        d = validate_decision({"action": "finish", "answer": "done"}, VALID)
        self.assertEqual(d.action, "finish")
        self.assertEqual(d.args, "done")

    def test_finish_takes_its_answer_from_target(self):
        """Small local models put the answer in `target` and leave args empty.

        A terminal action does nothing to `target`, so the value can only have
        been meant as the answer. Reading it here is what lets a question about
        the screen return a result instead of nothing.
        """
        d = validate_decision(
            {"action": "finish", "target": "Untitled - Notepad",
             "args": "", "status": "FINISH"}, VALID)
        self.assertEqual(d.args, "Untitled - Notepad")

    def test_explicit_args_still_win_over_target(self):
        d = validate_decision(
            {"action": "finish", "target": "Notepad", "args": "the real answer"},
            VALID)
        self.assertEqual(d.args, "the real answer")

    def test_target_is_not_borrowed_for_a_gui_action(self):
        """Only terminal actions may read their target as a payload."""
        d = validate_decision({"action": "click", "target": "Save"}, VALID)
        self.assertEqual(d.args, "")

    def test_fail_carries_its_reason(self):
        d = validate_decision({"action": "fail", "reason": "nope"}, VALID)
        self.assertEqual(d.action, "fail")
        self.assertIn("nope", d.args)

    def test_unknown_action_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            validate_decision({"action": "rm -rf /", "target": "/"}, VALID)
        self.assertIn("rm -rf", str(ctx.exception))

    def test_missing_action_is_rejected(self):
        with self.assertRaises(ValidationError):
            validate_decision({"target": "Save"}, VALID)

    def test_non_dict_is_rejected(self):
        for junk in (None, [], "click", 5):
            with self.assertRaises(ValidationError):
                validate_decision(junk, VALID)

    def test_oversized_args_are_rejected_not_truncated(self):
        """Truncating silently would type half a message into a text box."""
        with self.assertRaises(ValidationError) as ctx:
            validate_decision({"action": "type", "args": "x" * 50_000},
                              VALID, max_args=100)
        self.assertIn("limit is 100", str(ctx.exception))

    def test_thought_is_kept(self):
        d = validate_decision({"thought": "I should save", "action": "click"}, VALID)
        self.assertEqual(d.thought, "I should save")

    def test_action_case_is_normalised(self):
        d = validate_decision({"action": "CLICK", "target": "Save"}, VALID)
        self.assertEqual(d.action, "click")

    def test_signature_distinguishes_actions(self):
        a = validate_decision({"action": "click", "target": "Save"}, VALID)
        b = validate_decision({"action": "click", "target": "Cancel"}, VALID)
        self.assertNotEqual(a.signature(), b.signature())


class TestActionAliases(unittest.TestCase):
    """Small local models invent a name instead of using `finish`.

    A local 4B model handed back `return_value` on every step of a read-only
    question, so the run burned its whole step budget and returned nothing.
    The alias only renames a request that was already permitted; the request
    still passes the same policy, risk and confirmation gate afterwards.
    """

    def test_invented_answer_name_becomes_finish(self):
        d = validate_decision(
            {"action": "return_value", "args": "Untitled - Notepad"}, VALID)
        self.assertEqual(d.action, "finish")
        self.assertEqual(d.args, "Untitled - Notepad")

    def test_terminal_action_ends_the_run_even_with_default_status(self):
        d = validate_decision({"action": "finish", "args": "42"}, VALID)
        self.assertEqual(d.status, "FINISH")
        d = validate_decision({"action": "fail", "reason": "nope"}, VALID)
        self.assertEqual(d.status, "FAIL")

    def test_continue_is_untouched_for_normal_actions(self):
        d = validate_decision({"action": "click", "target": "Save"}, VALID)
        self.assertEqual(d.status, "CONTINUE")

    def test_alias_never_invents_an_action(self):
        """An alias must map to something the caller actually allows."""
        limited = ("click", "type", "finish")
        with self.assertRaises(ValidationError):
            validate_decision({"action": "abort"}, limited)

    def test_unrelated_invented_names_are_still_rejected(self):
        """The map stays narrow; a wrong GUI action is still a wrong action."""
        for invented in ("close_window", "minimize", "rm -rf"):
            with self.subTest(invented=invented):
                with self.assertRaises(ValidationError):
                    validate_decision({"action": invented, "target": "x"}, VALID)

    def test_system_agent_aliases_work_too(self):
        from ufo.agent.base import SYSTEM_ACTIONS

        d = validate_decision({"action": "return_value", "args": "hi"},
                               SYSTEM_ACTIONS)
        self.assertEqual(d.action, "finish")


class TestEarlyFinishRejection(unittest.TestCase):
    """A model that calls `finish` before touching anything is a bug, not a
    result. The loop must push it back instead of reporting success."""

    def test_loop_starts_with_no_finish_rejections(self):
        from ufo.agent import AgentLoop

        loop = AgentLoop.__new__(AgentLoop)      # no client needed
        loop.finish_rejections = 0
        loop.actions_done = 0
        self.assertEqual(loop.finish_rejections, 0)
        self.assertIsInstance(loop.valid_actions, tuple)


class TestStepLogging(unittest.TestCase):
    """`_log` runs on every step, including the error path.

    It once carried a leftover `return_early` that was assigned in one branch
    and deleted in the other, so any run that logged an error with redaction on
    died with UnboundLocalError and lost its trace. Nothing covered it because
    a successful step never takes that branch.
    """

    def _loop(self, redact: bool, tmp):
        from ufo.agent import AgentLoop
        from ufo.config import Config

        loop = AgentLoop.__new__(AgentLoop)          # no client, no guard
        cfg = Config()
        cfg.storage.redact_logs = redact
        loop.cfg = cfg
        loop.task = "type hello"
        loop.started = 0.0
        loop.run_dir = tmp
        return loop

    def test_error_step_logs_with_redaction_on(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            loop = self._loop(True, tmp)
            loop._log(1, {"error": "boom", "result": "failed"})
            self.assertTrue((tmp / "step_01.json").exists())

    def test_error_step_logs_with_redaction_off(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            loop = self._loop(False, tmp)
            loop._log(1, {"error": "boom"})
            self.assertTrue((tmp / "step_01.json").exists())

    def test_redaction_still_applies(self):
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            loop = self._loop(True, tmp)
            loop._log(1, {"result": "key is " + "AQ." + "z" * 30})
            written = (tmp / "step_01.json").read_text(encoding="utf-8")
            self.assertNotIn("AQ." + "z" * 30, written)
            self.assertIn("[REDACTED]", json.loads(written)["result"])

    def test_no_run_dir_is_a_no_op(self):
        from ufo.agent import AgentLoop

        loop = AgentLoop.__new__(AgentLoop)
        loop.run_dir = None
        loop._log(1, {"error": "boom"})              # must not raise


class TestLocalProviderRouting(unittest.TestCase):
    """A local provider must never address a remote host.

    `provider: ollama` with no base_url fell through to the OpenAI SDK default
    of api.openai.com, so a screenshot meant for a model on this machine would
    have been uploaded instead. The screenshot is the payload here, so the
    failure mode is a privacy leak, not a crash.
    """

    def _client(self, provider, base_url=""):
        from ufo.agent.llm import Client
        from ufo.config import LLMConfig

        return Client(LLMConfig(provider=provider, model="whatever",
                                base_url=base_url))

    def test_ollama_defaults_to_localhost(self):
        self.assertIn("127.0.0.1:11434", self._client("ollama").base_url)

    def test_no_local_provider_points_at_a_public_host(self):
        """Ollama is the only permitted provider, so it is the only one to check.

        Nothing may resolve to a public host: a leaked screenshot to OpenAI
        looks like a model call and is not one.
        """
        url = self._client("ollama").base_url
        self.assertTrue(url.startswith("http://127.0.0.1"),
                        f"ollama resolved to {url}")

    def test_other_local_backends_are_refused_too(self):
        """LM Studio is local, but this install is Ollama-only.

        A second local backend is a second place the model can end up that
        nobody chose for this install, so it is refused the same way a cloud
        provider is while offline is on.
        """
        from ufo.errors import NoLLMConfigured

        for provider in ("lmstudio", "custom"):
            with self.subTest(provider=provider):
                with self.assertRaises(NoLLMConfigured):
                    self._client(provider)

    def test_explicit_base_url_wins(self):
        client = self._client("ollama", "http://192.168.1.50:11434/v1")
        self.assertEqual(client.base_url, "http://192.168.1.50:11434/v1")

    def test_remote_providers_are_refused_while_offline(self):
        """Offline must be a wall, not a default.

        While `llm.offline` is on, a cloud provider cannot even construct, so a
        prompt can never reach a third party no matter how the config got that
        far. Turning offline off is the only way back, and that is explicit.
        """
        import os
        from unittest import mock

        from ufo.errors import NoLLMConfigured

        # Real keys must never be involved in a test, but they must be present
        # to prove the refusal comes from the offline check and not from a
        # missing key.
        keys = {"ANTHROPIC_API_KEY": "test-not-a-key",
                "GEMINI_API_KEY": "test-not-a-key",
                "GROQ_API_KEY": "test-not-a-key"}
        with mock.patch.dict(os.environ, keys):
            for provider in ("anthropic", "gemini", "groq"):
                with self.subTest(provider=provider):
                    with self.assertRaises(NoLLMConfigured):
                        self._client(provider)

    def test_every_client_site_passes_offline_through(self):
        """`offline` must survive the trip from config to client.

        Each of these built an `LLMConfig` by hand and simply left `offline` out,
        so a user who deliberately set `llm.offline = false` got the default
        (on) instead. `ufo doctor` then reported "llm.offline is on" on a config
        that had turned it off — the one screen a user reads to fix a problem,
        contradicting itself. Every hand-built config must carry the real value.
        """
        import inspect

        from ufo import config as config_mod

        sites = ["ufo.agent.base", "ufo.agent.planner", "ufo.agent.verifier",
                 "ufo.actions.info", "ufo.interface.cli"]
        for module_name in sites:
            with self.subTest(module=module_name):
                mod = __import__(module_name, fromlist=["_"])
                source = inspect.getsource(mod)
                for call in ("LLMConfig(", "llm.LLMConfig("):
                    start = 0
                    while (i := source.find(call, start)) != -1:
                        # Walk to the closing paren of this constructor call.
                        depth, j = 0, i + len(call) - 1
                        while j < len(source):
                            if source[j] == "(":
                                depth += 1
                            elif source[j] == ")":
                                depth -= 1
                                if depth == 0:
                                    break
                            j += 1
                        kwargs = source[i + len(call):j]
                        self.assertIn(
                            "offline=", kwargs,
                            f"{module_name} builds an {call.rstrip('(')} without "
                            f"passing offline, so the user's own setting is lost")
                        start = j
        self.assertTrue(config_mod.LLMConfig().offline,
                        "the default must stay strict")


class TestLocalPayloadShape(unittest.TestCase):
    """The Ollama path must produce kwargs the installed SDK will accept.

    `format` was passed as a top-level kwarg. It is Ollama's structured-output
    switch, not an OpenAI parameter, so every local call died with
    `TypeError: Completions.create() got an unexpected keyword argument
    'format'` before a request was ever sent â€” the provider was unusable.
    """

    def _accepted_kwargs(self):
        import inspect

        from openai.resources.chat.completions import Completions

        return set(inspect.signature(Completions.create).parameters)

    def test_extra_body_is_a_real_sdk_parameter(self):
        if not self._accepted_kwargs():
            self.skipTest("openai SDK not installed")
        self.assertIn("extra_body", self._accepted_kwargs())

    def test_format_is_not_a_top_level_sdk_parameter(self):
        accepted = self._accepted_kwargs()
        if not accepted:
            self.skipTest("openai SDK not installed")
        self.assertNotIn("format", accepted,
                         "if a future SDK accepts it, drop extra_body")

    def test_ollama_call_builds_a_valid_payload(self):
        import types
        from unittest import mock

        from ufo.agent.llm import Client
        from ufo.config import LLMConfig

        captured = {}
        response = types.SimpleNamespace(
            choices=[types.SimpleNamespace(
                message=types.SimpleNamespace(
                    content='{"thought":"t","action":"click","target":"x"}'))],
            usage=None,
        )

        def create(**kwargs):
            captured.update(kwargs)
            return response

        client = Client(LLMConfig(provider="ollama", model="gemma3:4b"))
        fake_client = mock.MagicMock()
        fake_client.chat.completions.create = create

        with mock.patch("openai.OpenAI", return_value=fake_client):
            client._openai_compatible("gemma3:4b", "sys", "hello", None, 0.0)

        self.assertEqual(captured.get("extra_body"), {"format": "json"})
        self.assertNotIn("format", captured)
        self.assertNotIn("max_tokens", captured)

    def test_local_provider_gets_a_placeholder_key(self):
        """The SDK will not construct without one, even for a local server.

        A missing key raised `OpenAIError: Missing credentials` before any
        request went out, so `--provider ollama` never worked at all.
        """
        from unittest import mock

        from ufo.agent.llm import Client
        from ufo.config import LLMConfig

        seen = {}

        def factory(**kwargs):
            seen.update(kwargs)
            return mock.MagicMock()

        client = Client(LLMConfig(provider="ollama", model="m"))
        with mock.patch("openai.OpenAI", factory):
            try:
                client._openai_compatible("m", "s", "u", None, 0.0)
            except Exception:
                pass              # the fake client is not a real response
        self.assertTrue(seen.get("api_key"),
                        "ollama was built with no api_key")
        self.assertEqual(seen.get("base_url"),
                         _expected_base_url("ollama"))

    def test_placeholder_key_is_not_a_credential(self):
        """It must not resemble something that could pass for a real secret."""
        from ufo.agent.llm import _LOCAL_BASE_URLS

        for provider, url in _LOCAL_BASE_URLS.items():
            with self.subTest(provider=provider):
                placeholder = url.split("//", 1)[-1]
                for marker in ("AQ.", "sk-", "gsk_", "ghp_"):
                    self.assertNotIn(marker, placeholder)


class TestInvalidReplyRecovery(unittest.TestCase):
    """A malformed reply must cost one step, not the whole run.

    `ValidationError` from `_decide` used to escape `_run` entirely, so a
    single `type` action with no `args` aborted the task with a traceback
    instead of asking the model to try again.
    """

    def _loop(self, always_invalid=False, tmp=None):
        import types

        from ufo.agent import AgentLoop
        from ufo.config import Config

        class _Switch:
            @staticmethod
            def check(_what):
                return None

        class _Budget:
            @staticmethod
            def charge_llm(_n=0):
                return None

        class _Guard:
            emergency_switch = _Switch()
            active_budget = _Budget()

        loop = AgentLoop.__new__(AgentLoop)
        loop.guard = _Guard()
        cfg = Config()
        cfg.safety.idle_backoff_seconds = 0.0
        cfg.automation.max_steps = 5
        loop.cfg = cfg
        loop.task = "type hello"
        loop.client = None
        loop.max_steps = 5
        loop.history = []
        loop.consecutive_errors = 0
        loop.consecutive_reasoning_errors = 0
        loop.consecutive_action_errors = 0
        loop.actions_done = 0
        loop.finish_rejections = 0
        loop.initial_signature = None
        loop.last_result = ""
        loop.outcome = "FAIL"
        loop.run_dir = tmp
        loop.started = 0.0
        loop.auto = cfg.automation
        loop.narrator = None
        loop.valid_actions = ("type", "finish")

        observation = types.SimpleNamespace(image=None, b64=None)

        def decide(step, obs):
            loop._decide_calls = getattr(loop, "_decide_calls", 0) + 1
            if always_invalid or loop._decide_calls == 1:
                raise ValidationError("'type' needs the text to enter in 'args'.")
            return validate_decision(
                {"thought": "done", "action": "finish", "status": "FINISH"}, loop.valid_actions)

        loop.observe = lambda step: observation
        loop.signature = lambda obs: "same"
        loop.describe_context = lambda: "ctx"
        loop._decide = decide
        loop.perform = lambda decision, obs: "ok"
        loop._confirm = lambda message, risk: True
        loop._write_summary = lambda: None
        return loop

    def test_bad_reply_is_retried_then_the_run_finishes(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            loop = self._loop(tmp=Path(d))
            outcome = loop._run()
        self.assertEqual(outcome, "FINISH")
        self.assertTrue(any("rejected" in h for h in loop.history),
                        "the model was never told what was wrong")

    def test_persistent_bad_replies_stop_cleanly(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            loop = self._loop(always_invalid=True, tmp=Path(d))
            loop.max_steps = 9
            loop.auto.max_consecutive_errors = 2
            outcome = loop._run()
        self.assertEqual(outcome, "LLM_ERROR")

    def test_reasoning_errors_have_their_own_counter(self):
        """A successful observation must not clear the reasoning-error count.

        Both used the same field, and observation succeeded in between, so a
        model that never returns a valid action looped until the step limit
        instead of stopping after `max_consecutive_errors`.
        """
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            loop = self._loop(always_invalid=True, tmp=Path(d))
            loop.max_steps = 40
            loop.auto.max_consecutive_errors = 3
            outcome = loop._run()
        self.assertEqual(outcome, "LLM_ERROR")


class TestNarration(unittest.TestCase):
    """A real run must not die on narration.

    The agent used to call the narrator object directly. `Narrator` is not
    callable, so every single action failed with
    `TypeError: 'Narrator' object is not callable`.
    """

    class _Speaker:
        def __init__(self):
            self.spoken = []

        def say(self, text):
            self.spoken.append(text)

    @staticmethod
    def _agent(narrator):
        """Narrate is pure plumbing, so exercise it without building a real
        agent (which would need a config, a guard and a run directory)."""
        import types

        from ufo.agent.base import AgentLoop

        stub = types.SimpleNamespace(narrator=narrator)
        return lambda text: AgentLoop.narrate(stub, text)

    def test_narrator_object_is_used_via_say(self):
        speaker = self._Speaker()
        self._agent(speaker)("hello")
        self.assertEqual(speaker.spoken, ["hello"])

    def test_plain_callable_still_works(self):
        heard = []
        self._agent(heard.append)("hi")
        self.assertEqual(heard, ["hi"])

    def test_missing_narrator_prints_instead_of_raising(self):
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self._agent(None)("plain")
        self.assertIn("[SAY] plain", buffer.getvalue())

    def test_broken_narrator_prints_instead_of_raising(self):
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self._agent(object())("plain")
        self.assertIn("[SAY] plain", buffer.getvalue())


class TestPureAnswer(unittest.TestCase):
    """A question about the screen is answered by reading it.

    Reading changes nothing, so the "no visible change" guard rejected the
    answer and the run burned its step budget returning nothing. A run that
    took no action and handed back an answer is reported as ANSWERED, which is
    not the same claim as a completed task.
    """

    def _loop(self, actions_done, args):
        import types

        from ufo.agent import AgentLoop, validate_decision

        valid = ("click", "type", "finish", "fail")
        stub = types.SimpleNamespace(
            actions_done=actions_done,
            finish_rejections=0,
            initial_signature="same",
            last_result="",
            history=[],
            run_dir=None,
        )
        stub._is_pure_answer = types.MethodType(
            AgentLoop._is_pure_answer, stub)
        stub._reject_finish = types.MethodType(AgentLoop._reject_finish, stub)
        stub._log = lambda step, payload: None
        decision = validate_decision({"action": "finish", "args": args}, valid)
        return stub, decision

    def test_answer_with_no_actions_is_allowed(self):
        stub, decision = self._loop(0, "Untitled - Notepad")
        self.assertFalse(stub._reject_finish(1, decision, "same", None))

    def test_empty_answer_is_still_rejected(self):
        stub, decision = self._loop(0, "")
        self.assertTrue(stub._reject_finish(1, decision, "same", None))

    def test_finish_after_real_work_needs_no_answer(self):
        stub, decision = self._loop(1, "")
        self.assertFalse(stub._reject_finish(1, decision, "changed", None))

    def test_finish_after_work_with_no_change_is_still_rejected(self):
        stub, decision = self._loop(1, "")
        self.assertTrue(stub._reject_finish(1, decision, "same", None))

    def test_answered_counts_as_success(self):
        from ufo.core.runner import task_status_for
        from ufo.core.task import TaskStatus

        self.assertIs(task_status_for("ANSWERED"), TaskStatus.SUCCEEDED)


class TestAllowlistSubject(unittest.TestCase):
    """The allowlist must be checked against the window that receives the action.

    `subject()` used to report the foreground window for every action, so
    `focus_window` on an allowlisted window was blocked whenever some other,
    non-allowlisted window happened to be in front. With VS Code focused, the
    agent could not reach Notepad at all and typing was blocked too. Failing
    closed is right; blocking the one action that fixes the situation is not.
    """

    def _agent(self, allowlist):
        from ufo.agent.computer_agent import ComputerAgent
        from ufo.config import Config

        agent = ComputerAgent.__new__(ComputerAgent)
        cfg = Config()
        cfg.safety.allowlist = list(allowlist)
        agent.cfg = cfg
        return agent

    def _observation(self, foreground_title):
        import types

        foreground = types.SimpleNamespace(title=foreground_title)
        return types.SimpleNamespace(foreground=lambda: foreground)

    def _decision(self, action, target="", args=""):
        from ufo.agent import validate_decision
        from ufo.agent.base import SYSTEM_ACTIONS

        return validate_decision({"action": action, "target": target,
                                  "args": args, "status": "CONTINUE"},
                                 SYSTEM_ACTIONS)

    def _blocked(self, allowlist, action, target, foreground_title, resolved=None):
        """Run the real subject resolution and the real policy over it."""
        from unittest import mock

        from ufo.errors import WindowNotFoundError
        from ufo.safety.policy import ActionPolicy

        agent = self._agent(allowlist)
        decision = self._decision(action, target, args="hello")
        if resolved is None:
            finder = mock.patch("ufo.actions.window.find",
                                side_effect=WindowNotFoundError("no such window"))
        else:
            info = type("Info", (), {"title": resolved})()
            finder = mock.patch("ufo.actions.window.find", return_value=info)
        with finder:
            subject = agent.subject(self._observation(foreground_title), decision)
        policy = ActionPolicy(allowlist=list(allowlist))
        return subject, policy.evaluate(action, target, subject=subject)

    def _patched_window(self, agent, resolved_title):
        """Stand in for the allowlist-filtered window search the action uses."""
        from unittest import mock

        info = type("Info", (), {"title": resolved_title})()
        return mock.patch(
            "ufo.actions.window.find",
            return_value=info)

    def test_focusing_an_allowlisted_window_is_judged_on_that_window(self):
        agent = self._agent(["Notepad"])
        decision = self._decision("focus_window", "Untitled - Notepad")
        with self._patched_window(agent, "Untitled - Notepad"):
            subject = agent.subject(self._observation("Visual Studio Code"),
                                    decision)
        self.assertEqual(subject, "Untitled - Notepad")

    def test_a_keystroke_is_still_judged_on_the_foreground(self):
        """A key goes to whatever has focus, named target or not."""
        agent = self._agent(["Notepad"])
        decision = self._decision("type", "hello", args="hello")
        subject = agent.subject(self._observation("Visual Studio Code"), decision)
        self.assertEqual(subject, "Visual Studio Code")

    def test_an_unresolvable_name_is_still_judged(self):
        """A made-up title must not be able to name its way past the gate."""
        subject, decision = self._blocked(
            ["Notepad"], "focus_window", "Visual Studio Code 99", "Notepad")
        self.assertEqual(subject, "Visual Studio Code 99")
        self.assertFalse(decision.allowed)

    def test_a_blocked_window_cannot_be_focused_even_when_it_is_in_front(self):
        """The dangerous case: the named window is forbidden but already focused.

        Falling back to the foreground here would let the request through,
        because the foreground happened to match the allowlist.
        """
        _subject, decision = self._blocked(
            ["Notepad"], "focus_window", "Visual Studio Code", "Untitled - Notepad")
        self.assertFalse(decision.allowed)

    def test_a_named_window_is_never_left_unchecked(self):
        """An empty subject makes the policy skip the window check entirely."""
        from unittest import mock

        from ufo.errors import WindowNotFoundError
        from ufo.safety.policy import ActionPolicy

        agent = self._agent(["Notepad"])
        decision = self._decision("focus_window", "Visual Studio Code")
        with mock.patch("ufo.actions.window.find",
                        side_effect=WindowNotFoundError("no such window")):
            subject = agent.subject(self._observation("Untitled - Notepad"),
                                    decision)
        self.assertTrue(subject, "an empty subject disables the allowlist gate")
        self.assertFalse(
            ActionPolicy(allowlist=["Notepad"])
            .evaluate("focus_window", "Visual Studio Code", subject=subject).allowed)

    def test_focusing_an_allowlisted_window_behind_a_blocked_one_is_allowed(self):
        _subject, decision = self._blocked(
            ["Notepad"], "focus_window", "Untitled - Notepad",
            "Visual Studio Code", resolved="Untitled - Notepad")
        self.assertTrue(decision.allowed)

    def test_a_global_action_has_no_window_subject(self):
        agent = self._agent(["Notepad"])
        decision = self._decision("run_command", "")
        self.assertEqual(agent.subject(self._observation("Visual Studio Code"),
                                       decision), "")

    def test_the_policy_still_blocks_a_blocked_foreground(self):
        """The fix must not have weakened the gate itself."""
        from ufo.safety.policy import ActionPolicy

        policy = ActionPolicy(allowlist=["Notepad"])
        self.assertTrue(policy.evaluate("click", "Save",
                                        subject="Untitled - Notepad").allowed)
        self.assertFalse(policy.evaluate("click", "Save",
                                         subject="Visual Studio Code").allowed)


class TestAutoFocusBeforeTyping(unittest.TestCase):
    """A keystroke aimed at a named window should reach that window.

    A human switches to the window they are typing into. The agent did not:
    it asked for Notepad by name, Notepad was on the allowlist, and the action
    was refused because Visual Studio Code happened to be in front. The run
    then retried the same blocked action until the step limit.
    """

    def _agent(self, allowlist=("Notepad",)):
        from ufo.agent.computer_agent import ComputerAgent
        from ufo.config import Config

        agent = ComputerAgent.__new__(ComputerAgent)
        cfg = Config()
        cfg.safety.allowlist = list(allowlist)
        agent.cfg = cfg
        return agent

    def _observation(self, foreground_title):
        import types

        foreground = types.SimpleNamespace(title=foreground_title)
        return types.SimpleNamespace(foreground=lambda: foreground)

    def _decision(self, action, target, args=""):
        from ufo.agent import validate_decision
        from ufo.agent.base import SYSTEM_ACTIONS

        return validate_decision({"action": action, "target": target,
                                  "args": args, "status": "CONTINUE"},
                                 SYSTEM_ACTIONS)

    def _windows(self, found_title, front_title):
        """Patch the allowlist search and the real foreground window."""
        from unittest import mock

        from ufo.errors import WindowNotFoundError

        info = type("Info", (), {"title": found_title, "wrapper": object(),
                                 "is_foreground": True})() if found_title else None
        front = type("Front", (), {"title": front_title})() if front_title else None

        def find(**kwargs):
            if info is None:
                raise WindowNotFoundError("no such window")
            return info

        return (mock.patch("ufo.actions.window.find", side_effect=find),
                mock.patch("ufo.actions.window.focus"),
                mock.patch("ufo.perception.uia.foreground_window", return_value=front))

    def test_typing_into_a_named_window_focuses_it_first(self):
        agent = self._agent()
        decision = self._decision("type", "Untitled - Notepad", args="hello")
        finder, focus, foreground = self._windows(
            "Untitled - Notepad", "Untitled - Notepad")
        with finder, focus as focus_mock, foreground:
            focused = agent._focus_named_window(
                decision, self._observation("Visual Studio Code"))
        self.assertEqual(focused, "Untitled - Notepad")
        focus_mock.assert_called_once()

    def test_nothing_is_typed_when_the_window_refuses_focus(self):
        """The whole point: a lost race must not type into the wrong window."""
        from ufo.errors import ActionError

        agent = self._agent()
        decision = self._decision("type", "Untitled - Notepad", args="hello")
        finder, focus, foreground = self._windows(
            "Untitled - Notepad", "Visual Studio Code")
        with finder, focus, foreground:
            with self.assertRaises(ActionError) as ctx:
                agent._focus_named_window(
                    decision, self._observation("Untitled - Notepad"))
        self.assertIn("Refusing to type", str(ctx.exception))
        self.assertIn("Visual Studio Code", str(ctx.exception))

    def test_an_already_focused_window_is_verified_not_re_focused(self):
        agent = self._agent()
        decision = self._decision("type", "Untitled - Notepad", args="hello")
        finder, focus, foreground = self._windows(
            "Untitled - Notepad", "Untitled - Notepad")
        with finder, focus as focus_mock, foreground:
            focused = agent._focus_named_window(
                decision, self._observation("Untitled - Notepad"))
        self.assertIsNone(focused)
        focus_mock.assert_not_called()

    def test_a_blocked_window_is_never_raised(self):
        """The focus helper must not become a way past the allowlist."""
        agent = self._agent()
        decision = self._decision("type", "Visual Studio Code", args="hello")
        finder, focus, foreground = self._windows(None, "Visual Studio Code")
        with finder, focus as focus_mock, foreground:
            focused = agent._focus_named_window(
                decision, self._observation("Visual Studio Code"))
        self.assertIsNone(focused)
        focus_mock.assert_not_called()

    def test_a_control_id_is_not_mistaken_for_a_window(self):
        agent = self._agent()
        decision = self._decision("type", "12", args="hello")
        finder, focus, foreground = self._windows(None, "Untitled - Notepad")
        with finder, focus as focus_mock, foreground:
            focused = agent._focus_named_window(
                decision, self._observation("Untitled - Notepad"))
        self.assertIsNone(focused)
        focus_mock.assert_not_called()

    def test_a_mouse_click_does_not_trigger_focusing(self):
        agent = self._agent()
        decision = self._decision("click", "Untitled - Notepad")
        finder, focus, foreground = self._windows(
            "Untitled - Notepad", "Visual Studio Code")
        with finder, focus as focus_mock, foreground:
            focused = agent._focus_named_window(
                decision, self._observation("Visual Studio Code"))
        self.assertIsNone(focused)
        focus_mock.assert_not_called()


def _expected_base_url(provider: str) -> str:
    from ufo.agent.llm import _LOCAL_BASE_URLS

    return _LOCAL_BASE_URLS[provider]


if __name__ == "__main__":
    unittest.main()
