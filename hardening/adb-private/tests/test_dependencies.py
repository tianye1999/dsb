"""Backend ABI lifetime: discover, freeze, diagnose, and restore."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('dependency_deploy', BASE / 'deploy.py')
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)
policy = deploy.policy


class Dependencies(unittest.TestCase):
    def test_real_readelf_closure_copies_a_compiled_library(self):
        if sys.platform not in ['linux', 'android']:
            self.skipTest('ELF host required')
        compiler, readelf = shutil.which('clang') or shutil.which('cc'), shutil.which('readelf')
        if not compiler or not readelf:
            self.skipTest('compiler/readelf unavailable')
        with tempfile.TemporaryDirectory() as d:
            base = Path(d).resolve()
            lib = base / 'lib'
            lib.mkdir()
            root = base / 'private'
            root.mkdir(mode=0o700)
            (base / 'fixture.c').write_text('int dsb_fixture(void) { return 7; }\n')
            (base / 'main.c').write_text('int dsb_fixture(void); int main(void) { return dsb_fixture() != 7; }\n')
            library = lib / 'libdsbfixture.so'
            backend = base / 'adb'
            subprocess.run([compiler, '-shared', '-fPIC', '-nostdlib', str(base / 'fixture.c'),
                            '-o', str(library)], check=True)
            subprocess.run([compiler, str(base / 'main.c'), '-L' + str(lib), '-ldsbfixture',
                            '-Wl,-rpath,$ORIGIN/lib', '-o', str(backend)], check=True)
            owners = {str(backend): 'fixture-backend', str(library): 'fixture-library'}
            versions = {'fixture-backend': '1', 'fixture-library': '1'}
            system = [Path('/lib/x86_64-linux-gnu'), Path('/lib64'), Path('/usr/lib'), *deploy.SYSTEM_DIRS]
            with patch.object(deploy, 'P', Path(readelf).parent.parent), patch.object(deploy, 'SYSTEM_DIRS', system):
                closure = deploy.backend_dependencies(backend, library_dir=lib,
                                                      package_owners=owners, package_versions=versions)
                self.assertIn(str(library), closure['libraries'])
                config = deploy.copy_runtime(root, backend, closure)
            self.assertIn('libdsbfixture.so', config['runtime']['needed']['adb.backend'])
            self.assertEqual((Path(config['runtime']['directory']) / 'lib/libdsbfixture.so').read_bytes(),
                             library.read_bytes())

    def test_new_hold_journal_uses_a_version_old_boolean_only_tools_refuse(self):
        with tempfile.TemporaryDirectory() as d:
            txn = deploy.Transaction(d)
            self.assertEqual(txn.data['tool_version'], 3)

    def test_recursive_closure_includes_transitive_owner_and_resolved_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            prefix = Path(d).resolve()
            (prefix / 'lib').mkdir()
            backend = prefix / 'adb'
            backend.write_bytes(b'backend')
            first = prefix / 'lib/libfirst.so'
            first.write_bytes(b'first')
            second = prefix / 'lib/libsecond.so.1'
            second.write_bytes(b'second')
            (prefix / 'lib/libsecond.so').symlink_to(second.name)
            graph = {str(backend): ['libfirst.so'], str(first): ['libsecond.so'],
                     str(second): ['libfirst.so']}
            def command(args, **kwargs):
                args = list(map(str, args))
                if args[0].endswith('readelf'):
                    return '\n'.join('0 (NEEDED) Shared library: [' + n + ']' for n in graph[args[-1]])
                if '-S' in args:
                    name = ('backend-pkg' if args[-1] == str(backend) else
                            'first-pkg' if args[-1] == str(first) else 'transitive-pkg')
                    if args[-1] == str(backend):
                        return ('local diversion from: ' + args[-1] + '\nlocal diversion to: ' +
                                args[-1] + '.dsb-vendor\n' + name + ': ' + args[-1])
                    return name + ': ' + args[-1]
                return 'installed\t1.0'
            with patch.object(deploy, 'P', prefix), patch.object(deploy, 'run', side_effect=command):
                closure = deploy.backend_dependencies(backend)
            self.assertEqual(set(closure['packages']), {'backend-pkg', 'first-pkg', 'transitive-pkg'})
            self.assertEqual(set(closure['libraries']), {str(first), str(prefix / 'lib/libsecond.so')})
            self.assertEqual(closure['libraries'][str(prefix / 'lib/libsecond.so')]['resolved'], str(second))

            frozen = prefix / 'adb.backend'
            frozen.write_bytes(b'backend')
            graph[str(frozen)] = graph[str(backend)]
            with patch.object(deploy, 'P', prefix), patch.object(deploy, 'run', side_effect=command):
                self.assertEqual(deploy.backend_dependencies(frozen, package_path=backend), closure)

    def test_missing_dependency_and_unowned_library_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / 'lib').mkdir()
            backend = p / 'adb'
            backend.write_bytes(b'elf')
            def command(args, **kwargs):
                if '-S' in args:
                    return 'backend-pkg: ' + str(backend) if args[-1] == backend else ''
                return '0 (NEEDED) Shared library: [missing.so]'
            with patch.object(deploy, 'P', p), patch.object(deploy, 'run', side_effect=command), \
                 self.assertRaisesRegex(deploy.DeployError, '无法解析'):
                deploy.backend_dependencies(backend)
            (p / 'lib/missing.so').write_bytes(b'unowned')
            with patch.object(deploy, 'P', p), patch.object(deploy, 'run', side_effect=command), \
                 self.assertRaisesRegex(deploy.DeployError, '唯一软件包属主'):
                deploy.backend_dependencies(backend)
            (p / 'lib/missing.so').unlink()
            (p / 'lib/missing.so').symlink_to(backend)
            with patch.object(deploy, 'P', p), patch.object(deploy, 'run', side_effect=command), \
                 self.assertRaisesRegex(deploy.DeployError, '逃出'):
                deploy.backend_dependencies(backend)

    def test_system_libraries_are_not_held_and_custom_runpath_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d).resolve()
            (p / 'lib').mkdir()
            backend = p / 'adb'
            backend.write_bytes(b'elf')
            def command(args, **kwargs):
                if '-S' in args:
                    return 'backend-pkg: ' + str(args[-1])
                if '-W' in args:
                    return 'installed\t1'
                return '0 (NEEDED) Shared library: [libc.so]'
            original = Path.is_file
            with patch.object(deploy, 'P', p), patch.object(deploy, 'run', side_effect=command), \
                 patch.object(Path, 'is_file', lambda path: str(path) == '/system/lib64/libc.so' or original(path)):
                closure = deploy.backend_dependencies(backend)
                self.assertEqual(closure, {'libraries': {}, 'packages': {'backend-pkg': '1'}})
            with patch.object(deploy, 'P', p), patch.object(deploy, 'run', side_effect=[
                    'backend-pkg: ' + str(backend),
                    '0 (RUNPATH) Library runpath: [' + str(p / 'lib') + ']\n'
                    '0 (NEEDED) Shared library: [libc.so]',
                    'installed\t1']), \
                 patch.object(Path, 'is_file', lambda path: str(path) == '/system/lib64/libc.so' or original(path)):
                self.assertEqual(deploy.backend_dependencies(backend)['packages'], {'backend-pkg': '1'})
            with patch.object(deploy, 'P', p), patch.object(deploy, 'run', side_effect=[
                    'backend-pkg: ' + str(backend), '0 (RUNPATH) Library runpath: [$ORIGIN/../../escape]']), \
                 self.assertRaisesRegex(deploy.DeployError, 'RUNPATH'):
                deploy.backend_dependencies(backend)
            with patch.object(deploy, 'P', p), patch.object(deploy, 'run', return_value=''), \
                 self.assertRaises(deploy.DeployError):
                deploy.backend_dependencies(backend)

    def test_hold_application_and_restoration_preserve_existing_and_unrelated_holds(self):
        calls = []
        held = {'preexisting', 'unrelated'}
        def command(args, **kwargs):
            args = list(map(str, args))
            calls.append(args)
            if args[-1] == 'showhold':
                return '\n'.join(sorted(held))
            for name in args[2:]:
                if args[1] == 'hold':
                    held.add(name)
                else:
                    held.discard(name)
            return ''
        ubuntu = set()
        def container(args):
            if args[-1] == 'showhold':
                return '\n'.join(ubuntu)
            if args[1] == 'hold':
                ubuntu.add('adb')
            else:
                ubuntu.discard('adb')
            return ''
        with patch.object(deploy, 'run', side_effect=command), patch.object(deploy, 'container', side_effect=container):
            before = deploy.package_holds({'preexisting': '1', 'transitive': '2'})
            deploy.set_holds({'termux': {name: True for name in before['termux']}, 'ubuntu': True})
            self.assertIn('transitive', held)
            deploy.set_holds(before)
            self.assertEqual(held, {'preexisting', 'unrelated'})

    def test_legacy_boolean_hold_journal_can_still_be_restored(self):
        with patch.object(deploy, 'run', return_value=''), patch.object(deploy, 'container', return_value=''):
            deploy.set_holds({'termux': False, 'ubuntu': False})

    def test_unsuccessful_hold_readback_is_a_failure(self):
        with patch.object(deploy, 'run', return_value=''), patch.object(deploy, 'container', return_value=''), \
             self.assertRaisesRegex(deploy.DeployError, '读回'):
            deploy.set_holds({'termux': {'transitive': True}, 'ubuntu': False})

    def test_dependency_hash_missing_file_and_symlink_retarget_are_detected(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'lib.so'
            path.write_bytes(b'v1')
            libraries = {str(path): {'resolved': str(path.resolve()), 'sha256': policy.digest(path)}}
            policy.check_dependencies({'backend_dependencies': {'libraries': libraries, 'packages': {'pkg': '1'}}})
            path.write_bytes(b'v2')
            with self.assertRaisesRegex(policy.PolicyError, 'ADB_BACKEND_DEPENDENCY_DRIFT.*重新加固'):
                policy.check_dependencies({'backend_dependencies': {'libraries': libraries, 'packages': {'pkg': '1'}}})
            path.unlink()
            with self.assertRaises(policy.PolicyError):
                policy.check_dependencies({'backend_dependencies': {'libraries': libraries, 'packages': {'pkg': '1'}}})
            target = Path(d) / 'other.so'
            target.write_bytes(b'v1')
            path.symlink_to(target)
            with self.assertRaises(policy.PolicyError):
                policy.check_dependencies({'backend_dependencies': {'libraries': libraries, 'packages': {'pkg': '1'}}})

    def test_untracked_old_manifest_requires_explicit_rehardening(self):
        with self.assertRaisesRegex(policy.PolicyError, '重新加固'):
            policy.check_dependencies({})

    def test_owner_check_survives_proot_uid_mapping_but_rejects_foreign_owner(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'lib.so'
            path.write_bytes(b'library')
            libraries = {str(path): {'resolved': str(path.resolve()), 'sha256': policy.digest(path),
                                    'uid': os.getuid() + 1000}}
            config = {'backend_dependencies': {'libraries': libraries, 'packages': {'pkg': '1'}}}
            policy.check_dependencies(config)
            with patch.object(policy.os, 'getuid', return_value=path.stat().st_uid + 1), \
                 self.assertRaisesRegex(policy.PolicyError, 'ADB_BACKEND_DEPENDENCY_DRIFT'):
                policy.check_dependencies(config)

    def test_loader_failure_and_timeout_are_actionable_without_server_start(self):
        for effect in [SimpleNamespace(returncode=1), TimeoutError('timeout'),
                       subprocess.TimeoutExpired(['/test/adb', 'version'], 4)]:
            with self.subTest(effect=effect), patch.object(policy, 'client_run') as run:
                if isinstance(effect, Exception):
                    run.side_effect = effect
                else:
                    run.return_value = effect
                with self.assertRaisesRegex(policy.PolicyError, 'ADB_BACKEND_LOAD_FAILED.*重新加固'):
                    policy.check_backend({'backend': '/test/adb'})
                self.assertEqual(run.call_args.args[0], ['/test/adb', 'version'])

    def test_policy_and_doctor_path_reject_drift_before_any_backend_request(self):
        with tempfile.TemporaryDirectory() as d:
            # Linux /tmp is root-owned; the private root must share its parent's owner.
            root = Path(d).resolve() / 'root'
            root.mkdir(mode=0o700)
            library = root / 'lib.so'
            library.write_bytes(b'original')
            config = {'backend': '/not-executed', 'backend_dependencies': {
                'libraries': {str(library): {'resolved': str(library), 'sha256': policy.digest(library)}},
                'packages': {'library-owner': '1'}}}
            (root / 'manifest.json').write_text(json.dumps(config))
            (root / 'manifest.json').chmod(0o600)
            library.write_bytes(b'upgraded')
            with patch.object(policy, 'client_run') as run, patch.object(policy, 'server_processes') as processes:
                report = policy.status(root)
                self.assertFalse(report['ok'])
                self.assertIn('ADB_BACKEND_DEPENDENCY_DRIFT', report['error'])
                with self.assertRaisesRegex(policy.PolicyError, '重新加固'):
                    policy.start(['devices'], root)
                self.assertTrue((root / 'blocked').is_file())
                run.assert_not_called()
                processes.assert_not_called()


if __name__ == '__main__':
    unittest.main()
