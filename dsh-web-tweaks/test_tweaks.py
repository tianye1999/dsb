"""No DSH service/device needed: apply the real chain to a disposable shell."""
from pathlib import Path
import os
import re
import subprocess
import tempfile
import unittest

import importlib.util

SPEC = importlib.util.spec_from_file_location("restore_index", Path(__file__).with_name("restore-index.py"))
restore_index = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(restore_index)

HERE = Path(__file__).resolve().parent


class TweaksTest(unittest.TestCase):
    def run_reload(self, private=False, selector_ok=True):
        with tempfile.TemporaryDirectory(prefix='dsb-reload-offline-') as temp:
            root = Path(temp)
            home, prefix = root / 'home', root / 'usr'
            (home / 'dsb').mkdir(parents=True)
            (prefix / 'bin').mkdir(parents=True)
            if private:
                (prefix / 'var/lib/dsb-adb').mkdir(parents=True)
            fake = prefix / 'bin/adb'
            fake.write_text('#!/bin/bash\nprintf "<%s>" "$@" >> "$HOME/calls"\n'
                            'printf "\\n" >> "$HOME/calls"\n'
                            'case "$*" in *pidof*) echo 42 ;; esac\n')
            fake.chmod(0o700)
            # Catch the old bare adb path without ever consulting a real ADB server.
            node = prefix / 'bin/node'
            node.write_text('#!/bin/bash\necho node >> "$HOME/calls"\n')
            node.chmod(0o700)
            (home / 'dsb/adb-target.sh').write_text(
                'exit 78\n' if not selector_ok else
                'DSB_ADB_COMMAND=("' + str(fake) + '" "bound executor")\n'
                'DSB_ADB_SELECTOR=(-t 7)\n')
            source = (HERE / 'reload-app-webview.sh').read_text().replace(
                '/data/data/com.termux/files/home', str(home))
            script = root / 'reload.sh'
            script.write_text(source)
            env = dict(os.environ, HOME=str(home), PREFIX=str(prefix),
                       PATH=str(prefix / 'bin') + os.pathsep + os.environ['PATH'])
            result = subprocess.run(['bash', str(script)], env=env,
                                    capture_output=True, text=True, timeout=5)
            calls = home / 'calls'
            return result, calls.read_text().splitlines() if calls.exists() else []

    def test_reload_uses_verified_selector_for_pid_and_forward(self):
        result, calls = self.run_reload()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [
            '<bound executor><-t><7><shell><pidof><com.dsb.assistant:web>',
            '<bound executor><-t><7><forward><tcp:9334><localabstract:webview_devtools_remote_42>',
            'node',
        ])

    def test_reload_refuses_private_adb_before_any_command(self):
        result, calls = self.run_reload(private=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('TCP', result.stderr)
        self.assertIn('ADB', result.stderr)
        self.assertEqual(calls, [])

    def test_reload_denied_selector_never_submits_commands(self):
        result, calls = self.run_reload(selector_ok=False)
        self.assertEqual(result.returncode, 78, result.stderr)
        self.assertEqual(calls, [])

    def test_restore_is_exact_idempotent_and_keeps_backup(self):
        fixtures = HERE.parent / 'dsh-plugin/tests/fixtures'
        clean = fixtures / 'pristine-0.2.1-rc.2.html'
        legacy = (fixtures / 'legacy-0.2.1-rc.2.html').read_bytes()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            index = root / 'index.html'
            index.write_bytes(legacy)
            for asset in re.findall(rb'assets/[\w.-]+\.(?:js|css)', clean.read_bytes()):
                target = root / asset.decode()
                target.parent.mkdir(exist_ok=True)
                target.touch()
            self.assertTrue(restore_index.restore(index, clean))
            self.assertEqual(index.read_bytes(), clean.read_bytes())
            self.assertEqual((root / 'index.html.before-dsh-dsb').read_bytes(), legacy)
            self.assertFalse(restore_index.restore(index, clean))

    def test_restore_refuses_unrecognized_or_wrong_version_shell(self):
        clean = HERE.parent / 'dsh-plugin/tests/fixtures/pristine-0.2.1-rc.2.html'
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'index.html'
            target.write_text('unknown')
            with self.assertRaisesRegex(ValueError, 'marker'):
                restore_index.restore(target, clean)
            target.write_text('<style id="dsh-rail-style"></style>')
            with self.assertRaisesRegex(ValueError, 'asset references'):
                restore_index.restore(target, clean)
            with self.assertRaisesRegex(ValueError, 'pinned'):
                restore_index.restore(target, target)


if __name__ == '__main__':
    unittest.main()
