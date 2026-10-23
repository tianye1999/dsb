"""Run with python3 -S: neither the phone tool nor its tests need site packages."""
import importlib.util
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('privacy_defaults', HERE / 'privacy_defaults.py')
privacy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(privacy)


class PrivacyDefaultsTest(unittest.TestCase):
    def test_all_public_profiles_disable_full_session_upload(self):
        for profile in ('web', 'phone', 'headless'):
            self.assertTrue(privacy.enabled_false((HERE / ('cordis.patch.%s.yml' % profile)).read_text()))

    def test_preserves_comments_fields_unknown_js_and_is_idempotent(self):
        cases = (
            ('# empty\n[] # keep empty comment\n', '# keep empty comment'),
            ('# empty file\n', '# empty file'),
            ('- id: other\n  config: {value: !!js process.env.EXAMPLE}\n', '!!js process.env.EXAMPLE'),
            ('- id: session-log-deepseek # keep id\n  config: {enabled: true, extra: 1} # keep config\n', 'extra: 1'),
            ('- id: "session-log-deepseek"\n  config:\n    extra: 1\n    enabled: true # keep flag\n', '# keep flag'),
            ('- id: session-log-deepseek\n  config:\n    extra: 1 # keep field\n', '# keep field'),
            ('- id: session-log-deepseek # no config\n  name: example\n', 'name: example'),
            ('---\n[]\n...\n', '...'),
            ('- id: other\n  config: {}\n...\n', '...'),
            ("- id: other\n  config:\n    prompt: |\n      Don't rewrite quotes or # comments here.\n", "Don't rewrite"),
        )
        for source, preserved in cases:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / 'patch.yml'
                path.write_text(source)
                privacy.enforce(path)
                first = path.read_text()
                self.assertTrue(privacy.enabled_false(first), first)
                self.assertIn(preserved, first)
                self.assertEqual(first.count('id: session-log-deepseek'), source.count('id: session-log-deepseek') or (0 if '"session-log-deepseek"' in source else 1))
                privacy.enforce(path)
                self.assertEqual(path.read_text(), first)

    def test_false_entry_is_unchanged_and_duplicate_overrides_are_all_closed(self):
        source = '# comment\n- id: session-log-deepseek\n  config: {enabled: false} # keep\n'
        self.assertEqual(privacy.normalize(source), source)
        output = privacy.normalize(source + '- id: session-log-deepseek\n  config: {enabled: true}\n')
        self.assertNotIn('enabled: true', output)
        self.assertTrue(privacy.enabled_false(output))

    def test_verifier_does_not_accept_unrelated_or_quoted_false(self):
        for source in ('- id: other\n  config: {enabled: false}\n',
                       '- id: session-log-deepseek\n  config: {enabled: true}\n- id: other\n  config: {enabled: false}\n',
                       '- id: session-log-deepseek\n  config: {enabled: "false"}\n'):
            self.assertFalse(privacy.enabled_false(source))
        self.assertFalse(privacy.enabled_false('- id: session-log-deepseek\n  config:\n    other: 1\n  unrelated:\n    enabled: false\n'))

    def test_unsupported_shapes_fail_without_writing(self):
        for source in ('{not: an-array}\n', '[{id: other}]\n',
                       '- id: session-log-deepseek\n  config: *alias\n'):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / 'patch.yml'
                path.write_text(source)
                with self.assertRaises(ValueError):
                    privacy.enforce(path)
                self.assertEqual(path.read_text(), source)

    def installer(self, root, fail_profile='', fail_mode=''):
        binary = root / 'bin'
        binary.mkdir(exist_ok=True)
        (binary / 'python3').symlink_to(sys.executable)
        dsh = binary / 'dsh'
        dsh.write_text('#!' + sys.executable + '\n'
                       'import os,pathlib,sys\n'
                       'assert sys.argv[1:2]==["--profile"] and sys.argv[3:]==["--dump-config"]\n'
                       'p=sys.argv[2]\n'
                       'root=pathlib.Path(os.environ["DSH_HOME"])/"profiles"\n'
                       'assert root==pathlib.Path(os.environ["DSH_PROFILES_ROOT"])\n'
                       'with open(os.environ["CALLS"],"a") as f: f.write(p+"\\n")\n'
                       'if p==os.environ.get("FAIL_PROFILE"):\n'
                       '    if os.environ.get("FAIL_MODE")=="wrong":\n'
                       '        print("- id: session-log-deepseek\\n  config: {enabled: true}\\n- id: other\\n  config: {enabled: false}")\n'
                       '        sys.exit(0)\n'
                       '    print("- id: session-log-deepseek\\n  config: {enabled: false}")\n'
                       '    sys.exit(4)\n'
                       'print((root/p/"cordis.patch.yml").read_text())\n')
        dsh.chmod(0o755)
        env = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ['PATH'],
                   DSH_PROFILES_ROOT=str(root / 'profiles'), CALLS=str(root / 'calls'),
                   FAIL_PROFILE=fail_profile, FAIL_MODE=fail_mode)
        return subprocess.run(['bash', str(HERE / 'install-profile-patches.sh')], env=env,
                              capture_output=True, text=True), env

    def test_installer_readback_and_export_without_site_packages(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            web = root / 'profiles/web'
            web.mkdir(parents=True)
            custom = '- id: custom\n  config: {keep: true}\n'
            (web / 'cordis.patch.yml').write_text(custom)
            result, env = self.installer(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / 'calls').read_text().splitlines(), ['web', 'phone', 'headless'])
            self.assertIn(custom, (web / 'cordis.patch.yml').read_text())
            self.assertEqual(len(list(web.glob('cordis.patch.yml.before-privacy-*'))), 1)
            for profile in ('web', 'phone', 'headless'):
                self.assertTrue(privacy.enabled_false((root / 'profiles' / profile / 'cordis.patch.yml').read_text()))
            destination = root / 'export'
            destination.mkdir()
            unsafe = '- id: session-log-deepseek\n  config: {enabled: true}\n'
            (web / 'cordis.patch.yml').write_text(unsafe)
            (destination / 'cordis.patch.web.yml').write_text(unsafe)
            env.update(DSH_CONFIG_SOURCE=str(root), DSH_CONFIG_DEST=str(destination))
            result = subprocess.run(['bash', str(HERE / 'sync-dsh-config.sh')], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(privacy.enabled_false((destination / 'cordis.patch.web.yml').read_text()))
            self.assertEqual((web / 'cordis.patch.yml').read_text(), unsafe)

    def test_install_rolls_back_all_touched_patches_on_bad_dump_or_nonzero_rc(self):
        for mode in ('wrong', 'nonzero'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                web = root / 'profiles/web'
                web.mkdir(parents=True)
                original = '# preserve me\n[]\n'
                (web / 'cordis.patch.yml').write_text(original)
                result, _ = self.installer(root, 'phone', mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((web / 'cordis.patch.yml').read_text(), original)
                self.assertFalse((root / 'profiles/phone/cordis.patch.yml').exists())
                self.assertFalse((root / 'profiles/headless/cordis.patch.yml').exists())
                self.assertEqual((root / 'calls').read_text().splitlines(), ['web', 'phone'])


if __name__ == '__main__':
    unittest.main()
