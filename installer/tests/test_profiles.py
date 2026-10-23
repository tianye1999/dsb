import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

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
            module.configure(root / '.dsh/profiles', brand)
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
