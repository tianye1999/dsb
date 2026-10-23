"""State integrity tests with temporary files and synthetic device interfaces."""
import contextlib
import io
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import types
import unittest
from unittest.mock import patch
import safe_tap as st
import test_safe_tap_credentials as fixture


class StateIntegrity(unittest.TestCase):
    setUp = fixture.Credentials.setUp
    reset = fixture.Credentials.reset
    action = fixture.Credentials.action

    def test_serialization_failure_preserves_previous_file(self):
        before = Path(st.STATE).read_bytes()
        with self.assertRaises(TypeError):
            st.save_state({"unsupported": object()})
        self.assertEqual(Path(st.STATE).read_bytes(), before)

    def test_replace_failure_preserves_previous_file_and_cleans_temp(self):
        before = Path(st.STATE).read_bytes()
        with patch.object(st.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                st.save_state({"new": True})
        self.assertEqual(Path(st.STATE).read_bytes(), before)
        self.assertEqual(list(Path(st.HERE).glob(".safe-state-*")), [])

    def test_file_fsync_failure_preserves_old_state(self):
        before = Path(st.STATE).read_bytes()
        with patch.object(st.os, "fsync", side_effect=OSError("fsync failed")):
            with self.assertRaises(OSError):
                st.save_state({"new": True})
        self.assertEqual(Path(st.STATE).read_bytes(), before)

    def test_file_sync_then_replace_then_directory_sync(self):
        events = []
        real_replace, real_sync = os.replace, os.fsync
        def replace(src, dst):
            self.assertEqual(Path(src).parent, Path(dst).parent)
            events.append("replace")
            return real_replace(src, dst)
        def sync(fd):
            events.append("sync")
            return real_sync(fd)
        with patch.object(st.os, "replace", side_effect=replace), \
                patch.object(st.os, "fsync", side_effect=sync):
            st.save_state({"new": True})
        self.assertEqual(events, ["sync", "replace", "sync"])
        self.assertEqual(st.load_state(), {"new": True})

    def test_invalid_state_is_not_treated_as_first_run(self):
        for raw in ('{"last_shot":', '[]', 'null', '{"taps":null}',
                    '{"keys":[42]}', '{"last_shot":[]}'):
            with self.subTest(raw=raw):
                Path(st.STATE).write_text(raw)
                with self.assertRaises(ValueError):
                    st.load_state()
                self.assertEqual(Path(st.STATE).read_text(), raw)

    def test_corrupt_state_blocks_look_before_any_device_access(self):
        raw = '{"last_shot":'
        Path(st.STATE).write_text(raw)
        with patch.object(st, "observe") as observe, \
                patch.object(st, "_ocr_cancel_check") as cancel, \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ValueError):
                st.cmd_look(types.SimpleNamespace(note="invalid"))
        observe.assert_not_called()
        cancel.assert_not_called()
        self.assertEqual(Path(st.STATE).read_text(), raw)

    def test_unreadable_state_does_not_become_empty(self):
        with patch("builtins.open", side_effect=PermissionError("unreadable")):
            with self.assertRaises(PermissionError):
                st.load_state()

    def test_missing_state_remains_first_run(self):
        os.unlink(st.STATE)
        self.assertEqual(st.load_state(), {})

    def test_explicit_nonstrict_diagnostic_compatibility(self):
        Path(st.STATE).write_text('{"last_shot":')
        self.assertEqual(st.load_state(strict=False), {})

    def test_all_actions_stop_before_dispatch_on_persist_error(self):
        for kind in ("tap", "key", "swipe", "long_press", "drag", "scroll"):
            with self.subTest(kind=kind):
                self.reset()
                with patch.object(st.os, "replace", side_effect=OSError("disk")):
                    with self.assertRaises(OSError):
                        self.action(kind, [4] if kind == "key" else [(600, 900), (600, 1200)])
                self.assertEqual(self.calls, [])
                self.assertNotIn("consumed_at", st.load_state()["last_shot"])

    def test_directory_sync_failure_does_not_dispatch_or_refund(self):
        real_sync = os.fsync
        calls = []
        def sync(fd):
            calls.append(fd)
            if len(calls) == 2:
                raise OSError("directory sync failed")
            return real_sync(fd)
        with patch.object(st.os, "fsync", side_effect=sync):
            with self.assertRaises(OSError):
                self.action()
        self.assertEqual(self.calls, [])
        self.assertIn("consumed_at", st.load_state()["last_shot"])

    def test_sigkill_leaves_complete_old_or_new_file(self):
        worker = '''
import os, sys, time
import safe_tap as st
st.STATE = sys.argv[1]
real = os.replace
def paused(src, dst):
    if sys.argv[2] == "after":
        real(src, dst)
    print("ready", flush=True)
    time.sleep(30)
    if sys.argv[2] == "before":
        real(src, dst)
st.os.replace = paused
st.save_state({"last_shot": {"id": "new", "consumed_at": 123}, "keys": [{"code": 4}]})
'''
        for stage in ("before", "after"):
            with self.subTest(stage=stage):
                self.reset()
                old = st.load_state()
                p = subprocess.Popen([sys.executable, "-c", worker, st.STATE, stage],
                    cwd=Path(st.__file__).parent, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(p.stdout, selectors.EVENT_READ)
                        self.assertTrue(selector.select(10), "child did not reach crash point")
                    self.assertEqual(p.stdout.readline(), b"ready\n")
                    p.kill()
                    p.communicate(timeout=5)
                    self.assertEqual(p.returncode, -signal.SIGKILL)
                finally:
                    if p.poll() is None:
                        p.kill()
                    p.communicate(timeout=5)
                actual = st.load_state()
                if stage == "before":
                    self.assertEqual(actual, old)
                else:
                    self.assertEqual(actual["last_shot"]["consumed_at"], 123)
                    self.assertEqual(actual["keys"], [{"code": 4}])


if __name__ == "__main__":
    unittest.main()
