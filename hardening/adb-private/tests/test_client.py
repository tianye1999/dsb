import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

PAYLOAD = Path(__file__).resolve().parents[1] / 'payload'
sys.path.insert(0, str(PAYLOAD))
import adb_client


class ParentDeathSetupTests(unittest.TestCase):
    def test_death_signal_is_armed_before_parent_check_and_process_filter(self):
        events = []
        with patch.object(adb_client, '_PRCTL', side_effect=lambda *args: events.append('death-signal') or 0) as prctl, \
             patch.object(adb_client.os, 'getppid', side_effect=lambda: events.append('parent-check') or 4242), \
             patch.object(adb_client, 'isolate_client', side_effect=lambda: events.append('filter')):
            adb_client.child_setup(4242)
        prctl.assert_called_once_with(1, signal.SIGKILL, 0, 0, 0)
        self.assertEqual(events, ['death-signal', 'parent-check', 'filter'])

    def test_parent_changed_before_signal_setup_exits_without_running_backend_filter(self):
        with patch.object(adb_client, '_PRCTL', return_value=0) as prctl, \
             patch.object(adb_client.os, 'getppid', return_value=1), \
             patch.object(adb_client.os, '_exit', side_effect=SystemExit(78)) as exit_child, \
             patch.object(adb_client, 'isolate_client') as isolate, \
             self.assertRaises(SystemExit) as exited:
            adb_client.child_setup(4242)
        self.assertEqual(exited.exception.code, 78)
        prctl.assert_called_once_with(1, signal.SIGKILL, 0, 0, 0)
        exit_child.assert_called_once_with(78)
        isolate.assert_not_called()

    def test_unavailable_or_failed_parent_death_protection_fails_closed(self):
        for available in [False, True]:
            with self.subTest(prctl_available=available), \
                 patch.object(adb_client, '_PRCTL', return_value=-1) as prctl, \
                 patch.object(adb_client, 'isolate_client') as isolate:
                if not available:
                    adb_client._PRCTL = None
                with self.assertRaises(OSError):
                    adb_client.child_setup(4242)
                isolate.assert_not_called()
                if available:
                    prctl.assert_called_once_with(1, signal.SIGKILL, 0, 0, 0)


@unittest.skipUnless(adb_client._SUPPORTED, 'kernel filter requires Linux arm64/x86_64')
class ClientIsolationTests(unittest.TestCase):
    def test_threads_work_but_process_creation_cannot_autostart_server(self):
        program = '''
import os, threading
thread = threading.Thread(target=lambda: print('THREAD_OK'))
thread.start(); thread.join()
try:
    child = os.fork()
except PermissionError:
    print('PROCESS_DENIED')
else:
    if not child: os._exit(0)
    os.waitpid(child, 0)
    raise SystemExit('process creation unexpectedly allowed')
'''
        result = adb_client.client_run([sys.executable, '-c', program], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b'THREAD_OK', result.stdout)
        self.assertIn(b'PROCESS_DENIED', result.stdout)

    def test_terminating_only_policy_parent_kills_the_running_backend(self):
        program = '''
import os, subprocess, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import adb_policy as policy
root, pidfile = sys.argv[2:]
backend = 'import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)'
policy.load = lambda root: {'backend': '/fixture/adb', 'socket': '/fixture/server.sock', 'target_file': str(Path(root) / 'target')}
policy.prepare = lambda args, config, root: ([sys.executable, '-c', backend, pidfile], os.environ.copy())
policy.require_closed_default = lambda: None
policy.server_ready = lambda config: True
raise SystemExit(policy.start(['devices'], root))
'''
        def running(pid):
            try:
                state = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()[0]
            except (FileNotFoundError, ProcessLookupError):
                return False
            return state not in ['Z', 'X']
        with tempfile.TemporaryDirectory() as d:
            pidfile = Path(d) / 'backend.pid'
            parent = subprocess.Popen([sys.executable, '-c', program, str(PAYLOAD), d, str(pidfile)],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            backend_pid = None
            try:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    self.assertIsNone(parent.poll(), 'Policy parent exited before its backend started')
                    try:
                        backend_pid = int(pidfile.read_text())
                        break
                    except (FileNotFoundError, ValueError):
                        time.sleep(.01)
                self.assertIsNotNone(backend_pid, 'Backend did not publish its fixture PID')
                self.assertTrue(running(backend_pid))
                parent.terminate()
                parent.wait(timeout=3)
                deadline = time.monotonic() + 2
                while running(backend_pid) and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertFalse(running(backend_pid), 'Backend outlived its terminated policy parent')
            finally:
                if parent.poll() is None:
                    parent.kill()
                parent.wait(timeout=3)
                if backend_pid is not None and running(backend_pid):
                    try:
                        os.kill(backend_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    deadline = time.monotonic() + 3
                    while running(backend_pid) and time.monotonic() < deadline:
                        time.sleep(.01)


if __name__ == '__main__':
    unittest.main()
