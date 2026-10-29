"""T18 offline transport tests. All ADB/tsh subprocesses are synthetic."""
import contextlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import types
import unittest
from unittest.mock import patch

from PIL import Image
import safe_tap as st
import vd_session as vd

REAL_RUN = subprocess.run
REAL_AUDIT = st.audit

def frame(tag, data):
    return ("DSB_%s %d\n" % (tag, len(data))).encode() + data


def observation(nonce="100000", did=35, image=b"x" * 1001, **updates):
    data = dict(nonce=nonce, displayId=did, image="look-9.jpg", jpg="look-9.jpg",
                jpgBytes=len(image), nodesRaw="[]")
    data.update(updates)
    return frame("JSON", json.dumps(data).encode()) + frame("JPEG", image)


class ObservationTransport(unittest.TestCase):
    def observe(self, output):
        ticks = iter([100 + i * .01 for i in range(10000)])
        with patch.object(st.time, "time", side_effect=lambda: next(ticks)), \
                patch.object(st.time, "sleep", return_value=None), \
                patch.object(st.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, output, b"")) as run:
            result = st.observe(display_id=35)
        return result, run.call_args_list

    def test_image_and_metadata_arrive_in_one_call_including_reused_name(self):
        result, calls = self.observe(observation(reused=True))
        self.assertIsNotNone(result)
        self.assertEqual(result["_jpg_bytes"], b"x" * 1001)
        self.assertEqual(len(calls), 1)
        self.assertIn("exec-out", calls[0].args[0])

    def test_corrupt_frames_fail_closed(self):
        good = observation()
        for bad in (b"", good.replace(b"DSB_JSON", b"BAD_JSON"), good[:-1], good + b"x",
                    observation(jpgBytes=1000), observation(nonce="other100000"),
                    observation(did=0), observation(jpg="../look-9.jpg"),
                    good.replace(b"DSB_JPEG 1001", b"DSB_JPEG 9999")):
            with self.subTest(output=bad[:50]):
                self.assertIsNone(self.observe(bad)[0])

    def test_timeout_is_observation_failure(self):
        with patch.object(st.subprocess, "run", side_effect=subprocess.TimeoutExpired("synthetic", 4)):
            self.assertIsNone(st.observe(display_id=35))

    def test_readonly_observation_ignores_stderr_noise_and_invalid_fg_utf8(self):
        fg = {}
        raw = observation() + frame("FOREGROUND", b"test\xff/.Page")
        with patch.object(st.time, "time", return_value=100), \
                patch.object(st.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 0, raw, b"warning: exception in unrelated diagnostic")):
            self.assertIsNotNone(st.observe(display_id=35, foreground_out=fg))
        self.assertEqual(fg["dump"], "test/.Page")

    def test_readonly_observation_still_rejects_nonzero_return_code(self):
        with patch.object(st.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 1, observation(), b"")):
            self.assertIsNone(st.observe(display_id=35))

    def test_settle_observation_errors_are_unstable(self):
        for error in (st.ActionDispatchError("paused", "not_submitted", 78),
                      st.EndpointUnavailable("rotated"), FileNotFoundError("gone")):
            with self.subTest(error=type(error).__name__), \
                    patch.object(st, "observe", side_effect=error), \
                    patch.object(st.time, "sleep", return_value=None), \
                    patch.object(st.time, "time", side_effect=[10, 10.1, 10.2, 10.3, 10.4]):
                _, polls, stable = st._settle_stable(budget=.3)
            self.assertEqual(polls, 2)
            self.assertFalse(stable)

    def test_cancellation_has_no_baseline_call_and_uses_read_time_for_throttle(self):
        with patch.object(st.subprocess, "run") as run, patch.object(st.time, "time", return_value=100):
            cancel = st._LookCancellation()
            run.assert_not_called()
        cancel.feed("ABORT 99999\n", 20)
        self.assertFalse(cancel.stopped)
        self.assertEqual(cancel.next_check, 20.5)
        cancel.feed("ABORT 100000\n", 21)
        self.assertTrue(cancel.stopped)

    def test_main_fallback_does_not_reuse_foreground_from_earlier_failed_frame(self):
        fg = {}
        def observe(**kwargs):
            kwargs["foreground_out"]["dump"] = "old frame foreground"
            return {"image": True, "_jpg_bytes": b"short"}
        with tempfile.TemporaryDirectory(prefix="dsb-t18-", dir="/tmp") as root, \
                patch.object(st, "observe", observe), \
                patch.object(st.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"x" * 1001, b"")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(st.grab(os.path.join(root, "fallback.jpg"), foreground_out=fg))
        self.assertEqual(fg, {})


class CommandTransport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dsb-t18-", dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.events, self.calls = [], []
        self.bad_observation = None
        self.receipt_status = "completed"
        self.abort = ""
        self.frame_dump = None
        self.query_overrides = {}
        self.audits = []
        self.after_snapshot = None
        self.after_observe = None
        self.display_id = 35
        self.batch_mode = False
        self.sess = dict(sid="test-35", state="active", displayId=35, width=720, height=1600,
                         uniqueId="virtual:test", package="test", pid="456", scrcpyPid=123,
                         expiresAt=time.time() + 600)
        self.dump = ("Display #35 (activities from top to bottom):\n"
                     " topResumedActivity=ActivityRecord{abc u0 test/.Page t1}\n"
                     "Display #0 (activities from top to bottom):\n"
                     " topResumedActivity=ActivityRecord{def u0 home/.Main t2}\n")
        path = os.path.join(self.tmp.name, "credential.jpg")
        Image.new("RGB", (720, 1600), "white").save(path)
        with open(path, "rb") as f:
            self.jpeg = f.read()
        for name, value in dict(STATE=os.path.join(self.tmp.name, "state.json"), SHOTS=self.tmp.name,
                                HERE=self.tmp.name, _VD_INSETS={}, _INSETS=None,
                                audit=self.audits.append, _prune_after_look=lambda *a: None).items():
            p = patch.object(st, name, value, create=True)
            p.start()
            self.addCleanup(p.stop)
        for target, name, value in ((vd, "load_session", lambda: dict(self.sess)),
                                    (st, "adb_command", lambda: ["synthetic-adb"]),
                                    (st.subprocess, "run", self.run_device)):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        st.save_state({"last_shot": dict(id="one", path=path, ts=time.time(), fg="test/.Page",
                        fg_app="test", display_id=35, vd_sid="test-35", shot_src="app",
                        display_width=720, display_height=1600, display_coverage="tree",
                        zone_coverage="clean", zones=[], nodes=[], pixel_nodes=[],
                        node_count=1, interactive=True, shot_black=False)})

    def run_device(self, argv, **kwargs):
        self.calls.append(argv)
        script = argv[-1]
        if argv[0] == "bash":
            self.events.append("liveness")
            return subprocess.CompletedProcess(argv, 0, b"alive\n", b"")
        if "__DSB_" in script:
            self.events.append("snapshot")
            parts = script.split("; r=$?; echo; echo ")
            values = dict(displays="0 35", display='DisplayInfo{displayId 35, real 720 x 1600, type VIRTUAL, uniqueId "virtual:test"}',
                          overlay="null", activities=self.dump, pidof="456", insets="Display: mDisplayId=35\n")
            values.update(self.query_overrides)
            out = b""
            for i in range(len(parts) - 1):
                command = parts[i]
                marker = parts[i + 1].split(" $r", 1)[0]
                if "abort.flag" in command:
                    value = self.abort
                elif "get-displays" in command:
                    value = values["displays"]
                elif "dumpsys display" in command:
                    value = values["display"]
                elif "settings get" in command:
                    value = values["overlay"]
                elif "dumpsys activity" in command:
                    value = values["activities"]
                elif "pidof" in command:
                    value = values["pidof"]
                elif "dumpsys window" in command:
                    value = values["insets"]
                else:
                    raise AssertionError(command)
                out += (value + "\n" + marker + " 0\n").encode()
            if self.after_snapshot:
                self.after_snapshot()
            return subprocess.CompletedProcess(argv, 0, out, b"")
        if "exec-out" in argv:
            self.assertEqual(len(argv), 3)
            script = argv[2]
            match = re.search(r"观测(?:屏图:35:|屏树:35:|图:|:)([0-9]+)(:f)?", script)
            if match:
                nonce = match[1]
                image = "观测屏图:" in script or "观测图:" in script
                if "emit_frame CANCELLED" in script:
                    start = int(re.search(r'\[\[ "\$epoch" < ([0-9]+)', script)[1])
                    flag = re.fullmatch(r"ABORT ([0-9]+)\s*", self.abort)
                    if self.abort and (flag is None or int(flag[1]) >= start):
                        return subprocess.CompletedProcess(argv, 0, frame("CANCELLED", self.abort.encode()), b"")
                self.events.append("image" if image else "settle")
                if self.bad_observation is not None:
                    bad = self.bad_observation
                    if callable(bad):
                        bad = bad(nonce)
                    if isinstance(bad, Exception):
                        raise bad
                    return subprocess.CompletedProcess(argv, 0, bad, b"")
                data = dict(nonce=nonce + (":f" if not image and self.display_id == 0 and match[2] else ""),
                            displayId=self.display_id, image="look-9.jpg" if image else None,
                            jpg="look-9.jpg", jpgBytes=len(self.jpeg), ts=int(time.time() * 1000),
                            w=720, h=1600, fg="test", treeHash="same", evCount=1,
                            appWindows=1, displayCoverage="tree", interactive=True,
                            nodesRaw=json.dumps([dict(text="Settings", rect=[100, 100, 200, 200])]))
                if self.batch_mode:
                    data.update(windowId=10, evCount=self.events.count("dispatch"))
                out = frame("JSON", json.dumps(data).encode())
                if image:
                    out += frame("JPEG", self.jpeg)
                if "emit_frame FOREGROUND" in script:
                    self.assertLess(script.index("emit_frame JPEG") if image else
                                    script.index("emit_frame JSON"), script.index("dumpsys activity"))
                    self.events.append("foreground")
                    out += frame("FOREGROUND", (self.dump if self.frame_dump is None else self.frame_dump).encode())
                if "emit_frame ABORT" in script:
                    out += frame("ABORT", self.abort.encode())
                if self.after_observe:
                    self.after_observe()
                return subprocess.CompletedProcess(argv, 0, out, b"")
            match = re.search(r"手势屏:35:tap:[0-9:]+:([0-9]+)", script)
            if match:
                self.events.append("dispatch")
                self.assertIn("consumed_at", st.load_state()["last_shot"])
                rec = dict(nonce=match[1], displayId=35, kind="tap", status=self.receipt_status)
                out = b"DSB_SUBMITTED\n" + frame("RECEIPT", json.dumps(rec).encode())
                return subprocess.CompletedProcess(argv, 0, out, b"")
        if "abort.flag" in argv[-1]:
            return subprocess.CompletedProcess(argv, 0, self.abort.encode(), b"")
        if "dumpsys activity activities" in script:
            self.events.append("policy_foreground")
            return subprocess.CompletedProcess(argv, 0, self.dump.encode(), b"")
        if argv[-2:] == ["wm", "size"]:
            return subprocess.CompletedProcess(argv, 0, b"Physical size: 720x1600", b"")
        if argv[-2:] == ["dumpsys", "window"]:
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if "input" in argv and "tap" in argv:
            self.events.append("dispatch")
            self.assertIn("consumed_at", st.load_state()["last_shot"])
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        raise AssertionError("unexpected subprocess: %r" % argv)

    def action(self, fast=False, cancel_check=None):
        args = types.SimpleNamespace(shot="one", display=self.display_id, fast=fast, ack_no_zones=False,
                                     allow_main=self.display_id == 0,
                                     expect=None, expect_activity=None,
                                     _batch_cancel_check=(cancel_check if cancel_check is not None else
                                                          lambda: self.events.append("cancel") or False))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return st.do_action(args, "tap", [(360, 800)])

    def test_cold_virtual_tap_seven_calls_and_gate_order(self):
        real_save, real_same, real_cfg = st.save_state, st._fg_same, st.cfg_get
        def save(state):
            if "consumed_at" in state.get("last_shot", {}):
                self.events.append("consume")
            return real_save(state)
        def same(a, b):
            self.events.append("fg_check")
            return real_same(a, b)
        def cfg(key, default=None):
            if key == "look.ttl":
                self.events.append("ttl")
            return real_cfg(key, default)
        with patch.object(st, "save_state", save), patch.object(st, "_fg_same", same), patch.object(st, "cfg_get", cfg):
            self.assertEqual(self.action(), 0)
        self.assertLessEqual(len(self.calls), 7, self.calls)
        start = self.events.index("image")
        wanted = ["image", "foreground", "fg_check", "ttl", "consume", "cancel", "dispatch",
                  "settle", "settle", "image", "foreground"]
        self.assertEqual(self.events[start:start + len(wanted)], wanted)

    def test_virtual_look_four_calls(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(st.cmd_look(types.SimpleNamespace(display=35, note="")), 0)
        self.assertLessEqual(len(self.calls), 4, self.calls)
        self.assertEqual(st.load_state()["last_shot"]["display_id"], 35)

    @contextlib.contextmanager
    def cancel_transport(self, failure=None, step=None, latency=0):
        reads = []
        def run(argv, **kwargs):
            if (len(argv) >= 3 and argv[1] == "shell" and "abort.flag" in argv[-1]
                    and "__DSB_" not in argv[-1]):
                reads.append(kwargs["timeout"])
                current = st._BATCH_CONTEXT or {}
                fault = failure if step is None or current.get("batch_index") == step else None
                if latency > kwargs["timeout"] or fault == "timeout":
                    raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
                if fault == "transport":
                    raise OSError("synthetic transport failure")
                if fault in ("nonzero", "forged78", "policy78"):
                    diagnostic = b"read failed"
                    if fault in ("forged78", "policy78"):
                        nonce = kwargs["env"]["DSB_ADB_RECEIPT_NONCE"] if fault == "policy78" else "wrong"
                        diagnostic = ("ADB_POLICY_PAUSED: synthetic\nADB_POLICY_RECEIPT:" + nonce + "\n").encode()
                    return subprocess.CompletedProcess(argv, 1 if fault == "nonzero" else 78, b"", diagnostic)
            return self.run_device(argv, **kwargs)
        with patch.object(st.subprocess, "run", side_effect=run), \
                patch.object(st.time, "monotonic", side_effect=iter(range(100000))):
            yield reads

    def run_two_step_batch(self):
        self.batch_mode = True
        state = st.load_state()
        state["last_shot"].update(window_id=10, ev_count=0, tree_hash="same")
        st.save_state(state)
        args = types.SimpleNamespace(shot=state["last_shot"]["id"], display=35, allow_main=False,
                                     why=None, raw_input=False, steps=json.dumps([
                                         dict(kind="tap", x=360, y=800, expect="first"),
                                         dict(kind="tap", x=520, y=800, expect="second")]))
        audit_path = os.path.join(self.tmp.name, "batch-audit.jsonl")
        if os.path.exists(audit_path):
            os.remove(audit_path)
        real_cfg = st.cfg_get
        out = io.StringIO()
        with patch.object(st, "audit", REAL_AUDIT), patch.object(st, "AUDIT", audit_path), \
                patch.object(st, "cfg_get", side_effect=lambda k, d=None: True if k == "batch.enabled" else real_cfg(k, d)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = st.cmd_batch(args)
        with open(audit_path) as f:
            rows = [json.loads(line) for line in f]
        return rc, json.loads(out.getvalue()), [r for r in rows if r.get("kind") == "tap"]

    def test_slow_cancellation_read_allows_look_and_two_step_batch(self):
        with self.cancel_transport(latency=1.5) as reads:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(st.cmd_look(types.SimpleNamespace(display=35, note="")), 0)
            self.assertIn("last_shot", st.load_state())
            rc, out, rows = self.run_two_step_batch()
        self.assertEqual((rc, out["stop_reason_code"]), (0, None))
        self.assertEqual(self.events.count("dispatch"), 2)
        self.assertEqual([r["dispatch_status"] for r in rows], ["command_completed"] * 2)
        self.assertGreaterEqual(len(reads), 4)
        self.assertEqual(set(reads), {10})

    def test_batch_cancel_read_failure_refuses_without_dispatch(self):
        initial = st.load_state()
        for failure in ("timeout", "transport", "nonzero", "forged78"):
            for step in (1, 2):
                with self.subTest(failure=failure, step=step):
                    st.save_state(initial)
                    self.events.clear()
                    with self.cancel_transport(failure, step=step):
                        rc, out, rows = self.run_two_step_batch()
                    self.assertEqual((rc, out["stopped_at"], out["stop_reason_code"]), (2, step, "BATCH_CANCELLED"))
                    self.assertEqual(len(rows), step)
                    self.assertEqual(self.events.count("dispatch"), step - 1)
                    self.assertEqual((rows[-1]["allowed"], rows[-1]["dispatch_status"]), (False, "not_submitted"))
                    self.assertIn("无法读取取消标志", rows[-1]["reason"])
                    self.assertIn("动作未下发", rows[-1]["reason"])
                    self.assertIn("consumed_at", st.load_state()["last_shot"])

    def test_raising_batch_cancel_callback_is_a_predispatch_refusal(self):
        def failed():
            raise st.ActionDispatchError("adb_timeout")
        self.assertEqual(self.action(cancel_check=failed), 2)
        self.assertNotIn("dispatch", self.events)
        self.assertEqual(self.audits[-1]["reason_code"], "BATCH_CANCELLED")
        self.assertEqual((self.audits[-1]["allowed"], self.audits[-1]["dispatch_status"]), (False, "not_submitted"))
        self.assertIn("consumed_at", st.load_state()["last_shot"])

    def test_final_cancel_read_failure_invalidates_look_credential(self):
        initial = st.load_state()
        for failure in ("timeout", "transport", "nonzero", "forged78"):
            with self.subTest(failure=failure):
                st.save_state(initial)
                out = io.StringIO()
                with self.cancel_transport(failure), contextlib.redirect_stdout(out):
                    self.assertEqual(st.cmd_look(types.SimpleNamespace(display=35, note="")), 3)
                result = json.loads(out.getvalue())
                self.assertEqual(result["ocr"]["status"], "cancelled")
                self.assertIn("无法读取取消标志", result["ocr"]["reason"])
                self.assertNotIn("last_shot", st.load_state())

    def test_ocr_cancel_read_failure_returns_cancelled_without_credential(self):
        def recognize(*args, cancel_check, **kwargs):
            cancel_check.next_check = 0
            self.assertTrue(cancel_check())
            return {"status": "cancelled", "rows": []}
        out = io.StringIO()
        with self.cancel_transport("timeout"), patch.object(st, "OCR_OK", True), \
                patch.object(st, "pixel_recognize_result", side_effect=recognize) as ocr, \
                contextlib.redirect_stdout(out):
            self.assertEqual(st.cmd_look(types.SimpleNamespace(display=35, note="", ocr=True)), 3)
        ocr.assert_called_once()
        result = json.loads(out.getvalue())
        self.assertEqual((result["error"], result["ocr"]["status"]), ("cancelled", "cancelled"))
        self.assertIn("无法读取取消标志", result["ocr"]["reason"])
        self.assertNotIn("last_shot", st.load_state())

    def test_cancel_read_authenticated_policy_preserves_not_submitted(self):
        with self.cancel_transport("policy78", step=1):
            rc, out, rows = self.run_two_step_batch()
        self.assertEqual((rc, out["stopped_at"]), (2, 1))
        self.assertEqual((rows[-1]["dispatch_status"], rows[-1]["returncode"]), ("not_submitted", 78))
        self.assertNotEqual(rows[-1].get("reason_code"), "BATCH_CANCELLED")
        self.assertNotIn("dispatch", self.events)

    def test_cancel_read_policy_during_ocr_is_not_swallowed(self):
        def recognize(*args, cancel_check, **kwargs):
            cancel_check.next_check = 0
            cancel_check()
            self.fail("authenticated policy pause must propagate")
        out = io.StringIO()
        with self.cancel_transport("policy78"), patch.object(st, "OCR_OK", True), \
                patch.object(st, "pixel_recognize_result", side_effect=recognize), \
                contextlib.redirect_stdout(out):
            self.assertEqual(st.cmd_look(types.SimpleNamespace(display=35, note="", ocr=True)), 3)
        self.assertIn("ADB policy paused", json.loads(out.getvalue())["error"])
        self.assertNotIn("last_shot", st.load_state())

    def test_final_cancel_read_policy_is_not_reclassified_as_cancellation(self):
        out = io.StringIO()
        with self.cancel_transport("policy78"), contextlib.redirect_stdout(out):
            self.assertEqual(st.cmd_look(types.SimpleNamespace(display=35, note="")), 3)
        self.assertIn("ADB policy paused", json.loads(out.getvalue())["error"])
        self.assertNotIn("last_shot", st.load_state())

    def test_main_tap_reuses_policy_foreground_and_captures_before_fg(self):
        self.display_id = 0
        self.dump = self.dump.replace("home/.Main", "test/.Page")
        state = st.load_state()
        state["last_shot"]["display_id"] = 0
        st.save_state(state)
        self.assertEqual(self.action(), 0)
        self.assertLessEqual(len(self.calls), 8, self.calls)
        self.assertEqual(self.events.count("policy_foreground"), 1)
        start = self.events.index("image")
        self.assertEqual(self.events[start:start + 7],
                         ["image", "foreground", "cancel", "dispatch", "settle", "settle", "image"])

    def test_bad_r3_observation_never_dispatches(self):
        for bad in (b"", b"DSB_JSON 999999\n{}", observation(nonce="wrong"),
                    lambda n: observation(nonce=n, did=0),
                    lambda n: observation(nonce=n, jpgBytes=0),
                    lambda n: observation(nonce=n)[:-1],
                    subprocess.TimeoutExpired("synthetic", 4)):
            with self.subTest(bad=repr(bad)[:60]):
                self.bad_observation = bad
                self.assertEqual(self.action(), 2)
                self.assertNotIn("dispatch", self.events)
                self.assertNotIn("consumed_at", st.load_state()["last_shot"])

    def test_post_frame_foreground_change_refuses_without_consumption(self):
        self.frame_dump = self.dump.replace("test/.Page", "other/.Page")
        self.assertEqual(self.action(), 2)
        self.assertNotIn("dispatch", self.events)
        self.assertNotIn("consumed_at", st.load_state()["last_shot"])

    def test_ttl_is_rechecked_after_r3_before_consumption(self):
        check = st.check_gate
        def expire_after_gate(*args, **kwargs):
            ok, why, last = check(*args, **kwargs)
            self.assertTrue(ok, why)
            last["ts"] = time.time() - 91
            return ok, why, last
        with patch.object(st, "check_gate", expire_after_gate):
            self.assertEqual(self.action(), 2)
        self.assertIn("image", self.events)
        self.assertNotIn("dispatch", self.events)
        self.assertNotIn("consumed_at", st.load_state()["last_shot"])

    def test_insets_cache_expires_with_session(self):
        for sid, expected in (("test-35", 1), ("test-35", 1), ("changed", 2)):
            self.sess["sid"] = sid
            with patch.object(st, "sh", return_value="Display: mDisplayId=35\n") as shell:
                st._display_gesture_insets(35, 1600, 720)
                self.events.extend(["insets"] * shell.call_count)
            self.assertEqual(self.events.count("insets"), expected)
        self.sess["state"] = "stopped"
        st._insets_session_key(35)
        self.assertEqual(st._VD_INSETS, {})

    def test_failed_snapshot_is_judged_after_credential_checks(self):
        with patch.object(st.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, b"", b"failure")):
            self.assertEqual(self.action(), 2)
            self.assertNotIn("consumed_at", st.load_state()["last_shot"])
            st.save_state({})
            self.assertEqual(self.action(), 2)

    def test_snapshot_dumpsys_diagnostic_keeps_binding_refusal(self):
        self.dump += "\nDUMP TIMEOUT Error dumping service\n"
        self.assertEqual(self.action(), 2)
        self.assertNotIn("dispatch", self.events)
        self.assertNotIn("consumed_at", st.load_state()["last_shot"])

    def test_cancel_while_waiting_for_lock_never_captures_or_issues_credential(self):
        @contextlib.contextmanager
        def lock():
            self.abort = "ABORT %d\n" % int(time.time() * 1000)
            yield
        with patch.object(st, "lock", lock), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(st.cmd_look(types.SimpleNamespace(display=35, note="")), 3)
        self.assertNotIn("image", self.events)
        self.assertNotIn("last_shot", st.load_state())

    def test_post_dispatch_observation_errors_preserve_completed_audit(self):
        original = st.load_state()
        failures = (st.ActionDispatchError("paused", "not_submitted", 78),
                    st.EndpointUnavailable("rotated"), FileNotFoundError("gone"))
        for stage in ("settle", "grab", "fast"):
            for error in failures:
                with self.subTest(stage=stage, error=type(error).__name__):
                    st.save_state(json.loads(json.dumps(original)))
                    self.audits.clear()
                    dispatched = [False]
                    dispatch = st._dispatch_display_gesture
                    observe = st.observe
                    grab = st.grab
                    def completed(*args, dispatch=dispatch, dispatched=dispatched, **kwargs):
                        result = dispatch(*args, **kwargs)
                        dispatched[0] = True
                        return result
                    def read(*args, dispatched=dispatched, stage=stage, error=error, observe=observe, **kwargs):
                        if dispatched[0] and stage == "fast" and not kwargs.get("force"):
                            raise error
                        return observe(*args, **kwargs)
                    def capture(*args, dispatched=dispatched, stage=stage, error=error, grab=grab, **kwargs):
                        if dispatched[0] and stage == "grab":
                            raise error
                        return grab(*args, **kwargs)
                    with contextlib.ExitStack() as stack:
                        stack.enter_context(patch.object(st, "_dispatch_display_gesture", completed))
                        stack.enter_context(patch.object(st, "observe", read))
                        stack.enter_context(patch.object(st, "grab", capture))
                        if stage == "settle":
                            stack.enter_context(patch.object(st, "_settle_stable", side_effect=error))
                        self.assertEqual(self.action(fast=stage == "fast"), 0)
                    self.assertEqual(len(self.audits), 1)
                    self.assertEqual(self.audits[0]["dispatch_status"], "command_completed")
                    self.assertEqual(st.load_state()["taps"][-1]["outcome"], "command_completed")
                    self.assertIn("settled", self.audits[0])
                    if stage == "settle":
                        self.assertFalse(self.audits[0]["settled"])
                    else:
                        self.assertEqual(self.audits[0]["shot_after"], "")

    def test_look_snapshot_policy_pause_is_structured_and_invalidates_credential(self):
        def denied(argv, **kwargs):
            nonce = kwargs["env"]["DSB_ADB_RECEIPT_NONCE"]
            return subprocess.CompletedProcess(argv, 78, b"", ("ADB_POLICY_PAUSED: test\nADB_POLICY_RECEIPT:" + nonce).encode())
        output = io.StringIO()
        with patch.object(st.subprocess, "run", denied), contextlib.redirect_stdout(output):
            self.assertEqual(st.cmd_look(types.SimpleNamespace(display=35, note="")), 3)
        self.assertIn("ADB", json.loads(output.getvalue())["error"])
        self.assertNotIn("last_shot", st.load_state())

    def test_image_write_failure_before_and_after_dispatch(self):
        original = st.load_state()
        real_open = open
        def failing_open(path, mode="r", *args, **kwargs):
            if mode == "wb":
                raise OSError("disk full")
            return real_open(path, mode, *args, **kwargs)
        with patch("builtins.open", failing_open), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(st.cmd_look(types.SimpleNamespace(display=35, note="")), 3)
        self.assertNotIn("last_shot", st.load_state())
        for stage in ("r3-", "after-"):
            with self.subTest(stage=stage):
                st.save_state(json.loads(json.dumps(original)))
                self.audits.clear()
                def stage_open(path, mode="r", *args, stage=stage, **kwargs):
                    if mode == "wb" and stage in str(path):
                        raise OSError("disk full")
                    return real_open(path, mode, *args, **kwargs)
                with patch("builtins.open", stage_open):
                    self.assertEqual(self.action(), 3 if stage == "r3-" else 0)
                if stage == "r3-":
                    self.assertNotIn("last_shot", st.load_state())
                else:
                    self.assertEqual(len(self.audits), 1)
                    self.assertEqual(self.audits[0]["dispatch_status"], "command_completed")
                    self.assertEqual(self.audits[0]["shot_after"], "")
                    self.assertEqual(st.load_state()["taps"][-1]["outcome"], "command_completed")

    def test_cancel_after_snapshot_before_capture_never_issues_credential(self):
        self.after_snapshot = lambda: setattr(self, "abort", "ABORT %d\n" % int(time.time() * 1000))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(st.cmd_look(types.SimpleNamespace(display=35, note="")), 3)
        self.assertNotIn("image", self.events)
        self.assertNotIn("last_shot", st.load_state())

    def test_cancel_after_observe_before_save_never_issues_credential(self):
        self.after_observe = lambda: setattr(self, "abort", "ABORT %d\n" % int(time.time() * 1000))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(st.cmd_look(types.SimpleNamespace(display=35, note="")), 3)
        self.assertIn("image", self.events)
        self.assertNotIn("last_shot", st.load_state())
        self.assertLessEqual(len(self.calls), 4)

    def test_cancel_flag_older_than_look_is_ignored(self):
        self.abort = "ABORT %d\n" % (int(time.time() * 1000) - 10000)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(st.cmd_look(types.SimpleNamespace(display=35, note="")), 0)
        self.assertLessEqual(len(self.calls), 4)

    def test_malformed_cancel_flag_is_explained_and_fails_closed(self):
        self.abort = "unrecognized"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(st.cmd_look(types.SimpleNamespace(display=35, note="")), 3)
        self.assertIn("format unrecognized", json.loads(output.getvalue())["ocr"]["reason"])
        self.assertNotIn("image", self.events)
        self.assertNotIn("last_shot", st.load_state())

    def test_snapshot_binding_verdict_matches_original_path(self):
        now = time.time()
        cases = ("expired", "pid_gone", "main_changed", "foreground_changed", "overlay_changed")
        for case in cases:
            with self.subTest(case=case):
                self.query_overrides.clear()
                self.sess["expiresAt"] = now + 600
                if case == "expired":
                    self.sess["expiresAt"] = now - 1
                elif case == "pid_gone":
                    self.query_overrides["pidof"] = "999"
                elif case == "main_changed":
                    self.query_overrides["activities"] = self.dump.replace("home/.Main", "other/.Main")
                elif case == "foreground_changed":
                    self.query_overrides["activities"] = self.dump.replace("test/.Page", "other/.Page")
                else:
                    self.query_overrides["overlay"] = "720x1600/320"
                args = types.SimpleNamespace(display=35, shot="one")
                with patch.object(vd, "_revive_on_display", return_value=""), \
                        patch.object(st.time, "time", return_value=now):
                    snapshot = st._verification_snapshot(35)
                    old = st.check_gate(args, "tap", [(360, 800)])
                    new = st.check_gate(args, "tap", [(360, 800)], snapshot=snapshot)
                self.assertEqual(new[:2], old[:2])
                self.assertEqual(new[0], case == "main_changed")

    def test_snapshot_keeps_twenty_second_timeout_and_ignores_stderr_noise(self):
        run = self.run_device
        def noisy(argv, **kwargs):
            self.assertEqual(kwargs["timeout"], 20)
            result = run(argv, **kwargs)
            result.stderr = b"warning: exception in unrelated diagnostic"
            return result
        with patch.object(st.subprocess, "run", noisy):
            snapshot = st._verification_snapshot(35)
        self.assertIsInstance(snapshot[1], dict)


class GestureTransport(unittest.TestCase):
    def dispatch(self):
        return st._dispatch_display_gesture(35, "tap", [(360, 800)], types.SimpleNamespace())

    def test_transport_failure_without_submitted_marker_is_unknown(self):
        failures = [subprocess.CompletedProcess([], 1, b"", b"failure"),
                    subprocess.CompletedProcess([], 0, b"", b""),
                    subprocess.TimeoutExpired("synthetic", 6), OSError("transport")]
        for failure in failures:
            with self.subTest(failure=failure), patch.object(st.subprocess, "run") as run:
                if isinstance(failure, Exception):
                    run.side_effect = failure
                else:
                    run.return_value = failure
                with self.assertRaises(st.ActionDispatchError) as caught:
                    self.dispatch()
                self.assertEqual(caught.exception.status, "unknown")
                self.assertEqual(run.call_count, 1)

    def test_cancelled_rejected_and_timeout_receipts(self):
        for status in ("cancelled", "rejected", "timeout"):
            rec = dict(nonce="100000", status=status, displayId=35)
            raw = b"DSB_SUBMITTED\n" + frame("RECEIPT", json.dumps(rec).encode())
            with patch.object(st.time, "time", return_value=100), \
                    patch.object(st.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, raw, b"")):
                with self.assertRaises(st.ActionDispatchError) as caught:
                    self.dispatch()
                self.assertEqual(caught.exception.reason, "gesture_" + status)
                self.assertEqual(caught.exception.status, status)

    def test_policy_receipt_and_missing_executable_are_not_submitted(self):
        def denied(argv, **kwargs):
            nonce = kwargs["env"]["DSB_ADB_RECEIPT_NONCE"]
            return subprocess.CompletedProcess(argv, 78, b"", ("ADB_POLICY_DENIED: test\nADB_POLICY_RECEIPT:" + nonce).encode())
        for outcome in (denied, FileNotFoundError("missing")):
            with patch.object(st.subprocess, "run", side_effect=outcome):
                with self.assertRaises(st.ActionDispatchError) as caught:
                    self.dispatch()
                self.assertEqual(caught.exception.status, "not_submitted")

    def test_timeout_after_submitted_marker_is_receipt_timeout(self):
        with patch.object(st.subprocess, "run", side_effect=subprocess.TimeoutExpired(
                "synthetic", 15, output=b"DSB_SUBMITTED\n")):
            with self.assertRaises(st.ActionDispatchError) as caught:
                self.dispatch()
            self.assertEqual((caught.exception.reason, caught.exception.status), ("gesture_timeout", "timeout"))

    def test_forged_policy_receipt_is_not_not_submitted(self):
        with patch.object(st.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 78, b"", b"ADB_POLICY_DENIED: fake\nADB_POLICY_RECEIPT:wrong\n")):
            with self.assertRaises(st.ActionDispatchError) as caught:
                self.dispatch()
            self.assertEqual(caught.exception.status, "unknown")

    def test_shell_values_reject_non_integer_inputs_without_subprocess(self):
        with patch.object(st.subprocess, "run") as run:
            for value in ("35; touch /tmp/no", "35", 35.0, True, None):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    st.observe(display_id=value)
                with self.subTest(coordinate=value), self.assertRaises(ValueError):
                    st._dispatch_display_gesture(35, "tap", [(value, 800)], types.SimpleNamespace())
            run.assert_not_called()

    def test_snapshot_package_is_data_not_shell_syntax(self):
        package = 'test; $(printf INJECTED) "quoted"\n'
        query = vd.verification_queries({"package": package}, integer_literals=True)[-1][1]
        self.assertNotIn(package, query)
        result = REAL_RUN(["/bin/sh", "-c", 'pidof() { printf "%s" "$1"; }; ' + query],
                          capture_output=True, timeout=2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.decode(), package)


class ShellProtocol(unittest.TestCase):
    """Execute the generated shell locally with fake am/dumpsys, never ADB."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="dsb-t18-", dir="/tmp")
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.remote_tmp = os.path.join(self.root, "remote-tmp")
        os.mkdir(self.remote_tmp)
        self.am_log = os.path.join(self.root, "am.log")
        self.image = bytes(range(256)) * 5 + b"\x00\nDSB_JSON 3\nerror\xff"
        self.data = dict(nonce="100000", displayId=35, image="look-9.jpg", jpg="look-9.jpg",
                         jpgBytes=len(self.image), nodesRaw="[]")
        with open(os.path.join(self.root, "look-9.jpg"), "wb") as f:
            f.write(self.image)
        uptime = os.path.join(self.root, "uptime")
        with open(uptime, "w") as f:
            f.write("100.08 0.00\n")
        self.calls = []
        self.adb_mode = "quoted"
        self.device_shell = "/bin/bash"
        self.shell_results = []
        self.external_commands = False
        self.foreground_file = os.path.join(self.root, "foreground.txt")
        with open(self.foreground_file, "w") as f:
            f.write("Display #35 (activities from top to bottom):\n")
        bindir = os.path.join(self.root, "bin")
        os.mkdir(bindir)
        fixtures = {
            "am": '#!/bin/sh\nprintf "am\\n" >> "$DSB_TEST_AM_LOG"\n',
            "dumpsys": '#!/bin/sh\ncat "$DSB_TEST_FG_FILE"\n',
        }
        for name, source in fixtures.items():
            path = os.path.join(bindir, name)
            with open(path, "w") as f:
                f.write(source)
            os.chmod(path, 0o700)
        self.shell_env = dict(os.environ, PATH=bindir + os.pathsep + os.environ["PATH"],
                              DSB_TEST_AM_LOG=self.am_log, DSB_TEST_FG_FILE=self.foreground_file)
        for target, name, value in ((st, "LOOK_DIR", self.root), (st.time, "time", lambda: 100),
                                    (st, "LOOK_JSON", os.path.join(self.root, "look.json")),
                                    (st._LookCancellation, "flag", os.path.join(self.root, "abort.flag")),
                                    (st, "_CLOCK_SH", st._CLOCK_SH.replace("/proc/uptime", uptime)),
                                    (st, "_FILES_SH", st._FILES_SH.replace("/data/local/tmp", self.remote_tmp)),
                                    (st, "adb_command", lambda: ["synthetic-adb"]),
                                    (st.subprocess, "run", self.shell)):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def write_json(self, name, value):
        with open(os.path.join(self.root, name), "w") as f:
            json.dump(value, f)

    def shell(self, argv, **kwargs):
        self.calls.append(argv)
        self.assertEqual(argv[0], "synthetic-adb")
        self.assertIn(argv[1], ("shell", "exec-out"))
        args = argv[2:]
        # A sole argument is already a complete command (the owner's device
        # reproduction). Multi-argument transport differs between ADB versions.
        if len(args) == 1:
            command = args[0]
        elif self.adb_mode == "quoted":
            command = " ".join(shlex.quote(arg) for arg in args)
        else:
            command = " ".join(args)
        if self.external_commands:
            command = "enable -n printf echo; " + command
        # This is a host-process watchdog, not the emulated device clock budget.
        result = REAL_RUN([self.device_shell, "-c", command], env=self.shell_env,
                          capture_output=True, timeout=30)
        self.shell_results.append(result)
        self.assertEqual(os.listdir(self.remote_tmp), [], "device-script temporary directory leaked")
        return result

    def use_external_output_commands(self):
        self.device_shell = "/bin/bash"
        self.external_commands = True
        for name, executable in (("printf", "/usr/bin/printf"), ("echo", "/bin/echo")):
            path = os.path.join(self.root, "bin", name)
            with open(path, "w") as f:
                f.write('#!/bin/bash\nexport LC_ALL=C\nfor arg do\n'
                        '  if [ "${#arg}" -gt 131072 ]; then\n'
                        '    printf "Argument list too long\\n" >&2; exit 126\n'
                        '  fi\ndone\n'
                        'printf "external\\n" >> "$DSB_TEST_EXTERNAL_LOG"\n'
                        'exec ' + executable + ' "$@"\n')
            os.chmod(path, 0o700)
        self.external_log = os.path.join(self.root, "external.log")
        self.shell_env["DSB_TEST_EXTERNAL_LOG"] = self.external_log

    def test_external_printf_and_echo_fixture_reject_oversized_arguments(self):
        self.use_external_output_commands()
        payload = os.path.join(self.root, "oversized.txt")
        with open(payload, "w") as f:
            f.write("x" * 131073)
        for name in ("printf", "echo"):
            with self.subTest(command=name):
                command = 'v=$(cat ' + shlex.quote(payload) + '); ' + name + ' "%s" "$v"'
                result = self.shell(["synthetic-adb", "exec-out", command])
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"argument list too long", result.stderr.lower())
        result = self.shell(["synthetic-adb", "exec-out", "printf '%s' small; echo small"])
        self.assertEqual((result.returncode, result.stdout), (0, b"smallsmall\n"))
        with open(self.external_log) as f:
            self.assertEqual(len(f.readlines()), 2)

    def test_large_observation_and_foreground_use_files_in_both_adb_modes(self):
        self.use_external_output_commands()
        self.image = bytes(range(256)) * 3200
        with open(os.path.join(self.root, "look-9.jpg"), "wb") as f:
            f.write(self.image)
        foreground = "Display #35 (activities from top to bottom):\n" + "f" * (500 * 1024)
        with open(self.foreground_file, "w") as f:
            f.write(foreground)
        nodes = [dict(text="x" * (300 * 1024), rect=[0, 0, 100, 100])]
        self.data.update(jpgBytes=len(self.image), nodesRaw=json.dumps(nodes))
        for mode in ("quoted", "joined"):
            self.adb_mode = mode
            for did in (0, 35):
                with self.subTest(mode=mode, display=did):
                    self.data["displayId"] = did
                    self.write_json("look-100000.json", self.data)
                    fg = {}
                    result = st.observe(display_id=did, foreground_out=fg)
                    self.assertIsNotNone(result)
                    self.assertEqual(result["nodes"], nodes)
                    self.assertEqual(result["_jpg_bytes"], self.image)
                    self.assertEqual(fg["dump"], foreground)
                    self.assertNotIn(b"Argument list too long", self.shell_results[-1].stderr)

    def test_large_shared_tree_and_receipt_use_files_in_both_adb_modes(self):
        self.use_external_output_commands()
        nodes = [dict(text="x" * (300 * 1024))]
        self.write_json("look.json", dict(nonce="100000:f", nodesRaw=json.dumps(nodes)))
        for mode in ("quoted", "joined"):
            self.adb_mode = mode
            with self.subTest(mode=mode, operation="shared tree"):
                result = st.observe(with_image=False, force=True)
                self.assertIsNotNone(result)
                self.assertEqual(result["nodes"], nodes)
            for status in ("completed", "cancelled", "rejected"):
                self.write_json("gesture-100000.json", dict(nonce="100000", displayId=35,
                                kind="tap", status=status, padding="r" * (300 * 1024)))
                with self.subTest(mode=mode, status=status):
                    if status == "completed":
                        result = st._dispatch_display_gesture(35, "tap", [(360, 800)], types.SimpleNamespace())
                        self.assertEqual(result["status"], status)
                    else:
                        with self.assertRaises(st.ActionDispatchError) as caught:
                            st._dispatch_display_gesture(35, "tap", [(360, 800)], types.SimpleNamespace())
                        self.assertEqual(caught.exception.status, status)

    def test_generated_scripts_do_not_pass_payload_variables_as_arguments(self):
        for script in (self.generated_script(cancel=st._LookCancellation(), foreground=True), self.generated_script(gesture=True),
                       st._LookCancellation().before_capture_shell(), st._LookCancellation().after_capture_shell()):
            self.assertNotRegex(script, r'"\$(?:j|fg|ab)"')
            self.assertNotRegex(script, r'\b(?:j|fg|ab)=\$\(')

    def test_large_abort_flags_stream_and_preserve_python_cancellation(self):
        self.use_external_output_commands()
        self.write_json("look-100000.json", self.data)
        flags = ("ABORT " + "0" * (300 * 1024) + "99999 \t\n",
                 "ABORT 1" + "0" * (300 * 1024) + "\n",
                 "ABORT 99999\n" + "x" * (300 * 1024), "ABORT 99999\x00\n")
        for mode in ("quoted", "joined"):
            self.adb_mode = mode
            for index, flag in enumerate(flags):
                with self.subTest(mode=mode, flag=index):
                    if os.path.exists(self.am_log):
                        os.remove(self.am_log)
                    with open(st._LookCancellation.flag, "w") as f:
                        f.write(flag)
                    expected = st._LookCancellation().feed(flag, 0)
                    cancel = st._LookCancellation()
                    result = st.observe(display_id=35, cancel_check=cancel)
                    self.assertEqual(cancel.stopped, expected)
                    self.assertEqual(result is None, expected)
                    self.assertEqual(os.path.exists(self.am_log), not expected)

    def test_large_post_capture_abort_frame_is_not_truncated(self):
        self.use_external_output_commands()
        self.write_json("look-100000.json", self.data)
        next_flag = os.path.join(self.root, "next-abort")
        flag = "ABORT " + "0" * (300 * 1024) + "100000\n"
        with open(next_flag, "w") as f:
            f.write(flag)
        with open(os.path.join(self.root, "bin", "am"), "w") as f:
            f.write('#!/bin/sh\ncp "$DSB_NEXT_ABORT" "$DSB_ABORT_FILE"\n')
        self.shell_env.update(DSB_NEXT_ABORT=next_flag, DSB_ABORT_FILE=st._LookCancellation.flag)
        for mode in ("quoted", "joined"):
            with self.subTest(mode=mode):
                self.adb_mode = mode
                with open(st._LookCancellation.flag, "w") as f:
                    f.write("ABORT 99999\n")
                cancel = st._LookCancellation()
                self.assertIsNotNone(st.observe(display_id=35, cancel_check=cancel))
                self.assertTrue(cancel.stopped)
                self.assertTrue(self.shell_results[-1].stdout.endswith(flag.encode()))

    def test_shared_tree_snapshot_survives_source_replacement(self):
        self.use_external_output_commands()
        replacement = os.path.join(self.root, "replacement.json")
        self.write_json("replacement.json", dict(nonce="another", nodes=[]))
        copier = os.path.join(self.root, "bin", "cp")
        with open(copier, "w") as f:
            f.write('#!/bin/sh\n/bin/cp "$@" || exit $?\n'
                    'if [ "$1" = "$DSB_SHARED_JSON" ]; then\n'
                    '  /bin/cp "$DSB_REPLACEMENT" "$DSB_SHARED_JSON"\nfi\n')
        os.chmod(copier, 0o700)
        self.shell_env.update(DSB_SHARED_JSON=st.LOOK_JSON, DSB_REPLACEMENT=replacement)
        nodes = [dict(text="n" * (300 * 1024))]
        for mode in ("quoted", "joined"):
            with self.subTest(mode=mode):
                self.adb_mode = mode
                self.write_json("look.json", dict(nonce="100000", nodesRaw=json.dumps(nodes)))
                result = st.observe(with_image=False)
                self.assertIsNotNone(result)
                self.assertEqual(result["nodes"], nodes)
                with open(st.LOOK_JSON) as f:
                    self.assertEqual(json.load(f)["nonce"], "another")

    def test_temporary_directory_mode_and_cleanup_on_failure_exit_and_signal(self):
        command = st._FILES_SH + 'ls -ld "$tmp"; exit 7'
        result = self.shell(["synthetic-adb", "exec-out", command])
        self.assertEqual(result.returncode, 7)
        self.assertTrue(result.stdout.startswith(b"drwx------"), result.stdout)
        result = self.shell(["synthetic-adb", "exec-out", st._FILES_SH + 'kill -TERM $$'])
        self.assertEqual(result.returncode, 143)
        self.assertIsNone(st.observe(display_id=35, timeout=0))
        self.write_json("look-100000.json", self.data)
        with open(os.path.join(self.root, "bin", "dumpsys"), "w") as f:
            f.write("#!/bin/sh\nexit 7\n")
        self.assertIsNone(st.observe(display_id=35, foreground_out={}))
        self.assertEqual(self.shell_results[-1].returncode, 7)

    def test_temporary_directory_creation_failure_never_broadcasts(self):
        path = os.path.join(self.root, "bin", "mktemp")
        with open(path, "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(path, 0o700)
        self.assertIsNone(st.observe(display_id=35))
        self.assertFalse(os.path.exists(self.am_log))
        with self.assertRaises(st.ActionDispatchError) as caught:
            st._dispatch_display_gesture(35, "tap", [(360, 800)], types.SimpleNamespace())
        self.assertEqual(caught.exception.status, "unknown")
        self.assertFalse(os.path.exists(self.am_log))

    def test_oversized_jpeg_name_is_rejected_before_becoming_an_argument(self):
        self.use_external_output_commands()
        self.data["jpg"] = "look-" + "9" * (300 * 1024) + ".jpg"
        self.write_json("look-100000.json", self.data)
        self.assertIsNone(st.observe(display_id=35))
        self.assertEqual(self.shell_results[-1].returncode, 65)
        self.assertNotIn(b"Argument list too long", self.shell_results[-1].stderr)

    def test_observe_and_gesture_through_both_adb_argument_modes(self):
        self.device_shell = "/bin/sh"
        self.write_json("look-100000.json", self.data)
        for mode in ("quoted", "joined"):
            self.adb_mode = mode
            with self.subTest(mode=mode, operation="observe"):
                fg = {}
                result = st.observe(display_id=35, foreground_out=fg)
                self.assertIsNotNone(result)
                self.assertEqual(result["_jpg_bytes"], self.image)
                self.assertIn("Display #35", fg["dump"])
            for status in ("completed", "cancelled", "rejected"):
                self.write_json("gesture-100000.json",
                                dict(nonce="100000", displayId=35, kind="tap", status=status))
                with self.subTest(mode=mode, operation="gesture", status=status):
                    if status == "completed":
                        result = st._dispatch_display_gesture(35, "tap", [(360, 800)],
                                                              types.SimpleNamespace())
                        self.assertEqual(result["status"], status)
                    else:
                        with self.assertRaises(st.ActionDispatchError) as caught:
                            st._dispatch_display_gesture(35, "tap", [(360, 800)], types.SimpleNamespace())
                        self.assertEqual(caught.exception.status, status)

    def test_prequoted_sh_c_fails_on_quoting_adb_without_broadcast(self):
        self.device_shell = "/bin/sh"
        real_sh = st._sh
        def old_call(*args, **kwargs):
            self.assertEqual(len(args), 2)
            return real_sh(args[0], "sh", "-c", shlex.quote(args[1]), **kwargs)
        with patch.object(st, "_sh", side_effect=old_call):
            self.assertIsNone(st.observe(display_id=35))
            with self.assertRaises(st.ActionDispatchError) as caught:
                st._dispatch_display_gesture(35, "tap", [(360, 800)], types.SimpleNamespace())
            self.assertEqual(caught.exception.status, "unknown")
        self.assertFalse(os.path.exists(self.am_log))
        self.assertEqual(len(self.shell_results), 2)
        for result in self.shell_results:
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"now_ms()", result.stderr)

    def test_fake_adb_quotes_multi_arguments_but_preserves_single_command(self):
        script = 'a=1; echo one "$a"; echo two'
        for service in ("shell", "exec-out"):
            for mode in ("quoted", "joined"):
                self.adb_mode = mode
                with self.subTest(service=service, mode=mode):
                    result = self.shell(["synthetic-adb", service, script])
                    self.assertEqual((result.returncode, result.stdout), (0, b"one 1\ntwo\n"))
            self.adb_mode = "quoted"
            result = self.shell(["synthetic-adb", service, "sh", "-c", script])
            self.assertEqual((result.returncode, result.stdout), (0, b"one 1\ntwo\n"))

    def test_cancel_read_missing_flag_is_empty_but_failed_cat_cancels(self):
        self.assertFalse(st._LookCancellation().read_now())
        with open(st._LookCancellation.flag, "w") as f:
            f.write("ABORT 99999\n")
        path = os.path.join(self.root, "bin", "cat")
        with open(path, "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(path, 0o700)
        cancel = st._LookCancellation()
        self.assertTrue(cancel.read_now())
        self.assertTrue(cancel.stopped)
        self.assertIn("无法读取取消标志", cancel.reason)
        calls = len(self.calls)
        self.assertTrue(cancel())
        self.assertEqual(len(self.calls), calls)

    def test_binary_bytes_and_post_capture_foreground_round_trip(self):
        self.write_json("look-100000.json", self.data)
        fg = {}
        result = st.observe(display_id=35, foreground_out=fg)
        self.assertIsNotNone(result)
        self.assertEqual(result["_jpg_bytes"], self.image)
        self.assertIn("Display #35", fg["dump"])
        self.assertEqual(len(self.calls), 1)

    def test_invalid_jpeg_name_does_not_read_a_file(self):
        self.data["jpg"] = "../look-9.jpg"
        self.write_json("look-100000.json", self.data)
        self.assertIsNone(st.observe(display_id=35))

    def test_gesture_receipt_script_completed_and_cancelled(self):
        for status in ("completed", "cancelled", "rejected"):
            self.write_json("gesture-100000.json", dict(nonce="100000", displayId=35, kind="tap", status=status))
            if status == "completed":
                self.assertEqual(st._dispatch_display_gesture(35, "tap", [(360, 800)],
                                 types.SimpleNamespace())["status"], status)
            else:
                with self.assertRaises(st.ActionDispatchError) as caught:
                    st._dispatch_display_gesture(35, "tap", [(360, 800)], types.SimpleNamespace())
                self.assertEqual(caught.exception.status, status)

    def test_main_tree_force_nonce_matches_existing_app_protocol(self):
        self.write_json("look.json", dict(nonce="100000:f", nodes=[], treeHash="same"))
        self.assertIsNotNone(st.observe(with_image=False, force=True))

    def test_shell_cancellation_prevents_broadcast_for_new_or_malformed_flag(self):
        self.write_json("look-100000.json", self.data)
        for flag, cancelled in (("ABORT 99999\n", False), ("ABORT 100000\n", True),
                                ("garbage", True), ("\nABORT 99999\n", True),
                                ("ABORT 99\n999\n", True), ("ABORT 99999 \t\n", False)):
            with self.subTest(flag=flag):
                if os.path.exists(self.am_log):
                    os.remove(self.am_log)
                with open(st._LookCancellation.flag, "w") as f:
                    f.write(flag)
                cancel = st._LookCancellation()
                result = st.observe(display_id=35, cancel_check=cancel)
                self.assertEqual(cancel.stopped, cancelled)
                self.assertEqual(result is None, cancelled)
                self.assertEqual(os.path.exists(self.am_log), not cancelled)

    def generated_script(self, gesture=False, cancel=None, foreground=False):
        with patch.object(st, "_sh", return_value=b"") as run:
            if gesture:
                with self.assertRaises(st.ActionDispatchError):
                    st._dispatch_display_gesture(35, "tap", [(360, 800)], types.SimpleNamespace())
            else:
                st.observe(display_id=35, timeout=.3, cancel_check=cancel,
                           foreground_out={} if foreground else None)
        self.assertEqual(len(run.call_args.args), 2)
        return run.call_args.args[1]

    def test_device_scripts_compare_elapsed_time_not_absolute_clock(self):
        for gesture in (False, True):
            script = self.generated_script(gesture)
            with self.subTest(gesture=gesture):
                self.assertNotIn("deadline=", script)
                self.assertIn("t0=$(now_ms)", script)
                self.assertIn("$(( $(now_ms) - t0 )) -lt", script)
                self.assertNotRegex(script, r"\[\s*\$\(now_ms\)\s+-[lg][te]")
        script = st._LookCancellation().before_capture_shell()
        self.assertNotRegex(script, r'\[\s*"?\$epoch"?\s+-[lg][te]')
        self.assertIn("[[", script)

    def run_clock_boundary(self, shell, emulate_int32):
        # Only the test redirects /proc/uptime. Fake sleep advances the clock and
        # optionally publishes a receipt; no device or wall-clock wait is used.
        uptime = os.path.join(self.root, "uptime")
        sleeps = os.path.join(self.root, "sleeps")
        for gesture in (False, True):
            for publish in (False, True):
                with self.subTest(shell=shell, gesture=gesture, publish=publish):
                    name = "gesture-100000.json" if gesture else "look-100000.json"
                    target = os.path.join(self.root, name)
                    if os.path.exists(target):
                        os.remove(target)
                    receipt = (dict(nonce="100000", displayId=35, status="completed")
                               if gesture else self.data)
                    self.write_json("pending.json", receipt)
                    with open(uptime, "w") as f:
                        f.write("2147483.600 0.00\n")
                    with open(sleeps, "w") as f:
                        f.write("0\n")
                    script = self.generated_script(gesture)
                    if emulate_int32:
                        # Host bash arithmetic is 64-bit. Wrap every generated
                        # arithmetic expansion, including the clock and elapsed
                        # subtraction, to the signed 32-bit Android result.
                        script = re.sub(r"\$\(\((.*?)\)\)",
                                        lambda m: "$(( ((" + m[1] +
                                        ") + 2147483648 & 4294967295) - 2147483648 ))",
                                        script)
                    step = 1000 if gesture else 100
                    prelude = ("am() { :; }; seconds=2147483; millis=600; polls=0; "
                               "sleep() { millis=$((millis + " + str(step) + ")); polls=$((polls + 1)); "
                               "seconds=$((seconds + millis / 1000)); millis=$((millis % 1000)); "
                               "printf '%s.%03d 0.00\\n' $seconds $millis > "
                               + shlex.quote(uptime) + "; printf '%s\\n' $polls > "
                               + shlex.quote(sleeps) + "; ")
                    if publish:
                        prelude += ("if [ $polls -eq 1 ]; then cp "
                                    + shlex.quote(os.path.join(self.root, "pending.json"))
                                    + " " + shlex.quote(target) + "; fi; ")
                    prelude += "}; "
                    result = REAL_RUN([shell, "-c", prelude + script], capture_output=True, timeout=30)
                    with open(sleeps) as f:
                        polls = int(f.read())
                    self.assertEqual(polls, 1 if publish else (6 if gesture else 3), result.stderr)
                    self.assertEqual(result.returncode, 0 if publish or gesture else 124, result.stderr)
                    if publish:
                        marker = b"DSB_SUBMITTED\nDSB_RECEIPT " if gesture else b"DSB_JSON "
                        self.assertTrue(result.stdout.startswith(marker), result.stdout)
                    elif gesture:
                        self.assertEqual(result.stdout, b"DSB_SUBMITTED\nDSB_RECEIPT 0\n")

    def test_observation_and_gesture_wait_across_signed_32bit_clock_boundary(self):
        self.run_clock_boundary("/bin/bash", emulate_int32=True)

    def run_abort_comparison(self, shell):
        started = 1793251232579
        flags = ("", "ABORT 1790000000000\n", "ABORT 1793251232578\n",
                 "ABORT 1793251232579\n", "ABORT 1793251232580\n",
                 "ABORT 999999999999\n", "ABORT 10000000000000\n",
                 "ABORT 0001793251232578\n", "ABORT 0001793251232579\n",
                 "ABORT 0000\n", "ABORT 000999999999999\n", "ABORT 99999999999999999999\n",
                 "garbage", "ABORT \n", " ABORT 1793251232578\n", "ABORT 123 456\n",
                 "ABORT 1793251232578 \t\r\n")
        for flag in flags:
            with self.subTest(shell=shell, flag=flag):
                with open(st._LookCancellation.flag, "w") as f:
                    f.write(flag)
                cancel = st._LookCancellation()
                cancel.started_ms = started
                expected = cancel.feed(flag, 0)
                result = REAL_RUN([shell, "-c", st._FILES_SH + cancel.before_capture_shell() + "printf CAPTURE"],
                                  capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.startswith(b"DSB_CANCELLED "), expected, result.stderr)
                self.assertEqual(result.stdout == b"CAPTURE", not expected, result.stderr)

    def test_abort_decimal_comparison_matches_python_for_large_and_padded_values(self):
        self.run_abort_comparison("/bin/bash")

    @unittest.skipUnless(shutil.which("mksh"), "mksh unavailable; no installation requested")
    def test_native_mksh_clock_boundary_and_abort_comparison(self):
        self.run_clock_boundary(shutil.which("mksh"), emulate_int32=False)
        self.run_abort_comparison(shutil.which("mksh"))


if __name__ == "__main__":
    unittest.main()
