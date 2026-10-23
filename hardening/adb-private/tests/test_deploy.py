import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from contextlib import contextmanager, redirect_stderr

spec = importlib.util.spec_from_file_location('deploy', Path(__file__).parents[1] / 'deploy.py')
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class TransactionTests(unittest.TestCase):
    def test_rolled_back_after_reboot_allows_empty_default_port_then_archives(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'dsb-adb'
            root.mkdir()
            txn = deploy.Transaction(root)
            txn.data['baseline'] = {'holds': {'termux': {}, 'ubuntu': False}}
            txn.event('rolled-back')
            with patch.object(deploy, 'ROOT', root), \
                 patch.object(deploy, 'SOCKDIR', Path(d) / 'socket'), \
                 patch.object(deploy, 'SOCK', Path(d) / 'socket/server.sock'), \
                 patch.object(deploy.bootstrap.policy, 'probe', return_value='closed'), \
                 patch.object(deploy.bootstrap.policy, 'server_processes', return_value=([], [])), \
                 patch.object(deploy, 'set_holds'), \
                 patch.object(deploy, 'package_holds', return_value={'termux': {}, 'ubuntu': False}), \
                 patch.object(deploy, 'record_state'):
                deploy.finish_rolled_back()
            self.assertFalse(root.exists())
            self.assertEqual(len(list(Path(d).glob('dsb-adb-rollback-*'))), 1)

    def test_rolled_back_unknown_default_port_still_refuses_archive(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'dsb-adb'
            root.mkdir()
            txn = deploy.Transaction(root)
            txn.data['baseline'] = {'holds': {'termux': {}, 'ubuntu': False}}
            txn.event('rolled-back')
            with patch.object(deploy, 'ROOT', root), \
                 patch.object(deploy.bootstrap.policy, 'probe', return_value='unknown'), \
                 patch.object(deploy.bootstrap.policy, 'server_processes', return_value=([], [])), \
                 patch.object(deploy, 'set_holds') as holds, \
                 self.assertRaises(deploy.bootstrap.Conflict):
                deploy.finish_rolled_back()
            self.assertTrue((root / 'journal.json').exists())
            holds.assert_not_called()

    def test_apply_finishes_rolled_back_without_fresh_preflight(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'dsb-adb'
            root.mkdir()
            txn = deploy.Transaction(root)
            txn.data['baseline'] = {'holds': {'termux': {}, 'ubuntu': False}}
            txn.event('rolled-back')
            state = Path(d) / 'state.json'
            with patch.object(deploy, 'ROOT', root), \
                 patch.object(deploy, 'STATE', state), \
                 patch.object(deploy, 'SOCKDIR', Path(d) / 'socket'), \
                 patch.object(deploy, 'SOCK', Path(d) / 'socket/server.sock'), \
                 patch.object(deploy.bootstrap.policy, 'probe', return_value='closed'), \
                 patch.object(deploy.bootstrap.policy, 'server_processes', return_value=([], [])), \
                 patch.object(deploy, 'set_holds'), \
                 patch.object(deploy, 'package_holds', return_value={'termux': {}, 'ubuntu': False}):
                self.assertEqual(deploy.apply(), 3)
            self.assertFalse(root.exists())
            self.assertEqual(json.loads(state.read_text())['status'], 'rolled-back')

    def test_finish_rolled_back_preserves_offline_reconnect_reason(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'dsb-adb'
            root.mkdir()
            txn = deploy.Transaction(root)
            txn.data['baseline'] = {'holds': {'termux': {}, 'ubuntu': False}}
            txn.event('rolled-back')
            state = Path(d) / 'state.json'
            state.write_text(json.dumps({'status': 'rolled-back',
                                         'reason': '设备离线，请打开无线调试并重新连接'}))
            state.chmod(0o600)
            with patch.object(deploy, 'ROOT', root), patch.object(deploy, 'STATE', state), \
                 patch.object(deploy, 'SOCKDIR', Path(d) / 'socket'), \
                 patch.object(deploy, 'SOCK', Path(d) / 'socket/server.sock'), \
                 patch.object(deploy.bootstrap.policy, 'probe', return_value='closed'), \
                 patch.object(deploy.bootstrap.policy, 'server_processes', return_value=([], [])), \
                 patch.object(deploy, 'set_holds'), \
                 patch.object(deploy, 'package_holds', return_value={'termux': {}, 'ubuntu': False}):
                deploy.finish_rolled_back()
            self.assertIn('设备离线', json.loads(state.read_text())['reason'])

    def test_rollback_warns_on_unknown_refresh_init_but_refuses_known_pending_init(self):
        for known in (False, True):
            with self.subTest(known=known), tempfile.TemporaryDirectory() as d:
                root = Path(d) / 'dsb-adb'
                root.mkdir()
                txn = deploy.Transaction(root)
                txn.data['private_config'] = {}
                txn.event('applied')
                init = root / '.refresh-init-fixture'
                init.mkdir(mode=0o700)
                if known:
                    refresh = deploy.Transaction(init)
                    refresh.data['kind'] = 'private-runtime-refresh'
                    refresh.save()
                else:
                    (init / 'owner-data').write_text('do not delete')
                output = io.StringIO()
                with patch.object(deploy, 'ROOT', root), patch.object(deploy, 'require_native'), \
                     patch.object(deploy, 'reject_legacy'), patch.object(deploy, 'rollback_preflight'), \
                     patch.object(deploy, '_rollback') as body, patch.object(deploy, 'SUP', Path(d) / 'absent'), \
                     redirect_stderr(output):
                    if known:
                        with self.assertRaisesRegex(deploy.DeployError, '未完成的私有刷新'):
                            deploy.rollback()
                        body.assert_not_called()
                    else:
                        deploy.rollback()
                        body.assert_called_once()
                self.assertTrue(init.exists())
                if not known:
                    self.assertIn('未知', output.getvalue())

    def test_rollback_warns_on_unknown_refresh_transaction_without_blocking(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'dsb-adb'
            root.mkdir()
            txn = deploy.Transaction(root)
            txn.data['private_config'] = {}
            txn.event('applied')
            unknown = root / 'refresh-transaction'
            unknown.mkdir(mode=0o700)
            (unknown / 'journal.json').write_text('[]')
            output = io.StringIO()
            with patch.object(deploy, 'ROOT', root), patch.object(deploy, 'require_native'), \
                 patch.object(deploy, 'reject_legacy'), patch.object(deploy, 'rollback_preflight'), \
                 patch.object(deploy, '_rollback') as body, patch.object(deploy, 'SUP', Path(d) / 'absent'), \
                 redirect_stderr(output):
                deploy.rollback()
            body.assert_called_once()
            self.assertTrue(unknown.exists())
            self.assertIn('未知刷新残留', output.getvalue())

    def test_cutover_binding_and_local_verification_hold_startup_lock(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'maintenance').write_text('cutover')
            locked = {'value': False}

            @contextmanager
            def startup(_root):
                locked['value'] = True
                try:
                    yield
                finally:
                    locked['value'] = False

            def bind(_config):
                self.assertTrue(locked['value'])
                self.assertFalse((root / 'maintenance').exists())

            def status(_root, *, check_inventory):
                self.assertTrue(locked['value'])
                self.assertFalse(check_inventory)
                return {'ok': True}

            with patch.object(deploy, 'ROOT', root), patch.object(deploy.policy, 'startup_lock', startup), \
                 patch.object(deploy, 'bind_private', side_effect=bind), \
                 patch.object(deploy.policy, '_status_locked', side_effect=status):
                deploy.activate_cutover({})
            self.assertFalse(locked['value'])

    def test_recover_rebuilds_only_matching_missing_launcher(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            config = {'files': {}}
            (root / 'manifest.json').write_text('{"files": {}}')
            txn = deploy.Transaction(root)
            txn.data.update(private_config=config, baseline={'diversions': []},
                            launcher_sha256=deploy.hashlib.sha256(b'launcher').hexdigest())
            txn.save()
            with patch.object(deploy, 'ROOT', root), \
                 patch.object(deploy, 'compile_launcher', return_value=b'launcher'), \
                 patch.object(deploy.policy, 'check_runtime'), patch.object(deploy.policy, 'check_backend'), \
                 patch.object(txn, 'check_restore', side_effect=deploy.DeployError('stop after validation')):
                with self.assertRaisesRegex(deploy.DeployError, 'stop after validation'):
                    deploy._recover_private_locked(txn)
            self.assertEqual((root / 'launcher.bin').read_bytes(), b'launcher')
            (root / 'launcher.bin').unlink()
            with patch.object(deploy, 'ROOT', root), \
                 patch.object(deploy, 'compile_launcher', return_value=b'different'), \
                 patch.object(deploy.policy, 'check_runtime'), patch.object(deploy.policy, 'check_backend'), \
                 self.assertRaisesRegex(deploy.DeployError, '原哈希'):
                deploy._recover_private_locked(txn)
            self.assertFalse((root / 'launcher.bin').exists())

    def test_prepublication_empty_no_journal_and_prepared_stage_with_root_are_cleaned(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'dsb-adb'
            root.mkdir()
            empty = Path(d) / 'dsb-adb-stage-empty'
            empty.mkdir(mode=0o700)
            prepared = Path(d) / 'dsb-adb-stage-prepared'
            prepared.mkdir(mode=0o700)
            deploy.Transaction(prepared).save()
            with patch.object(deploy, 'ROOT', root):
                deploy.cleanup_prepublication_staging()
            self.assertTrue(root.exists())
            self.assertFalse(empty.exists())
            self.assertFalse(prepared.exists())

    def test_unknown_journal_free_stage_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'dsb-adb'
            stage = Path(d) / 'dsb-adb-stage-unknown'
            stage.mkdir(mode=0o700)
            (stage / 'unknown').write_text('owner data')
            with patch.object(deploy, 'ROOT', root), self.assertRaisesRegex(deploy.DeployError, '非空'):
                deploy.cleanup_prepublication_staging()
            self.assertTrue((stage / 'unknown').exists())

    def test_rolled_back_recover_finishes_terminal_archive_without_restoring_entries(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'dsb-adb'
            root.mkdir()
            txn = deploy.Transaction(root)
            txn.data['baseline'] = {'holds': {'termux': {}, 'ubuntu': False}}
            txn.event('rolled-back')
            with patch.object(deploy, 'ROOT', root), patch.object(deploy, 'SOCKDIR', Path(d) / 'socket'), \
                 patch.object(deploy, 'SOCK', Path(d) / 'socket/server.sock'), \
                 patch.object(deploy, 'reject_legacy'), patch.object(deploy, 'require_native'), \
                 patch.object(deploy, 'recover_refresh'), \
                 patch.object(deploy.bootstrap, 'tcp_owner') as owner, \
                 patch.object(deploy, 'set_holds') as holds, \
                 patch.object(deploy, 'package_holds', return_value={'termux': {}, 'ubuntu': False}), \
                 patch.object(deploy, 'record_state') as state, \
                 patch.object(deploy, '_rollback') as body:
                deploy.recover()
                self.assertFalse(root.exists())
                self.assertEqual(len(list(Path(d).glob('dsb-adb-rollback-*'))), 1)
                owner.assert_called_once_with(deploy.NATIVE, allow_empty=True)
                holds.assert_called_once_with({'termux': {}, 'ubuntu': False})
                state.assert_called_once()
                body.assert_not_called()

    def test_terminal_rollback_hold_readback_failure_keeps_recoverable_root(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'dsb-adb'
            root.mkdir()
            txn = deploy.Transaction(root)
            txn.data['baseline'] = {'holds': {'termux': {'android-tools': True}, 'ubuntu': True}}
            txn.event('rolled-back')
            with patch.object(deploy, 'ROOT', root), patch.object(deploy.bootstrap, 'tcp_owner'), \
                 patch.object(deploy, 'set_holds'), \
                 patch.object(deploy, 'package_holds', return_value={'termux': {'android-tools': False}, 'ubuntu': True}), \
                 self.assertRaisesRegex(deploy.DeployError, 'hold 未恢复'):
                deploy.finish_rolled_back()
            self.assertTrue((root / 'journal.json').exists())

    def test_legacy_interrupted_recover_uses_preflighted_ordinary_rollback(self):
        for version in (1, 2):
            for phase in ('prepared', 'entrypoints-installed', 'activating'):
                with self.subTest(version=version, phase=phase), tempfile.TemporaryDirectory() as d:
                    root = Path(d) / 'dsb-adb'
                    root.mkdir()
                    txn = deploy.Transaction(root)
                    txn.data.update(tool_version=version, baseline={'rootfs': d})
                    txn.event(phase)
                    with patch.object(deploy, 'ROOT', root), patch.object(deploy, 'reject_legacy'), \
                         patch.object(deploy, 'require_native'), patch.object(deploy, 'recover_refresh'), \
                         patch.object(deploy, 'rollback_preflight') as preflight, \
                         patch.object(deploy, '_rollback') as ordinary, \
                         patch.object(deploy, '_recover_private_locked') as private, \
                         patch.object(deploy, 'SUP', Path(d) / 'absent'):
                        deploy.recover()
                        preflight.assert_called_once()
                        ordinary.assert_called_once()
                        private.assert_not_called()

    def test_legacy_interrupted_preflight_refusal_preserves_journal(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'dsb-adb'
            root.mkdir()
            txn = deploy.Transaction(root)
            txn.data.update(tool_version=1, baseline={'rootfs': d})
            txn.event('activating')
            before = (root / 'journal.json').read_bytes()
            with patch.object(deploy, 'ROOT', root), patch.object(deploy, 'reject_legacy'), \
                 patch.object(deploy, 'require_native'), patch.object(deploy, 'recover_refresh'), \
                 patch.object(deploy, 'rollback_preflight', side_effect=deploy.DeployError('vendor mismatch')), \
                 patch.object(deploy, '_rollback') as ordinary, \
                 self.assertRaisesRegex(deploy.DeployError, 'vendor mismatch'):
                deploy.recover()
            self.assertEqual((root / 'journal.json').read_bytes(), before)
            ordinary.assert_not_called()

    def test_recover_diversion_after_partial_rollback_restored_vendor_to_canonical_path(self):
        with tempfile.TemporaryDirectory() as d:
            path, vendor = Path(d) / 'adb', Path(d) / 'adb.dsb-vendor'
            path.write_bytes(b'original')
            original = deploy.fingerprint(path)
            deploy.ensure_vendor_backup(path, vendor, original)
            self.assertFalse(path.exists())
            self.assertEqual(deploy.fingerprint(vendor), original)
            deploy.ensure_vendor_backup(path, vendor, original)
            path.write_bytes(b'newer-user-file')
            with self.assertRaisesRegex(deploy.DeployError, 'vendor'):
                vendor.unlink()
                deploy.ensure_vendor_backup(path, vendor, original)

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
             patch.object(deploy, 'rollback_preflight'), \
             patch.object(deploy, '_rollback', side_effect=deploy.DeployError('conflict')), \
             patch.object(deploy, 'record_state'), \
             patch.object(deploy, 'SUP', Path(__file__)), \
             patch.object(deploy, 'start_supervisor') as start:
            txn = deploy.Transaction(d)
            txn.data['private_config'] = {}
            txn.event('applied')
            with self.assertRaises(deploy.DeployError):
                deploy.rollback()
            start.assert_called_once()

    def test_late_rollback_failure_rolls_forward_private_not_failed_state(self):
        with tempfile.TemporaryDirectory() as d, patch.object(deploy, 'ROOT', Path(d)), \
             patch.object(deploy, 'require_native'), patch.object(deploy, 'reject_legacy'), \
             patch.object(deploy, 'rollback_preflight'), \
             patch.object(deploy, '_rollback', side_effect=deploy.DeployError('tcp failed')), \
             patch.object(deploy, '_recover_private_locked', return_value={'ok': True}) as rescue, \
             patch.object(deploy, 'record_state') as record, patch.object(deploy, 'SUP', Path(__file__)), \
             patch.object(deploy, 'start_supervisor'):
            txn = deploy.Transaction(d)
            txn.data['private_config'] = {}
            txn.event('applied')
            with self.assertRaisesRegex(deploy.DeployError, '私有'):
                deploy.rollback()
            rescue.assert_called_once()
            self.assertNotEqual(deploy.Transaction(d).data['state'], 'rollback-failed')
            self.assertNotIn('rollback-failed', [call.args[0] for call in record.call_args_list])

    def test_recover_completes_incomplete_private_transaction_not_tcp(self):
        with tempfile.TemporaryDirectory() as d, patch.object(deploy, 'ROOT', Path(d)), \
             patch.object(deploy, 'recover_private') as private, patch.object(deploy, 'rollback') as rollback:
            txn = deploy.Transaction(Path(d))
            txn.data['private_config'] = {}
            txn.event('entrypoints-installed')
            deploy.recover()
            private.assert_called_once()
            rollback.assert_not_called()
            txn.event('applied')
            deploy.recover()
            rollback.assert_not_called()

    def test_unverified_cutover_cannot_claim_private_or_attempt_tcp(self):
        with tempfile.TemporaryDirectory() as d, patch.object(deploy, 'ROOT', Path(d)), \
             patch.object(deploy, 'require_native'), patch.object(deploy, 'reject_legacy'), \
             patch.object(deploy, 'rollback_preflight') as ordinary:
            deploy.Transaction(d).event('prepared')
            with self.assertRaisesRegex(deploy.DeployError, '私有运行时'):
                deploy.recover_private()
            ordinary.assert_not_called()

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

    def test_prepublication_failure_does_not_try_tcp_rollback(self):
        with tempfile.TemporaryDirectory() as d, patch.object(deploy, 'ROOT', Path(d) / 'absent'), \
             patch.object(deploy, 'preflight', return_value={}), \
             patch.object(deploy, 'cutover', side_effect=deploy.DeployError('self-check')), \
             patch.object(deploy, 'rollback') as rollback, \
             patch.object(deploy, 'record_state') as record:
            with self.assertRaisesRegex(deploy.DeployError, '发布前失败'):
                deploy.apply()
            rollback.assert_not_called()
            record.assert_not_called()

    def test_prepublication_failure_removes_only_owned_prepared_stage(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'private'
            staging = Path(d) / 'dsb-adb-stage-failed'

            def fail_cutover(_original):
                staging.mkdir(mode=0o700)
                deploy.Transaction(staging).save()
                (staging / 'launcher.bin').write_bytes(b'prepared')
                raise deploy.DeployError('before publication')

            with patch.object(deploy, 'ROOT', root), patch.object(deploy, 'preflight', return_value={}), \
                 patch.object(deploy, 'cutover', side_effect=fail_cutover), \
                 patch.object(deploy, 'record_state'):
                with self.assertRaisesRegex(deploy.DeployError, '发布前失败'):
                    deploy.apply()
            self.assertFalse(root.exists())
            self.assertFalse(staging.exists())

    def test_preflight_skip_never_mutates_transport(self):
        with tempfile.TemporaryDirectory() as d, patch.object(deploy, 'ROOT', Path(d) / 'absent'), \
             patch.object(deploy, 'preflight', side_effect=deploy.PreflightSkip('unsupported')), \
             patch.object(deploy, 'cutover') as cutover, \
             patch.object(deploy, 'record_state') as record:
            self.assertEqual(deploy.apply(), 0)
            cutover.assert_not_called()
            record.assert_called_once_with('skipped', 'unsupported')

    def test_missing_native_tool_is_preflight_failure_not_successful_skip(self):
        with tempfile.TemporaryDirectory() as d:
            prefix = Path(d)
            (prefix / 'var/lib/proot-distro/containers/ubuntu/rootfs/etc').mkdir(parents=True)
            (prefix / 'var/lib/proot-distro/containers/ubuntu/rootfs/etc/os-release').write_text('ubuntu')
            with patch.object(deploy, 'P', prefix), patch.object(deploy, 'ROOT', prefix / 'var/lib/dsb-adb'), \
                 patch.object(deploy, 'require_native'), patch.object(deploy, 'trusted_source'), \
                 patch.object(deploy, 'record_state') as state, patch.object(deploy, 'cutover') as cutover:
                with self.assertRaisesRegex(deploy.DeployError, '缺少原生工具'):
                    deploy.apply()
                state.assert_called_once()
                self.assertEqual(state.call_args.args[0], 'preflight-error')
                cutover.assert_not_called()


if __name__ == '__main__':
    unittest.main()
