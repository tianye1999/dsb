"""Process-level credential tests. No ADB, socket observations or real images."""
import contextlib
import fcntl
import importlib.util
import io
import json
import multiprocessing as mp
import os
from pathlib import Path
import tempfile
import time
import types
import unittest

CAND = Path(os.environ.get("DSB_CANDIDATE_DIR", str(Path(__file__).resolve().parent)))


def fixture(candidate, directory, mode, entered, release, observed):
    spec = importlib.util.spec_from_file_location("subject", Path(candidate) / "safe_tap.py")
    st = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(st)
    st.HERE = directory
    st.STATE = str(Path(directory) / "state.json")
    st.SHOTS = str(Path(directory) / "shots")
    st.AUDIT = str(Path(directory) / "audit.jsonl")
    st.cfg_get = lambda key, default=None: default
    # Spawned workers need their own fixture; policy is covered by safe_tap_vd_test.py.
    st.vd_policy_block = lambda *a, **kw: None
    st._r3_verify = lambda *a, **kw: {"ok": True, "diff": 0.0, "why": ""}   # R3 由 test_safe_tap_r3.py 覆盖
    st.foreground = lambda: "test/.Page"
    st.screen_size = lambda: (1200, 2400)
    st.gesture_insets = lambda *a: {"left": 0, "right": 0, "bottom": 0}
    st._ball = lambda *a: None
    st._ocr_cancel_check = lambda: lambda: False
    st.grab = lambda *a, **kw: "/synthetic.png"
    st.save_look_jpg = lambda *a: True
    st.dump_nodes = lambda: [{"text": "Settings"}]
    st.build_zones = lambda *a: []
    st.wakefulness = lambda: "Awake"
    st._settle_stable = lambda **kw: (0, 1, True)

    def forbidden(*a, **kw):
        raise AssertionError("test attempted unmocked device/engine access")

    st.subprocess.run = forbidden
    st.pixel_recognize_result = forbidden

    def observe(**kw):
        if mode == "look" and observed is not None:
            observed.set()
        return None

    def dispatch(*a, **kw):
        with open(Path(directory) / "attempts", "a") as stream:
            stream.write(mode + "\n")
        if mode == "block":
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test release deadline exceeded")
        if mode == "error":
            raise RuntimeError("synthetic dispatch error")
        if mode == "interrupt":
            raise KeyboardInterrupt("synthetic interruption")
        return ""

    st.observe = observe
    st.sh = dispatch
    return st


def worker(candidate, directory, mode, ready, start, results, entered=None,
           release=None, observed=None):
    try:
        st = fixture(candidate, directory, mode, entered, release, observed)
        ready.put(mode)
        if not start.wait(5):
            raise RuntimeError("test start deadline exceeded")
        args = types.SimpleNamespace(shot="seed", fast=True, ack_no_zones=False,
                                     expect=None, expect_activity=None)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                rc = st.cmd_look(types.SimpleNamespace(note="synthetic")) if mode == "look" \
                    else st.do_action(args, "tap", [(600, 900)])
            except (RuntimeError, KeyboardInterrupt) as error:
                # Keep the exception traceback alive while testing lock release.
                with open(Path(directory) / ".safe_tap.lock", "a") as stream:
                    try:
                        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        unlocked = True
                    except BlockingIOError:
                        unlocked = False
                results.put({"mode": mode, "exception": type(error).__name__,
                             "unlocked": unlocked})
                return
        results.put({"mode": mode, "rc": rc})
    except BaseException as error:
        results.put({"mode": mode, "unexpected": repr(error)})


class Concurrency(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = self.tmp.name
        self.ctx = mp.get_context("spawn")
        self.procs = []
        self.ready = self.ctx.Queue()
        self.results = self.ctx.Queue()
        with open(Path(self.directory) / "state.json", "w") as stream:
            json.dump({"last_shot": {"id": "seed", "ts": time.time(), "fg": "test/.Page",
                "zones": [], "nodes": [], "zone_coverage": "clean"}}, stream)

    def tearDown(self):
        for proc in self.procs:
            if proc.is_alive():
                proc.terminate()
            proc.join(2)
            if proc.is_alive():
                proc.kill()
                proc.join(2)
        for queue in (self.ready, self.results):
            queue.close()
            queue.join_thread()
        self.tmp.cleanup()

    def launch(self, mode, start, **events):
        proc = self.ctx.Process(target=worker, args=(str(CAND), self.directory, mode,
            self.ready, start, self.results), kwargs=events)
        proc.start()
        self.procs.append(proc)
        return proc

    def result(self):
        result = self.results.get(timeout=12)
        self.assertNotIn("unexpected", result)
        return result

    def join_cleanly(self):
        for proc in self.procs:
            proc.join(3)
            self.assertFalse(proc.is_alive(), "worker did not exit")
            self.assertEqual(proc.exitcode, 0)

    def attempts(self):
        return (Path(self.directory) / "attempts").read_text().splitlines()

    def state(self):
        return json.loads((Path(self.directory) / "state.json").read_text())

    def test_two_processes_accept_only_one_request(self):
        start = self.ctx.Event()
        self.launch("tap", start)
        self.launch("tap", start)
        self.ready.get(timeout=5)
        self.ready.get(timeout=5)
        start.set()
        results = [self.result(), self.result()]
        self.join_cleanly()
        self.assertEqual(sorted(r.get("rc") for r in results), [0, 2])
        self.assertEqual(self.attempts(), ["tap"])
        self.assertIn("consumed_at", self.state()["last_shot"])

    def test_look_waits_until_action_releases_lock(self):
        start = self.ctx.Event()
        entered, release, observed = (self.ctx.Event() for _ in range(3))
        self.launch("block", start, entered=entered, release=release)
        self.ready.get(timeout=5)
        start.set()
        self.assertTrue(entered.wait(5), "action never reached dispatch")
        self.launch("look", start, observed=observed)
        self.ready.get(timeout=5)
        self.assertFalse(observed.wait(.3), "look observed while action held lock")
        release.set()
        results = [self.result(), self.result()]
        self.join_cleanly()
        self.assertEqual({r["mode"]: r.get("rc") for r in results}, {"block": 0, "look": 0})
        self.assertTrue(observed.is_set())
        self.assertEqual(self.attempts(), ["block"])
        self.assertNotIn("consumed_at", self.state()["last_shot"])
        self.assertGreater(self.state()["last_shot"]["ts"], self.state()["last_action_ts"])

    def check_exception(self, mode, exception):
        start = self.ctx.Event()
        self.launch(mode, start)
        self.ready.get(timeout=5)
        start.set()
        result = self.result()
        self.join_cleanly()
        self.assertEqual(result.get("exception"), exception)
        self.assertTrue(result.get("unlocked"), "traceback retained the file lock")
        self.assertIn("consumed_at", self.state()["last_shot"])
        self.assertEqual(self.attempts(), [mode])

    def test_exception_releases_lock_and_preserves_consumption(self):
        self.check_exception("error", "RuntimeError")

    def test_interrupt_releases_lock_and_preserves_consumption(self):
        self.check_exception("interrupt", "KeyboardInterrupt")


if __name__ == "__main__":
    unittest.main(verbosity=2)
