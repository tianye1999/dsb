"""Offline contracts for the read-only, per-profile hooks import diagnostic."""

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import check_hooks_import as checker


HOOK_PACKAGE = '@deepseek-ai/dsh-hooks-claude-code'
IMPORT_ERROR = "SyntaxError: The requested module '@deepseek-ai/dsh-hook-protocol' does not provide an export named 'selectHookGroups'"


class HooksImportChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='dsb-hooks-offline-')
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name) / 'profiles'
        self.root.mkdir()

    def profile(self, name, marker='package.json'):
        directory = self.root / name
        directory.mkdir()
        (directory / marker).write_text('{}\n' if marker == 'package.json' else '[]\n')
        return directory

    def success(self, *args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout='ok\n', stderr='')

    def test_each_configured_direct_profile_is_imported_from_its_own_directory(self):
        for name, marker in (('web', 'package.json'), ('phone', 'cordis.patch.yml'),
                             ('headless', 'cordis.patch.yml')):
            self.profile(name, marker)
        ignored = self.root / 'unconfigured'
        (ignored / 'nested').mkdir(parents=True)
        (ignored / 'nested' / 'package.json').write_text('{}\n')
        (self.root / 'not-a-profile').write_text('not a directory\n')
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob('*'))

        with patch.object(checker.subprocess, 'run', side_effect=self.success) as run:
            results = checker.check_profiles(self.root, timeout=2.5)

        self.assertEqual([result['profile'] for result in results], ['headless', 'phone', 'web'])
        self.assertTrue(all(result['ok'] for result in results))
        self.assertTrue(all(not result['error'] for result in results))
        self.assertEqual(run.call_count, 3)
        self.assertEqual([pathlib.Path(call.kwargs['cwd']).name for call in run.call_args_list],
                         ['headless', 'phone', 'web'])
        for call in run.call_args_list:
            argv = call.args[0]
            self.assertIsInstance(argv, (list, tuple))
            self.assertEqual(pathlib.Path(argv[0]).name, 'node')
            self.assertEqual(argv[1], '-e')
            self.assertIn('import(', argv[2])
            self.assertIn(HOOK_PACKAGE, argv[2])
            self.assertFalse(call.kwargs.get('shell', False))
            self.assertEqual(call.kwargs['timeout'], 2.5)
            self.assertTrue(call.kwargs.get('capture_output'))
            self.assertTrue(call.kwargs.get('text'))
        self.assertEqual(before, sorted(str(path.relative_to(self.root)) for path in self.root.rglob('*')))

    def test_default_timeout_is_bounded(self):
        self.profile('web')
        with patch.object(checker.subprocess, 'run', side_effect=self.success) as run:
            checker.check_profiles(self.root)
        self.assertEqual(run.call_args.kwargs['timeout'], 10)

    def test_import_failure_reports_actual_export_error_and_does_not_skip_other_profiles(self):
        self.profile('phone')
        self.profile('web')

        def launch(argv, **kwargs):
            if pathlib.Path(kwargs['cwd']).name == 'web':
                return subprocess.CompletedProcess(argv, 1, stdout='', stderr=IMPORT_ERROR)
            return self.success(argv, **kwargs)

        with patch.object(checker.subprocess, 'run', side_effect=launch) as run:
            results = checker.check_profiles(self.root)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(results[0]['profile'], 'phone')
        self.assertTrue(results[0]['ok'])
        self.assertEqual(results[1]['profile'], 'web')
        self.assertFalse(results[1]['ok'])
        self.assertIn('selectHookGroups', results[1]['error'])

    def test_headless_without_hook_package_is_reported_as_failed_not_silently_skipped(self):
        self.profile('headless', 'cordis.patch.yml')
        error = "Error [ERR_MODULE_NOT_FOUND]: Cannot find package '" + HOOK_PACKAGE + "'"
        with patch.object(checker.subprocess, 'run', return_value=subprocess.CompletedProcess(
                ['node'], 1, stdout='', stderr=error)):
            results = checker.check_profiles(self.root)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['profile'], 'headless')
        self.assertFalse(results[0]['ok'])
        self.assertIn('ERR_MODULE_NOT_FOUND', results[0]['error'])

    def test_missing_node_reports_failure_for_each_profile(self):
        self.profile('phone')
        self.profile('web')
        with patch.object(checker.subprocess, 'run', side_effect=FileNotFoundError('node not found')) as run:
            results = checker.check_profiles(self.root)
        self.assertEqual(run.call_count, 2)
        self.assertEqual([result['profile'] for result in results], ['phone', 'web'])
        self.assertTrue(all(not result['ok'] and result['error'] for result in results))

    def test_timeout_is_reported_and_next_profile_is_still_checked(self):
        self.profile('phone')
        self.profile('web')
        with patch.object(checker.subprocess, 'run', side_effect=[
                subprocess.TimeoutExpired(['node'], 0.1),
                subprocess.CompletedProcess(['node'], 0, stdout='ok\n', stderr='')]) as run:
            results = checker.check_profiles(self.root, timeout=0.1)
        self.assertEqual(run.call_count, 2)
        self.assertFalse(results[0]['ok'])
        self.assertTrue(results[0]['error'])
        self.assertTrue(results[1]['ok'])

    def run_cli(self, failing=False):
        profile = self.profile('web')
        if failing:
            (profile / '.offline-import-fails').touch()
        binary = pathlib.Path(self.temporary.name) / 'bin'
        binary.mkdir()
        node = binary / 'node'
        node.write_text('#!' + sys.executable + '\n'
                        'import pathlib, sys\n'
                        'if (pathlib.Path.cwd() / ".offline-import-fails").exists():\n'
                        '    sys.stderr.write(' + repr(IMPORT_ERROR) + ')\n'
                        '    raise SystemExit(1)\n')
        node.chmod(0o755)
        env = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ['PATH'])
        return subprocess.run([sys.executable, str(pathlib.Path(checker.__file__).resolve()),
                               '--profiles-root', str(self.root)], env=env,
                              capture_output=True, text=True, timeout=10)

    def test_cli_returns_zero_when_each_profile_imports(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('web', result.stdout + result.stderr)

    def test_cli_returns_nonzero_and_names_failed_profile_and_error(self):
        result = self.run_cli(failing=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        output = result.stdout + result.stderr
        self.assertIn('web', output)
        self.assertIn('selectHookGroups', output)


if __name__ == '__main__':
    unittest.main()
