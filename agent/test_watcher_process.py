"""Real, isolated synthetic processes only; no device or real subscription access."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import watcher as w


def wait_file(path):
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not path.exists():
        raise AssertionError("synthetic child did not start")


class Processes(unittest.TestCase):
    def assert_not_running(self, pid):
        result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                                capture_output=True, text=True, timeout=2)
        self.assertTrue(result.returncode == 1 or result.stdout.strip().startswith("Z"),
                        (pid, result.stdout, result.stderr))

    def test_normal_child(self):
        rc, out, err, status, cleaned = w.run_child(
            [sys.executable, "-c", "print('synthetic')"], dict(os.environ), 2)
        self.assertEqual((rc, out.strip(), status, cleaned), (0, "synthetic", "completed", True))

    def test_nonzero_not_replayed(self):
        with tempfile.TemporaryDirectory() as tmp:
            counter = Path(tmp) / "count"
            code = "from pathlib import Path; p=Path(%r); p.write_text(p.read_text()+'x' if p.exists() else 'x'); raise SystemExit(3)" % str(counter)
            result = w.run_child([sys.executable, "-c", code], dict(os.environ), 2)
            self.assertEqual(result[0], 3)
            self.assertEqual(counter.read_text(), "x")

    def test_timeout_reaps_child(self):
        result = w.run_child([sys.executable, "-c", "import time; time.sleep(30)"], dict(os.environ), .15)
        self.assertEqual((result[0], result[3], result[4]), (124, "timed_out", True))

    def test_timeout_kills_term_ignoring_child(self):
        code = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)"
        before = time.monotonic()
        result = w.run_child([sys.executable, "-c", code], dict(os.environ), .3)
        self.assertEqual((result[0], result[3], result[4]), (124, "timed_out", True))
        self.assertLess(time.monotonic() - before, 10)

    def test_normal_exit_cleans_same_group_background_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "grandchild.pid"
            child = "import os,time; from pathlib import Path; Path(%r).write_text(str(os.getpid())); time.sleep(30)" % str(marker)
            code = ("import subprocess,sys,time; from pathlib import Path; "
                    "p=subprocess.Popen([sys.executable,'-c',%r],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                    "\nwhile not Path(%r).exists(): time.sleep(.01)\n") % (child, str(marker))
            pid = None
            try:
                result = w.run_child([sys.executable, "-c", code], dict(os.environ), 3)
                pid = int(marker.read_text())
                self.assert_not_running(pid)
                # Linux PID 1 can retain a zombie: no live work, but group absence is unconfirmed.
                self.assertIn((result[0], result[3], result[4]),
                              ((0, "completed", True), (3, "cleanup_unknown", False)))
            finally:
                if pid:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_escaped_child_is_explicitly_outside_group_guarantee(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "escaped.pid"
            child = "import os,time; from pathlib import Path; Path(%r).write_text(str(os.getpid())); time.sleep(30)" % str(marker)
            code = ("import subprocess,sys,time; from pathlib import Path; "
                    "p=subprocess.Popen([sys.executable,'-c',%r],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True); "
                    "\nwhile not Path(%r).exists(): time.sleep(.01)\n") % (child, str(marker))
            pid = None
            try:
                result = w.run_child([sys.executable, "-c", code], dict(os.environ), 3)
                pid = int(marker.read_text())
                self.assertEqual((result[0], result[4]), (0, True))
                os.kill(pid, 0)  # This proves the limitation, not full-tree cleanup.
            finally:
                if pid:
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_cancel_main_reaps_owned_child_and_removes_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copyfile(w.__file__, root / "watcher.py")
            (root / "safe_tap.py").write_text(
                "import json\nfrom pathlib import Path\ndef audit(row):\n"
                " with open(Path(__file__).with_name('audit.jsonl'),'a') as f: f.write(json.dumps(row)+'\\n')\n")
            (root / "watch_subs.json").write_text('[{"id":"express","last_run":0}]')
            marker = root / "child.pid"
            (root / "watch_express.py").write_text(
                "import os,time\nfrom pathlib import Path\nPath(%r).write_text(str(os.getpid()))\ntime.sleep(30)\n" % str(marker))
            proc = subprocess.Popen([sys.executable, str(root / "watcher.py"), "--once"],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    start_new_session=True)
            child_pid = None
            try:
                wait_file(marker)
                child_pid = int(marker.read_text())
                proc.send_signal(signal.SIGTERM)
                stdout, stderr = proc.communicate(timeout=12)
                self.assertEqual(proc.returncode, 130, (stdout, stderr))
                self.assertFalse((root / ".watcher.pid").exists())
                rows = [json.loads(line) for line in (root / "audit.jsonl").read_text().splitlines()]
                self.assertEqual([row["kind"] for row in rows], ["task_start", "task_end"])
                self.assertEqual(rows[-1]["terminal_status"], "cancelled")
                self.assertTrue(rows[-1]["group_cleanup_confirmed"])
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)
            finally:
                for pid in (child_pid, proc.pid):
                    if pid:
                        try:
                            os.killpg(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                proc.communicate(timeout=3)

    @unittest.skipUnless(os.path.isfile("/proc/self/cmdline"), "PID identity requires procfs")
    def test_stop_cli_confirms_real_watcher_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "watcher.py"
            shutil.copyfile(w.__file__, script)
            (root / "watch_subs.json").write_text("[]")
            proc = subprocess.Popen([sys.executable, str(script)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    start_new_session=True, text=True)
            waiter = threading.Thread(target=proc.wait, daemon=True)
            try:
                wait_file(root / ".watcher.pid")
                waiter.start()
                result = subprocess.run([sys.executable, str(script), "--stop"],
                                        capture_output=True, text=True, timeout=13)
                self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
                waiter.join(timeout=2)
                self.assertFalse(waiter.is_alive())
                self.assertEqual(proc.returncode, 130)
                self.assertFalse((root / ".watcher.pid").exists())
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate(timeout=3)

    @unittest.skipUnless(os.path.isfile("/proc/self/cmdline"), "PID identity requires procfs")
    def test_stop_cli_refuses_unrelated_owned_sleeper(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "watcher.py"
            shutil.copyfile(w.__file__, script)
            proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                    start_new_session=True)
            try:
                pidfile = root / ".watcher.pid"
                pidfile.write_text(str(proc.pid))
                result = subprocess.run([sys.executable, str(script), "--stop"],
                                        capture_output=True, text=True, timeout=3)
                self.assertEqual(result.returncode, 3, (result.stdout, result.stderr))
                self.assertIsNone(proc.poll())
                self.assertTrue(pidfile.exists())
            finally:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
