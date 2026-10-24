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
    def load(self):
        module = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(module)
        return module

    def test_existing_self_dependency_and_modules_survive_idempotent_brand_link(self):
        module = self.load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            web = root / 'profiles/web'
            modules = web / 'node_modules'
            modules.mkdir(parents=True)
            sentinel = modules / 'dsh-recap'
            sentinel.mkdir()
            (sentinel / 'sentinel').write_text('untouched')
            original = {'name': 'custom', 'private': True,
                        'dependencies': {'dsh-recap': 'file:' + str(sentinel)},
                        'dsh': {'profile': {'bundles': ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-web-app', 'dsh-recap']}}}
            manifest = web / 'package.json'
            manifest.write_text(json.dumps(original))
            brand = root / 'brand'
            brand.mkdir()
            module.configure(root / 'profiles', brand)
            first = manifest.read_bytes()
            module.configure(root / 'profiles', brand)
            result = json.loads(manifest.read_text())
            self.assertEqual(result['dependencies'], original['dependencies'])
            self.assertEqual(result['dsh']['profile']['bundles'], original['dsh']['profile']['bundles'] + ['dsb-brand'])
            self.assertEqual(first, manifest.read_bytes())
            self.assertEqual((sentinel / 'sentinel').read_text(), 'untouched')
            self.assertEqual((modules / 'dsb-brand').resolve(), brand.resolve())
            self.assertEqual(json.loads((root / 'profiles/phone/package.json').read_text())['dsh']['profile']['bundles'],
                             ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-sdk-app'])

    def test_foreign_brand_install_is_not_replaced(self):
        module = self.load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'profiles/web/node_modules/dsb-brand').mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, 'dsb-brand'):
                module.configure(root / 'profiles', root / 'brand')

    def test_web_report_is_linked_without_touching_existing_profile_modules(self):
        module = self.load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            brand, report = root / 'brand', root / 'report'
            brand.mkdir()
            report.mkdir()
            module.configure(root / 'profiles', brand, report)
            web = root / 'profiles/web'
            self.assertEqual(json.loads((web / 'package.json').read_text())['dsh']['profile']['bundles'][-2:],
                             ['dsb-brand', 'dsb-report'])
            self.assertEqual((web / 'node_modules/dsb-report').resolve(), report.resolve())
            module.configure(root / 'profiles', brand, report)

    def test_manifest_failure_restores_prior_bytes_and_removes_new_links(self):
        module = self.load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            brand, report = root / 'brand', root / 'report'
            brand.mkdir()
            report.mkdir()
            web = root / 'profiles/web'
            web.mkdir(parents=True)
            manifest = web / 'package.json'
            original = b'{"name":"custom","dsh":{"profile":{"bundles":[]}}}\n'
            manifest.write_bytes(original)
            real_replace = Path.replace
            def fail_phone(source, target):
                self.assertTrue((web / 'node_modules/dsb-report').is_symlink())
                if str(target).endswith('/phone/package.json'):
                    raise OSError('synthetic manifest failure')
                return real_replace(source, target)
            with patch.object(Path, 'replace', fail_phone), self.assertRaisesRegex(OSError, 'synthetic'):
                module.configure(root / 'profiles', brand, report)
            self.assertEqual(manifest.read_bytes(), original)
            self.assertFalse((web / 'node_modules/dsb-brand').exists())
            self.assertFalse((web / 'node_modules/dsb-report').exists())
            self.assertEqual(list((root / 'profiles').rglob('*.dsb-tmp')), [])

    @unittest.skipUnless(os.environ.get('DSB_DSH_TEST_ROOT'), 'requires the actual locked DSH installation')
    def test_main_privacy_installer_reads_back_all_three_real_profiles(self):
        module = self.load()
        repo = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            brand = root / 'brand'
            shutil.copytree(repo / 'dsh-web-tweaks/dsb-brand', brand, ignore=shutil.ignore_patterns('build'))
            subprocess.run(['node', str(brand / 'build.mjs'), str(repo / 'app/res/mipmap-xxxhdpi/ic_launcher_foreground.png')],
                           check=True, capture_output=True)
            module.configure(root / '.dsh/profiles', brand, repo / 'dsh-web-tweaks/dsb-report')
            for name in module.TEMPLATES:
                shutil.copyfile(repo / 'installer/cordis.patch.yml', root / '.dsh/profiles' / name / 'cordis.patch.yml')
            env = {**os.environ, 'HOME': str(root), 'DSH_HOME': str(root / '.dsh'),
                   'DSH_PROFILES_ROOT': str(root / '.dsh/profiles'),
                   'PATH': str(Path(os.environ['DSB_DSH_TEST_ROOT']) / 'node_modules/.bin') + os.pathsep + os.environ['PATH']}
            env.pop('DEEPSEEK_API_KEY', None)
            result = subprocess.run(['bash', str(repo / 'dsh-config/install-profile-patches.sh')],
                                    env=env, capture_output=True, text=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.count('enabled=false'), 3)


if __name__ == '__main__':
    unittest.main()
