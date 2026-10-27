"""Offline filesystem and look integration tests for screenshot retention."""
import argparse
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

import shot_retention as retention
import safe_tap as st


class ShotRetentionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.now = time.time()

    def file(self, name, age_days=4, root=None):
        path = (root or self.root) / name
        path.write_bytes(b"12345")
        stamp = self.now - age_days * 86400
        os.utime(path, (stamp, stamp))
        return path

    def prune(self, **kwargs):
        kwargs.setdefault("now", self.now)
        return retention.prune_shots(self.root, **kwargs)

    def test_old_new_names_symlink_and_subdirectory(self):
        old = [self.file("shot-1.jpg"), self.file("before-2.jpeg"), self.file("after-3.png")]
        new = self.file("shot-4.jpg", 1)
        other = [self.file("shot-5.gif"), self.file("xshot-6.jpg"), self.file("shot-x.jpg")]
        sub = self.root / "nested"
        sub.mkdir()
        nested = self.file("shot-7.jpg", root=sub)
        external = tempfile.TemporaryDirectory()
        self.addCleanup(external.cleanup)
        outside = self.file("shot-8.jpg", root=Path(external.name))
        link = self.root / "shot-9.jpg"
        link.symlink_to(outside)
        result = self.prune()
        self.assertEqual(result["deleted"], 3)
        self.assertEqual(result["bytes"], 15)
        self.assertEqual(result["kept"], 1)
        self.assertEqual(result["errors"], 0)
        self.assertTrue(all(not p.exists() for p in old))
        self.assertTrue(all(p.exists() for p in [new, *other, nested, outside, link]))

    def test_throttle_and_nonblocking_lock(self):
        first = self.file("shot-1.jpg")
        self.assertEqual(self.prune()["deleted"], 1)
        second = self.file("shot-2.jpg")
        self.assertEqual(self.prune(now=self.now + 10)["skipped_reason"], "throttled")
        self.assertTrue(second.exists())
        fd = os.open(self.root / ".retention-lock", os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertEqual(self.prune(throttle=False)["skipped_reason"], "locked")
        self.assertTrue(second.exists())
        fcntl.flock(fd, fcntl.LOCK_UN)
        self.assertEqual(self.prune(now=self.now + 6 * 3600 + 1)["deleted"], 1)
        self.assertFalse(second.exists())
        self.assertFalse(first.exists())

    def test_invalid_environment_defaults_to_three_days(self):
        recent = self.file("shot-1.jpg", 2)
        old = self.file("shot-2.jpg", 4)
        for value in ("", "0", "366", "2.5", "garbage", "-1"):
            with self.subTest(value=value), patch.dict(os.environ, {"DSB_SHOT_RETENTION_DAYS": value}):
                self.assertEqual(retention.retention_days(), 3)
        with patch.dict(os.environ, {"DSB_SHOT_RETENTION_DAYS": "garbage"}):
            self.assertEqual(self.prune()["deleted"], 1)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertEqual(self.prune(days=0, throttle=False)["skipped_reason"], "invalid_days")

    def test_file_and_time_budgets(self):
        for i in range(3):
            self.file(f"shot-{i}.jpg")
        result = self.prune(max_files=2)
        self.assertEqual(result["deleted"], 2)
        self.assertTrue(result["truncated"])
        self.assertFalse((self.root / ".retention-stamp").exists())
        self.assertEqual(len(list(self.root.glob("shot-*.jpg"))), 1)
        result = self.prune(throttle=False, max_seconds=0)
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(len(list(self.root.glob("shot-*.jpg"))), 1)

    def test_unlink_failure_does_not_stop_later_files(self):
        bad = self.file("shot-1.jpg")
        good = self.file("shot-2.jpg")
        real_unlink = os.unlink
        def unlink(path, *args, **kwargs):
            if os.fspath(path) == bad.name:
                raise PermissionError("denied")
            return real_unlink(path, *args, **kwargs)
        with patch.object(retention.os, "unlink", side_effect=unlink):
            result = self.prune()
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["deleted"], 1)
        self.assertFalse((self.root / ".retention-stamp").exists())
        self.assertTrue(bad.exists())
        self.assertFalse(good.exists())

    def test_dry_run_does_not_delete_or_stamp(self):
        old = self.file("shot-1.png")
        result = self.prune(dry_run=True)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["bytes"], 5)
        self.assertTrue(old.exists())
        self.assertFalse((self.root / ".retention-stamp").exists())
        self.assertFalse((self.root / ".retention-lock").exists())

    def test_directory_symlink_and_replacement_cannot_reach_target(self):
        external = tempfile.TemporaryDirectory()
        self.addCleanup(external.cleanup)
        outside = Path(external.name)
        victim = self.file("shot-1.jpg", root=outside)
        link = self.root / "shots-link"
        link.symlink_to(outside, target_is_directory=True)
        result = retention.prune_shots(link, now=self.now)
        self.assertEqual(result["skipped_reason"], "io_error")
        self.assertTrue(victim.exists())
        self.assertFalse((outside / ".retention-lock").exists())
        self.assertFalse((outside / ".retention-stamp").exists())
        self.assertEqual(retention.directory_usage(link)["errors"], 1)

        original = self.root / "original"
        original.mkdir()
        old = self.file("shot-2.jpg", root=original)
        real_scandir = os.scandir
        def replace_then_scan(fd):
            original.rename(self.root / "renamed")
            original.symlink_to(outside, target_is_directory=True)
            return real_scandir(fd)
        with patch.object(retention.os, "scandir", side_effect=replace_then_scan):
            result = retention.prune_shots(original, now=self.now, throttle=False)
        self.assertEqual(result["deleted"], 1)
        self.assertFalse((self.root / "renamed" / old.name).exists())
        self.assertTrue(victim.exists())
        self.assertFalse((outside / ".retention-lock").exists())

    def test_fifo_and_hardlink_stamp_never_block_or_truncate(self):
        fifo = self.root / ".retention-stamp"
        os.mkfifo(fifo)
        worker = "import shot_retention,sys; print(shot_retention.prune_shots(sys.argv[1]))"
        completed = subprocess.run([sys.executable, "-c", worker, str(self.root)],
                                   cwd=Path(__file__).parent, capture_output=True, text=True, timeout=3)
        self.assertEqual(completed.returncode, 0)
        self.assertTrue(fifo.is_fifo())
        fifo.unlink()
        external = tempfile.TemporaryDirectory()
        self.addCleanup(external.cleanup)
        victim = Path(external.name) / "victim.txt"
        victim.write_text("keep me")
        os.link(victim, fifo)
        self.prune()
        self.assertEqual(victim.read_text(), "keep me")
        self.assertEqual(fifo.read_text(), "keep me")

    def test_bad_lock_is_skipped_without_mutation(self):
        old = self.file("shot-1.jpg")
        os.mkfifo(self.root / ".retention-lock")
        result = self.prune()
        self.assertIsNotNone(result["skipped_reason"])
        self.assertTrue(old.exists())
        (self.root / ".retention-lock").unlink()
        external = tempfile.TemporaryDirectory()
        self.addCleanup(external.cleanup)
        victim = Path(external.name) / "victim.txt"
        victim.write_text("keep me")
        os.link(victim, self.root / ".retention-lock")
        result = self.prune()
        self.assertEqual(result["skipped_reason"], "invalid_lock")
        self.assertEqual(victim.read_text(), "keep me")
        self.assertTrue(old.exists())

    def test_no_nofollow_support_refuses_cleanup(self):
        old = self.file("shot-1.jpg")
        with patch.object(retention.os, "O_NOFOLLOW", 0):
            result = self.prune()
        self.assertEqual(result["skipped_reason"], "io_error")
        self.assertTrue(old.exists())

    def test_protect_manual_and_future_mtime(self):
        protected = self.file("shot-1.jpg")
        removable = self.file("shot-2.jpg")
        future = self.file("shot-3.jpg", -2)
        result = self.prune(protect={str(protected)}, throttle=False)
        self.assertEqual(result["deleted"], 1)
        self.assertTrue(protected.exists())
        self.assertFalse(removable.exists())
        self.assertTrue(future.exists())
        self.assertFalse((self.root / ".retention-stamp").exists())
        result = self.prune(protect={str(protected)})
        self.assertEqual(result["deleted"], 0)
        self.assertIsNone(result["skipped_reason"])
        self.assertTrue((self.root / ".retention-stamp").exists())

    def test_missing_directory_is_empty_success(self):
        missing = self.root / "missing"
        for dry_run in (False, True):
            result = retention.prune_shots(missing, dry_run=dry_run)
            self.assertEqual((result["deleted"], result["errors"]), (0, 0))
            self.assertIsNone(result["skipped_reason"])
        self.assertFalse(missing.exists())
        output = io.StringIO()
        with patch.object(st, "SHOTS", str(missing)), patch.object(st, "audit"), \
                contextlib.redirect_stdout(output):
            self.assertEqual(st.cmd_prune_shots(types.SimpleNamespace(days=None, dry_run=False)), 0)
        self.assertEqual(json.loads(output.getvalue())["deleted"], 0)

    def test_doctor_reports_bounded_usage_errors(self):
        output = io.StringIO()
        with patch.object(st, "SHOTS", str(self.root)), \
                patch.object(st, "transport_check", return_value=("fail", "offline")), \
                patch.object(st.shot_retention, "directory_usage",
                             return_value={"files": 2, "bytes": 1048576, "oldest_days": 4,
                                           "errors": 1, "truncated": True}), \
                contextlib.redirect_stdout(output):
            self.assertEqual(st.cmd_doctor(types.SimpleNamespace()), 2)
        self.assertIn("2 文件, 1.0 MB", output.getvalue())
        self.assertIn("扫描错误 1, 统计已截断", output.getvalue())

    def test_usage_scan_has_budget(self):
        self.file("shot-1.jpg")
        self.file("shot-2.jpg")
        usage = retention.directory_usage(self.root, now=self.now, max_entries=1)
        self.assertTrue(usage["truncated"])
        self.assertEqual(usage["scanned"], 1)

    def test_non_ascii_digits_are_not_candidates(self):
        foreign = self.file("shot-١.jpg")
        self.assertEqual(self.prune()["deleted"], 0)
        self.assertTrue(foreign.exists())

    def test_usage_and_manual_command(self):
        old = self.file("shot-1.jpg")
        self.file("unrelated.txt")
        usage = retention.directory_usage(self.root, now=self.now)
        self.assertEqual((usage["files"], usage["bytes"]), (1, 5))
        self.assertAlmostEqual(usage["oldest_days"], 4)
        self.assertEqual(st._retention_days_arg("365"), 365)
        with self.assertRaises(argparse.ArgumentTypeError):
            st._retention_days_arg("366")
        output = io.StringIO()
        with patch.object(st, "SHOTS", str(self.root)), contextlib.redirect_stdout(output):
            rc = st.cmd_prune_shots(types.SimpleNamespace(days=3, dry_run=True))
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(output.getvalue())["deleted"], 1)
        self.assertTrue(old.exists())

    def test_manual_command_protects_current_credential(self):
        current = self.file("shot-1.jpg")
        stale = self.file("shot-2.jpg")
        output = io.StringIO()
        with patch.object(st, "SHOTS", str(self.root)), \
                patch.object(st, "load_state", return_value={"last_shot": {"path": str(current)}}), \
                patch.object(st, "audit") as audit, contextlib.redirect_stdout(output):
            self.assertEqual(st.cmd_prune_shots(types.SimpleNamespace(days=1, dry_run=False)), 0)
        self.assertTrue(current.exists())
        self.assertFalse(stale.exists())
        self.assertFalse((self.root / ".retention-stamp").exists())
        self.assertEqual(audit.call_args.args[0]["kind"], "shot_retention")


class LookRetentionTests(unittest.TestCase):
    def test_prune_failure_does_not_change_look_result(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = {}
        def save(value):
            state.clear()
            state.update(value)
        replacements = {
            "SHOTS": tmp.name, "load_state": lambda: state.copy(), "save_state": save,
            "observe": lambda **kw: None, "grab": lambda *a, **kw: str(Path(tmp.name) / "shot-1.png"),
            "dump_nodes": lambda: [{"text": "Settings"}], "foreground": lambda: "test/.Page",
            "build_zones": lambda *a: [], "_ball": lambda *a: None,
            "wakefulness": lambda: "Awake", "screen_size": lambda: (1260, 2800),
            "cfg_get": lambda k, d=None: d, "OCR_OK": False,
            "_ocr_cancel_check": lambda: lambda: False,
        }
        patches = [patch.object(st, key, value, create=True) for key, value in replacements.items()]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        out = io.StringIO()
        with patch.object(st.shot_retention, "prune_shots", side_effect=RuntimeError("disk")), \
                patch.object(st, "audit") as audit, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = st.cmd_look(types.SimpleNamespace(note="test"))
        self.assertEqual(rc, 0)
        self.assertIn("shot_id", json.loads(out.getvalue()))
        self.assertIn("last_shot", state)
        self.assertEqual([c.args[0]["kind"] for c in audit.call_args_list],
                         ["read", "shot_retention"])
        self.assertEqual(audit.call_args_list[1].args[0]["errors"], 1)

        summary = {"deleted": 2, "bytes": 10, "kept": 1, "errors": 1,
                   "skipped_reason": "io_error"}
        locked = [False]
        @contextlib.contextmanager
        def observed_lock():
            locked[0] = True
            try:
                yield
            finally:
                locked[0] = False
        def prune_outside_lock(*args, **kwargs):
            self.assertFalse(locked[0])
            return summary
        with patch.object(st, "lock", observed_lock), \
                patch.object(st.shot_retention, "prune_shots", side_effect=prune_outside_lock) as prune, \
                patch.object(st, "audit") as audit, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(st.cmd_look(types.SimpleNamespace(note="test")), 0)
        self.assertEqual(len(prune.call_args.kwargs["protect"]), 1)
        self.assertEqual([c.args[0]["kind"] for c in audit.call_args_list],
                         ["read", "shot_retention"])
        self.assertEqual(audit.call_args_list[1].args[0],
                         {"kind": "shot_retention", "deleted": 2, "bytes": 10,
                          "kept": 1, "errors": 1, "truncated": False})


if __name__ == "__main__":
    unittest.main()
