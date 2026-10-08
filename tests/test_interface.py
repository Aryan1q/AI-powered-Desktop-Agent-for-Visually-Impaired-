"""Interface tests: CLI dispatch, the HTTP API and the dashboard.

The API tests drive `dispatch()` directly (no sockets) plus one real
loopback round-trip, so they stay fast and deterministic.
"""
import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from tests._support import ROOT  # noqa: F401
from ufo.config import Config
from ufo.core import memory as memory_mod
from ufo.core import queue as queue_mod
from ufo.core import scheduler as scheduler_mod
from ufo.interface.server import AgentServer


def make_config(tmp: Path, **server_kw) -> Config:
    cfg = Config()
    cfg.storage.home = tmp
    cfg.storage.log_root = tmp / "logs"
    cfg.storage.memory_path = tmp / "memory.json"
    cfg.storage.history_path = tmp / "history.json"
    cfg.storage.tasks_path = tmp / "queue.json"
    cfg.storage.schedule_path = tmp / "schedules.json"
    cfg.storage.state_path = tmp / "state.json"
    cfg.storage.secrets_path = tmp / "secrets.json"
    cfg.server.enabled = True
    cfg.server.host = "127.0.0.1"
    cfg.server.port = 0
    cfg.server.token = "test-token"
    for key, value in server_kw.items():
        setattr(cfg.server, key, value)
    return cfg


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.dir.name)
        self.cfg = make_config(self.tmp)
        self.server = AgentServer(self.cfg)
        self.headers = {"X-UFO-Token": "test-token"}

    def tearDown(self):
        queue_mod.set_default(None)
        memory_mod.set_default(None)
        scheduler_mod.set_default(None)
        self.dir.cleanup()

    def get(self, path, **query):
        return self.server.dispatch("GET", path, query, {}, {})

    def post(self, path, body, headers=None):
        return self.server.dispatch("POST", path, {}, body,
                                    headers if headers is not None else self.headers)


class TestRouting(ServerCase):
    def test_unknown_route_is_404(self):
        self.assertEqual(self.get("/api/nope")[0], 404)

    def test_status_is_public(self):
        code, body = self.get("/api/status")
        self.assertEqual(code, 200)
        self.assertIn("emergency", body)

    def test_get_on_a_post_route_is_404(self):
        self.assertEqual(self.get("/api/submit")[0], 404)

    def test_mutation_requires_a_token(self):
        code, body = self.post("/api/tasks", {"task": "x"}, headers={})
        self.assertEqual(code, 401)
        self.assertIn("token", body["error"].lower())

    def test_wrong_token_is_rejected(self):
        code, _ = self.post("/api/tasks", {"task": "x"},
                            headers={"X-UFO-Token": "wrong"})
        self.assertEqual(code, 401)

    def test_bearer_header_is_accepted(self):
        code, _ = self.post("/api/tasks", {"task": "x"},
                            headers={"Authorization": "Bearer test-token"})
        self.assertEqual(code, 200)

    def test_handler_exception_becomes_500(self):
        def boom(_body):
            raise RuntimeError("kaboom")

        self.server.route("/api/boom", "POST", boom, mutating=False)
        code, body = self.post("/api/boom", {})
        self.assertEqual(code, 500)
        self.assertIn("kaboom", body["error"])

    def test_every_dashboard_endpoint_is_registered(self):
        """A dashboard button that 404s is worse than no dashboard."""
        import re

        from ufo.interface import dashboard

        called = set(re.findall(r"""["'`](/api/[\w/]+)""", dashboard.DASHBOARD_HTML))
        self.assertTrue(called, "no API calls found in the dashboard")
        for path in called:
            self.assertTrue(any(p == path for p, _m in self.server._routes),
                            f"the dashboard calls {path}, which is not registered")


class TestApi(ServerCase):
    def test_submit_queues_a_task(self):
        code, body = self.post("/api/tasks", {"task": "summarise my notes"})
        self.assertEqual(code, 200)
        self.assertIn("id", body)
        listed = self.get("/api/tasks")[1]
        self.assertTrue(any(t["goal"] == "summarise my notes"
                            for t in listed["queued"]))

    def test_submit_without_a_task_is_400(self):
        self.assertEqual(self.post("/api/tasks", {})[0], 400)

    def test_submit_with_a_bad_priority_is_400(self):
        code, body = self.post("/api/tasks",
                               {"task": "x", "priority": "immediately"})
        self.assertEqual(code, 400)
        self.assertIn("priority", body["error"])

    def test_schedule_add_and_list(self):
        code, body = self.post("/api/schedule",
                               {"task": "water the plants", "when": "in 2 hours"})
        self.assertEqual(code, 200)
        self.assertTrue(body["task_id"])
        listed = self.get("/api/schedule")[1]["schedules"]
        self.assertTrue(any(s["goal"] == "water the plants" for s in listed))

    def test_schedule_add_rejects_nonsense(self):
        self.assertEqual(
            self.post("/api/schedule", {"task": "x", "when": "someday"})[0], 400)

    def test_cancel_unknown_task_is_400(self):
        code, body = self.post("/api/tasks/cancel", {"id": "nope"})
        self.assertEqual(code, 400)
        self.assertIn("error", body)

    def test_memory_endpoint(self):
        self.post("/api/tasks", {"task": "remember something"})
        code, body = self.get("/api/memory")
        self.assertEqual(code, 200)
        self.assertIn("facts", body)

    def test_emergency_trips(self):
        from ufo.safety.emergency import emergency

        try:
            code, body = self.post("/api/emergency", {"reason": "test"})
            self.assertEqual(code, 200)
            self.assertTrue(body["tripped"])
            self.assertTrue(emergency.tripped)
        finally:
            emergency.reset()

    def test_screenshots_endpoint_matches_the_dashboard(self):
        """The dashboard fetches /api/screenshots; keep the two in step."""
        from ufo.interface import dashboard

        code, body = self.get("/api/screenshots", format=["jpeg"], quality=["40"])
        self.assertEqual(code, 200)
        self.assertIn("data", body)
        self.assertIn("/api/screenshots?", dashboard.DASHBOARD_HTML)
        self.assertNotIn("/api/screenshot?", dashboard.DASHBOARD_HTML)


class TestOverHttp(ServerCase):
    def setUp(self):
        super().setUp()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.server.make_handler())
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def fetch(self, path, data=None, token="test-token"):
        url = f"http://127.0.0.1:{self.port}{path}"
        headers = {"X-UFO-Token": token}
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()

    def test_status_over_http(self):
        status, raw = self.fetch("/api/status")
        self.assertEqual(status, 200)
        self.assertIn("emergency", json.loads(raw))

    def test_dashboard_is_served(self):
        status, raw = self.fetch("/")
        self.assertEqual(status, 200)
        self.assertIn(b"<!doctype html>", raw[:64].lower())

    def test_unauthorised_post_is_401(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.fetch("/api/tasks", {"task": "x"}, token="nope")
        self.assertEqual(ctx.exception.code, 401)

    def test_post_over_http(self):
        status, raw = self.fetch("/api/tasks", {"task": "over http"})
        self.assertEqual(status, 200)
        self.assertIn("id", json.loads(raw))


class TestCliSurface(unittest.TestCase):
    def test_parser_builds(self):
        from ufo.interface.cli import build_parser

        parser = build_parser()
        self.assertIsNotNone(parser.parse_args(["doctor"]))
        self.assertIsNotNone(parser.parse_args(["run", "open notepad"]))
        self.assertIsNotNone(
            parser.parse_args(["schedule", "add", "tomorrow 08:00", "--task", "x"]))

    def test_help_exits_cleanly(self):
        from ufo.interface.cli import build_parser

        with self.assertRaises(SystemExit) as ctx:
            build_parser().parse_args(["--help"])
        self.assertEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
