"""Private generations must survive public upgrades without loader fallback."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('runtime_deploy', BASE / 'deploy.py')
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)
import adb_client
import adb_policy


class PrivateRuntime(unittest.TestCase):
    def test_v2_backend_and_orphan_backup_are_transactionally_retired(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            backend = root / 'adb.backend'
            backup = root / 'backup-0'
            backend.write_bytes(b'old backend')
            backup.write_bytes(b'old backend')
            journal = deploy.Transaction(root)
            journal.data['files'][str(backend)] = {'backup': str(backup)}
            refresh_dir = root / 'refresh-transaction'
            refresh_dir.mkdir()
            txn = deploy.Transaction(refresh_dir)
            config = {'files': {str(backend): 'old'}, 'modes': {str(backend): 0o700}}
            with patch.object(deploy, 'ROOT', root):
                deploy.retire_legacy_backend(txn, journal, config)
            self.assertFalse(backend.exists())
            self.assertFalse(backup.exists())
            self.assertNotIn(str(backend), journal.data['files'])
            self.assertNotIn(str(backend), config['files'])
            txn.restore_files()
            self.assertEqual(backend.read_bytes(), b'old backend')
            self.assertEqual(backup.read_bytes(), b'old backend')

    def test_refresh_5037_conflict_before_generating_or_stopping_server(self):
        with patch.object(deploy, 'require_applied'), patch.object(deploy, 'trusted_source'), \
             patch.object(deploy, 'recover_refresh'), \
             patch.object(deploy.policy, 'require_closed_default', side_effect=deploy.policy.PolicyError('5037 occupied')), \
             patch.object(deploy, 'repository_runtime') as generation, \
             patch.object(deploy, 'stop_private') as stop:
            with self.assertRaisesRegex(deploy.policy.PolicyError, '5037 occupied'):
                deploy.refresh()
            generation.assert_not_called()
            stop.assert_not_called()

    def test_interrupted_cutover_after_public_upgrade_completes_private(self):
        for phase in ['prepared', 'entrypoints-installed', 'activating']:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as d:
                root = Path(d).resolve() / 'private'
                root.mkdir(mode=0o700)
                config = self.bundle(root)
                native = root.parent / 'bin/adb'
                native.parent.mkdir()
                native.write_bytes(b'ordinary')
                native.chmod(0o700)
                vendor = native.with_name('adb.dsb-vendor')
                rf = root.parent / 'rf'
                entry = rf / 'usr/local/bin/adb'
                entry.parent.mkdir(parents=True)
                launcher = b'known-launcher'
                (root / 'launcher.bin').write_bytes(launcher)
                core = root / 'adb_policy.py'
                core.write_bytes(b'known-policy')
                core.chmod(0o600)
                row = {'side': 'termux', 'path': str(native), 'destination': str(vendor)}
                config.update(schema=deploy.SCHEMA, tool_version=deploy.TOOL_VERSION,
                              socket=str(root / 'sock/server.sock'), vendor_hashes={str(vendor): deploy.policy.digest(native)},
                              files={str(native): deploy.policy.digest(root / 'launcher.bin'),
                                     str(entry): deploy.policy.digest(root / 'launcher.bin'),
                                     str(core): deploy.policy.digest(core)},
                              modes={str(native): 0o700, str(entry): 0o700, str(core): 0o600})
                (root / 'manifest.json').write_text(json.dumps(config))
                txn = deploy.Transaction(root)
                txn.data.update(baseline={'rootfs': str(rf), 'holds': {'termux': {'android-tools': False}},
                                          'diversions': [row], 'server': {'pid': 42, 'start': '1'},
                                          'server_sha256': 'original'}, private_config=config,
                                launcher_sha256=deploy.policy.digest(root / 'launcher.bin'),
                                original_files={str(native): deploy.fingerprint(native),
                                                str(entry): deploy.fingerprint(entry)})
                txn.save()
                if phase != 'prepared':
                    txn.remember(native)
                    txn.remember(entry)
                    native.rename(vendor)
                    txn.data['diversions'].append(row)
                    txn.write(native, launcher, 0o700)
                    txn.write(entry, launcher, 0o700)
                    txn.event(phase)
                (root / 'public/libsample.so.1').write_bytes(b'public-upgraded')
                def diversion(_row, operation, _native=native, _vendor=vendor):
                    if operation == '--add':
                        _native.rename(_vendor)
                with patch.object(deploy, 'ROOT', root), patch.object(deploy, 'SOCKDIR', root / 'sock'), \
                     patch.object(deploy, 'SUP', root / 'absent'), patch.object(deploy, 'require_native'), \
                     patch.object(deploy, 'reject_legacy'), patch.object(deploy, 'set_holds'), \
                     patch.object(deploy, 'truename', side_effect=lambda _, _native=native, _vendor=vendor:
                                  str(_vendor) if _vendor.exists() else str(_native)), \
                     patch.object(deploy, 'divert', side_effect=diversion), \
                     patch.object(deploy.policy, 'check_backend'), \
                     patch.object(deploy.policy, 'server_processes', return_value=([], [])), \
                     patch.object(deploy, 'wait_tcp_closed'), \
                     patch.object(deploy, 'bind_private', return_value={'ok': True}) as private, \
                     patch.object(deploy, 'record_state'), patch.object(deploy, 'rollback') as tcp:
                    deploy.recover_private()
                private.assert_called_once()
                self.assertEqual(private.call_args.args[0]['backend'], config['backend'])
                self.assertFalse(private.call_args.kwargs['require_device'])
                tcp.assert_not_called()
                self.assertEqual(native.read_bytes(), launcher)
                self.assertEqual(deploy.Transaction(root).data['state'], 'applied')

    def test_ubuntu_vendor_link_is_resolved_inside_rootfs(self):
        with tempfile.TemporaryDirectory() as d:
            rf = Path(d)
            (rf / 'usr/bin').mkdir(parents=True)
            (rf / 'usr/lib').mkdir()
            (rf / 'usr/lib/adb').write_bytes(b'elf')
            link = rf / 'usr/bin/adb.dsb-vendor'
            link.symlink_to('/usr/lib/adb')
            self.assertEqual(deploy.rootfs_link_target(link, rf), '/usr/lib/adb')
            link.unlink()
            link.symlink_to('../../../outside')
            with self.assertRaises(deploy.DeployError):
                deploy.rootfs_link_target(link, rf)

    def test_sealed_runtime_uses_metadata_fast_path_but_audit_hashes(self):
        with tempfile.TemporaryDirectory() as d:
            config = self.bundle(Path(d).resolve())
            with patch.object(adb_client, 'file_hash', side_effect=AssertionError('unexpected hash')):
                adb_client.check_runtime(config)
            with patch.object(adb_client, 'file_hash', wraps=adb_client.file_hash) as hashed:
                adb_client.check_runtime(config, full=True)
            self.assertGreaterEqual(hashed.call_count, 2)
            backend = Path(config['backend'])
            os.utime(backend, None)
            with patch.object(adb_client, 'file_hash', side_effect=AssertionError('metadata path must not hash')):
                with self.assertRaisesRegex(OSError, 'DRIFT'):
                    adb_client.check_runtime(config)

    def test_managed_file_metadata_mismatch_rejects_without_hash(self):
        with tempfile.TemporaryDirectory() as d:
            # Linux /tmp is root-owned; the private root must share its parent's owner.
            root = Path(d).resolve() / 'root'
            root.mkdir(mode=0o700)
            managed = root / 'managed.py'
            managed.write_bytes(b'policy')
            managed.chmod(0o600)
            socket = root / 'socket'
            socket.mkdir(mode=0o700)
            config = {'files': {str(managed): 'unused'}, 'modes': {str(managed): 0o600},
                      'vendor_hashes': {}, 'file_metadata': {str(managed): {'inode': -1}},
                      'socket': str(socket / 'adb.sock')}
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps(config))
            manifest.chmod(0o600)
            with patch.object(adb_policy, 'digest', side_effect=AssertionError('metadata path must not hash')):
                with self.assertRaisesRegex(adb_policy.PolicyError, 'metadata changed'):
                    adb_policy.load(root, verify_runtime=False)

    def test_staged_generation_metadata_survives_atomic_root_publication(self):
        with tempfile.TemporaryDirectory() as d:
            stage = Path(d).resolve() / 'stage'
            stage.mkdir(mode=0o700)
            config = self.bundle(stage)
            root = stage.with_name('published')
            stage.rename(root)
            config['deployment_root'] = str(root)
            config['backend'] = config['backend'].replace(str(stage), str(root), 1)
            config['runtime']['directory'] = config['runtime']['directory'].replace(str(stage), str(root), 1)
            adb_client.check_runtime(config)

    def test_one_locked_operation_reuses_runtime_verification(self):
        with tempfile.TemporaryDirectory() as d:
            config = self.bundle(Path(d).resolve())
            with patch.object(adb_client, 'file_metadata', wraps=adb_client.file_metadata) as metadata:
                with adb_client.runtime_verification_scope():
                    adb_client.check_runtime(config)
                    first = metadata.call_count
                    adb_client.check_runtime(config)
                    self.assertEqual(metadata.call_count, first)
                adb_client.check_runtime(config)
                self.assertGreater(metadata.call_count, first)

    def test_policy_fast_load_does_not_probe_backend_version(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps({'socket': str(root / 'server.sock'), 'files': {}, 'modes': {}}))
            manifest.chmod(0o600)
            root.chmod(0o700)
            with patch.object(adb_policy, 'check_dependencies'), \
                 patch.object(adb_policy, 'check_backend') as version, \
                 patch.object(adb_policy, 'private_dir'):
                adb_policy.load(root)
            version.assert_not_called()

    def test_installer_refreshes_existing_private_runtime_before_bootstrap_guard(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'usr/var/lib/dsb-adb'
            root.mkdir(parents=True)
            (root / 'journal.json').write_text('{}')
            script = '''
source "$REPO/install.sh"
touch "$LOG"
python3() {
  [[ "$*" == *"deploy.py apply"* ]] || { command python3 "$@"; return; }
  echo apply >> "$HOME/steps"
  return 0
}
setup_adb() { echo setup >> "$HOME/steps"; }
harden_adb
'''
            env = dict(HOME=d, PREFIX=str(Path(d) / 'usr'), PATH=os.environ['PATH'],
                       REPO=str(BASE.parents[1]))
            result = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((Path(d) / 'steps').read_text().splitlines(), ['apply', 'setup'])

    def test_refresh_doctor_source_is_inside_the_same_trust_checks(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d).resolve()
            source = home / 'repo/hardening/adb-private'
            source.mkdir(parents=True)
            endpoint = home / 'repo/agent/adb_endpoint.py'
            endpoint.parent.mkdir()
            endpoint.write_text('# trusted\n')
            with patch.object(deploy, 'H', home), patch.object(deploy, 'STAGE', source):
                deploy.trusted_source()
                endpoint.chmod(0o666)
                with self.assertRaises(deploy.DeployError):
                    deploy.trusted_source()

    def test_shared_hold_cleanup_is_one_time_and_later_user_hold_is_not_owned(self):
        baseline = {'termux': {'android-tools': False, 'public-lib': False, 'user-lib': True}, 'ubuntu': False}
        legacy = {'tool_version': 2}
        with patch.object(deploy, 'package_holds', return_value={'termux': {
                'android-tools': True, 'public-lib': True, 'user-lib': True}, 'ubuntu': True}):
            self.assertEqual(deploy.refresh_holds(legacy, baseline),
                             {'termux': {'android-tools': True, 'public-lib': False, 'user-lib': True}, 'ubuntu': True})
        current = {'tool_version': 3, 'runtime': {'directory': '/private'}}
        self.assertEqual(deploy.refresh_holds(current, baseline),
                         {'termux': {'android-tools': True}, 'ubuntu': True})
        journal = {'baseline': {'holds': baseline}}
        deploy.retire_dependency_holds(journal)
        self.assertEqual(journal['baseline']['holds'], {'termux': {'android-tools': False}, 'ubuntu': False})
        self.assertEqual(journal['legacy_dependency_holds'], baseline)
        # Refresh and ordinary rollback only touch the narrowed owned keys.
        with patch.object(deploy, 'run') as run, patch.object(deploy, 'container'), \
             patch.object(deploy, 'package_holds', return_value=journal['baseline']['holds']):
            deploy.set_holds(journal['baseline']['holds'])
        self.assertNotIn('public-lib', str(run.call_args_list))
        self.assertNotIn('user-lib', str(run.call_args_list))

    def test_pending_refresh_refuses_ordinary_rollback_before_any_mutation(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            refresh_root = root / 'refresh-transaction'
            refresh_root.mkdir(mode=0o700)
            refresh = deploy.Transaction(refresh_root)
            refresh.data['kind'] = 'private-runtime-refresh'
            refresh.save()
            with patch.object(deploy, 'ROOT', root), patch.object(deploy, 'require_native'), \
                 patch.object(deploy, 'reject_legacy'), patch.object(deploy, 'rollback_preflight') as probe, \
                 patch.object(deploy, '_rollback') as mutate, \
                 self.assertRaisesRegex(deploy.DeployError, 'ADB_ROLLBACK_REFUSED'):
                deploy.rollback()
            probe.assert_not_called()
            mutate.assert_not_called()
            self.assertFalse((root / 'journal.json').exists())

    def test_refresh_initialization_publishes_complete_journal_or_nothing(self):
        with tempfile.TemporaryDirectory() as d, patch.object(deploy, 'ROOT', Path(d)):
            root = Path(d)
            with patch.object(deploy, 'atomic', side_effect=OSError('disk full')), self.assertRaises(OSError):
                deploy.prepare_refresh({'old_config': {}, 'new_config': {}})
            self.assertEqual(list(root.iterdir()), [])
            # Crash before journal publication leaves only an inert staging dir.
            inert = root / '.refresh-init-crashed'
            inert.mkdir(mode=0o700)
            (inert / '.dsb-partial').write_bytes(b'{')
            (inert / '.dsb-partial').chmod(0o600)
            deploy.recover_refresh()
            self.assertEqual(list(root.iterdir()), [])
            txn = deploy.prepare_refresh({'old_config': {}, 'new_config': {}})
            self.assertEqual(txn.directory, root / 'refresh-transaction')
            self.assertEqual(json.loads(txn.file.read_text())['kind'], 'private-runtime-refresh')
            self.assertEqual(txn.data['state'], 'prepared')

    def test_failed_refresh_removes_unused_generation_and_bounds_history(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            new = root / 'runtime-new'
            new.mkdir()
            (new / 'adb.backend').write_bytes(b'failed generation')
            for index in range(6):
                history = root / ('refresh-history-' + str(index))
                history.mkdir()
                (history / 'journal.json').write_text(json.dumps({
                    'kind': 'private-runtime-refresh', 'tool_version': deploy.TOOL_VERSION}))
            active = root / 'refresh-transaction'
            active.mkdir()
            txn = deploy.Transaction(active)
            txn.data.update(kind='private-runtime-refresh', new_config={'runtime': {'directory': str(new)}})
            txn.event('restored-private')
            with patch.object(deploy, 'ROOT', root), \
                 patch.object(deploy.policy, 'server_processes', return_value=([], [])):
                deploy.finish_refresh(txn)
            self.assertFalse(new.exists())
            self.assertFalse(active.exists())
            self.assertEqual(len(list(root.glob('refresh-history-*'))), 5)

    def test_prepared_refresh_cleans_recorded_candidate_but_keeps_unknown_history(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            candidate = root / 'runtime-candidate'
            candidate.mkdir()
            (candidate / 'adb.backend').write_bytes(b'unpublished')
            unknown = root / 'refresh-history-unknown'
            unknown.mkdir()
            (unknown / 'owner-note').write_text('leave me')
            for index in range(6):
                old = root / f'refresh-history-{index}'
                old.mkdir()
                (old / 'journal.json').write_text(json.dumps({
                    'kind': 'private-runtime-refresh', 'tool_version': deploy.TOOL_VERSION}))
            active = root / 'refresh-transaction'
            active.mkdir()
            txn = deploy.Transaction(active)
            txn.data.update(kind='private-runtime-refresh', candidate_runtime=str(candidate))
            txn.event('prepared')
            with patch.object(deploy, 'ROOT', root), \
                 patch.object(deploy.policy, 'server_processes', return_value=([], [])):
                deploy.finish_refresh(txn)
            self.assertFalse(candidate.exists())
            self.assertTrue((unknown / 'owner-note').exists())

    def test_unknown_refresh_initialization_warns_without_blocking_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            unknown = root / '.refresh-init-unknown'
            unknown.mkdir(mode=0o700)
            (unknown / 'owner-note').write_text('leave me')
            with patch.object(deploy, 'ROOT', root):
                deploy.recover_refresh()
            self.assertEqual((unknown / 'owner-note').read_text(), 'leave me')

    def bundle(self, root):
        source = root / 'public'
        source.mkdir()
        backend = source / 'adb'
        backend.write_bytes(b'backend')
        library = source / 'libsample.so.1'
        library.write_bytes(b'library')
        alias = source / 'libsample.so'
        alias.symlink_to(library.name)
        dependencies = {'packages': {'android-tools': '1', 'sample': '2'}, 'libraries': {
            str(alias): {'resolved': str(library), 'sha256': deploy.policy.digest(library),
                         'mode': 0o600, 'uid': os.getuid()}}}
        with patch.object(deploy, 'elf_needed', side_effect=lambda p: ['libsample.so'] if p.name == 'adb.backend' else []):
            return deploy.copy_runtime(root, backend, dependencies)

    def test_copy_is_independent_and_public_upgrade_does_not_drift(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            config = self.bundle(root)
            adb_client.check_runtime(config)
            (root / 'public/libsample.so.1').write_bytes(b'upgraded')
            adb_client.check_runtime(config)
            self.assertEqual(Path(config['runtime']['directory']).stat().st_mode & 0o777, 0o700)
            self.assertEqual(config['runtime']['size_bytes'], len(b'library'))
            self.assertEqual(os.readlink(Path(config['runtime']['directory']) / 'lib/libsample.so'), 'libsample.so.1')

    def test_missing_retargeted_tampered_and_bad_mode_fail_before_exec(self):
        for change in ['missing', 'retarget', 'content', 'mode', 'extra']:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as d:
                config = self.bundle(Path(d).resolve())
                lib = Path(config['runtime']['directory']) / 'lib'
                if change == 'missing':
                    (lib / 'libsample.so.1').unlink()
                elif change == 'retarget':
                    (lib / 'libsample.so').unlink()
                    (lib / 'libsample.so').symlink_to('/public/libsample.so')
                elif change == 'content':
                    (lib / 'libsample.so.1').write_bytes(b'bad')
                elif change == 'mode':
                    (lib / 'libsample.so.1').chmod(0o666)
                else:
                    (lib / 'surprise.so').write_bytes(b'extra')
                with patch.object(adb_client.subprocess, 'run') as run, self.assertRaises(OSError):
                    adb_client.client_run([config['backend'], 'version'], runtime=config)
                run.assert_not_called()

    def test_only_backend_gets_fixed_private_environment_even_after_resanitizing(self):
        with tempfile.TemporaryDirectory() as d:
            config = self.bundle(Path(d).resolve())
            caller = {'LD_PRELOAD': 'evil', 'LD_LIBRARY_PATH': 'evil', 'LD_DEBUG': 'all', 'HOME': '/home'}
            with patch.object(adb_client.subprocess, 'run') as run:
                adb_client.client_run([config['backend'], 'version'], env=caller, runtime=config)
            env = run.call_args.kwargs['env']
            self.assertEqual(env, {'HOME': '/home', 'LD_LIBRARY_PATH': config['runtime']['directory'] + '/lib'})
            self.assertEqual(adb_client.backend_env(env), {'HOME': '/home'})
            self.assertEqual(caller['LD_LIBRARY_PATH'], 'evil')

    def test_environment_validates_once_after_resolving_active_generation(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            config = self.bundle(root)
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps(config))
            manifest.chmod(0o600)
            with patch.object(adb_client, 'check_runtime', wraps=adb_client.check_runtime) as check:
                adb_client.backend_env(backend=config['backend'])
            check.assert_called_once_with(config)

    def test_direct_identity_caller_resolves_active_manifest_not_arbitrary_generation(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            config = self.bundle(root)
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps(config))
            manifest.chmod(0o600)
            env = adb_client.backend_env(backend=config['backend'])
            self.assertEqual(env['LD_LIBRARY_PATH'], config['runtime']['directory'] + '/lib')
            config['backend'] = '/wrong/backend'
            manifest.write_text(json.dumps(config))
            with self.assertRaises(OSError):
                adb_client.backend_env(backend=str(Path(env['LD_LIBRARY_PATH']).parent / 'adb.backend'))

    def test_actual_maps_reject_public_deleted_or_replaced_inodes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            config = self.bundle(root)
            proc = root / 'proc/42'
            proc.mkdir(parents=True)
            files = [Path(config['backend']), Path(config['runtime']['directory']) / 'lib/libsample.so.1']
            def mapping(path):
                s = path.stat()
                return f'1000-2000 r-xp 00000000 {os.major(s.st_dev):x}:{os.minor(s.st_dev):x} {s.st_ino} {path}\n'
            good = ''.join(mapping(p) for p in files)
            (proc / 'maps').write_text(good)
            adb_client.check_runtime_maps(42, config, root / 'proc')
            for bad in [good.replace(str(files[1]), str(root / 'public/libsample.so.1')),
                        good.replace(str(files[1]), str(files[1]) + ' (deleted)'),
                        good.replace(str(files[1].stat().st_ino) + ' ', '999999 '), mapping(files[0])]:
                (proc / 'maps').write_text(bad)
                with self.assertRaises(OSError):
                    adb_client.check_runtime_maps(42, config, root / 'proc')

    def test_sealed_maps_reject_unexpected_system_executable(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            config = self.bundle(root)
            proc = root / 'proc/42'
            proc.mkdir(parents=True)
            files = [Path(config['backend']), Path(config['runtime']['directory']) / 'lib/libsample.so.1']
            lines = []
            for path in files:
                info = path.stat()
                lines.append(f'1000-2000 r-xp 0000 {os.major(info.st_dev):x}:{os.minor(info.st_dev):x} {info.st_ino} {path}\n')
            config['runtime']['system_mappings'] = {'stale-boot-entry': {'dev': [1, 1], 'inode': 123}}
            (proc / 'maps').write_text(''.join(lines) + '3000-4000 r-xp 0000 1:1 123 /system/lib64/unexpected.so\n')
            with self.assertRaisesRegex(OSError, '系统映射'):
                adb_client.check_runtime_maps(42, config, root / 'proc')

    def test_system_mappings_follow_current_boot_not_manifest_inode(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            config = self.bundle(root)
            system = root / 'system/lib64/libsystem.so'
            system.parent.mkdir(parents=True)
            system.write_bytes(b'system')
            system.chmod(0o444)
            config['runtime']['system_libraries'] = {'libsystem.so': str(system)}
            config['runtime']['system_mappings'] = {str(system): {'dev': [9, 9], 'inode': 1}}
            proc = root / 'proc/42'
            proc.mkdir(parents=True)
            private = [Path(config['backend']), Path(config['runtime']['directory']) / 'lib/libsample.so.1']
            original_stat = Path.stat
            current = {'uid': 0, 'dev': system.stat().st_dev, 'inode': system.stat().st_ino, 'mode': system.stat().st_mode}

            def system_stat(path, *args, **kwargs):
                info = original_stat(path, *args, **kwargs)
                if str(path) == str(system):
                    return SimpleNamespace(st_uid=current['uid'], st_dev=current['dev'],
                                           st_ino=current['inode'], st_mode=current['mode'])
                return info

            def maps():
                lines = []
                for path in private:
                    info = original_stat(path)
                    lines.append(f'1000-2000 r-xp 0000 {os.major(info.st_dev):x}:{os.minor(info.st_dev):x} {info.st_ino} {path}\n')
                lines.append(f'3000-4000 r-xp 0000 {os.major(current["dev"]):x}:{os.minor(current["dev"]):x} {current["inode"]} {system}\n')
                return ''.join(lines)

            with patch.object(adb_client, 'SYSTEM_ROOTS', (str(system.parent) + '/',)), \
                 patch.object(Path, 'stat', system_stat):
                (proc / 'maps').write_text(maps())
                adb_client.check_runtime_maps(42, config, root / 'proc')
                self.assertIn(str(system), adb_client.capture_system_maps(42, config, root / 'proc'))
                current['dev'] += 1
                current['inode'] += 1
                with self.assertRaisesRegex(OSError, '系统映射'):
                    adb_client.check_runtime_maps(42, config, root / 'proc')
                (proc / 'maps').write_text(maps())
                adb_client.check_runtime_maps(42, config, root / 'proc')
                current['uid'] = 1
                with self.assertRaisesRegex(OSError, '系统映射'):
                    adb_client.check_runtime_maps(42, config, root / 'proc')
                current['uid'] = 0
                current['mode'] |= 0o020
                with self.assertRaisesRegex(OSError, '系统映射'):
                    adb_client.check_runtime_maps(42, config, root / 'proc')

    def test_apex_versioned_symlink_change_keeps_soname_requirement(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            config = self.bundle(root)
            apex = root / 'apex'
            v1 = apex / 'runtime@1/lib64/libsystem.so'
            v2 = apex / 'runtime@2/lib64/libsystem.so'
            for path in (v1, v2):
                path.parent.mkdir(parents=True)
                path.write_bytes(b'system')
                path.chmod(0o444)
            alias = apex / 'runtime'
            alias.symlink_to(v1.parent.parent, target_is_directory=True)
            config['runtime']['system_libraries'] = {'libsystem.so': str(v1)}  # existing manifest
            proc = root / 'proc/42'
            proc.mkdir(parents=True)
            private = [Path(config['backend']), Path(config['runtime']['directory']) / 'lib/libsample.so.1']
            original_stat = Path.stat

            def system_stat(path, *args, **kwargs):
                info = original_stat(path, *args, **kwargs)
                if str(path).startswith(str(apex)):
                    return SimpleNamespace(st_uid=0, st_dev=info.st_dev, st_ino=info.st_ino, st_mode=info.st_mode)
                return info

            def mapping(path):
                info = original_stat(path)
                return (f'1000-2000 r-xp 0000 {os.major(info.st_dev):x}:'
                        f'{os.minor(info.st_dev):x} {info.st_ino} {path}\n')

            with patch.object(adb_client, 'SYSTEM_ROOTS', (str(apex) + '/',)), \
                 patch.object(Path, 'stat', system_stat):
                (proc / 'maps').write_text(''.join(mapping(p) for p in private) + mapping(alias / 'lib64/libsystem.so'))
                adb_client.check_runtime_maps(42, config, root / 'proc')
                alias.unlink()
                alias.symlink_to(v2.parent.parent, target_is_directory=True)
                v1.unlink()
                (proc / 'maps').write_text(''.join(mapping(p) for p in private) + mapping(alias / 'lib64/libsystem.so'))
                adb_client.check_runtime_maps(42, config, root / 'proc')

    def test_android_apex_system_uid_is_allowed_only_for_apex(self):
        info = SimpleNamespace(st_uid=1000, st_dev=os.makedev(7, 88), st_ino=24,
                               st_mode=stat.S_IFREG | 0o644)
        apex = '/apex/com.android.runtime/lib64/bionic/libc.so'
        with patch.object(Path, 'resolve', lambda path, **_kw: path), \
             patch.object(Path, 'stat', return_value=info):
            fields = ['0-1', 'r-xp', '0', '7:58', '24', apex]
            self.assertEqual(adb_client._check_system_mapping(fields), apex)
            fields[5] = '/system/lib64/libc.so'
            with self.assertRaisesRegex(OSError, '系统映射'):
                adb_client._check_system_mapping(fields)
            info.st_uid = 0
            self.assertEqual(adb_client._check_system_mapping(fields), fields[5])
            info.st_uid = 1001
            fields[5] = apex
            with self.assertRaisesRegex(OSError, '系统映射'):
                adb_client._check_system_mapping(fields)

    def test_rollback_probe_failure_preserves_applied_private_state(self):
        with tempfile.TemporaryDirectory() as d, patch.object(deploy, 'ROOT', Path(d)), \
             patch.object(deploy, 'require_native'), patch.object(deploy, 'reject_legacy'), \
             patch.object(deploy, 'rollback_preflight', side_effect=deploy.DeployError('普通 ADB 不可加载')), \
             patch.object(deploy, '_rollback') as mutate, patch.object(deploy, 'start_supervisor'):
            txn = deploy.Transaction(d)
            txn.event('applied')
            before = txn.file.read_bytes()
            with self.assertRaisesRegex(deploy.DeployError, '不可加载'):
                deploy.rollback()
            self.assertEqual(txn.file.read_bytes(), before)
            mutate.assert_not_called()

    def test_refresh_crash_at_each_publication_boundary_restores_private_not_tcp(self):
        for phase in ['prepared', 'old-server-stopped', 'manifest-published']:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as d:
                root = Path(d).resolve()
                old = {'backend': '/old/private/adb.backend'}
                new = {'backend': '/new/private/adb.backend'}
                manifest = root / 'manifest.json'
                manifest.write_text(json.dumps(old))
                journal = root / 'journal.json'
                journal.write_text('{"state":"applied"}')
                txn = deploy.Transaction(root / 'refresh-transaction')
                txn.directory.mkdir(mode=0o700)
                txn.data.update(kind='private-runtime-refresh', old_config=old, new_config=new,
                                holds={'termux': {'android-tools': True, 'user-held': True}, 'ubuntu': True})
                txn.save()
                txn.write(root / 'maintenance', b'refresh', 0o600)
                txn.write(journal, b'{"state":"applied","tool_version":3}', 0o600)
                txn.event('activating')
                if phase == 'manifest-published':
                    txn.write(manifest, json.dumps(new).encode(), 0o600)
                    (root / 'maintenance').unlink()
                with patch.object(deploy, 'ROOT', root), patch.object(deploy, 'stop_private') as stop, \
                     patch.object(deploy, 'bind_private', return_value={'ok': True}) as bind, \
                     patch.object(deploy, 'set_holds') as holds, patch.object(deploy, '_rollback') as tcp:
                    deploy.recover_refresh()
                stop.assert_called_once_with(new if phase == 'manifest-published' else old, require_runtime=False)
                bind.assert_called_once_with(old, require_device=False)
                holds.assert_called_once_with(txn.data['holds'])
                tcp.assert_not_called()
                self.assertEqual(json.loads(manifest.read_text()), old)
                self.assertEqual(journal.read_text(), '{"state":"applied"}')
                self.assertFalse((root / 'maintenance').exists())
                self.assertFalse(txn.directory.exists())

    def test_pre_stop_busy_recovery_requires_startup_lock(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            txn = deploy.Transaction(root / 'refresh-transaction')
            txn.directory.mkdir(mode=0o700)
            txn.data.update(kind='private-runtime-refresh')
            txn.write(root / 'maintenance', b'refresh', 0o600)
            txn.event('quiescing')
            with patch.object(deploy, 'ROOT', root), patch.object(deploy, 'SUP', root / 'absent'), \
                 patch.object(deploy.policy, 'startup_lock', side_effect=TimeoutError('busy')), \
                 patch.object(deploy, 'stop_private') as stop, patch.object(deploy, 'bind_private') as bind:
                with self.assertRaisesRegex(TimeoutError, 'busy'):
                    deploy.recover_refresh()
            self.assertTrue((root / 'maintenance').exists())
            stop.assert_not_called()
            bind.assert_not_called()

    def test_committed_generation_keeps_new_private_and_only_retries_hold_cleanup(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            txn = deploy.Transaction(root / 'refresh-transaction')
            txn.directory.mkdir(mode=0o700)
            desired = {'termux': {'android-tools': True, 'public-lib': False}, 'ubuntu': True}
            txn.data.update(kind='private-runtime-refresh', desired_holds=desired,
                            holds={'termux': {'public-lib': True}, 'ubuntu': True})
            txn.event('committed')
            with patch.object(deploy, 'ROOT', root), patch.object(deploy, 'SUP', root / 'absent'), \
                 patch.object(deploy, 'restore_refresh') as restore, patch.object(deploy, 'set_holds') as holds:
                deploy.recover_refresh()
            restore.assert_not_called()
            holds.assert_called_once_with(desired)

    def test_old_private_recovery_does_not_require_online_device(self):
        with tempfile.TemporaryDirectory() as d, patch.object(deploy, 'ROOT', Path(d)), \
             patch.object(deploy, 'TARGET', Path(d) / 'target'), \
             patch.object(deploy.policy, 'ensure_server_locked') as ensure, \
             patch.object(deploy.policy, '_status_locked', return_value={'ok': True}) as status, \
             patch.object(deploy.adb_identity, 'connect_verified') as connect:
            (Path(d) / 'adb-transport.json').write_text('stale binding')
            deploy.bind_private({'backend': '/old'}, require_device=False)
            ensure.assert_called_once()
            connect.assert_not_called()
            status.assert_called_once_with(Path(d), check_inventory=False)
            self.assertEqual((Path(d) / 'adb-transport.json').read_text(), 'stale binding')

    def test_failed_new_runtime_can_be_stopped_by_pidfd_without_healthy_maps(self):
        row = {'pid': 42, 'exe': '/private/adb.backend', 'socket': 'localfilesystem:/private/socket'}
        c = {'backend': row['exe'], 'socket': '/private/socket', 'runtime': {'backend_sha256': 'trusted'}}
        for digest in ['trusted', 'different']:
            with self.subTest(digest=digest), patch.object(deploy.policy, 'server_processes', return_value=([row], [])), \
                 patch.object(deploy, 'proc_identity', side_effect=[row, row, None]), \
                 patch.object(deploy.policy, 'digest', return_value=digest), \
                 patch.object(deploy.policy, 'check_runtime_maps', side_effect=OSError('bad maps')) as maps, \
                 patch.object(deploy.adb_guard, 'open_pidfd', return_value=99), \
                 patch.object(deploy.adb_guard, 'send_pidfd') as signal, patch.object(deploy.os, 'close'):
                if digest == 'trusted':
                    deploy.stop_private(c, require_runtime=False)
                    signal.assert_called_once_with(99)
                else:
                    with self.assertRaises(deploy.DeployError):
                        deploy.stop_private(c, require_runtime=False)
                    signal.assert_not_called()
                maps.assert_not_called()

    def test_copy_rejects_new_needed_not_present_in_the_verified_system(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            backend = root / 'adb'
            backend.write_bytes(b'elf')
            with patch.object(deploy, 'elf_needed', return_value=['not-a-system-library.so']), \
                 self.assertRaisesRegex(deploy.DeployError, '拒绝公共库回落'):
                deploy.copy_runtime(root, backend, {'libraries': {}, 'packages': {'android-tools': '1'}})

    def test_ordinary_probe_checks_server_not_just_version_and_reaps_failed_child(self):
        with tempfile.TemporaryDirectory() as d, patch.object(deploy, 'P', Path(d)), \
             patch.object(deploy.subprocess, 'Popen') as spawn:
            (Path(d) / 'tmp').mkdir()
            child = spawn.return_value
            child.poll.return_value = 1
            with self.assertRaisesRegex(OSError, 'cannot start'):
                deploy.probe_ordinary_server('/vendor/adb')
            self.assertEqual(spawn.call_args.args[0][-2:], ['server', 'nodaemon'])
            self.assertTrue(spawn.call_args.args[0][2].startswith('localfilesystem:'))
            child.wait.assert_called_once()
            child.terminate.assert_not_called()

    def test_successful_ordinary_probe_closes_only_its_child_and_never_inherits_private_path(self):
        with tempfile.TemporaryDirectory() as d, patch.object(deploy, 'P', Path(d)), \
             patch.object(deploy.subprocess, 'Popen') as spawn, \
             patch.object(deploy.policy, 'socket_info'), patch.object(deploy.policy, 'host_version', return_value='0029'), \
             patch.dict(os.environ, {'LD_LIBRARY_PATH': '/private/old/lib', 'ADB_SERVER_SOCKET': 'tcp:localhost:5037'}):
            (Path(d) / 'tmp').mkdir()
            child = spawn.return_value
            child.poll.return_value = None
            deploy.probe_ordinary_server('/vendor/adb')
            env = spawn.call_args.kwargs['env']
            self.assertNotIn('LD_LIBRARY_PATH', env)
            self.assertNotIn('ADB_SERVER_SOCKET', env)
            child.terminate.assert_called_once()
            child.wait.assert_called_once()

    def test_vendor_version_success_but_server_start_failure_is_zero_mutation_refusal(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            native, vendor = root / 'adb', root / 'adb.dsb-vendor'
            native.write_bytes(b'original')
            native.chmod(0o700)
            vendor.write_bytes(native.read_bytes())
            vendor.chmod(0o700)
            txn = deploy.Transaction(root)
            txn.write(native, b'launcher', 0o700)
            txn.data['baseline'] = {'rootfs': str(root / 'rf'), 'packages': {}, 'diversions': [
                {'side': 'termux', 'path': str(native), 'destination': str(vendor)}]}
            txn.event('applied')
            before = txn.file.read_bytes()
            with patch.object(deploy, 'ROOT', root), patch.object(deploy, 'require_native'), \
                 patch.object(deploy, 'packages', return_value={}), \
                 patch.object(deploy.policy, 'client_run', return_value=SimpleNamespace(returncode=0)), \
                 patch.object(deploy, 'probe_ordinary_server', side_effect=OSError('server failed')) as probe, \
                 patch.object(deploy, '_rollback') as mutate, patch.object(deploy, 'start_supervisor') as start, \
                 self.assertRaisesRegex(deploy.DeployError, 'ADB_ROLLBACK_REFUSED'):
                deploy.rollback()
            probe.assert_called_once_with(vendor)
            mutate.assert_not_called()
            start.assert_not_called()
            self.assertEqual(txn.file.read_bytes(), before)
            self.assertEqual(native.read_bytes(), b'launcher')
            self.assertFalse((root / 'maintenance').exists())


if __name__ == '__main__':
    unittest.main()
