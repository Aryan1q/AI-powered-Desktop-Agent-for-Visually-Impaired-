"""Risk classification and the allow/deny policy.

`RiskClassifier` answers "how bad is it if this runs unchecked?" for a
concrete action. `ActionPolicy` answers "may it run at all, and does a human
have to say yes first?" — including for non-UI actions like shell commands,
file writes, process kills and network calls.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..config import SafetyConfig
from ..errors import PolicyViolation
from ..utils import text as textutil

# ---- risk scale ----
RISK_NONE, RISK_LOW, RISK_MEDIUM, RISK_HIGH, RISK_CRITICAL = 0, 1, 2, 3, 4

_RISK_NAMES = {
    0: "none", 1: "low", 2: "medium", 3: "high", 4: "critical",
}


@dataclass(frozen=True)
class Risk:
    level: int
    label: str
    reasons: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return _RISK_NAMES.get(self.level, "unknown")

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.name} ({self.level}/4)"


RiskLevel = Risk


# ---- destructive vocabulary ----
DESTRUCTIVE_PATTERNS = [
    r"\bdelete\b", r"\bdelet\b", r"\bremove\b", r"\bdiscard\b", r"\berase\b",
    r"\bwipe\b", r"\bclear\b", r"\bpurge\b", r"\bdestroy\b", r"\btrash\b",
    r"\bsend\b", r"\bsubmit\b", r"\bpublish\b", r"\bpost\b", r"\bforward\b",
    r"\bconfirm\b", r"\baccept\b", r"\bagree\b", r"\byes\b", r"\bok\b",
    r"\bpay\b", r"\bpurchase\b", r"\bbuy\b", r"\bcheckout\b", r"\btransfer\b",
    r"\border\b", r"\bplace order\b", r"\brefund\b", r"\bwithdraw\b",
    r"\boverwrite\b", r"\breplace\b", r"\bformat\b", r"\breset\b",
    r"\bpermanently\b", r"\birreversible\b",
    r"\bpublish\b", r"\bmerge\b", r"\bcommit\b", r"\bpush\b", r"\bdeploy\b",
    r"\bforce quit\b", r"\bend task\b", r"\bkill\b", r"\bterminate\b",
    r"\buninstall\b", r"\bregister\b", r"\bremove-item\b",
    r"\bshut ?down\b", r"\brestart\b", r"\breboot\b", r"\bsign out\b",
    r"\blog ?out\b", r"\block\b", r"\bdisable\b", r"\bgrant\b", r"\brevoke\b",
]
_DESTRUCTIVE_RE = re.compile("|".join(DESTRUCTIVE_PATTERNS), re.IGNORECASE)

#: Action names arrive as `file_delete`, `app.uninstall`, `remove-item`. Every
#: word-bounded pattern below has to see those as separate words.
_SEPARATORS = re.compile(r"[_\-.:/\\]+")

# ---- fields the agent must never fill ----
SENSITIVE_FIELD_PATTERNS = [
    r"\bpassw(or)?d\b", r"\bpasscode\b", r"\bpin\b", r"\botp\b",
    r"\bcvv2?\b", r"\bcvc2?\b", r"\bsecurity code\b", r"\bcard ?number\b",
    r"\bcredit card\b", r"\bdebit card\b", r"\bexpiry\b", r"\bexpiration\b",
    r"\bssn\b", r"\bsocial security\b", r"\btax ?id\b", r"\bpan\b",
    r"\baccount number\b", r"\brouting number\b", r"\biban\b", r"\bswift\b",
    r"\bmfa\b", r"\botp code\b", r"\bverification code\b", r"\brecovery key\b",
    r"\bseed phrase\b", r"\bprivate key\b", r"\b2fa secret\b",
]
_SENSITIVE_RE = re.compile("|".join(SENSITIVE_FIELD_PATTERNS), re.IGNORECASE)

# ---- shell / system risk ----
_CRITICAL_SHELL = re.compile(
    r"(?i)(format\s+[a-z]:|del\s+/[sqf]|rmdir\s+/s|rd\s+/s|"
    r"remove-item\b.*-recurse|diskpart|cipher\s+/w|"
    r"reg\s+delete\s+hk(lm|ey_local_machine)|"
    r"bcdedit|shutdown\b|restart-computer|stop-computer|"
    r"set-executionpolicy\s+unrestricted|"
    r"invoke-expression|iex\s|downloadstring|"
    r"curl\b.*\|\s*(iex|sh|bash)|wget\b.*\|\s*(sh|bash)|"
    r"vssadmin\s+delete|wbadmin\s+delete|"
    r"takeown|icacls\b.*(/grant|/reset)|"
    r"net\s+user\s+.*\s+/add|net\s+localgroup\s+.*administrators)"
)

#: Read-only shell commands, matched against the *whole* command line.
#:
#: Two rules, and anything else counts as a change:
#:   1. every PowerShell `Get-*` cmdlet, because Get cannot mutate anything;
#:   2. an explicit list of standalone tools, with the arguments that make them
#:      read-only spelled out. `ipconfig /all` observes; `ipconfig /flushdns`
#:      changes, so the safe form is matched whole and the unsafe one is not.
#:      `sfc /scannow` repairs and is deliberately absent.
_READONLY_SHELL = re.compile(
    r"(?i)^\s*(?:"
    r"get-[a-z][\w-]*"                      # any Get-* cmdlet
    r"|ls|dir|gci|cat|type|more|echo|whoami|hostname|ver|"
    r"systeminfo|ifconfig|"
    r"(?:ipconfig(?:\s+/all|\s+/a\b)?(?!\s*/(?:flushdns|release|renew)))|"
    r"ping|netstat(?:\s+-\w+)?|tasklist|driverquery|quser|query\s+user|"
    r"pnputil\s+/enum-drivers|"
    r"reg\s+query|wevtutil\s+qe|"
    r"wmic\s+\S+\s+get|"
    r"write-host|measure-object|group-object|sort-object|"
    r"select-object|where-object|foreach-object|compare-object|"
    r"test-path|measure-command|resolve-path|convertto-json"
    r")\b"
)

_DANGEROUS_SHELL = re.compile(
    r"(?i)(remove-item|del\s|rmdir|\bdel\b|rm\s+-rf|"
    r"stop-process|taskkill|kill|"
    r"reg\s+(delete|add|import)|set-itemproperty|new-itemproperty|"
    r"set-service|stop-service|start-service|start-process\s+.*-windowstyle\s+hidden|"
    r"invoke-webrequest|curl|wget|invoke-restmethod|"
    r"net\s+(user|localgroup|share|use|stop|start)|"
    r"schtasks|new-scheduledtask|"
    r"sc\s+(create|config|delete)|"
    r"manage-bde|cipher|"
    r"netsh|"
    r"set-|new-|remove-|clear-|disable-|enable-|uninstall-|install-|"
    r"move-item|copy-item|rename-item|new-item|out-file\s+(?!/n)|"
    r"add-content|set-content|"
    r"start|restart|reset|optimize|compact|repair|update)"
)

_EXFIL_SHELL = re.compile(
    r"(?i)(invoke-webrequest|iwr|curl|Invoke-RestMethod)\b[^|;&\n]*"
    r"(upload|post|put|-outfile|-infile|-bodyfile|uploadfile|s3|"
    r"pastebin|ngrok|transfer\.sh|webhook\.site|discord\.com/api/webhooks)"
)

# Network destinations the agent should not reach unprompted.
_SENSITIVE_HOSTS = re.compile(
    r"(?i)(^|\.)(pastebin\.com|ngrok\.(io|dev)|transfer\.sh|"
    r"webhook\.site|requestbin|pipedream\.net|termbin\.com|"
    r"anonfiles|file\.io|0x0\.st|hastebin|ix\.io|"
    r"telegram\.org/bot|discord\.com/api/webhooks)$"
)

# Windows internals an agent has no business automating unattended.
_PROTECTED_WINDOWS = re.compile(
    r"(?i)^(credential manager|control panel|windows security|"
    r"windows firewall|local security policy|"
    r"registry editor|task manager|system configuration|"
    r"local users and groups|computer management|"
    r"device manager|disk management|system information)$"
)


# --------------------------------------------------------------------------
@dataclass
class PolicyDecision:
    allowed: bool
    risk: Risk
    needs_confirmation: bool
    reason: str = ""
    matched_denylist: str | None = None

    def __bool__(self) -> bool:
        return self.allowed

    def raise_if_blocked(self) -> None:
        if not self.allowed:
            raise PolicyViolation(self.reason or "Blocked by safety policy.")


@dataclass
class RiskClassifier:
    """Turns a concrete action into a Risk. Stateless and independently testable."""

    extra_destructive: list[str] = field(default_factory=list)
    extra_sensitive: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._destructive = re.compile(
            "|".join(DESTRUCTIVE_PATTERNS + self.extra_destructive), re.IGNORECASE
        )
        self._sensitive = re.compile(
            "|".join(SENSITIVE_FIELD_PATTERNS + self.extra_sensitive), re.IGNORECASE
        )

    # -- helpers --
    def is_destructive(self, *parts: str) -> bool:
        # Separators are normalised first. The patterns are word-bounded, and
        # `_` counts as a word character, so without this every underscore-named
        # action slipped past the check: "file_delete" and "app_uninstall" were
        # not seen as destructive at all.
        text = _SEPARATORS.sub(" ", " ".join(str(p or "") for p in parts))
        return bool(self._destructive.search(text))

    def is_sensitive_field(self, name: str) -> bool:
        return bool(self._sensitive.search(str(name or "")))

    def changes_machine(self, action: str, kind: str, haystack: str = "") -> bool:
        """Would running this alter the machine? Used by read-only mode.

        Explicit rather than inferred from the risk level: a risk of 2 can mean
        "changes a device setting" or "reads a file", and read-only mode has to
        tell those apart. Reads, queries and observation are allowed. Anything
        that writes to disk, changes a setting, ends a process, sends data or
        runs a shell command is a change.
        """
        action = (action or "").lower()
        haystack = (haystack or "").lower()
        # Same separator normalisation as is_destructive, so the per-kind
        # verb tests below see "file delete" rather than "file_delete".
        both = _SEPARATORS.sub(" ", action + " " + haystack)

        if kind == "fs":
            return bool(re.search(
                r"(?i)write|create|move|rename|copy|delete|remove|erase|wipe|"
                r"purge|archive|zip|unzip|extract|chmod|encrypt|decrypt|"
                r"hide|restore|organise|organize|trash|recycle", both))
        if kind == "process":
            return bool(re.search(r"(?i)kill|terminate|end task|stop|launch|start|open", both))
        if kind == "system":
            return bool(re.search(
                r"(?i)set|enable|disable|install|uninstall|restart|shutdown|"
                r"reboot|lock|signout|logoff|format|reset|update|registry|"
                r"service|firewall|defender|registry|delete|remove|add|toggle|"
                r"volume|brightness|theme|wallpaper|notification|wifi|bluetooth|"
                r"airplane|dns|proxy|vpn|power|plan|print|date|time|language",
                both))
        if kind == "network":
            return bool(re.search(
                r"(?i)upload|post|put|send|publish|submit|firewall|route|dns|"
                r"adapter|disable|enable|reset|connect|disconnect|forget|hotspot",
                both))
        if kind == "shell":
            # Only the command line can say whether a shell action is read-only.
            # The action name is not a command, so it is not evidence: with
            # nothing to inspect this fails closed and counts as a change.
            command = haystack.strip()
            return not (command and _READONLY_SHELL.match(command))
        if kind in ("ui", "keyboard", "mouse", "window"):
            # Typing, pasting, clicking buttons and closing windows all change
            # state that the user would miss. Window observation is not enough.
            # Minimising, maximising and snapping rearrange what is on screen —
            # a user in read-only mode has not agreed to their desktop being
            # moved out from under them, so those count as changes too.
            return bool(re.search(
                r"(?i)type|paste|write|set_text|fill|click|invoke|press|hotkey|"
                r"close|kill|drag|scroll|select|combo|checkbox|slider|expand|"
                r"collapse|append|toggle|double|right|hover|"
                r"minimi|maximi|snap|move_window|resize|restore|fullscreen|"
                r"show_desktop|focus", both))
        return bool(re.search(
            r"(?i)set|write|create|update|delete|remove|install|enable|disable|"
            r"reset|start|stop|run|send|post|put|patch", both))

    def is_protected_window(self, title: str) -> bool:
        return bool(_PROTECTED_WINDOWS.search(textutil.collapse_ws(title)))

    def is_exfiltration(self, target: str) -> bool:
        return bool(_EXFIL_SHELL.search(target or "")) or bool(
            _SENSITIVE_HOSTS.search(textutil.collapse_ws(target))
        )

    # -- the classifier --
    def classify(
        self,
        action: str,
        target: str = "",
        args: str = "",
        *,
        kind: str = "ui",
        floor: int = 0,
    ) -> Risk:
        """Risk for one action. `kind` is ui | mouse | keyboard | shell | fs |
        process | system | network | window.

        `floor` is a known minimum: the catalog states the risk of a capability
        by hand, and that statement outranks anything guessed from the words.
        A read-only capability must not be escalated to high risk because its
        usage hint happens to contain the word "remove".
        """
        action = (action or "").strip().lower()
        haystack = f"{target} {action} {args}"
        reasons: list[str] = []
        level = RISK_LOW

        if kind == "shell":
            command = (args or target or "").strip()
            if _CRITICAL_SHELL.search(haystack):
                level, _ = max(level, RISK_CRITICAL), reasons.append(
                    "shell command matches a system-destroying pattern")
            elif self.is_exfiltration(command):
                level = RISK_CRITICAL
                reasons.append("command appears to upload data to an external service")
            elif _READONLY_SHELL.match(command):
                reasons.append("read-only shell command")
            elif _DANGEROUS_SHELL.search(haystack):
                level = max(level, RISK_MEDIUM)
                reasons.append("shell command modifies system or network state")
            else:
                reasons.append("unrecognised shell command")

        elif kind == "fs":
            if self.is_destructive(haystack):
                level = RISK_CRITICAL if re.search(r"(?i)recurse|permanent|rmtree|rm\s+-rf", haystack) else RISK_HIGH
                reasons.append("filesystem operation removes or overwrites data")
            elif re.search(r"(?i)write|create|move|rename|copy", action):
                level = max(level, RISK_MEDIUM)
                reasons.append("filesystem operation changes data on disk")
            else:
                reasons.append("filesystem read")

        elif kind == "process":
            if re.search(r"(?i)kill|terminate", action) or self.is_destructive(haystack):
                level, _ = max(level, RISK_HIGH), reasons.append("terminates a running process")
            else:
                reasons.append("process inspection or launch")

        elif kind == "system":
            if re.search(r"(?i)shutdown|restart|reboot|poweroff|signout|lock|format|factoryreset|uninstall", action + " " + args):
                level = RISK_CRITICAL
                reasons.append("changes power, account or system state")
            elif re.search(r"(?i)registry|regedit|set|delete|install|uninstall|service|firewall|defender", action + " " + args):
                level = RISK_HIGH
                reasons.append("modifies registry, services or security settings")
            elif re.search(r"(?i)volume|audio|theme|wallpaper|notification|display|mouse|keyboard|bluetooth|wifi", action):
                level = RISK_MEDIUM
                reasons.append("changes a device or desktop setting")
            else:
                reasons.append("system query")

        elif kind == "network":
            if self.is_exfiltration(args or target) or re.search(r"(?i)upload|post|put|send|publish", action):
                level, _ = max(level, RISK_HIGH), reasons.append("sends data off this machine")
            elif re.search(r"(?i)download|fetch|get|request", action):
                level = max(level, RISK_MEDIUM)
                reasons.append("performs an outbound request")
            elif re.search(r"(?i)firewall|route|dns|adapter|disable|enable|reset", action):
                level = RISK_HIGH
                reasons.append("changes network configuration")

        elif kind == "window":
            if re.search(r"(?i)close|kill|destroy", action):
                level = max(level, RISK_MEDIUM)
                reasons.append("closes a window; unsaved work may be lost")
            if self.is_protected_window(target):
                level = max(level, RISK_HIGH)
                reasons.append("targets a protected system window")

        if self.is_sensitive_field(target) and action in ("type", "set_text", "paste", "fill"):
            level = RISK_CRITICAL
            reasons.append("writes into a credential or payment field")

        if self.is_destructive(haystack):
            level = max(level, RISK_HIGH)
            reasons.append("action name or argument looks irreversible")

        if floor > level:
            # The catalog knows better than the word list. Keep any reason the
            # classifier had, and say why the floor applies.
            level = floor
            reasons.append(f"catalog states a risk floor of {floor}")

        return Risk(min(level, RISK_CRITICAL), _RISK_NAMES.get(level, "unknown"), tuple(reasons))


def classify_risk(action: str, target: str = "", args: str = "",
                  kind: str = "ui") -> Risk:
    return RiskClassifier().classify(action, target, args, kind=kind)


# --------------------------------------------------------------------------
@dataclass
class ActionPolicy:
    """Allow/deny decisions. Fail-closed: an empty allowlist allows nothing."""

    allowlist: list[str] = field(default_factory=list)
    denylist: list[str] = field(default_factory=list)
    allowlist_is_regex: bool = True
    allow_sensitive_fields: bool = False
    block_at_or_above: int = RISK_CRITICAL
    classifier: RiskClassifier = field(default_factory=RiskClassifier)

    # Standing constraints, checked before anything else. See `constraints()`.
    read_only: bool = False
    never_paths: list[str] = field(default_factory=list)
    never_domains: list[str] = field(default_factory=list)
    work_only_in: str = ""
    require_password_for_delete: bool = False
    protected_folders: list[str] = field(default_factory=list)
    # Category -> "allow" | "confirm" | "block". Checked against the capability's
    # catalog category, so a whole family of actions can be gated at once.
    category_permissions: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_config(cls, cfg: SafetyConfig) -> "ActionPolicy":
        return cls(
            allowlist=list(cfg.allowlist),
            denylist=list(cfg.denylist),
            allowlist_is_regex=cfg.allowlist_is_regex,
            allow_sensitive_fields=cfg.allow_sensitive_fields,
            block_at_or_above=cfg.block_at_or_above,
            read_only=cfg.read_only,
            never_paths=list(cfg.never_paths),
            never_domains=list(cfg.never_domains),
            work_only_in=cfg.work_only_in,
            require_password_for_delete=cfg.require_password_for_delete,
            protected_folders=list(cfg.protected_folders),
            category_permissions=dict(cfg.category_permissions),
        )

    # ---- standing constraints ----
    def constraints(self, action: str, target: str = "", args: str = "",
                    kind: str = "ui") -> str:
        """The first reason this action is forbidden, or "" if it is allowed.

        Checked before the allowlist and the risk score, because these are
        promises the user made in words. A risk score can be argued with; a
        "never touch this folder" cannot.
        """
        haystack = f"{target} {args}".strip()

        if self.read_only and self.classifier.changes_machine(action, kind, haystack):
            return ("read-only mode is on, so nothing that changes the machine "
                    f"is allowed. Refused: {action} on {target!r}. "
                    "Turn it off with: ufo config set safety.read_only false")

        for folder in self.protected_folders:
            if folder.lower() in haystack.lower():
                return (f"{folder} is a system folder and is never written to "
                        f"by this agent. Refused: {action} on {target!r}")

        for path in self.never_paths:
            if self._path_touches(haystack, path):
                return (f"{path} is on the never-touch list. Refused: "
                        f"{action} on {target!r}. Remove it with: "
                        f"ufo config set safety.never_paths")

        if self.work_only_in:
            paths = self._paths_in(haystack)
            outside = [p for p in paths if not self._path_touches(p, self.work_only_in)]
            if paths and outside:
                return (f"work is limited to {self.work_only_in}, and this "
                        f"touches {outside[0]}. Refused: {action}")

        for domain in self.never_domains:
            host = self._host_in(haystack)
            if host and (host == domain.lower() or host.endswith("." + domain.lower())):
                return (f"{domain} is on the never-open list. Refused: "
                        f"{action} on {target!r}")

        if (self.require_password_for_delete
                and self.classifier.is_destructive(f"{action} {haystack}")
                and any(w in action.lower() for w in
                        ("delete", "remove", "erase", "purge", "wipe", "format"))):
            return ("deletions need your password while "
                    "require_password_for_delete is on")

        return ""

    @staticmethod
    def _paths_in(text: str) -> list[str]:
        """Rough path extraction: enough to see what an action names."""
        found = re.findall(r"[A-Za-z]:[\\/][^\s\"'|,;]*|~[\\/][^\s\"'|,;]*"
                           r"|\\\\[^\s\"'|,;]+", text or "")
        return found

    @staticmethod
    def _path_touches(candidate: str, boundary: str) -> bool:
        """True when `candidate` is `boundary` or lives inside it."""
        try:
            left = str(Path(candidate).resolve(strict=False)).lower()
            right = str(Path(boundary).expanduser().resolve(strict=False)).lower()
        except (OSError, ValueError):
            left, right = candidate.lower(), str(boundary).lower()
        return left == right or left.startswith(right.rstrip("\\/") + os.sep)

    @staticmethod
    def _host_in(text: str) -> str:
        match = re.search(r"(?i)\b(?:https?://)?((?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+)"
                          r"(?::\d+)?\b", text or "")
        return match.group(1).split(":")[0].lower() if match else ""

    def _match(self, pattern: str, value: str) -> bool:
        if not value:
            return False
        if self.allowlist_is_regex:
            try:
                return bool(re.search(pattern, value, re.IGNORECASE))
            except re.error:
                return pattern.lower() in value.lower()
        return pattern.strip().lower() in value.lower()

    def window_allowed(self, title: str) -> bool:
        """Empty allowlist == nothing allowed. A matching denylist always wins."""
        if not self.allowlist:
            return False
        title = title or ""
        if any(self._match(p, title) for p in self.denylist):
            return False
        if self.classifier.is_protected_window(title):
            return False
        return any(self._match(p, title) for p in self.allowlist)

    def process_allowed(self, process_name: str) -> bool:
        if not self.allowlist:
            return False
        if any(self._match(p, process_name) for p in self.denylist):
            return False
        return any(self._match(p, process_name) for p in self.allowlist)

    def category_gate(self, action: str) -> str:
        """What does the user want done with this action's whole category?

        Looks the action up in the catalog to find its category, then applies
        the per-category setting. Returns "allow", "confirm" or "block"; an
        action with no category, or a category with no setting, is allowed —
        per-category gating is opt-in, not opt-out.
        """
        if not self.category_permissions:
            return "allow"
        try:
            from ..capabilities.catalog import get
        except Exception:
            return "allow"
        cap = get(action)
        if cap is None:
            return "allow"
        setting = str(self.category_permissions.get(str(cap.category), "")).lower()
        return setting if setting in ("allow", "confirm", "block") else "allow"

    def evaluate(self, action: str, target: str = "", args: str = "",
                 kind: str = "ui", *, subject: str = "",
                 floor: int = 0) -> PolicyDecision:
        """Full decision for one action.

        `subject` is the window/app the action happens in, when known; it is
        checked against the allowlist so per-window scoping still applies.
        `floor` is a risk the caller already knows: the catalog states it by
        hand for every capability, and a hand-written floor outranks the guess.
        """
        risk = self.classifier.classify(action, target, args, kind=kind,
                                        floor=floor)

        gate = self.category_gate(action)
        if gate == "block":
            return PolicyDecision(
                False, risk, False,
                f"Category {action!r} is set to 'block' in your permissions. "
                f"Refused. Change it with: ufo config set "
                f"safety.category_permissions")

        # Standing constraints come first. These are promises made in words, and
        # a risk score can be argued with while "never touch this folder" cannot.
        refused = self.constraints(action, target, args, kind=kind)
        if refused:
            return PolicyDecision(False, risk, False, refused)

        if self.denylist and any(self._match(p, f"{target} {args}") for p in self.denylist):
            return PolicyDecision(False, risk, False,
                                  f"Action matches a denylist pattern: {target!r}")

        if subject and not self.window_allowed(subject):
            return PolicyDecision(
                False, risk, False,
                f"Target window {subject!r} is not in the allowlist. "
                "Add it with --allow or ALLOWED_APPS.")

        if (action in ("type", "set_text", "paste", "fill")
                and self.classifier.is_sensitive_field(target)
                and not self.allow_sensitive_fields):
            return PolicyDecision(
                False, risk, False,
                f"Refusing to write into a credential/payment field: {target!r}")

        if risk.level >= self.block_at_or_above:
            return PolicyDecision(
                False, risk, False,
                f"Action risk is {risk.name}, at or above the hard block "
                f"threshold ({self.block_at_or_above}). "
                f"Reasons: {'; '.join(risk.reasons) or 'n/a'}")

        if gate == "confirm":
            return PolicyDecision(
                True, risk, True,
                f"Category is set to 'confirm' in your permissions.")

        return PolicyDecision(True, risk, risk.level > 0, "")

    def blocked_windows(self, titles: Iterable[str]) -> list[str]:
        return [t for t in titles if not self.window_allowed(t)]
