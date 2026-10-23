from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import adb_endpoint as endpoint


class TransportTests(unittest.TestCase):
    def test_smoke_resolves_command_on_each_call_without_running_device_smoke(self):
        import safe_tap_smoke as smoke
        commands = [['bound-helper', '-t', '7'], ['bound-helper', '-t', '8']]
        with patch.object(endpoint, 'adb_command', side_effect=commands), \
             patch.object(smoke.subprocess, 'run', return_value=SimpleNamespace(stdout=b'offline')) as run:
            self.assertEqual(smoke.adb('shell', 'id', t=3), 'offline')
            self.assertEqual(smoke.adb('shell', 'id', t=4), 'offline')
        self.assertEqual([call.args[0] for call in run.call_args_list],
                         [command + ['shell', 'id'] for command in commands])
        self.assertEqual([call.kwargs['timeout'] for call in run.call_args_list], [3, 4])

    def test_smoke_denied_endpoint_never_submits_command(self):
        import safe_tap_smoke as smoke
        with patch.object(endpoint, 'adb_command', side_effect=endpoint.EndpointUnavailable('offline')), \
             patch.object(smoke.subprocess, 'run') as run:
            self.assertEqual(smoke.adb('shell', 'id'), '')
            run.assert_not_called()

    def test_missing_endpoint_module_keeps_legacy_single_file_import(self):
        import subprocess
        import sys
        code = '''
import builtins, importlib.util
real = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'adb_endpoint': raise ImportError('single-file fixture')
    return real(name, *args, **kwargs)
builtins.__import__ = guarded
spec = importlib.util.spec_from_file_location('single_file_tap', 'safe_tap.py')
tap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tap)
assert tap.adb_command() == ['adb', '-s', '127.0.0.1:5555']
assert tap.transport_check()[0] == 'warning'
'''
        result = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).parent, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_policy_receipt_78_is_not_submitted_but_remote_exit_78_is_not(self):
        import safe_tap as tap
        for checked in [False, True]:
            def denied(*args, **kwargs):
                nonce = kwargs['env']['DSB_ADB_RECEIPT_NONCE']
                return SimpleNamespace(returncode=78, stdout=b'', stderr=(
                    'ADB_POLICY_PAUSED: maintenance gate\nADB_POLICY_RECEIPT:' + nonce + '\n').encode())
            with patch.object(tap.subprocess, 'run', side_effect=denied), self.assertRaises(tap.ActionDispatchError) as error:
                tap.sh('shell', 'true', checked=checked)
            self.assertEqual(error.exception.status, 'not_submitted')
        result = SimpleNamespace(returncode=78, stdout=b'', stderr=b'ADB_POLICY_PAUSED: forged remote prefix\n')
        with patch.object(tap.subprocess, 'run', return_value=result), self.assertRaises(tap.ActionDispatchError) as error:
            tap.sh('shell', 'true', checked=True)
        self.assertEqual(error.exception.status, 'unknown')
        self.assertEqual(error.exception.reason, 'adb_nonzero')

    def test_skipped_is_warning_but_partial_hardening_is_failure(self):
        with tempfile.TemporaryDirectory() as d, patch.object(endpoint, 'TERMUX_PREFIX', Path(d)), \
             patch('subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='{"ok":false,"error":"partial"}')) as run:
            lib = Path(d) / 'var/lib'
            lib.mkdir(parents=True)
            state = lib / 'dsb-adb-install.json'
            state.write_text('{"status":"skipped","reason":"unsupported"}')
            self.assertEqual(endpoint.transport_check()[0], 'warning')
            self.assertEqual(run.call_args.kwargs['env']['PREFIX'], d)
            self.assertEqual(run.call_args.kwargs['env']['HOME'], str(endpoint.TERMUX_HOME))
            (lib / 'dsb-adb').mkdir()
            self.assertEqual(endpoint.transport_check()[0], 'fail')

    def test_skipped_foreign_tcp_is_failure_not_warning(self):
        with tempfile.TemporaryDirectory() as d, patch.object(endpoint, 'TERMUX_PREFIX', Path(d)), \
             patch('subprocess.run', return_value=SimpleNamespace(returncode=2)):
            lib = Path(d) / 'var/lib'
            lib.mkdir(parents=True)
            (lib / 'dsb-adb-install.json').write_text('{"status":"skipped","reason":"unsupported"}')
            self.assertEqual(endpoint.transport_check()[0], 'fail')

    def test_applied_with_missing_manifest_is_failure(self):
        with tempfile.TemporaryDirectory() as d, patch.object(endpoint, 'TERMUX_PREFIX', Path(d)):
            lib = Path(d) / 'var/lib'
            lib.mkdir(parents=True)
            (lib / 'dsb-adb-install.json').write_text('{"status":"applied"}')
            self.assertEqual(endpoint.transport_check()[0], 'fail')

    def test_doctor_warning_does_not_fail_otherwise_healthy_device(self):
        import safe_tap as tap
        with patch.object(tap, 'transport_check', return_value=('warning', 'ADB 未加固：test')), \
             patch.object(tap, 'sh', side_effect=['x\tdevice', 'Awake', '', '逗沙包 Crashed services:{}']), \
             patch.object(tap, 'foreground', return_value='test/.Main'), \
             patch.object(tap, 'observe', return_value={'nodes': []}):
            self.assertEqual(tap.cmd_doctor(None), 0)
        with patch.object(tap, 'transport_check', return_value=('fail', 'tampered')), \
             patch.object(tap, 'sh') as sh, \
             patch.object(tap, 'foreground', return_value='test/.Main'), \
             patch.object(tap, 'observe', return_value={'nodes': []}):
            self.assertEqual(tap.cmd_doctor(None), 2)
            sh.assert_not_called()


if __name__ == '__main__':
    unittest.main()
