import errno
from contextlib import nullcontext
import os
import signal
from pathlib import Path
import socket
import sys
import tempfile
import types
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'payload'))
import adb_policy as policy


class Arguments(unittest.TestCase):
    def test_overrides_rejected_before_exec(self):
        cases = [['-H', 'localhost'], ['-Hlocalhost'], ['-P', '5037'], ['-P5037'],
                 ['-L', 'invalid'], ['-Linvalid'], ['-a'], ['server', '-P5037'],
                 ['nodaemon', '-Hlocalhost'], ['--reply-fd', '4'], ['fork-server'],
                 ['--unknown'], ['-s'], ['-t'], ['--one-device']]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(policy.PolicyError):
                policy.validate(args + ([] if args in [['-s'], ['-t'], ['--one-device']] else ['start-server']), '/private/server.sock')

    def test_global_options_and_shell_payload_kept(self):
        for args in [[], ['version'], ['server', 'nodaemon'],
                     ['-s', '127.0.0.1:5555', 'shell', 'printf', '-P5037'],
                     ['-s127.0.0.1:5555', 'get-state'], ['-t', '2', 'get-state'],
                     ['--one-device', 'x', 'start-server'], ['devices', '-l'],
                     ['--exit-on-write-error', 'shell', 'echo', '-L'],
                     ['forward', 'tcp:0', 'localabstract:chrome_devtools_remote']]:
            self.assertEqual(policy.validate(args, '/private/server.sock'), args)

    def test_exact_private_L_only(self):
        self.assertEqual(policy.validate(['-L', 'localfilesystem:/private/server.sock', 'devices'], '/private/server.sock'), ['devices'])

    def test_private_bridge_and_raw_rejected(self):
        for args in [['reverse', 'tcp:0', 'localfilesystem:/private/server.sock'],
                     ['reverse', '--no-rebind', 'tcp:0', 'localfilesystem:/private/../private/server.sock'],
                     ['forward', 'tcp:0', 'localfilesystem:/private/server.sock'], ['raw', 'host:version']]:
            with self.subTest(args=args), self.assertRaises(policy.PolicyError):
                policy.validate(args, '/private/server.sock')

    def test_private_bridge_bind_alias_rejected_by_inode(self):
        with patch.object(policy.os.path, 'samefile', return_value=True), self.assertRaises(policy.PolicyError):
            policy.validate(['reverse', 'tcp:0', 'localfilesystem:/alias/server.sock'], '/private/server.sock')


class Probes(unittest.TestCase):
    def test_offline_inventory_does_not_prevent_local_audit_receipt(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'manifest.json').write_text('{}')
            config = {'backend': '/private/adb', 'socket': '/private/server.sock'}
            row = {'pid': 42, 'exe': config['backend'], 'socket': 'localfilesystem:' + config['socket']}
            with patch.object(policy, 'startup_lock', return_value=nullcontext()), \
                 patch.object(policy, 'pause_reason', return_value=None), \
                 patch.object(policy, 'load', return_value=config), \
                 patch.object(policy, 'socket_info'), patch.object(policy, 'host_version', return_value='1'), \
                 patch.object(policy, 'server_processes', return_value=([row], [])), \
                 patch.object(policy, 'check_runtime_maps'), \
                 patch.object(policy, 'probe', return_value='closed'), \
                 patch.object(policy, '_tcp_inventory_locked', side_effect=policy.PolicyError('device offline')):
                report = policy.status(root, audit=True)
            self.assertFalse(report['ok'])
            self.assertIn('device offline', report['error'])
            policy.require_recent_audit(root)

    def test_live_same_uid_status_read_failure_is_unknown_not_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ['self', '123']:
                (root / name).mkdir()
                (root / name / 'status').write_text('Uid:\t42\t42\t42\t42\n')
            original = Path.read_text
            def read(path, *args, **kwargs):
                if path == root / '123/status':
                    raise PermissionError(errno.EACCES, 'live same uid inaccessible')
                return original(path, *args, **kwargs)
            with patch.object(Path, 'read_text', read):
                self.assertEqual(policy.server_processes(root), ([], ['123']))

    def test_eight_cold_calls_start_exactly_one_server(self):
        barrier = Barrier(8)
        starts = []
        live = []
        def ensure(*args):
            starts.append(1)
            time.sleep(.08)
            live.append(True)
        def call(root):
            barrier.wait(timeout=2)
            return policy.start(['devices'], root)
        with tempfile.TemporaryDirectory() as d, \
             patch.object(policy, 'load', return_value={'backend': '/test/adb', 'socket': '/test/server.sock'}), \
             patch.object(policy, 'prepare', return_value=(['not-executed'], {})), \
             patch.object(policy, 'require_closed_default'), \
             patch.object(policy, 'server_ready', side_effect=lambda _: bool(live)), \
             patch.object(policy, 'ensure_server_locked', side_effect=ensure), \
             patch.object(policy, 'client_run', return_value=types.SimpleNamespace(returncode=0)):
            with ThreadPoolExecutor(max_workers=8) as pool:
                self.assertEqual(list(pool.map(call, [d] * 8)), [0] * 8)
        self.assertEqual(len(starts), 1)

    def test_managed_server_disables_emulator_scanner_even_if_caller_enables_it(self):
        with patch.dict(os.environ, {'ADB_EMU': '1'}), patch.object(policy.os, 'umask'):
            _, env = policy.prepare(['start-server'], {'backend': '/test/adb', 'socket': '/test/server.sock'}, '/nonexistent')
        self.assertEqual(env['ADB_EMU'], '0')

    def test_exited_proc_esrch_is_not_unknown_but_live_permission_failure_is(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'self').mkdir()
            (root / 'self/status').write_text('Uid:\t42\t42\t42\t42\n')
            (root / '123').mkdir()
            (root / '123/status').write_text('Uid:\t42\t42\t42\t42\n')
            for error, expected in [(ProcessLookupError(errno.ESRCH, 'gone'), []),
                                    (PermissionError(errno.EACCES, 'denied'), ['123'])]:
                with self.subTest(errno=error.errno), patch.object(Path, 'read_bytes', side_effect=error):
                    self.assertEqual(policy.server_processes(root), ([], expected))

    def test_ordinary_backend_calls_share_execution_lock(self):
        active, peak = 0, 0
        mutex = Lock()
        def backend(*args, **kwargs):
            nonlocal active, peak
            with mutex:
                active += 1
                peak = max(peak, active)
            time.sleep(.05)
            with mutex:
                active -= 1
            return types.SimpleNamespace(returncode=0)
        with tempfile.TemporaryDirectory() as d, \
             patch.object(policy, 'load', return_value={'backend': '/test/adb', 'socket': '/test/server.sock'}), \
             patch.object(policy, 'prepare', return_value=(['not-executed'], {})), \
             patch.object(policy, 'require_closed_default'), patch.object(policy, 'ensure_server_locked'), \
             patch.object(policy, 'server_ready', return_value=True, create=True), \
             patch.object(policy.subprocess, 'run', side_effect=backend):
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(list(pool.map(lambda _: policy.start(['devices'], d), range(2))), [0, 0])
        self.assertEqual(peak, 2)

    def test_shell_sleep_60_does_not_block_shell_true(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            pidfile = Path(d) / 'sleep.pid'
            def command(args, config, root):
                script = 'echo $$ > "$1"; exec sleep 60' if args[-1] == '60' else 'true'
                return ['/bin/sh', '-c', script, 'test', str(pidfile)], os.environ.copy()
            with patch.object(policy, 'load', return_value={'backend': '/test/adb', 'socket': '/test/server.sock',
                                                          'target_file': str(Path(d) / 'adb-target')}), \
                 patch.object(policy, 'prepare', side_effect=command), \
                 patch.object(policy, 'require_closed_default'), patch.object(policy, 'ensure_server_locked'), \
                 patch.object(policy, 'server_ready', return_value=True, create=True), \
                 patch.object(policy, 'bind_command', side_effect=lambda args, config: args), \
                 patch.object(policy.adb_identity, 'binding_lease', return_value=nullcontext()), \
                 patch.object(policy, 'client_run', side_effect=subprocess.run), \
                 ThreadPoolExecutor(max_workers=2) as pool:
                long_call = pool.submit(policy.start, ['shell', 'sleep', '60'], d)
                try:
                    deadline = time.monotonic() + 2
                    while not pidfile.exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue(pidfile.exists(), 'long backend did not start')
                    short_call = pool.submit(policy.start, ['shell', 'true'], d)
                    self.assertEqual(short_call.result(timeout=2), 0)
                    self.assertFalse(long_call.done())
                finally:
                    if pidfile.exists():
                        os.kill(int(pidfile.read_text()), signal.SIGTERM)

    def test_backend_keeps_shared_lock_after_policy_parent_exits_until_backend_ends(self):
        import subprocess
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
# Test only the inherited lock descriptor, without the Linux-only death/filter setup.
policy.client_run = subprocess.run
raise SystemExit(policy.start(['devices'], root))
'''
        with tempfile.TemporaryDirectory() as d:
            pidfile = Path(d) / 'backend.pid'
            payload = Path(__file__).resolve().parents[1] / 'payload'
            parent = subprocess.Popen([sys.executable, '-c', program, str(payload), d, str(pidfile)],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            backend_pid = None
            def exclusive_available():
                try:
                    with policy.startup_lock(d, timeout=0):
                        return True
                except policy.PolicyBusy:
                    return False
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
                self.assertFalse(exclusive_available())
                parent.terminate()
                parent.wait(timeout=3)
                os.kill(backend_pid, 0)
                self.assertFalse(exclusive_available(), 'Policy exit released a lock still needed by its backend')
                os.kill(backend_pid, signal.SIGTERM)
                deadline = time.monotonic() + 3
                while not exclusive_available() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(exclusive_available(), 'Backend exit did not release its inherited shared lock')
                backend_pid = None
            finally:
                if parent.poll() is None:
                    parent.kill()
                parent.wait(timeout=3)
                if backend_pid is not None:
                    try:
                        os.kill(backend_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    deadline = time.monotonic() + 3
                    while not exclusive_available() and time.monotonic() < deadline:
                        time.sleep(.01)

    def test_exclusive_operation_cannot_enter_during_shared_call(self):
        with tempfile.TemporaryDirectory() as d:
            with policy.startup_lock(d, shared=True):
                with self.assertRaises(policy.PolicyBusy):
                    with policy.startup_lock(d, timeout=0):
                        self.fail('exclusive operation entered a running client')

    def test_concurrent_private_starts_are_serialized(self):
        active, peak = 0, 0
        mutex = Lock()
        def spawn(*args, **kwargs):
            nonlocal active, peak
            with mutex:
                active += 1
                peak = max(peak, active)
            time.sleep(.04)
            with mutex:
                active -= 1
        with tempfile.TemporaryDirectory() as d, \
             patch.object(policy, 'prepare', return_value=(['not-executed'], {})), \
             patch.object(policy.subprocess, 'run', side_effect=spawn), \
             patch.object(policy, 'require_closed_default'), \
             patch.object(policy, 'publish_ready'):
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda _: policy.ensure_server({}, d), range(2)))
            self.assertEqual(peak, 1)
            self.assertEqual((Path(d) / 'start.lock').stat().st_mode & 0o777, 0o600)

    def test_ready_retries_startup_gap_but_never_unknown_server(self):
        config = {'backend': '/p/adb.backend', 'socket': '/p/server.sock'}
        good = {'pid': 42, 'exe': config['backend'], 'socket': 'localfilesystem:/p/server.sock'}
        with patch.object(policy, 'server_processes', side_effect=[([], []), ([good], [])]), \
             patch.object(policy.time, 'sleep'), \
             patch.object(Path, 'read_text', return_value='42 (adb) S ' + '0 ' * 18 + '123'), \
             patch.object(policy, 'write_state') as write:
            policy.publish_ready(config)
            write.assert_called_once()
        with patch.object(policy, 'server_processes', return_value=([dict(good, exe='/foreign')], [])), \
             patch.object(policy.time, 'sleep') as sleep, self.assertRaises(policy.PolicyError):
            policy.publish_ready(config)
        sleep.assert_not_called()

    def test_diverted_adb_process_is_not_invisible(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for p in ['self', '123']:
                (root / p).mkdir()
                (root / p / 'status').write_text('Uid:\t10001\t10001\t10001\t10001\n')
            (root / '123/cmdline').write_bytes(b'/x/adb.dsb-vendor\0-L\0tcp:5037\0fork-server\0server\0')
            (root / '123/exe').symlink_to('/x/adb.dsb-vendor')
            rows, unknown = policy.server_processes(root)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['socket'], 'tcp:5037')
            self.assertEqual(unknown, [])
            (root / '123/cmdline').write_bytes(b'/x/vendor-tool\0-L\0tcp:15037\0server\0nodaemon\0')
            (root / '123/exe').unlink()
            (root / '123/exe').symlink_to('/x/vendor-tool')
            rows, unknown = policy.server_processes(root)
            self.assertEqual(rows, [{'pid': 123, 'socket': 'tcp:15037', 'exe': '/x/vendor-tool'}])
            self.assertEqual(unknown, [])

    def test_server_option_values_do_not_make_clients_into_servers(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for p in ['self', '123']:
                (root / p).mkdir()
                (root / p / 'status').write_text('Uid:\t10001\t10001\t10001\t10001\n')
            (root / '123/exe').symlink_to('/x/adb')
            for args in [b'-L\0tcp:5037\0-s\0server\0wait-for-device',
                         b'--one-device\0server\0start-server', b'shell\0server', b'server']:
                (root / '123/cmdline').write_bytes(b'/x/adb\0' + args + b'\0')
                self.assertEqual(policy.server_processes(root), ([], []))

    def test_cold_policy_checks_both_ports_before_any_backend_request(self):
        for results in [('listening', 'closed'), ('closed', 'listening'), ('closed', 'unknown')]:
            with self.subTest(results=results), tempfile.TemporaryDirectory() as d, \
                 patch.object(policy, 'probe', side_effect=results), \
                 patch.object(policy.subprocess, 'run') as run, self.assertRaises(policy.PolicyError):
                policy.ensure_server({}, d)
            run.assert_not_called()

    def test_fifo_gate_does_not_block(self):
        with tempfile.TemporaryDirectory() as d:
            os.mkfifo(Path(d)/'maintenance')
            self.assertIn('pause reason unavailable',policy.pause_reason(d))

    def test_inventory_unknown_never_passes(self):
        config = {'backend': '/not-executed', 'socket': '/not-connected', 'target_file': '/not-read'}
        for response in ['', 'sl local remote\n', 'unparseable\n']:
            with patch.object(Path, 'read_text', return_value='127.0.0.1:12345'), \
                 patch.object(policy, 'server_processes', return_value=([], [])), \
                 patch.object(policy, 'startup_lock'), patch.object(policy, 'require_closed_default'), \
                 patch.object(policy.adb_identity, 'authenticated_shell', return_value=response), self.assertRaises(policy.PolicyError):
                policy.tcp_inventory(config, [])

    def test_three_states(self):
        class Fake:
            def settimeout(self, seconds): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def connect_ex(self, address): return rc
        for rc, expected in [(0, 'listening'), (errno.ECONNREFUSED, 'closed'),
                             (errno.ETIMEDOUT, 'unknown'), (errno.EACCES, 'unknown')]:
            with self.subTest(rc=rc), patch.object(policy.socket, 'socket', return_value=Fake()):
                self.assertEqual(policy.probe(socket.AF_INET, '127.0.0.1', 1), expected)
        with patch.object(policy.socket, 'socket', side_effect=OSError(errno.EMFILE, 'full')):
            self.assertEqual(policy.probe(socket.AF_INET6, '::1', 1), 'unknown')

    def test_private_dir_symlink_and_bad_mode_fail(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            target = root / 'target'
            target.mkdir(mode=0o700)
            link = root / 'link'
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaises(policy.PolicyError): policy.private_dir(link)
            target.chmod(0o755)
            with self.assertRaises(policy.PolicyError): policy.private_dir(target)
            target.chmod(0o700)
            policy.private_dir(target)


if __name__ == '__main__':
    unittest.main()
