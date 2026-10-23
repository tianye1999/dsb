"""审查回归：未知 peer 不接收配对，旧 journal 永不写入。"""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import os
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('security_deploy', BASE / 'deploy.py')
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class ReviewRegressions(unittest.TestCase):
    def test_compiled_launcher_removes_ld_environment_before_python_exec(self):
        compiler = shutil.which('cc')
        if compiler is None:
            self.skipTest('C compiler unavailable')
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / 'capture.py'
            output = root / 'environment.json'
            target.write_text('#!/usr/bin/env python3\nimport json, os\n'
                              'open(os.environ["DSB_TEST_ENV_FILE"], "w").write(json.dumps(dict(os.environ)))\n')
            target.chmod(0o700)
            launcher = root / 'launcher'
            subprocess.run([compiler, '-Wall', '-Wextra', '-Werror',
                            '-DROOT_PATH=' + json.dumps(str(root)),
                            '-DSOCKET_PATH=' + json.dumps(str(root / 'server.sock')),
                            '-DPYTHON=' + json.dumps(str(target)),
                            str(BASE / 'payload/adb_launcher.c'), '-o', str(launcher)], check=True)
            env = dict(os.environ, LD_FAKE='must-not-reach-python', DSB_TEST_ENV_FILE=str(output))
            subprocess.run([str(launcher), 'devices'], check=True, env=env)
            self.assertFalse(any(name.startswith('LD_') for name in json.loads(output.read_text())))

    def test_bootstrap_missing_manifest_fields_refuses_before_any_lock_or_guard_write(self):
        with tempfile.TemporaryDirectory() as d:
            prefix = Path(d)
            root = prefix / 'var/lib/dsb-adb'
            root.mkdir(parents=True)
            (root / 'journal.json').write_text(json.dumps({'schema': deploy.SCHEMA, 'tool_version': 1,
                                                         'state': 'applied'}))
            (root / 'manifest.json').write_text(json.dumps({'schema': deploy.SCHEMA, 'tool_version': 1}))
            before = {str(p): p.read_bytes() for p in prefix.rglob('*') if p.is_file()}
            with patch.object(deploy.bootstrap.subprocess, 'run') as run, \
                 self.assertRaisesRegex(deploy.bootstrap.Conflict, '先运行 bash install.sh rollback'):
                deploy.bootstrap.prepare(prefix)
            self.assertEqual(before, {str(p): p.read_bytes() for p in prefix.rglob('*') if p.is_file()})
            self.assertFalse((prefix / 'var/run').exists())
            run.assert_not_called()

    def test_activated_listener_receipt_requires_pid_start_and_held_inode(self):
        for held in ['socket:[12345]', 'socket:[54321]']:
            with self.subTest(held=held), tempfile.TemporaryDirectory() as d:
                native = Path(d) / 'bin/adb'
                directory = Path(d) / 'var/run'
                directory.mkdir(parents=True)
                receipt = directory / 'dsb-adb-tcp.json'
                receipt.write_text(json.dumps({'schema': 'dsb-tcp-listener/v1', 'address': ['127.0.0.1', 5037],
                                              'pid': 42, 'fd': 4, 'start': '100', 'inode': 12345,
                                              'exe_sha256': 'hash'}))
                receipt.chmod(0o600)
                row = {'pid': 42, 'exe': str(native), 'socket': 'acceptfd:4'}
                with patch.object(deploy.policy, 'probe', side_effect=['listening', 'closed']), \
                     patch.object(deploy.policy, 'server_processes', return_value=([row], [])), \
                     patch.object(deploy.policy, 'digest', return_value='hash'), \
                     patch.object(deploy.bootstrap, 'process_start', return_value='100'), \
                     patch.object(Path, 'iterdir', return_value=iter([Path('/proc/42/fd/8')])), \
                     patch.object(os, 'readlink', return_value=held):
                    if held == 'socket:[12345]':
                        self.assertEqual(deploy.bootstrap.tcp_owner(native), row)
                    else:
                        with self.assertRaises(deploy.bootstrap.Conflict):
                            deploy.bootstrap.tcp_owner(native)

    def test_future_or_missing_staging_version_is_never_deleted(self):
        for version in [None, deploy.TOOL_VERSION + 1]:
            with self.subTest(version=version), tempfile.TemporaryDirectory() as d:
                root = Path(d) / 'dsb-adb'
                staging = Path(d) / 'dsb-adb-stage-other'
                staging.mkdir()
                journal = staging / 'journal.json'
                journal.write_text(json.dumps({'schema': deploy.SCHEMA, 'tool_version': version,
                                               'state': 'prepared', 'files': [], 'diversions': []}))
                before = journal.read_bytes()
                with patch.object(deploy, 'ROOT', root), self.assertRaises(deploy.DeployError):
                    deploy.recover()
                self.assertEqual(journal.read_bytes(), before)

    def test_legacy_cli_rejects_before_state_directory_or_lock_creation(self):
        for operation in ['recover', 'apply', 'refresh', 'rollback', 'resume', 'maintenance']:
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as d:
                base = Path(d)
                root = base / 'deployment'
                root.mkdir()
                for kind in ['journal', 'manifest']:
                    (root / (kind + '.json')).write_bytes((BASE / 'tests/fixtures' / ('legacy-' + kind + '.json')).read_bytes())
                before = {str(p.relative_to(base)): p.read_bytes() for p in base.rglob('*') if p.is_file()}
                with patch.object(deploy, 'ROOT', root), patch.object(deploy, 'STATE', base / 'new-state/install.json'), \
                     patch.object(deploy, 'require_native'), patch.object(deploy.sys, 'argv', ['deploy.py', operation]), \
                     self.assertRaisesRegex(deploy.DeployError, '旧版部署'):
                    deploy.main()
                self.assertFalse((base / 'new-state').exists())
                self.assertEqual(before, {str(p.relative_to(base)): p.read_bytes() for p in base.rglob('*') if p.is_file()})

    def test_matching_native_process_without_listener_receipt_is_conflict(self):
        row = {'pid': 42, 'exe': '/test/adb', 'socket': 'tcp:5037'}
        with patch.object(deploy.bootstrap.policy, 'probe', side_effect=['listening', 'closed']), \
             patch.object(deploy.bootstrap.policy, 'server_processes', return_value=([row], [])), \
             patch.object(deploy.bootstrap.policy, 'digest', return_value='matching'), \
             self.assertRaises(deploy.bootstrap.Conflict):
            deploy.bootstrap.tcp_owner(Path('/test/adb'))

    def test_supervisor_does_not_inherit_install_bootstrap_endpoint(self):
        with tempfile.TemporaryDirectory() as d, patch.object(deploy, 'H', Path(d)), \
             patch.object(deploy, 'supervisors', return_value=[]), \
             patch.dict(deploy.os.environ, {'ADB_SERVER_SOCKET': 'localfilesystem:/install-only',
                                           'ANDROID_ADB_SERVER_ADDRESS': 'untrusted.invalid',
                                           'LD_FAKE': 'must-not-propagate'}), \
             patch.object(deploy.subprocess, 'Popen') as spawn:
            (Path(d) / 'wx').mkdir()
            deploy.start_supervisor()
            self.assertNotIn('ADB_SERVER_SOCKET', spawn.call_args.kwargs['env'])
            self.assertNotIn('ANDROID_ADB_SERVER_ADDRESS', spawn.call_args.kwargs['env'])
            self.assertNotIn('LD_FAKE', spawn.call_args.kwargs['env'])

    def test_hardened_restart_uses_guard_before_reconnect_not_full_offline_inventory(self):
        with tempfile.TemporaryDirectory() as d:
            prefix = Path(d)
            root = prefix / 'var/lib/dsb-adb'
            root.mkdir(parents=True)
            data = {'schema': deploy.SCHEMA, 'tool_version': 2, 'state': 'applied',
                    'target_file': '/test/target', 'container_paths': []}
            for name in ['journal.json', 'manifest.json']:
                (root / name).write_text(json.dumps(data))
            with patch.object(deploy.bootstrap.subprocess, 'run') as run:
                deploy.bootstrap.prepare(prefix)
                self.assertIn(str(root / 'adb_guard.py'), run.call_args.args[0])
                self.assertNotIn('--policy-check', run.call_args.args[0])

    def test_rollback_foreign_listener_never_records_rolled_back(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'txn'
            root.mkdir()
            target = Path(d) / 'target'
            target.write_text('127.0.0.1:45678')
            txn = deploy.Transaction(root)
            txn.data['baseline'] = {'rootfs': d, 'packages': {}, 'holds': {}, 'server': {'pid': 42}}
            txn.data['private_config'] = {}
            txn.event('applied')
            with patch.object(deploy, 'ROOT', root), patch.object(deploy, 'TARGET', target), \
                 patch.object(deploy, 'require_native'), patch.object(deploy, 'packages', return_value={}), \
                 patch.object(deploy, 'supervisors', return_value=[]), \
                 patch.object(deploy, 'start_supervisor'), patch.object(deploy, 'run', return_value=''), \
                 patch.object(deploy.policy, 'server_processes', return_value=([], [])), \
                 patch.object(deploy.policy, 'probe', side_effect=['listening', 'closed']), \
                 patch.object(deploy, 'record_state') as record, \
                 self.assertRaises(deploy.DeployError):
                deploy.rollback()
            self.assertEqual(deploy.Transaction(root).data['state'], 'rollback-failed')
            self.assertEqual(record.call_args.args[0], 'rollback-failed')

    def test_identity_answer_not_sent_and_inventory_is_same_shell(self):
        identity = deploy.adb_identity
        nonce = 'dsb-challenge-' + 'ab' * 32
        expected = (nonce + '\x0010\x00').encode()
        from unittest.mock import Mock
        child = Mock(pid=4242)
        child.poll.return_value = None
        for output, passes in [(b'public-boot-id', False), (expected + b'\nDSB_TCP\ntables', True)]:
            with self.subTest(passes=passes), patch.object(identity.secrets, 'token_hex', return_value='ab' * 32), \
                 patch.object(identity.subprocess, 'Popen', return_value=child), \
                 patch.object(Path, 'read_bytes', return_value=expected), \
                 patch.object(identity.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=output, stderr=b'')) as run:
                if passes:
                    self.assertEqual(identity.challenge_transport(['/test/adb'], 7, inventory=True), 'tables')
                else:
                    with self.assertRaises(identity.IdentityError):
                        identity.challenge_transport(['/test/adb'], 7, inventory=True)
                command = run.call_args.args[0][-1]
                self.assertNotIn(nonce, command)
                self.assertIn('/proc/4242/cmdline', command)
                self.assertIn('/proc/net/tcp /proc/net/tcp6', command)

    def test_both_foreign_loopback_variants_fail_before_any_adb_request(self):
        for probes in [('listening', 'closed'), ('closed', 'listening')]:
            with self.subTest(probes=probes), \
                 patch.object(deploy.bootstrap.policy, 'probe', side_effect=probes), \
                 patch.object(deploy.bootstrap.policy, 'server_processes', return_value=([], [])), \
                 patch.object(deploy.bootstrap.subprocess, 'run') as run, \
                 self.assertRaises(deploy.bootstrap.Conflict):
                deploy.bootstrap.tcp_owner(Path('/bin/adb'), allow_empty=True)
            run.assert_not_called()

    def test_legacy_original_shapes_refuse_all_write_operations_without_mutation(self):
        # 原版 transport_manifest / Transaction 字段，省去设备私有快照和版本号。
        for operation in ['recover', 'apply', 'rollback', 'resume', 'maintenance']:
            for fixture in ['journal', 'manifest']:
                with self.subTest(operation=operation, fixture=fixture), tempfile.TemporaryDirectory() as d:
                    root = Path(d)
                    file = root / (fixture + '.json')
                    file.write_bytes((BASE / 'tests/fixtures' / ('legacy-' + fixture + '.json')).read_bytes())
                    before = {p.name: p.read_bytes() for p in root.iterdir()}
                    with patch.object(deploy, 'ROOT', root), patch.object(deploy, 'require_native'), \
                         patch.object(deploy, 'start_supervisor') as start, \
                         self.assertRaisesRegex(deploy.DeployError, '旧版部署，需 owner 在场迁移'):
                        if operation in ['resume', 'maintenance']:
                            deploy.require_applied()
                        else:
                            getattr(deploy, operation)()
                    self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()})
                    start.assert_not_called()

    def test_unknown_schema_state_not_recovered(self):
        with tempfile.TemporaryDirectory() as d, patch.object(deploy, 'ROOT', Path(d)):
            txn = deploy.Transaction(d)
            txn.event('unknown-future')
            with patch.object(deploy, 'rollback') as rollback, self.assertRaises(deploy.DeployError):
                deploy.recover()
            rollback.assert_not_called()

    def test_resume_requires_applied_journal(self):
        with tempfile.TemporaryDirectory() as d, patch.object(deploy, 'ROOT', Path(d)):
            txn = deploy.Transaction(d)
            txn.event('entrypoints-installed')
            with self.assertRaises(deploy.DeployError):
                deploy.require_applied()


if __name__ == '__main__':
    unittest.main()
