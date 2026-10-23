"""No DSH service/device needed: apply the real chain to a disposable shell."""
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from patch_privacy_ui import patch

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

    def test_effort_tag_cannot_wrap_inside_a_word_on_a_phone(self):
        source = patch('<html><head></head><body></body></html>')
        effort = re.search(r'\._7KE1Ra_triggerEffort\s*\{([^}]+)\}', source).group(1)
        self.assertIn('white-space:nowrap', effort)
        self.assertIn('flex:0 0 auto', effort)
        self.assertIn('overflow-wrap:normal', effort)

    def test_privacy_patch_is_repeatable_and_scripts_parse_individually(self):
        source = patch('<html><head></head><body></body></html>')
        self.assertEqual(patch(source), source)
        for body in re.findall(r'<script[^>]*>(.*?)</script>', source, re.S):
            result = subprocess.run(['node', '--check'], input=body, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(source.count('id="dsh-privacy-ui-script"'), 1)

    def test_entire_chain_reapply_and_restore_without_historical_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tweaks = root / 'tweaks'
            shutil.copytree(HERE, tweaks, ignore=shutil.ignore_patterns('backups', '__pycache__', 'build'))
            dist = root / 'dist'
            dist.mkdir()
            target = dist / 'index.html'
            target.write_text('<html><head></head><body><div id="root"></div></body></html>')
            env = dict(os.environ, DSH_TWEAKS_DIST=str(dist))
            def apply(*args):
                result = subprocess.run(['bash', str(tweaks / 'apply-all.sh'), *args],
                                        env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            apply()
            first = target.read_text()
            apply()
            self.assertEqual(target.read_text(), first)
            apply('--verify')
            ids = re.findall(r'<(?:script|style) id="([^"]+)"', first)
            self.assertEqual(len(ids), len(set(ids)))
            self.assertIn('dsh-privacy-ui-script', ids)
            self.assertNotIn('content:"Bao"', first)
            rail = re.search(r'<style id="dsh-rail-style">(.*?)</style>', first, re.S).group(1)
            self.assertIn('z-index:100;', rail)
            self.assertIn('z-index:120;', rail)
            self.assertNotIn('2147483000', rail)
            target.write_text('broken')
            apply('--restore')
            self.assertEqual(target.read_text(), first)
            legacy = rail.replace('z-index:100;', 'z-index:2147483000;').replace('z-index:120;', 'z-index:2147483000;')
            legacy += '\n.keep-external-css{color:green}\n'
            target.write_text(first.replace(rail, legacy))
            apply()
            upgraded = target.read_text()
            self.assertIn('.keep-external-css{color:green}', upgraded)
            rail = re.search(r'<style id="dsh-rail-style">(.*?)</style>', upgraded, re.S).group(1)
            self.assertNotIn('2147483000', rail)
            apply()
            self.assertEqual(target.read_text(), upgraded)


if __name__ == '__main__':
    unittest.main()
