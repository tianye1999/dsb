#!/usr/bin/env python3
"""自测入口主流程回归：不只测 judge()，要覆盖真实漏洞路径。"""
import contextlib
import io
import json, os, sys, tempfile, unittest
from unittest.mock import patch
HERE = os.path.dirname(os.path.abspath(__file__)); SK = os.path.dirname(HERE)
sys.path.insert(0, SK)
import vd_selftest as vst

MAIN = "com.example.launcher/.MainActivity"
PKG = vst.PKG


def shot(i, **kw):
    p = kw.pop("path", None)
    if p is None:
        fd, p = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
    with open(p, "wb") as f:
        f.write(b"x" * 2000)
    d = {"id": i, "display_id": 77, "vd_sid": "vd-x", "shot_src": "app",
         "fg_app": PKG, "shot_black": False, "path": p, "tree_hash": "h",
         "nodes": [{"text": "COUNT=%d" % kw.pop("count", 0), "rect": [0, 0, 10, 10]},
                   {"text": "TAP+1", "rect": [60, 820, 660, 1080]}]}
    d.update(kw); return d


class FakeDeps:
    def __init__(self, main_before=MAIN, main_after=MAIN, main_after_stop=MAIN,
                 shot2_over=None, stop_ev=None):
        self.main_before, self.main_after, self.main_after_stop = main_before, main_after, main_after_stop
        self.sess = {"displayId": 77, "sid": "vd-x", "state": "active",
                     "recording": "/tmp/vd-no-such.mkv", "pidFile": "/tmp/vd-no-such.pid",
                     "scrcpyPid": None}
        self.s1 = shot("1", count=0)
        self.s2 = shot("2", count=1)
        if shot2_over: self.s2.update(shot2_over)
        self.stop_ev = stop_ev or {"displayStillPresent": False, "recordingDeleted": True}
        self.stop_called = False; self.start_called = False; self.n = 0
        self.foreground_reads = 0

    def load_active_session(self): return None
    def foreground(self, did):
        if did: return PKG + "/.MainActivity"
        self.foreground_reads += 1
        if self.foreground_reads == 1: return self.main_before
        return self.main_after if self.foreground_reads == 2 else self.main_after_stop
    def start(self):
        self.start_called = True
        return {"displayId": 77, "session": self.sess}
    def verify(self, sid): return {"ok": True, "session": self.sess}
    def look(self, did, note):
        self.n += 1
        return {"shot_id": "1" if self.n == 1 else "2"}, 0
    def last_shot(self): return self.s1 if self.n <= 1 else self.s2
    def tap(self, *a): return {"dispatch_status": "command_completed"}, 0
    def stop(self, sess):
        self.stop_called = True
        return {"ok": True, "evidence": self.stop_ev}
    def blue(self): pass


class MainFlowTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.TemporaryDirectory(prefix="vdself-index-")
        self.addCleanup(self.base.cleanup)
        base_patch = patch.object(vst, "BASE", self.base.name)
        base_patch.start()
        self.addCleanup(base_patch.stop)

    def _run(self, deps):
        self.addCleanup(os.unlink, deps.s1["path"])
        self.addCleanup(os.unlink, deps.s2["path"])
        with tempfile.TemporaryDirectory(prefix="vdself-") as d:
            return vst.execute(deps, d)

    def test_main_unknown_before_start_fails(self):
        d = FakeDeps(main_before="")
        s = self._run(d)
        self.assertFalse(s["pass"]); self.assertIn("main_foreground_unknown", s["error"])
        self.assertFalse(d.start_called)

    def test_after_credential_cross_display_fails(self):
        d = FakeDeps(shot2_over={"display_id": 99})
        s = self._run(d)
        self.assertFalse(s["pass"]); self.assertIn("credential_display_mismatch", s["error"])
        self.assertTrue(d.stop_called, "失败路径也必须执行清理")

    def test_stop_ok_but_recording_not_deleted_fails(self):
        d = FakeDeps(stop_ev={"displayStillPresent": False, "recordingDeleted": False})
        s = self._run(d)
        self.assertFalse(s["pass"]); self.assertIn("cleanup_failed", s["error"])
        self.assertIn("recording_not_deleted", s["cleanup"]["problems"])

    def test_main_changed_after_stop_fails(self):
        d = FakeDeps(main_after_stop="com.other/.Main")
        s = self._run(d)
        self.assertFalse(s["pass"])
        self.assertIn("main_changed_after_stop", s["cleanup"]["problems"])

    def test_offline_execution_preserves_live_result_index(self):
        for name in ("summary.json", "latest.json"):
            with open(os.path.join(vst.BASE, name), "w") as f:
                f.write("verified-device-result")
        self._run(FakeDeps())
        for name in ("summary.json", "latest.json"):
            with open(os.path.join(vst.BASE, name)) as f:
                self.assertEqual(f.read(), "verified-device-result")

    def test_cleanup_process_check_transport_failure_is_not_success(self):
        d = FakeDeps()
        d.sess["scrcpyPid"] = 123
        with patch.object(vst.vd, "_termux_alive", return_value=False), patch.object(
            vst.vd, "_termux", side_effect=vst.vd.VDError("termux_timeout")
        ):
            s = self._run(d)
        self.assertFalse(s["pass"])
        self.assertIn("scrcpy_exit_unverified", s["cleanup"]["problems"])

    def test_cli_failure_returns_two_and_publishes_failed_run(self):
        d = FakeDeps(main_before="")
        self.addCleanup(os.unlink, d.s1["path"])
        self.addCleanup(os.unlink, d.s2["path"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(vst.main([], deps=d), 2)
        with open(os.path.join(vst.BASE, "summary.json")) as f:
            self.assertFalse(json.load(f)["pass"])

    def test_happy_path_passes(self):
        s = self._run(FakeDeps())
        self.assertTrue(s["pass"], s.get("error"))


if __name__ == "__main__":
    unittest.main()
