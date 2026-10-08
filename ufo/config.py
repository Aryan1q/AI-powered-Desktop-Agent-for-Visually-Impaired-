"""Layered configuration.

Precedence, lowest to highest:
    dataclass defaults  <  config file (json or yaml)  <  UFO_<SECTION>_* env
    vars  <  explicit overrides passed by the caller (CLI flags)

Every field is documented in `config.example.json`. Nothing here reads the
network or mutates global state at import time, so it is safe to import from
tests.
"""
from __future__ import annotations

import copy
import json
import os
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError

ENV_PREFIX = "UFO_"
CONFIG_ENV = "UFO_CONFIG"


def _default_home() -> Path:
    return Path(os.getenv(ENV_HOME, Path.home() / ".ufo"))


# Placeholder replaced at first use; kept lazy so importing config never
# creates directories as a side effect.
ENV_HOME = "UFO_HOME"


# --------------------------------------------------------------------------
# sections
# --------------------------------------------------------------------------
@dataclass
class LLMConfig:
    #: Ollama on this machine. The default is local on purpose: this install is
    #: meant to run without a cloud key, so nothing — screenshot, task text or
    #: transcript — leaves the laptop unless you deliberately ask for it.
    provider: str = "ollama"           # ollama is the only permitted value
    #: Blank on purpose. `Client` asks the Ollama server which models it has and
    #: prefers one that can read a screenshot, so a fresh install works without
    #: anyone typing a model name — and a name that is not installed cannot be
    #: silently baked in. Set it to pin one specific model.
    model: str = ""
    temperature: float = 0.0
    max_tokens: int = 2000
    timeout: float = 120.0
    max_retries: int = 4
    #: Blank on purpose. `Client` resolves Ollama's own address from its table,
    #: so hardcoding one here would go stale the moment the port moves. Set it
    #: only to point Ollama somewhere other than its default.
    base_url: str = ""
    fallbacks: list[str] = field(default_factory=list)
    redact_screenshots: bool = False    # never send images to a remote provider
    offline: bool = True                # refuse all non-local providers


@dataclass
class SafetyConfig:
    # Fail-closed by default: nothing is allowed until the user opts in.
    allowlist: list[str] = field(default_factory=list)
    denylist: list[str] = field(default_factory=list)
    allowlist_is_regex: bool = True

    dry_run: bool = False              # plan + narrate, never touch the machine
    confirm_all: bool = False          # ask before every single action
    confirm_risk_at_or_above: int = 2  # 0 none, 1 low, 2 medium, 3 high, 4 critical
    block_at_or_above: int = 4         # refuse outright at/above this risk
    allow_sensitive_fields: bool = False   # typing into password/payment boxes
    redact_secrets_in_logs: bool = True

    # Standing constraints the user can impose with words, not just a flag.
    # Each is checked by ActionPolicy.evaluate on every action, so they hold for
    # the agent loop, the CLI, the daemon and the phone page alike.
    read_only: bool = False                # refuse anything that changes the machine
    never_paths: list[str] = field(default_factory=list)   # never touch these paths
    never_domains: list[str] = field(default_factory=list)  # never open these sites
    work_only_in: str = ""                 # do nothing outside this folder
    require_password_for_delete: bool = False  # ask for a password before deleting
    protected_folders: list[str] = field(
        default_factory=lambda: ["System Volume Information", "Windows",
                                 "Program Files", "Program Files (x86)"])
    # Category number (as a string, since config is JSON) -> allow|confirm|block.
    # Lets a whole family be gated at once, e.g. {"31": "confirm"} for finance.
    category_permissions: dict[str, str] = field(default_factory=dict)

    emergency_hotkey: str = "ctrl+alt+q"   # global abort, any time
    panic_phrases: list[str] = field(
        default_factory=lambda: ["stop", "cancel", "abort", "halt", "kill", "emergency stop"]
    )
    max_actions_per_run: int = 200
    max_wall_seconds_per_run: int = 3600
    max_llm_calls_per_run: int = 200
    max_cost_usd_per_run: float = 1.00
    loop_detection_threshold: int = 4   # identical (action,target) repeats
    idle_backoff_seconds: float = 5.0


@dataclass
class VisionConfig:
    max_controls: int = 150
    include_control_types: list[str] = field(
        default_factory=lambda: [
            "Button", "Edit", "Document", "MenuItem", "TabItem", "ListItem",
            "CheckBox", "RadioButton", "ComboBox", "Hyperlink", "TreeItem",
            "SplitButton", "Slider", "Spinner", "DataItem", "Text",
        ]
    )
    screenshot_quality: int = 70      # jpeg quality for the model
    screenshot_format: str = "png"     # png | jpeg
    max_image_edge: int = 1560        # downscale cap, keeps token cost sane
    annotate: bool = True
    annotate_color: str = "red"
    diff_detection: bool = True       # report what changed since last frame
    ocr_enabled: bool = True
    ocr_fallback: bool = True         # OCR when the UIA tree is empty
    screenshot_on_every_step: bool = True
    retain_screenshots: bool = True
    redact_screenshots: bool = False  # blur regions matching deny patterns


@dataclass
class VoiceConfig:
    enabled: bool = False
    tts: bool = True
    stt: bool = False
    stt_command: str = ""             # e.g. "-m en-US --model tiny.en"
    wake_word: str = ""               # e.g. "computer"; empty disables wake mode
    rate: int = 175                   # words per minute
    volume: float = 1.0
    narrate_actions: bool = True
    narrate_thoughts: bool = False
    confirmation_style: str = "voice_and_console"  # voice | console | voice_and_console

    # --- persona ---
    persona: str = "none"             # none | jarvis | butler | terse
    address: str = ""                 # "sir" / "ma'am"; empty never addresses you
    chime: bool = True                # audible acknowledgement before speaking
    cadence: str = "dry"              # dry | warm

    # --- model-backed speech (optional; falls back to local engines) ---
    tts_model: str = ""               # e.g. "gemini-3.8-flash-tts"; empty = auto
    stt_model: str = "gemini-3.5-transcribe"
    stt_language: str = "en-US"


@dataclass
class PhoneConfig:
    """Voice instructions from a phone on the same Wi-Fi.

The phone runs a small page in its own browser and captures the microphone.
    What happens to the audio depends on `transcription`:

    - ``local`` (default): the phone records raw audio and the laptop
      transcribes it with faster-whisper. Only the audio crosses the LAN, it
      needs no API key, and nothing is sent to a third party. This is the
      default because this install is local-only.
    - ``browser``: the phone transcribes with the Web Speech API. No install and
      no audio bandwidth, but Chrome and Safari hand the recording to their own
      cloud recogniser, so the audio still leaves the phone.

    There is no server-side mode: nothing here can call a hosted transcription
    service.
    """

    enabled: bool = False
    host: str = "0.0.0.0"             # 0.0.0.0 so the phone on the LAN can reach it
    port: int = 8766
    transcription: str = "local"       # local | browser
    language: str = "en-US"
    auto_run: bool = False            # False = read the instruction back first
    push_to_talk: bool = True         # hold the button, or toggle with a wake word
    max_instruction_chars: int = 2000


@dataclass
class AutomationConfig:
    step_delay: float = 0.8
    observation_timeout: float = 20.0
    action_timeout: float = 30.0
    window_timeout: float = 30.0
    max_steps: int = 30
    max_consecutive_errors: int = 5
    retry_on_observation_error: bool = True
    auto_launch_apps: bool = True
    auto_focus_window: bool = True
    close_window_on_finish: bool = False
    planner_enabled: bool = True
    verifier_enabled: bool = True
    self_heal_enabled: bool = True
    persist_memory: bool = True
    remember_failures: bool = True


@dataclass
class StorageConfig:
    home: Path = field(default_factory=_default_home)
    log_root: Path = field(default_factory=lambda: _default_home() / "logs")
    memory_path: Path = field(default_factory=lambda: _default_home() / "memory.json")
    history_path: Path = field(default_factory=lambda: _default_home() / "history.jsonl")
    tasks_path: Path = field(default_factory=lambda: _default_home() / "tasks.json")
    schedule_path: Path = field(default_factory=lambda: _default_home() / "schedule.json")
    state_path: Path = field(default_factory=lambda: _default_home() / "state.json")
    secrets_path: Path = field(default_factory=lambda: _default_home() / "secrets.json")
    keep_runs: int = 50
    keep_screenshots: int = 400
    log_level: str = "INFO"
    json_logs: bool = False
    redact_logs: bool = True


@dataclass
class ServerConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8765
    token: str = ""                   # empty -> generated & stored on first boot
    dashboard: bool = True
    cors_origin: str = ""
    max_body_bytes: int = 2_000_000


@dataclass
class ScheduleConfig:
    enabled: bool = False
    timezone_note: str = "local time"
    jitter_seconds: int = 5
    run_on_start: bool = False
    catch_up_missed: bool = False
    max_concurrent: int = 1


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    automation: AutomationConfig = field(default_factory=AutomationConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    phone: PhoneConfig = field(default_factory=PhoneConfig)

    # provenance
    config_path: Path | None = None

    # ---- persistence ----
    def to_dict(self) -> dict:
        data = asdict(self)
        data["config_path"] = str(self.config_path) if self.config_path else None
        return data

    def save(self, path: str | os.PathLike | None = None) -> Path:
        target = Path(path) if path else (self.config_path or self._default_config_path())
        target.parent.mkdir(parents=True, exist_ok=True)
        data = self.to_dict()
        data.pop("config_path", None)
        target.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        self.config_path = target
        return target

    @staticmethod
    def _default_config_path() -> Path:
        return _default_home() / "config.json"

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Config":
        """Build a config from defaults + file + environment + overrides.

        An explicit path or `UFO_CONFIG` is strict: if it is named and missing,
        that is an error worth stopping for. The default `~/.ufo/config.json` is
        not — it simply may not exist yet — but when it does exist it must be
        read, or every `ufo config set` would be silently discarded by the next
        command.
        """
        cfg = cls()

        target = path or os.getenv(CONFIG_ENV)
        if target:
            p = Path(target)
            if not p.exists():
                raise ConfigError(f"Config file not found: {p}")
            cfg._merge_file(p)
        else:
            default = cls._default_config_path()
            if default.exists():
                cfg._merge_file(default)

        cfg._merge_env()
        return cfg

    def _merge_file(self, path: Path) -> None:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as e:
            raise ConfigError(f"Cannot read config {path}: {e}") from e

        data: Any
        if path.suffix.lower() in (".yaml", ".yml"):
            try:
                import yaml  # type: ignore
            except ImportError as e:
                raise ConfigError(
                    f"{path} is YAML but PyYAML is not installed. "
                    "Install it or use a .json config."
                ) from e
            data = yaml.safe_load(raw)
        else:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                raise ConfigError(f"Invalid JSON in {path}: {e}") from e

        if not isinstance(data, dict):
            raise ConfigError(f"Config root must be an object, got {type(data).__name__}")

        unknown = set(data) - {f.name for f in fields(self) if f.name != "config_path"}
        if unknown:
            raise ConfigError(f"Unknown config sections: {sorted(unknown)}")

        self._apply(data, source=str(path))
        self.config_path = path

    def _apply(self, data: dict, *, source: str) -> None:
        for key, value in data.items():
            if not hasattr(self, key):
                continue
            current = getattr(self, key)
            if is_dataclass(current) and isinstance(value, dict):
                known = {f.name for f in fields(current)}
                bad = set(value) - known
                if bad:
                    raise ConfigError(f"Unknown keys in [{key}] from {source}: {sorted(bad)}")
                for sub_key, sub_value in value.items():
                    setattr(current, sub_key,
                            _coerce(getattr(current, sub_key), sub_value, where=f"{key}.{sub_key}"))
            else:
                setattr(self, key, _coerce(current, value, where=key))

    def _merge_env(self) -> None:
        """Read UFO_<SECTION>_<FIELD> overrides, e.g. UFO_SAFETY_DRY_RUN=1."""
        for f in fields(self):
            section = getattr(self, f.name)
            if not is_dataclass(section):
                continue
            for sf in fields(section):
                env_key = f"{ENV_PREFIX}{f.name.upper()}_{sf.name.upper()}"
                if env_key not in os.environ:
                    continue
                raw = os.environ[env_key]
                try:
                    setattr(section, sf.name, _coerce(getattr(section, sf.name), raw))
                except (TypeError, ValueError) as e:
                    raise ConfigError(f"Bad value for {env_key}: {raw!r} ({e})") from e

    def apply_overrides(self, overrides: dict) -> "Config":
        """Apply flat or nested overrides (typically from CLI flags).

        `{"safety.dry_run": True}` and `{"safety": {"dry_run": True}}` both work.
        """
        flat: dict[str, dict] = {}
        for key, value in (overrides or {}).items():
            if value is None:
                continue
            if "." in key:
                section, _, name = key.partition(".")
                flat.setdefault(section, {})[name] = value
            else:
                flat.setdefault("_root", {})[key] = value

        for section, values in flat.items():
            if section == "_root":
                self._apply(values, source="overrides")
                continue
            if not hasattr(self, section):
                raise ConfigError(f"Unknown config section: {section}")
            current = getattr(self, section)
            if not is_dataclass(current):
                self._apply({section: values}, source="overrides")
                continue
            known = {f.name for f in fields(current)}
            bad = set(values) - known
            if bad:
                raise ConfigError(f"Unknown keys in [{section}]: {sorted(bad)}")
            for name, value in values.items():
                setattr(current, name,
                        _coerce(getattr(current, name), value, where=f"{section}.{name}"))
        return self

    def ensure_dirs(self) -> "Config":
        for p in (
            self.storage.home,
            self.storage.log_root,
            self.storage.history_path.parent,
            self.storage.memory_path.parent,
        ):
            p.mkdir(parents=True, exist_ok=True)
        return self

    def copy(self) -> "Config":
        return copy.deepcopy(self)


def _coerce(current: Any, raw: Any, where: str = "") -> Any:
    """Coerce a value to the type of the existing default.

    Every source funnels through here — env vars, the config file and CLI
    overrides — because a safety switch that accepts the *string* "false"
    silently leaves the switch on. That is not a cosmetic bug: `dry_run`
    would read as enabled after the user turned it off.
    """
    label = f" for {where}" if where else ""
    if raw is None:
        return current
    try:
        if isinstance(current, bool):
            if isinstance(raw, str):
                return raw.strip().lower() in ("1", "true", "yes", "on", "y")
            return bool(raw)
        if isinstance(current, int) and not isinstance(current, bool):
            return int(raw)
        if isinstance(current, float):
            return float(raw)
        if isinstance(current, list):
            if isinstance(raw, str):
                return [p.strip() for p in raw.split(",") if p.strip()]
            return list(raw)
        if isinstance(current, Path):
            return Path(str(raw)).expanduser()
    except (TypeError, ValueError) as e:
        raise ConfigError(f"Bad value{label}: {raw!r} ({e})") from e
    return raw


_ACTIVE: Config | None = None


def active() -> Config:
    """Process-wide config, loaded on first use."""
    global _ACTIVE
    if _ACTIVE is None:
        _ACTIVE = Config.load().ensure_dirs()
    return _ACTIVE


def set_active(cfg: Config | None) -> None:
    global _ACTIVE
    _ACTIVE = cfg
