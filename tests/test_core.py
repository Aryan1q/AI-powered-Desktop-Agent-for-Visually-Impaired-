"""Task queue, long-term memory and history persistence."""
import json
import tempfile
import time
import unittest
from pathlib import Path

from tests._support import ROOT  # noqa: F401
from ufo.core.memory import Memory
from ufo.core.queue import TaskQueue
from ufo.core.task import Task, TaskPriority, TaskResult, TaskStatus


class TestTask(unittest.TestCase):
    def test_defaults_are_safe(self):
        t = Task("do a thing")
        self.assertEqual(t.status, TaskStatus.PENDING)
        self.assertFalse(t.dry_run)
        self.assertEqual(t.mode, "computer")
        self.assertTrue(t.id)

    def test_priority_ordering(self):
        self.assertLess(TaskPriority.LOW, TaskPriority.NORMAL)
        self.assertLess(TaskPriority.NORMAL, TaskPriority.HIGH)
        self.assertLess(TaskPriority.HIGH, TaskPriority.CRITICAL)

    def test_result_and_status(self):
        t = Task("x")
        t.result = TaskResult(status=TaskStatus.SUCCEEDED, outcome="done")
        t.status = TaskStatus.SUCCEEDED
        self.assertTrue(t.status.terminal)
        self.assertTrue(t.status.successful)

    def test_round_trip_json(self):
        t = Task("do a thing", priority=TaskPriority.HIGH, tags=["a"], app="Notepad")
        again = Task.from_dict(t.to_dict())
        self.assertEqual(again.goal, t.goal)
        self.assertEqual(again.priority, TaskPriority.HIGH)
        self.assertEqual(again.tags, ["a"])
        self.assertEqual(again.app, "Notepad")


class TestQueue(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.q = TaskQueue(Path(self.dir.name) / "queue.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_add_and_next(self):
        self.q.add(Task("first"))
        self.assertEqual(self.q.next().goal, "first")

    def test_priority_then_fifo(self):
        self.q.add(Task("low", priority=TaskPriority.LOW))
        self.q.add(Task("urgent", priority=TaskPriority.CRITICAL))
        self.q.add(Task("normal"))
        self.assertEqual([t.goal for t in self.q.list()],
                         ["urgent", "normal", "low"])
        self.assertEqual(self.q.next().goal, "urgent")

    def test_next_is_none_when_empty(self):
        self.assertIsNone(self.q.next())

    def test_next_is_a_peek_not_a_claim(self):
        t = self.q.add(Task("first"))
        first = self.q.next()
        self.assertEqual(first.id, t.id)
        self.assertEqual(self.q.next().id, t.id,
                         "next() peeks; the caller must move the task out of PENDING")
        self.assertEqual(self.q.find(t.id).status, TaskStatus.PENDING)

    def test_future_scheduled_task_is_not_ready(self):
        self.q.add(Task("later", scheduled_for=time.time() + 3600))
        self.assertIsNone(self.q.next())

    def test_cancel(self):
        t = self.q.add(Task("first"))
        cancelled = self.q.cancel(t.id)
        self.assertEqual(cancelled.status, TaskStatus.CANCELLED)
        self.assertIsNone(self.q.next())
        with self.assertRaises(Exception):
            self.q.cancel("nope")

    def test_cancel_all(self):
        self.q.add(Task("a"))
        self.q.add(Task("b"))
        self.assertEqual(self.q.cancel_all(), 2)
        self.assertEqual(self.q.list(include_terminal=False), [])

    def test_persistence(self):
        self.q.add(Task("persisted", priority=TaskPriority.HIGH))
        again = TaskQueue(self.q.path)
        self.assertEqual(again.next().goal, "persisted")

    def test_persisted_priority_survives(self):
        t = self.q.add(Task("p", priority=TaskPriority.CRITICAL))
        again = TaskQueue(self.q.path)
        self.assertEqual(again.find(t.id).priority, TaskPriority.CRITICAL)

    def test_corrupt_file_does_not_crash(self):
        self.q.path.write_text("{broken", encoding="utf-8")
        self.assertEqual(TaskQueue(self.q.path).list(), [])

    def test_stats(self):
        self.q.add(Task("a"))
        stats = self.q.stats()
        self.assertEqual(stats["length"], 1)
        self.assertEqual(stats["by_status"]["pending"], 1)
        self.assertIsNotNone(stats["next"])

    def test_update_status(self):
        t = self.q.add(Task("a"))
        t.status = TaskStatus.RUNNING
        self.q.update(t)
        self.assertEqual(self.q.find(t.id).status, TaskStatus.RUNNING)

    def test_remove_and_clear(self):
        t = self.q.add(Task("a"))
        self.assertTrue(self.q.remove(t.id))
        self.assertIsNone(self.q.find(t.id))
        self.q.add(Task("b"))
        self.q.clear()
        self.assertEqual(self.q.list(), [])


class TestMemory(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.mem = Memory(Path(self.dir.name) / "memory.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_remember_and_recall(self):
        self.mem.remember("editor", "vscode")
        self.assertEqual(self.mem.recall("editor"), "vscode")

    def test_recall_default(self):
        self.assertEqual(self.mem.recall("nope", "fallback"), "fallback")

    def test_recall_is_case_insensitive(self):
        self.mem.remember("Editor", "vscode")
        self.assertEqual(self.mem.recall("editor"), "vscode")

    def test_forget(self):
        self.mem.remember("k", "v")
        self.assertTrue(self.mem.forget("k"))
        self.assertFalse(self.mem.forget("k"))
        self.assertEqual(self.mem.recall("k"), "")

    def test_persistence(self):
        self.mem.remember("k", "v")
        self.assertEqual(Memory(self.mem.path).recall("k"), "v")

    def test_facts_listing(self):
        self.mem.remember("a", "1", source="test")
        self.assertIn("a", self.mem.facts())
        self.assertEqual(self.mem.facts()["a"], "1")

    def test_secrets_are_not_stored(self):
        self.mem.remember("token", "sk-abcdef0123456789ABCDEF")
        self.mem.save()
        self.assertNotIn("sk-abcdef", self.mem.path.read_text(encoding="utf-8"),
                         "a secret must never reach the memory file verbatim")

    def test_brief_is_bounded(self):
        for i in range(500):
            self.mem.remember(f"k{i}", "v" * 500)
        self.assertLess(len(self.mem.brief()), 4000,
                        "brief() must stay small enough for a prompt")

    def test_stats(self):
        self.mem.remember("k", "v")
        self.assertGreaterEqual(self.mem.stats().get("facts", 0), 1)
    def test_clear(self):
        self.mem.remember("k", "v")
        self.mem.clear()
        self.assertEqual(self.mem.recall("k"), "")


if __name__ == "__main__":
    unittest.main()
