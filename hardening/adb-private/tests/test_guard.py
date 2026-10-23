import os
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from contextlib import contextmanager

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'payload'))
import adb_guard as guard
import adb_policy as policy


class Healing(unittest.TestCase):
    def test_busy_shared_client_still_renews_file_audit_without_health_write(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'manifest.json').write_text('{}')

            @contextmanager
            def lock(*, shared=False, timeout=0):
                if not shared:
                    raise policy.PolicyBusy('long client')
                yield

            with patch.object(guard, 'ROOT', root), patch.object(guard, 'startup_lock', lock), \
                 patch.object(guard, 'require_recent_audit', side_effect=policy.PolicyError('expired')), \
                 patch.object(guard, 'pause_reason', return_value=None), \
                 patch.object(guard, 'load', return_value={}) as full, \
                 patch.object(guard, 'write_audit_receipt', side_effect=lambda: policy.write_audit_receipt(root)):
                self.assertEqual(guard.main(), 0)
                full.assert_called_once_with(audit=True)
            policy.require_recent_audit(root)
            self.assertFalse((root / 'health.json').exists())

    def test_guard_renews_audit_at_121_seconds_while_reader_accepts_300(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'manifest.json').write_text('{}')
            with patch.object(policy.time, 'time', return_value=1000):
                policy.write_audit_receipt(root)
            row = {'pid': 42, 'exe': '/private/adb.backend', 'socket': 'localfilesystem:/private/server.sock'}
            config = {'backend': row['exe'], 'socket': '/private/server.sock'}
            with patch.object(policy.time, 'time', return_value=1121), \
                 patch.object(guard, 'ROOT', root), \
                 patch.object(guard, 'require_recent_audit', side_effect=lambda **kw: policy.require_recent_audit(root, **kw)), \
                 patch.object(guard, 'load', return_value=config) as load, \
                 patch.object(guard, 'server_processes', return_value=([row], [])), \
                 patch.object(guard, 'probe', return_value='closed'), \
                 patch.object(guard, 'pause_reason', return_value=None), \
                 patch.object(guard, 'publish_ready'), \
                 patch.object(guard, '_status_locked', return_value={'ok': True}), \
                 patch.object(guard, 'write_audit_receipt', side_effect=lambda: policy.write_audit_receipt(root)):
                policy.require_recent_audit(root)
                self.assertEqual(guard.check_locked(), 0)
                load.assert_called_once_with(audit=True)
            receipt = json.loads((root / 'runtime-audit.json').read_text())
            self.assertEqual(receipt['checked_at'], 1121)

    def test_termination_requires_pidfd_and_never_falls_back_to_numeric_kill(self):
        identity = {'pid': 21, 'exe': '/p/adb.backend', 'socket': 'localfilesystem:/p/server.sock',
                    'uid': os.getuid(), 'start': '100'}
        with patch.object(guard.os, 'pidfd_open', None, create=True), patch.object(guard, '_PIDFD_OPEN', None), \
             patch.object(guard.os, 'kill') as numeric:
            with self.assertRaisesRegex(policy.PolicyError, 'requires pidfd'):
                guard.terminate_managed(identity, {})
            numeric.assert_not_called()
        with patch.object(guard.os, 'pidfd_open', return_value=91, create=True), \
             patch.object(guard.signal, 'pidfd_send_signal', create=True) as send, \
             patch.object(guard.os, 'close') as close, \
             patch.object(guard.os, 'kill') as numeric, \
             patch.object(guard, 'managed_identity', return_value=identity):
            guard.terminate_managed(identity, {})
            send.assert_called_once_with(91, guard.signal.SIGTERM)
            close.assert_called_once_with(91)
            numeric.assert_not_called()
        with patch.object(guard.os, 'pidfd_open', return_value=91, create=True), \
             patch.object(guard.signal, 'pidfd_send_signal', create=True) as send, \
             patch.object(guard.os, 'close'), \
             patch.object(guard, 'managed_identity', return_value=dict(identity, start='101')):
            with self.assertRaisesRegex(policy.PolicyError, 'starttime changed'):
                guard.terminate_managed(identity, {})
            send.assert_not_called()

    def test_android_uses_exported_bionic_pidfd_api_not_numeric_pid_signal(self):
        with patch.object(guard.os, 'pidfd_open', None, create=True), \
             patch.object(guard.signal, 'pidfd_send_signal', None, create=True), \
             patch.object(guard, '_PIDFD_OPEN', return_value=91) as opened, \
             patch.object(guard, '_PIDFD_SEND', return_value=0) as sent, \
             patch.object(guard.os, 'kill') as numeric:
            self.assertEqual(guard.open_pidfd(21), 91)
            guard.send_pidfd(91)
            opened.assert_called_once_with(21, 0)
            sent.assert_called_once_with(91, guard.signal.SIGTERM, None, 0)
            numeric.assert_not_called()

    def test_all_identities_are_checked_before_any_signal_and_changed_pid_is_not_killed(self):
        config = {'backend': '/p/adb.backend', 'socket': '/p/server.sock'}
        rows = [{'pid': n, 'exe': config['backend'], 'socket': 'localfilesystem:/p/server.sock'} for n in [21, 22]]
        identities = [{**r, 'uid': os.getuid(), 'start': '100'} for r in rows]
        with patch.object(guard, 'managed_identity', side_effect=[identities[0], policy.PolicyError('PID changed')], create=True), \
             patch.object(guard, 'terminate_managed', create=True) as terminate:
            with self.assertRaisesRegex(policy.PolicyError, 'PID changed'):
                guard.heal_duplicates(config, rows)
            terminate.assert_not_called()

    def test_heal_signals_only_verified_set_then_starts_once(self):
        config = {'backend': '/p/adb.backend', 'socket': '/p/server.sock'}
        rows = [{'pid': n, 'exe': config['backend'], 'socket': 'localfilesystem:/p/server.sock'} for n in [21, 22]]
        identities = [{**r, 'uid': os.getuid(), 'start': '100'} for r in rows]
        with patch.object(guard, 'managed_identity', side_effect=identities, create=True), \
             patch.object(guard, 'terminate_managed', create=True) as terminate, \
             patch.object(guard, 'server_processes', return_value=([], [])), \
             patch.object(guard, 'ensure_server_locked') as start:
            guard.heal_duplicates(config, rows)
            self.assertEqual([c.args[0] for c in terminate.call_args_list], identities)
            start.assert_called_once_with(config)

    def test_identity_checks_uid_socket_exe_starttime_and_running_image_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proc = root / '21'
            proc.mkdir()
            (root / 'self').mkdir()
            (root / 'self/status').write_text('Uid:\t42\t42\t42\t42\n')
            (proc / 'status').write_text('Uid:\t42\t42\t42\t42\n')
            (proc / 'stat').write_text('21 (adb) S ' + '0 ' * 18 + '100')
            backend = root / 'adb.backend'
            backend.write_bytes(b'fixture executable')
            (proc / 'exe').symlink_to(backend)
            (proc / 'cmdline').write_bytes(os.fsencode(backend) + b'\0-L\0localfilesystem:/p/server.sock\0fork-server\0server\0')
            config = {'backend': str(backend), 'socket': '/p/server.sock'}
            row = {'pid': 21, 'exe': str(backend), 'socket': 'localfilesystem:/p/server.sock'}
            with patch.object(guard.os, 'getuid', return_value=42):
                identity = guard.managed_identity(row, config, root)
                self.assertEqual(identity['start'], '100')
                self.assertEqual(identity['uid'], 42)
                for field, value in [('exe', '/foreign'), ('socket', 'tcp:5037')]:
                    with self.subTest(field=field), self.assertRaises(policy.PolicyError):
                        guard.managed_identity(dict(row, **{field: value}), config, root)
                (proc / 'status').write_text('Uid:\t43\t43\t43\t43\n')
                with self.assertRaises(policy.PolicyError):
                    guard.managed_identity(row, config, root)


if __name__ == '__main__':
    unittest.main()
