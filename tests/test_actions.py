"""Action-layer tests.

Anything that touches the real desktop is either skipped or redirected at a
temp directory: the point is to test argument handling, path safety and
fail-closed behaviour, not to move the user's mouse.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from tests._support import ROOT  # noqa: F401
from ufo.actions import filesystem as fs
from ufo.actions import network, process, shell
from ufo.errors import FilesystemError, ProcessError


class TestPathExpansion(unittest.TestCase):
    def test_tilde_and_env(self):
        self.assertTrue(str(fs.expand("~")).startswith(str(Path.home())))

    def test_known_folders(self):
        for name in ("desktop", "downloads", "documents", "pictures"):
            self.assertTrue(str(fs.expand(f"~/{name}")).strip(),
                            f"{name} did not expand")

    def test_env_vars(self):
        self.assertEqual(str(fs.expand("$UFO_TEST_DIR")), "C:\\somewhere")

    def test_relative_paths_stay_relative_to_the_cwd(self):
        self.assertEqual(fs.expand("./x"), Path("x"))

    def setUp(self):
        os.environ["UFO_TEST_DIR"] = "C:\\somewhere"

    def tearDown(self):
        os.environ.pop("UFO_TEST_DIR", None)


class TestFilesystemSafety(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_read_and_write(self):
        target = self.root / "a.txt"
        self.assertIn("wrote", fs.write_text(str(target), "hello"))
        self.assertEqual(fs.read_text(str(target)).strip(), "hello")

    def test_read_text_is_capped(self):
        target = self.root / "big.txt"
        target.write_text("x" * 10_000_000, encoding="utf-8")
        self.assertLessEqual(len(fs.read_text(str(target), max_bytes=1000)), 1000)

    def test_read_missing_file_raises(self):
        with self.assertRaises(FilesystemError):
            fs.read_text(str(self.root / "nope.txt"))

    def test_copy_refuses_to_clobber(self):
        src, dst = self.root / "s.txt", self.root / "d.txt"
        src.write_text("a", encoding="utf-8")
        dst.write_text("b", encoding="utf-8")
        with self.assertRaises(FilesystemError):
            fs.copy(str(src), str(dst), overwrite=False)
        self.assertEqual(dst.read_text(encoding="utf-8"), "b")

    def test_move_missing_source_raises(self):
        with self.assertRaises(FilesystemError):
            fs.move(str(self.root / "ghost.txt"), str(self.root / "x.txt"))

    def test_recursive_delete_needs_permission(self):
        target = self.root / "sub"
        (target / "nested").mkdir(parents=True)
        (target / "nested" / "f.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(FilesystemError):
            fs.delete_path(str(target), recursive=False)
        self.assertTrue((target / "nested" / "f.txt").exists(),
                        "a refused delete must change nothing")

    def test_recursive_check_also_applies_to_the_recycle_bin(self):
        """The bin path must not become a way around `recursive=False`."""
        target = self.root / "sub"
        (target / "nested").mkdir(parents=True)
        (target / "nested" / "f.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(FilesystemError):
            fs.delete_path(str(target), recursive=False, to_recycle=True)
        self.assertTrue((target / "nested" / "f.txt").exists())

    def test_rename_cannot_clobber(self):
        a, b = self.root / "a.txt", self.root / "b.txt"
        a.write_text("1", encoding="utf-8")
        b.write_text("2", encoding="utf-8")
        with self.assertRaises(FilesystemError):
            fs.rename(str(a), b.name)
        self.assertEqual(b.read_text(encoding="utf-8"), "2")

    def test_unique_path_avoids_collisions(self):
        taken = self.root / "x.txt"
        taken.write_text("1", encoding="utf-8")
        self.assertNotEqual(fs.unique_path(str(taken)), taken)

    def test_zip_round_trip(self):
        src = self.root / "src"
        src.mkdir()
        (src / "a.txt").write_text("hello", encoding="utf-8")
        archive = fs.zip_folder(str(src), str(self.root / "out.zip"))
        dest = Path(fs.unzip(archive, str(self.root / "back")))
        self.assertTrue((dest / "a.txt").exists())
        self.assertEqual((dest / "a.txt").read_text(encoding="utf-8"), "hello")

    def test_zip_refuses_to_write_inside_itself(self):
        src = self.root / "src"
        src.mkdir()
        (src / "a.txt").write_text("hello", encoding="utf-8")
        with self.assertRaises(FilesystemError):
            fs.zip_folder(str(src), str(src / "out.zip"))

    def test_unzip_rejects_a_non_zip(self):
        fake = self.root / "fake.zip"
        fake.write_text("not a zip", encoding="utf-8")
        with self.assertRaises(FilesystemError):
            fs.unzip(str(fake))

    def test_search_finds_nested_files(self):
        (self.root / "x" / "y").mkdir(parents=True)
        (self.root / "x" / "y" / "note.txt").write_text("hi", encoding="utf-8")
        hits = [p.name for p in fs.search(str(self.root), "*.txt")]
        self.assertIn("note.txt", hits)

    def test_grep_finds_content(self):
        (self.root / "a.txt").write_text("the needle is here", encoding="utf-8")
        hits = fs.grep(str(self.root), "needle")
        self.assertTrue(any(p.name == "a.txt" for p, _n, _l in hits))

    def test_grep_ignores_case_only_misses(self):
        (self.root / "a.txt").write_text("the needle is here", encoding="utf-8")
        self.assertEqual(fs.grep(str(self.root), "haystack"), [])

    def test_empty_dir_removes_contents(self):
        d = self.root / "d"
        (d / "sub").mkdir(parents=True)
        (d / "sub" / "f.txt").write_text("x", encoding="utf-8")
        (d / "top.txt").write_text("y", encoding="utf-8")
        fs.empty_dir(str(d))
        self.assertTrue(d.exists(), "the directory itself should survive")
        self.assertEqual(list(d.iterdir()), [])

    def test_empty_dir_refuses_a_file(self):
        f = self.root / "a.txt"
        f.write_text("x", encoding="utf-8")
        with self.assertRaises(FilesystemError):
            fs.empty_dir(str(f))


class TestShell(unittest.TestCase):
    def test_powershell_runs(self):
        result = shell.run_powershell("Write-Output hello")
        self.assertTrue(result.ok)
        self.assertIn("hello", result.output)
        self.assertEqual(result.returncode, 0)

    def test_failing_command_is_reported_not_raised(self):
        result = shell.run_powershell("Write-Error boom")
        self.assertFalse(result.ok)
        self.assertIn("boom", result.output)

    def test_timeout_is_flagged(self):
        result = shell.run_powershell("Start-Sleep -Seconds 30", timeout=2)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.ok)

    def test_result_is_json_serialisable(self):
        json.dumps(shell.run_powershell("Write-Output hi").to_dict())

    def test_command_is_redacted_in_the_result(self):
        result = shell.run_powershell(
            "Write-Output done; echo sk-abcdef0123456789ABCDEF")
        self.assertNotIn("sk-abcdef", json.dumps(result.to_dict()))

    def test_which(self):
        self.assertTrue(shell.which("powershell"))

    def test_env_dump_never_leaks_secrets(self):
        from ufo.safety.secrets import scrub_data

        os.environ["UFO_FAKE_TOKEN"] = "sk-abcdef0123456789ABCDEF"
        try:
            dumped = scrub_data(shell.env_dump(["UFO_FAKE_TOKEN"]))
            self.assertNotIn("sk-abcdef", str(dumped))
        finally:
            del os.environ["UFO_FAKE_TOKEN"]


class TestNetworkGuards(unittest.TestCase):
    def test_private_addresses_are_recognised(self):
        for host in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "localhost"):
            self.assertTrue(network.is_private(host), host)

    def test_public_addresses_are_not_private(self):
        for host in ("1.1.1.1", "8.8.8.8", "example.com"):
            self.assertFalse(network.is_private(host), host)

    def test_fetch_rejects_non_http_schemes(self):
        for url in ("file:///C:/Windows/win.ini", "ftp://example.com/x",
                    "javascript:alert(1)"):
            with self.assertRaises(Exception, msg=url):
                network.fetch_text(url, timeout=2)


class TestProcessListing(unittest.TestCase):
    def test_lists_processes(self):
        procs = process.list_processes(limit=10)
        self.assertIsInstance(procs, list)
        self.assertTrue(procs)

    def test_find_by_name(self):
        self.assertTrue(process.find(name="python"))
        self.assertEqual(process.find(name="no-such-process-xyz"), [])

    def test_info_for_own_pid_is_fast(self):
        """info() must not build the whole system table to find one process."""
        import time

        pid = os.getpid()
        started = time.time()
        info = process.info(pid)
        self.assertEqual(info.pid, pid)
        self.assertLess(time.time() - started, 2.0,
                        "info() should query psutil directly, not list everything")

    def test_info_rejects_an_unknown_pid(self):
        with self.assertRaises(ProcessError):
            process.info(999_999)

    def test_terminating_nonsense_pid_is_reported(self):
        with self.assertRaises(ProcessError):
            process.terminate(999_999)

if __name__ == "__main__":
    unittest.main()
