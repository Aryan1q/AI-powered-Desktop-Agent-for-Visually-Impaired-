"""A local HTTP API and dashboard, built on the standard library only.

No Flask, no FastAPI, no extra dependency to install — `http.server` is
enough for a single-user loopback control panel, and it means the agent has one
fewer thing to go wrong.

Security posture, deliberately conservative:
  * binds to 127.0.0.1 by default;
  * a bearer token is generated and stored on first start, required for every
    mutating call, and printed once so the user can paste it into the dashboard;
  * request bodies are size-capped before being parsed.

This exists so the agent can be driven from a phone on the same LAN (via a
tunnel) or by a screen reader with a real web view, without a terminal.
"""
from __future__ import annotations

import json
import secrets as pysecrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from ..config import Config
from ..errors import (ConfigError, SchedulerError, TaskNotFound, ValidationError)
from ..logging_setup import get_logger, record
from ..safety.emergency import emergency
from ..utils import text as textutil

log = get_logger(__name__)

Routes = dict[str, tuple[str, Callable]]


def _json_default(value: Any) -> Any:
    try:
        from enum import Enum

        if isinstance(value, Enum):
            return value.value
    except Exception:
        pass
    return str(value)


class AgentServer:
    """Owns the routing table and the request handler class."""

    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or Config.load().ensure_dirs()
        self.cfg.server.enabled = True
        self._token = self.cfg.server.token or self._ensure_token()
        self._lock = threading.Lock()
        self._routes: Routes = {}
        self._register_defaults()

    # ---- token ----
    def _ensure_token(self) -> str:
        from ..safety.secrets import SecretStore

        store = SecretStore(self.cfg.storage.secrets_path)
        token = store.get("api_token")
        if not token:
            token = pysecrets.token_urlsafe(24)
            store.set("api_token", token)
        self.cfg.server.token = token
        return token

    @property
    def token(self) -> str:
        return self._token

    # ---- routing ----
    def route(self, path: str, method: str, handler: Callable,
              *, mutating: bool = False) -> None:
        self._routes[(path, method.upper())] = (handler, mutating)

    def _register_defaults(self) -> None:
        self.route("/api/status", "GET", self.api_status)
        self.route("/api/screens", "GET", self.api_screens)
        self.route("/api/screenshots", "GET", self.api_screenshots)
        self.route("/api/tasks", "GET", self.api_tasks)
        self.route("/api/tasks", "POST", self.api_submit, mutating=True)
        self.route("/api/tasks/cancel", "POST", self.api_cancel, mutating=True)
        self.route("/api/schedule", "GET", self.api_schedule)
        self.route("/api/schedule", "POST", self.api_schedule_add, mutating=True)
        self.route("/api/memory", "GET", self.api_memory)
        self.route("/api/emergency", "POST", self.api_emergency, mutating=True)
        self.route("/api/clipboard", "GET", self.api_clipboard)
        self.route("/api/say", "POST", self.api_say, mutating=True)

    # ---- handlers ----
    def api_status(self, _query: dict) -> dict:
        from ..core.daemon import status
        from ..perception import uia

        windows = uia.list_all_windows()
        return {
            "version": _version(),
            "time": time.time(),
            "daemon": status(self.cfg),
            "queue": _queue_stats(self.cfg),
            "allowlist": self.cfg.safety.allowlist,
            "dry_run": self.cfg.safety.dry_run,
            "llm": {"provider": self.cfg.llm.provider, "model": self.cfg.llm.model},
            "windows": {
                "total": len(windows),
                "allowed": sum(1 for w in windows if uia.is_allowed(w, self.cfg.safety.allowlist)),
                "titles": [w.title for w in windows[:15]],
            },
            "emergency": emergency.tripped,
            "emergency_hotkey": self.cfg.safety.emergency_hotkey,
        }

    def api_screens(self, _query: dict) -> dict:
        from ..utils import win32

        return {
            "virtual": list(win32.virtual_screen()),
            "monitors": [m.__dict__ for m in win32.monitors()],
            "dpi_scale": round(win32.dpi_scale(), 2),
            "dpi_awareness": win32.dpi_awareness_set(),
            "cursor": list(win32.cursor_pos()),
        }

    def api_screenshots(self, query: dict) -> dict:
        from ..perception import screen
        from ..utils import image as imageutil

        monitor = int((query.get("monitor") or ["-1"])[0])
        cap = screen.capture_monitor(monitor) if monitor >= 0 else screen.capture_full()
        fmt = (query.get("format") or ["jpeg"])[0]
        quality = int((query.get("quality") or ["60"])[0])
        img = imageutil.fit_to_edge(cap.image, self.cfg.vision.max_image_edge)
        return {
            "bbox": list(cap.bbox),
            "width": img.width, "height": img.height,
            "format": fmt,
            "data": imageutil.encode(img, fmt, quality),
        }

    def api_tasks(self, _query: dict) -> dict:
        from ..core.queue import get_queue
        from ..core.task import load_history

        q = get_queue(self.cfg.storage.tasks_path)
        return {
            "queued": [t.to_dict() for t in q.list()],
            "recent": [t.to_dict() for t in
                       load_history(self.cfg.storage.history_path, limit=25)],
        }

    def api_submit(self, body: dict) -> dict:
        from ..core.queue import get_queue
        from ..core.task import Task, TaskPriority

        goal = textutil.collapse_ws(body.get("task") or body.get("goal") or "")
        if not goal:
            raise ValueError("'task' is required.")
        try:
            priority = TaskPriority[str(body.get("priority", "normal")).upper()]
        except KeyError:
            raise ValueError(
                "'priority' must be one of "
                + ", ".join(p.label for p in TaskPriority) + "."
            ) from None
        task = Task(
            goal=goal,
            priority=priority,
            allow=[str(a) for a in (body.get("allow") or [])],
            mode=str(body.get("mode", "computer")),
            app=str(body.get("app", "")),
            max_steps=int(body.get("max_steps") or 0),
            dry_run=bool(body.get("dry_run", False)),
            confirm=bool(body.get("confirm", False)),
        )
        q = get_queue(self.cfg.storage.tasks_path)
        q.add(task)
        record("http_submit", id=task.id, goal=goal[:120])
        return {"id": task.id, "status": task.status.value,
                "note": "queued; start the daemon with: ufo daemon start"}

    def api_cancel(self, body: dict) -> dict:
        from ..core.daemon import Daemon
        from ..core.queue import get_queue

        task_id = str(body.get("id") or "")
        if task_id:
            return {"cancelled": get_queue(self.cfg.storage.tasks_path).cancel(task_id).id}
        cancelled = get_queue(self.cfg.storage.tasks_path).cancel_all()
        running = Daemon(self.cfg).cancel_current()
        return {"cancelled": cancelled, "running_cancelled": running}

    def api_schedule(self, _query: dict) -> dict:
        from ..core.scheduler import get_scheduler

        s = get_scheduler(self.cfg.storage.schedule_path).load()
        return {"schedules": [x.to_dict() for x in s.list()]}

    def api_schedule_add(self, body: dict) -> dict:
        from ..core.scheduler import get_scheduler

        s = get_scheduler(self.cfg.storage.schedule_path).load()
        schedule = s.add(
            body.get("task", ""), when=body.get("when", "now"),
            repeat=body.get("repeat", ""), cron=body.get("cron", ""),
        )
        return schedule.to_dict()

    def api_memory(self, _query: dict) -> dict:
        from ..core.memory import get_memory

        store = get_memory(self.cfg.storage.memory_path)
        return {"brief": store.brief(), "stats": store.stats(),
                "facts": store.facts()}

    def api_emergency(self, body: dict) -> dict:
        reason = str(body.get("reason") or "emergency stop from the dashboard")
        emergency.trip(reason)
        record("http_emergency", reason=reason)
        return {"tripped": True, "reason": reason}

    def api_clipboard(self, _query: dict) -> dict:
        from ..actions import keyboard

        return {"text": textutil.truncate(keyboard.get_clipboard(), 20_000)}

    def api_say(self, body: dict) -> dict:
        from ..voice import Narrator

        text = str(body.get("text", ""))
        narrator = Narrator(enabled=self.cfg.voice.enabled, rate=self.cfg.voice.rate,
                            volume=self.cfg.voice.volume, console=False)
        narrator.say(text)
        return {"spoken": text}

    # ---- request handling ----
    def _authorised(self, handler, headers) -> bool:
        if not handler[1]:
            return True
        supplied = headers.get("Authorization", "") or ""
        token = headers.get("X-UFO-Token", "")
        candidates = {supplied.removeprefix("Bearer ").strip(), token.strip()}
        return any(c and _constant_eq(c, self._token) for c in candidates)

    def dispatch(self, method: str, path: str, query: dict,
                 body: dict, headers) -> tuple[int, Any]:
        """Route one request. GET handlers receive the query dict, POST handlers
        the parsed body — so a handler only ever deals with one mapping."""
        entry = self._routes.get((path, method.upper()))
        if entry is None:
            return 404, {"error": f"no route for {method} {path}"}
        handler, mutating = entry
        if mutating and not self._authorised(entry, headers):
            return 401, {"error": "missing or wrong X-UFO-Token"}
        payload = body if method.upper() == "POST" else query
        try:
            return 200, handler(payload)
        except (ValueError, ValidationError, ConfigError, SchedulerError,
                TaskNotFound) as e:
            # The caller sent something we cannot use: that is their mistake,
            # not a server fault, and a 400 keeps the dashboard from showing
            # an alarming red 500 for "someday" not being a real time.
            return 400, {"error": str(e)}
        except Exception as e:
            log.exception("request %s %s failed", method, path)
            return 500, {"error": f"{type(e).__name__}: {e}"}

    # ---- server ----
    def make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "ufo"
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):  # quieter default logging
                log.debug("%s - %s", self.address_string(), fmt % args)

            def _send(self, code: int, payload: Any, content_type="application/json",
                      raw: bytes | None = None) -> None:
                body = raw if raw is not None else json.dumps(
                    payload, default=_json_default).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                if server.cfg.server.cors_origin:
                    self.send_header("Access-Control-Allow-Origin",
                                     server.cfg.server.cors_origin)
                    self.send_header("Access-Control-Allow-Headers",
                                     "Content-Type, Authorization, X-UFO-Token")
                    self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):  # noqa: N802
                self._send(204, None, raw=b"")

            def do_GET(self):  # noqa: N802
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                if parsed.path in ("/", "/index.html") and server.cfg.server.dashboard:
                    from .dashboard import DASHBOARD_HTML

                    return self._send(200, None, "text/html; charset=utf-8",
                                      DASHBOARD_HTML.encode("utf-8"))
                code, payload = server.dispatch("GET", parsed.path, query, {}, self.headers)
                self._send(code, payload)

            def do_POST(self):  # noqa: N802
                parsed = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                if length > server.cfg.server.max_body_bytes:
                    return self._send(413, {"error": "request body too large"})
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except json.JSONDecodeError as e:
                    return self._send(400, {"error": f"invalid JSON: {e}"})
                if not isinstance(body, dict):
                    return self._send(400, {"error": "body must be a JSON object"})
                query = parse_qs(parsed.query)
                code, payload = server.dispatch("POST", parsed.path, query, body,
                                                self.headers)
                self._send(code, payload)

        return Handler

    def serve_forever(self) -> None:
        handler = self.make_handler()
        httpd = ThreadingHTTPServer((self.cfg.server.host, self.cfg.server.port), handler)
        httpd.daemon_threads = True
        emergency.start_hotkey_watch(self.cfg.safety.emergency_hotkey)
        url = f"http://{self.cfg.server.host}:{self.cfg.server.port}/"
        print(f"ufo API listening on {url}")
        print(f"dashboard: {url}")
        print(f"API token (X-UFO-Token): {self._token}")
        print("Press Ctrl+C to stop.")
        record("server_start", host=self.cfg.server.host, port=self.cfg.server.port)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopping")
        finally:
            httpd.server_close()
            emergency.stop_hotkey_watch()
            record("server_stop")


def _constant_eq(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(a, b)


def _version() -> str:
    from .. import __version__

    return __version__


def _queue_stats(cfg: Config) -> dict:
    from ..core.queue import get_queue

    try:
        return get_queue(cfg.storage.tasks_path).stats()
    except Exception as e:
        return {"error": str(e)}


def serve(cfg: Config | None = None) -> int:
    AgentServer(cfg).serve_forever()
    return 0


def token_for(cfg: Config | None = None) -> str:
    return AgentServer(cfg).token
