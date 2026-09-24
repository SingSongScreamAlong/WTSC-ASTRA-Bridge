"""Local Git transport tests; no Blender or network connection is needed."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from bridge import astra_bridge as bridge


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.remote = root / "remote.git"
        self.repo = root / "local"
        self.other = root / "other"
        git(root, "init", "--bare", str(self.remote))
        git(root, "clone", str(self.remote), str(self.repo))
        git(self.repo, "config", "user.name", "ASTRA Test")
        git(self.repo, "config", "user.email", "test@example.invalid")
        for folder in bridge.EXCHANGE_PATHS:
            (self.repo / folder).mkdir()
            (self.repo / folder / ".gitkeep").touch()
        (self.repo / ".gitignore").write_text("runtime/\n", encoding="utf-8")
        (self.repo / "notes.txt").write_text("original\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "initial")
        git(self.repo, "branch", "-M", "main")
        git(self.repo, "push", "-u", "origin", "main")
        git(root, "clone", "--branch", "main", str(self.remote), str(self.other))
        git(self.other, "config", "user.name", "ASTRA Test")
        git(self.other, "config", "user.email", "test@example.invalid")
        (self.repo / "commands" / "inbox").mkdir()

    def test_only_exchange_files_are_committed(self):
        (self.repo / "notes.txt").write_text("private local edit\n", encoding="utf-8")
        git(self.repo, "add", "notes.txt")
        (self.repo / "results" / "one.json").write_text('{"status":"success"}\n')

        result = bridge.sync_once(self.repo)

        self.assertTrue(result["committed"])
        self.assertTrue(result["pushed"])
        self.assertEqual(git(self.repo, "show", "HEAD:notes.txt"), "original")
        self.assertEqual(git(self.repo, "diff", "--cached", "--name-only"), "notes.txt")
        git(self.other, "pull", "--ff-only", "origin", "main")
        self.assertTrue((self.other / "results" / "one.json").exists())

    def test_remote_commands_are_pulled_and_push_failure_retries(self):
        (self.other / "commands" / "inbox").mkdir()
        (self.other / "commands" / "inbox" / "incoming.json").write_text(
            '{"id":"incoming","op":"bridge.ping"}\n'
        )
        git(self.other, "add", "-A")
        git(self.other, "commit", "-m", "new command")
        git(self.other, "push", "origin", "main")
        first = bridge.sync_once(self.repo)
        self.assertTrue(first["pulled"])
        self.assertEqual(first["pending_commands"], 1)

        (self.repo / "results" / "incoming.json").write_text('{"status":"success"}\n')
        git(self.repo, "remote", "set-url", "--push", "origin", str(self.repo / "missing.git"))
        with self.assertRaisesRegex(RuntimeError, "git push"):
            bridge.sync_once(self.repo)
        self.assertTrue((self.repo / "results" / "incoming.json").exists())
        self.assertEqual(git(self.repo, "show", "HEAD:results/incoming.json"), '{"status":"success"}')

        git(self.repo, "remote", "set-url", "--push", "origin", str(self.remote))
        retry = bridge.sync_once(self.repo)
        self.assertFalse(retry["committed"])
        self.assertTrue(retry["pushed"])

    def test_concurrent_command_and_result_are_rebased_and_pushed(self):
        (self.repo / "results" / "previous.json").write_text('{"status":"success"}\n')
        (self.repo / "notes.txt").write_text("private local edit\n", encoding="utf-8")
        git(self.repo, "add", "notes.txt")
        (self.other / "commands" / "inbox").mkdir()
        (self.other / "commands" / "inbox" / "next.json").write_text(
            '{"id":"next","op":"bridge.ping"}\n'
        )
        git(self.other, "add", "-A")
        git(self.other, "commit", "-m", "next command")
        git(self.other, "push", "origin", "main")

        outcome = bridge.sync_once(self.repo)

        self.assertTrue(outcome["committed"])
        self.assertTrue(outcome["pulled"])
        self.assertTrue(outcome["pushed"])
        self.assertTrue((self.repo / "commands" / "inbox" / "next.json").exists())
        self.assertEqual((self.repo / "notes.txt").read_text(), "private local edit\n")
        git(self.other, "pull", "--ff-only", "origin", "main")
        self.assertTrue((self.other / "results" / "previous.json").exists())
        self.assertEqual((self.other / "notes.txt").read_text(), "original\n")

    def test_status_heartbeat_is_local_and_replaceable(self):
        bridge.write_status(self.repo, {"state": "syncing", "heartbeat_at": "first"})
        bridge.write_status(self.repo, {"state": "ready", "heartbeat_at": "second"})
        status = json.loads((self.repo / bridge.STATUS_FILE).read_text())
        self.assertEqual(status["state"], "ready")
        self.assertEqual(status["heartbeat_at"], "second")
        self.assertFalse((self.repo / (str(bridge.STATUS_FILE) + ".tmp")).exists())
        self.assertFalse(git(self.repo, "status", "--porcelain"))


if __name__ == "__main__":
    unittest.main()
