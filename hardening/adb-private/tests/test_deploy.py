import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('deploy', Path(__file__).parents[1] / 'deploy.py')
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class TransactionTests(unittest.TestCase):
    def test_connect_receipt_is_followed_by_bounded_transport_readiness(self):
        with patch.object(deploy, 'run', return_value='') as run:
            deploy.connect_ready(['/native/adb', '-L', 'tcp:localhost:5037'], '127.0.0.1:45678')
            self.assertEqual(run.call_args_list[0].args[0],
                             ['/native/adb', '-L', 'tcp:localhost:5037', 'connect', '127.0.0.1:45678'])
            self.assertEqual(run.call_args_list[1].args[0][-3:], ['-s', '127.0.0.1:45678', 'wait-for-device'])
            self.assertEqual(run.call_args_list[1].kwargs, {'timeout': 15})

    def test_transport_readiness_timeout_is_failure_not_successful_connect(self):
        with patch.object(deploy, 'run', side_effect=['connected', deploy.DeployError('timeout')]), \
             self.assertRaisesRegex(deploy.DeployError, 'timeout'):
            deploy.connect_ready(['/native/adb'], '127.0.0.1:45678')

    def test_failed_rollback_still_keeps_non_adb_supervision(self):
        with tempfile.TemporaryDirectory() as d, patch.object(deploy, 'ROOT', Path(d)), \
             patch.object(deploy, 'require_native'), \
             patch.object(deploy, '_rollback', side_effect=deploy.DeployError('conflict')), \
             patch.object(deploy, 'record_state'), \
             patch.object(deploy, 'SUP', Path(__file__)), \
             patch.object(deploy, 'start_supervisor') as start:
            deploy.Transaction(d).save()
            with self.assertRaises(deploy.DeployError):
                deploy.rollback()
            start.assert_called_once()

    def test_recover_only_rolls_back_incomplete_transaction(self):
        with tempfile.TemporaryDirectory() as d, patch.object(deploy, 'ROOT', Path(d)), \
             patch.object(deploy, 'rollback') as rollback:
            txn = deploy.Transaction(Path(d))
            txn.event('entrypoints-installed')
            deploy.recover()
            rollback.assert_called_once()
            rollback.reset_mock()
            txn.event('applied')
            deploy.recover()
            rollback.assert_not_called()

    def test_tcp_close_wait_is_bounded_and_requires_both_families(self):
        with patch.object(deploy.policy, 'probe', side_effect=['listening', 'closed', 'closed', 'closed']), \
             patch.object(deploy.time, 'sleep'):
            deploy.wait_tcp_closed()
        with patch.object(deploy.policy, 'probe', return_value='unknown'), \
             patch.object(deploy.policy, 'server_processes', return_value=([], [])), \
             self.assertRaises(deploy.DeployError):
            deploy.wait_tcp_closed(timeout=0)

    def test_diversion_rename_does_not_break_original_process_attestation(self):
        original = {'server': {'pid': 42, 'start': '123', 'exe': '/bin/adb'}, 'server_sha256': 'original'}
        moved = {'pid': 42, 'start': '123', 'exe': '/bin/adb.dsb-vendor'}
        with patch.object(deploy, 'proc_identity', return_value=moved), \
             patch.object(deploy.policy, 'digest', return_value='original'), \
             patch.object(deploy, 'stop') as stop:
            deploy.stop_original_server(original)
            stop.assert_called_once_with(moved)

    def test_new_termux_python_uses_android_platform(self):
        from types import SimpleNamespace
        with patch.object(deploy.sys, 'platform', 'android'), \
             patch.object(deploy.sys, 'executable', '/data/data/com.termux/files/usr/bin/python3'), \
             patch.object(deploy, 'P', Path('/data/data/com.termux/files/usr')), \
             patch.object(deploy, 'H', Path('/data/data/com.termux/files/home')), \
             patch.object(Path, 'read_text', return_value='TracerPid:\t0\n'), \
             patch.object(deploy.os, 'getuid', return_value=10001), \
             patch.object(Path, 'stat', return_value=SimpleNamespace(st_uid=10001)), \
             patch.object(Path, 'is_file', return_value=True):
            deploy.require_native()

    def test_restore_existing_missing_and_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            journal = root / 'txn'
            journal.mkdir()
            a, b, c = [root / n for n in ['a', 'b', 'c']]
            a.write_bytes(b'original')
            a.chmod(0o640)
            c.symlink_to('a')
            txn = deploy.Transaction(journal)
            for p in [a, b, c]:
                txn.write(p, b'wrapper', 0o700)
            deploy.Transaction(journal).restore_files()
            self.assertEqual(a.read_bytes(), b'original')
            self.assertEqual(a.stat().st_mode & 0o777, 0o640)
            self.assertFalse(b.exists())
            self.assertTrue(c.is_symlink())

    def test_restore_refuses_unrelated_changes(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'a'
            txn = deploy.Transaction(Path(d))
            txn.write(p, b'wrapper', 0o700)
            p.write_bytes(b'user edit')
            with self.assertRaises(deploy.DeployError):
                txn.restore_files()
            self.assertEqual(p.read_bytes(), b'user edit')

    def test_failed_selfcheck_rolls_back(self):
        with tempfile.TemporaryDirectory() as d, patch.object(deploy, 'ROOT', Path(d) / 'absent'), \
             patch.object(deploy, 'preflight', return_value={}), \
             patch.object(deploy, 'cutover', side_effect=deploy.DeployError('self-check')), \
             patch.object(deploy, 'rollback') as rollback, \
             patch.object(deploy, 'record_state') as record:
            self.assertEqual(deploy.apply(), 2)
            rollback.assert_called_once()
            self.assertEqual(record.call_args.args[0], 'rolled-back')

    def test_preflight_skip_never_mutates_transport(self):
        with tempfile.TemporaryDirectory() as d, patch.object(deploy, 'ROOT', Path(d) / 'absent'), \
             patch.object(deploy, 'preflight', side_effect=deploy.DeployError('unsupported')), \
             patch.object(deploy, 'cutover') as cutover, \
             patch.object(deploy, 'record_state') as record:
            self.assertEqual(deploy.apply(), 0)
            cutover.assert_not_called()
            record.assert_called_once_with('skipped', 'unsupported')


if __name__ == '__main__':
    unittest.main()
