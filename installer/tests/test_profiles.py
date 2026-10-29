import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('profiles', Path(__file__).resolve().parents[1] / 'profiles.py')


class ProfilesTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(self.module)
        self.temp = tempfile.TemporaryDirectory(prefix='dsb-t19-profiles-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / 'dsh-plugin'
        self.bundle.mkdir()
        self.profiles = self.root / 'profiles'
        self.web = self.profiles / 'web'
        self.modules = self.web / 'node_modules'
        self.modules.mkdir(parents=True)
        self.manifest = self.web / 'package.json'
        self.original = {'name': 'custom', 'private': True,
                         'dependencies': {'dsh-recap': 'file:untouched'},
                         'dsh': {'profile': {'bundles': ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-web-app', 'dsh-recap']}}}
        self.manifest.write_text(json.dumps(self.original))

    def legacy(self):
        for name in ('dsb-brand', 'dsb-report'):
            target = self.root / 'dsh-web-tweaks' / name
            target.mkdir(parents=True)
            (self.modules / name).symlink_to(target, target_is_directory=True)
            self.original['dsh']['profile']['bundles'].append(name)
        self.manifest.write_text(json.dumps(self.original))

    def test_existing_dependencies_and_modules_survive_idempotent_bundle_link(self):
        sentinel = self.modules / 'dsh-recap'
        sentinel.mkdir()
        (sentinel / 'sentinel').write_text('untouched')
        self.assertTrue(self.module.configure(self.profiles, self.bundle))
        first = self.manifest.read_bytes()
        self.assertFalse(self.module.configure(self.profiles, self.bundle))
        result = json.loads(first)
        self.assertEqual(result['dependencies'], {**self.original['dependencies'], 'dsh-dsb': 'link:' + str(self.bundle.resolve())})
        self.assertEqual(result['dsh']['profile']['bundles'], self.original['dsh']['profile']['bundles'] + ['dsh-dsb'])
        self.assertEqual(first, self.manifest.read_bytes())
        self.assertEqual((sentinel / 'sentinel').read_text(), 'untouched')
        self.assertEqual((self.modules / 'dsh-dsb').resolve(), self.bundle.resolve())
        self.assertEqual(json.loads((self.profiles / 'phone/package.json').read_text())['dsh']['profile']['bundles'],
                         ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-sdk-app'])

    def test_managed_legacy_links_are_replaced_not_reinstalled(self):
        self.legacy()
        self.module.configure(self.profiles, self.bundle)
        bundles = json.loads(self.manifest.read_text())['dsh']['profile']['bundles']
        self.assertEqual(bundles, self.original['dsh']['profile']['bundles'][:-2] + ['dsh-dsb'])
        self.assertFalse(os.path.lexists(self.modules / 'dsb-brand'))
        self.assertFalse(os.path.lexists(self.modules / 'dsb-report'))
        self.assertTrue((self.root / 'dsh-web-tweaks/dsb-report').is_dir())

    def test_verified_patch_changes_also_require_restart(self):
        self.module.configure(self.profiles, self.bundle)
        def verify(*args, **kwargs):
            (self.web / 'cordis.patch.yml').write_text('[]\n')
        with patch.object(self.module.subprocess, 'run', verify):
            self.assertTrue(self.module.configure(self.profiles, self.bundle, ['verify']))
            self.assertFalse(self.module.configure(self.profiles, self.bundle, ['verify']))

    def test_foreign_old_and_new_packages_are_not_replaced(self):
        for name in ('dsb-brand', 'dsb-report', 'dsh-dsb'):
            with self.subTest(name=name):
                foreign = self.modules / name
                foreign.mkdir()
                before = self.manifest.read_bytes()
                with self.assertRaisesRegex(ValueError, name):
                    self.module.configure(self.profiles, self.bundle)
                self.assertEqual(self.manifest.read_bytes(), before)
                foreign.rmdir()

    def test_foreign_symlink_and_unproven_bundle_name_are_rejected(self):
        foreign = self.modules / 'dsb-report'
        foreign.symlink_to(self.root / 'somewhere-else')
        with self.assertRaisesRegex(ValueError, 'dsb-report'):
            self.module.configure(self.profiles, self.bundle)
        foreign.unlink()
        self.original['dsh']['profile']['bundles'].append('dsb-report')
        self.manifest.write_text(json.dumps(self.original))
        with self.assertRaisesRegex(ValueError, 'dsb-report'):
            self.module.configure(self.profiles, self.bundle)

    def assert_rolled_back(self, before):
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertFalse(os.path.lexists(self.modules / 'dsh-dsb'))
        for name in ('dsb-brand', 'dsb-report'):
            self.assertEqual((self.modules / name).resolve(), (self.root / 'dsh-web-tweaks' / name).resolve())
        self.assertEqual(list(self.profiles.rglob('*.dsb-tmp')), [])

    def test_manifest_failure_restores_prior_bytes_links_and_mode(self):
        self.legacy()
        self.manifest.chmod(0o640)
        before = self.manifest.read_bytes()
        real_replace = Path.replace
        def fail_phone(source, target):
            if str(target).endswith('/phone/package.json'):
                raise OSError('synthetic manifest failure')
            return real_replace(source, target)
        with patch.object(Path, 'replace', fail_phone), self.assertRaisesRegex(OSError, 'synthetic'):
            self.module.configure(self.profiles, self.bundle)
        self.assert_rolled_back(before)
        self.assertEqual(self.manifest.stat().st_mode & 0o777, 0o640)

    def test_external_verification_failure_rolls_back_the_whole_migration(self):
        self.legacy()
        before = self.manifest.read_bytes()
        with self.assertRaises(subprocess.CalledProcessError):
            self.module.configure(self.profiles, self.bundle, ['false'])
        self.assert_rolled_back(before)
        self.assertFalse((self.profiles / 'phone/package.json').exists())

    def test_interrupts_restore_bytes_modes_and_links(self):
        self.legacy()
        self.manifest.chmod(0o640)
        before = self.manifest.read_bytes()
        for failure in (KeyboardInterrupt, SystemExit):
            with self.subTest(failure=failure.__name__):
                with patch.object(self.module.subprocess, 'run', side_effect=failure), self.assertRaises(failure):
                    self.module.configure(self.profiles, self.bundle, ['verify'])
                self.assert_rolled_back(before)
                self.assertEqual(self.manifest.stat().st_mode & 0o777, 0o640)

    def test_interrupt_during_legacy_unlink_restores_removed_links(self):
        self.legacy()
        before = self.manifest.read_bytes()
        unlink = Path.unlink
        def interrupt(file, *args, **kwargs):
            if file.name == 'dsb-report':
                raise KeyboardInterrupt
            return unlink(file, *args, **kwargs)
        with patch.object(Path, 'unlink', interrupt), self.assertRaises(KeyboardInterrupt):
            self.module.configure(self.profiles, self.bundle)
        self.assert_rolled_back(before)

    def test_interrupt_after_unlink_restores_link_and_verified_patches(self):
        self.legacy()
        before = self.manifest.read_bytes()
        patch_file = self.web / 'cordis.patch.yml'
        patch_file.write_text('original\n')
        patch_file.chmod(0o640)
        unlink = Path.unlink
        def interrupt(file, *args, **kwargs):
            result = unlink(file, *args, **kwargs)
            if file.name == 'dsb-report':
                raise KeyboardInterrupt
            return result
        def verify(*args, **kwargs):
            patch_file.write_text('changed\n')
            (self.profiles / 'phone/cordis.patch.yml').write_text('created\n')
        with patch.object(Path, 'unlink', interrupt), patch.object(self.module.subprocess, 'run', verify):
            with self.assertRaises(KeyboardInterrupt):
                self.module.configure(self.profiles, self.bundle, ['verify'])
        self.assert_rolled_back(before)
        self.assertEqual(patch_file.read_text(), 'original\n')
        self.assertEqual(patch_file.stat().st_mode & 0o777, 0o640)
        self.assertFalse((self.profiles / 'phone/cordis.patch.yml').exists())

    @unittest.skipUnless(os.environ.get('DSB_DSH_TEST_ROOT'), 'requires the actual locked DSH installation')
    def test_privacy_installer_rejects_missing_root_components_and_assets(self):
        repo = Path(__file__).resolve().parents[2]
        shutil.copytree(repo / 'dsh-plugin', self.bundle, dirs_exist_ok=True)
        profiles = self.root / '.dsh/profiles'
        self.module.configure(profiles, self.bundle)
        env = {**os.environ, 'HOME': str(self.root), 'DSH_HOME': str(self.root / '.dsh'),
               'DSH_PROFILES_ROOT': str(profiles),
               'PATH': str(Path(os.environ['DSB_DSH_TEST_ROOT']) / 'node_modules/.bin') + os.pathsep + os.environ['PATH']}
        env.pop('DEEPSEEK_API_KEY', None)
        for relative in ('index.js', 'dsb-brand/index.js', 'dsb-report/index.js', 'dsb-ui/index.js',
                         'dsb-ui/blocks.json', 'runtime/icon.json'):
            with self.subTest(missing=relative):
                missing = self.bundle / relative
                original = missing.read_bytes()
                missing.unlink()
                try:
                    result = subprocess.run(['bash', str(repo / 'dsh-config/install-profile-patches.sh')],
                                            env=env, capture_output=True, text=True, timeout=45)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('component import/asset validation failed', result.stderr)
                finally:
                    missing.write_bytes(original)

    @unittest.skipUnless(os.environ.get('DSB_DSH_TEST_ROOT'), 'requires the actual locked DSH installation')
    def test_main_privacy_installer_reads_back_all_three_real_profiles(self):
        repo = Path(__file__).resolve().parents[2]
        shutil.copytree(repo / 'dsh-plugin', self.bundle, dirs_exist_ok=True)
        profiles = self.root / '.dsh/profiles'
        self.module.configure(profiles, self.bundle)
        for name in self.module.TEMPLATES:
            shutil.copyfile(repo / 'installer/cordis.patch.yml', profiles / name / 'cordis.patch.yml')
        env = {**os.environ, 'HOME': str(self.root), 'DSH_HOME': str(self.root / '.dsh'),
               'DSH_PROFILES_ROOT': str(profiles),
               'PATH': str(Path(os.environ['DSB_DSH_TEST_ROOT']) / 'node_modules/.bin') + os.pathsep + os.environ['PATH']}
        env.pop('DEEPSEEK_API_KEY', None)
        result = subprocess.run(['bash', str(repo / 'dsh-config/install-profile-patches.sh')],
                                env=env, capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count('enabled=false'), 3)


if __name__ == '__main__':
    unittest.main()
