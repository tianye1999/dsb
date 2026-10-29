import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('plugin_deploy', REPO / 'installer/plugin_deploy.py')
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)
RESTORE_SPEC = importlib.util.spec_from_file_location('restore_index', REPO / 'dsh-web-tweaks/restore-index.py')
restore = importlib.util.module_from_spec(RESTORE_SPEC)
RESTORE_SPEC.loader.exec_module(restore)


class PluginMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='dsb-t19-migration-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.rf = self.root / 'rootfs'
        (self.rf / 'etc').mkdir(parents=True)
        (self.rf / 'etc/os-release').write_text('ID=ubuntu\n')
        (self.rf / 'root').mkdir()
        (self.rf / 'root/dsh-plugin/runtime').mkdir(parents=True)
        shutil.copyfile(REPO / 'dsh-plugin/runtime/pristine-0.2.1-rc.2.html',
                        self.rf / 'root/dsh-plugin/runtime/pristine-0.2.1-rc.2.html')
        self.home = self.root / 'home'
        (self.home / '.dsb-ipc').mkdir(parents=True)
        self.index = self.rf / 'opt/dsb-dsh/node_modules/@deepseek-ai/dsh-web-frontend/dist/index.html'
        self.index.parent.mkdir(parents=True)
        self.clean = (REPO / 'dsh-plugin/runtime/pristine-0.2.1-rc.2.html').read_bytes()
        self.legacy = (REPO / 'dsh-plugin/tests/fixtures/legacy-0.2.1-rc.2.html').read_bytes()
        for asset in re.findall(rb'assets/[\w.-]+\.(?:js|css)', self.clean):
            path = self.index.parent / asset.decode()
            path.parent.mkdir(exist_ok=True)
            path.touch()
        (self.home / 'start-dsh.sh').write_text('echo "restart:$*" >> "$HOME/calls"\n'
                                              'touch "$HOME/restarted"\n'
                                              'echo local-test-token > "$HOME/.dsh-web-token"\n')
        (self.home / 'dsb').mkdir()
        (self.home / 'dsb/ready-probe.sh').write_text('exit "${READY_FAIL:-0}"\n')

    def shell(self, action, extra=''):
        script = '''
source "$REPO/install.sh"
RF="$TEST_RF"
pkill() { echo stop >> "$HOME/calls"; touch "$HOME/stopped"; }
pgrep() {
  if [ -f "$HOME/restarted" ]; then echo 202
  elif [ ! -f "$HOME/stopped" ]; then echo 101
  else return 1; fi
}
in_ubuntu() {
  if [ "$1" = bash ]; then return 0; fi
  if [ "$2" = /root/.dsb-profiles.py ]; then
    echo profiles >> "$HOME/calls"
    if [ "${PROFILE_FAIL:-0}" != 0 ]; then
      mkdir -p "$RF/root/.dsh/profiles/web" "$RF/root/.dsh/profiles/phone"
      echo changed > "$RF/root/.dsh/profiles/web/cordis.patch.yml"
      echo created > "$RF/root/.dsh/profiles/phone/cordis.patch.yml"
      return 1
    fi
    echo "${PROFILE_RESULT:-DSB_PROFILES_CHANGED}"
    return 0
  fi
  echo "$*" >> "$HOME/calls"
  if [ "${RESTORE_FAIL:-0}" = 1 ] && [ "$5" != --check-legacy ]; then return 1; fi
  local args=(--index "$RF$4")
  if [ "$5" = --check-legacy ]; then
    args+=(--check-legacy)
    [ "${CHECK_BANNER:-0}" = 0 ] || echo wrapper-banner
  fi
  python3 "$REPO/dsh-plugin/runtime/restore-index.py" "${args[@]}"
}
''' + extra + '\n' + action
        return subprocess.run(['bash', '-c', script], env={**os.environ, 'REPO': str(REPO),
                              'TEST_RF': str(self.rf), 'HOME': str(self.home)},
                              capture_output=True, text=True, timeout=15)

    def test_legacy_is_stopped_restored_then_profiles_restart(self):
        self.index.write_bytes(self.legacy)
        result = self.shell('restore_legacy_index; setup_profiles; restart_profile_service')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.index.read_bytes(), self.clean)
        self.assertEqual(self.index.with_name('index.html.before-dsh-dsb').read_bytes(), self.legacy)
        calls = (self.home / 'calls').read_text()
        self.assertLess(calls.index('stop'), calls.index('--pristine'))
        self.assertLess(calls.index('--pristine'), calls.index('profiles'))
        self.assertLess(calls.index('profiles'), calls.index('restart:--restart'))
        self.assertFalse((self.home / '.dsb-ipc/maintenance').exists())
        self.assertIn('已受控重启', result.stdout)

    def test_restore_success_then_later_failure_leaves_no_maintenance(self):
        self.index.write_bytes(self.legacy)
        result = self.shell('restore_legacy_index; test ! -e "$HOME/.dsb-ipc/maintenance"; exit 42')
        self.assertEqual(result.returncode, 42, result.stdout + result.stderr)
        self.assertEqual(self.index.read_bytes(), self.clean)
        self.assertFalse((self.home / '.dsb-ipc/maintenance').exists())
        self.assertNotIn('restart', (self.home / 'calls').read_text())

    def test_owned_residual_marker_on_pristine_rerun_requests_restart(self):
        self.index.write_bytes(self.clean)
        marker = self.home / '.dsb-ipc/maintenance'
        marker.write_text('dsb-installer index-restore 1234567890\n')
        result = self.shell('restore_legacy_index; setup_profiles; restart_profile_service',
                            'PROFILE_RESULT=DSB_PROFILES_UNCHANGED')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(self.index.read_bytes(), self.clean)
        self.assertFalse(self.index.with_name('index.html.before-dsh-dsb').exists())
        calls = (self.home / 'calls').read_text()
        self.assertNotIn('stop', calls)
        self.assertEqual(calls.count('restart:--restart'), 1)

    def test_foreign_marker_is_preserved_and_rejected_before_restore(self):
        marker = self.home / '.dsb-ipc/maintenance'
        for content in (self.clean, self.legacy):
            for owner in ('', 'other maintenance\n', 'dsb-installer index-restore invalid\n'):
                with self.subTest(pristine=content == self.clean, owner=owner):
                    self.index.write_bytes(content)
                    marker.write_text(owner)
                    result = self.shell('restore_legacy_index; setup_profiles; restart_profile_service')
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(marker.read_text(), owner)
                    self.assertEqual(self.index.read_bytes(), content)
                    self.assertFalse((self.home / 'calls').exists())

    def test_owned_marker_with_nonpristine_unpatched_index_clears_with_evidence(self):
        marker = self.home / '.dsb-ipc/maintenance'
        owner = 'dsb-installer index-restore 1234567890\n'
        self.index.write_bytes(b'custom unpatched shell')
        marker.write_text(owner)
        result = self.shell('restore_legacy_index')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(self.index.read_bytes(), b'custom unpatched shell')
        evidence = list((self.home / '.dsb-ipc').glob('index-restore-evidence.*'))
        self.assertEqual(len(evidence), 1)
        self.assertIn(owner, evidence[0].read_text())
        self.assertTrue((self.home / '.dsb-ipc/restart-required').is_file())

    def test_clean_and_unknown_indexes_are_untouched_without_stop(self):
        for content in (self.clean, b'custom unpatched shell'):
            self.index.write_bytes(content)
            result = self.shell('restore_legacy_index')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.index.read_bytes(), content)
            self.assertNotIn('stop', (self.home / 'calls').read_text())
            self.assertFalse((self.home / '.dsb-ipc/maintenance').exists())

    def test_restore_failure_keeps_maintenance_and_prevents_profiles(self):
        self.index.write_bytes(self.legacy)
        result = self.shell('restore_legacy_index; setup_profiles; restart_profile_service', 'RESTORE_FAIL=1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.index.read_bytes(), self.legacy)
        self.assertIn('python3 /root/dsh-plugin/runtime/restore-index.py', result.stdout)
        self.assertTrue((self.home / '.dsb-ipc/maintenance').exists())
        self.assertRegex((self.home / '.dsb-ipc/maintenance').read_text(),
                         r'^dsb-installer index-restore [0-9]+\n$')
        self.assertNotIn('profiles', (self.home / 'calls').read_text())

    def test_even_unchanged_profiles_request_a_verified_restart(self):
        for changed in (True, False):
            calls = self.home / 'calls'
            calls.unlink(missing_ok=True)
            (self.home / 'restarted').unlink(missing_ok=True)
            result = self.shell('setup_profiles; restart_profile_service',
                                'PROFILE_RESULT=DSB_PROFILES_' + ('CHANGED' if changed else 'UNCHANGED'))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('restart:--restart', calls.read_text())
            self.assertFalse((self.home / '.dsb-ipc/restart-required').exists())

    def test_interrupted_installation_persists_restart_across_shells(self):
        result = self.shell('setup_profiles; exit 42')
        self.assertEqual(result.returncode, 42)
        self.assertTrue((self.home / '.dsb-ipc/restart-required').is_file())
        result = self.shell('restart_profile_service')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.home / '.dsb-ipc/restart-required').exists())

    def test_restart_exit_zero_with_old_process_or_no_token_is_rejected(self):
        for body in ('echo "DSH_WEB_UP (already)"\n', 'touch "$HOME/restarted"\necho NO_TOKEN_FOUND\n'):
            (self.home / 'start-dsh.sh').write_text(body)
            result = self.shell('request_profile_restart; restart_profile_service')
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((self.home / '.dsb-ipc/restart-required').is_file())
            self.assertNotIn('已受控重启', result.stdout)

    def test_new_process_without_ready_plugins_keeps_restart_request(self):
        result = self.shell('request_profile_restart; restart_profile_service', 'export READY_FAIL=1')
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.home / '.dsb-ipc/restart-required').exists())

    def test_legacy_check_banner_does_not_bypass_restoration(self):
        self.index.write_bytes(self.legacy)
        result = self.shell('restore_legacy_index', 'CHECK_BANNER=1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.index.read_bytes(), self.clean)
        self.assertIn('stop', (self.home / 'calls').read_text())

    def test_owned_marker_and_legacy_index_retry_restoration(self):
        self.index.write_bytes(self.legacy)
        (self.home / '.dsb-ipc/maintenance').write_text('dsb-installer index-restore 123\n')
        result = self.shell('restore_legacy_index')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.index.read_bytes(), self.clean)
        self.assertFalse((self.home / '.dsb-ipc/maintenance').exists())

    def test_shell_failure_restores_manifest_and_cordis_patch(self):
        web = self.rf / 'root/.dsh/profiles/web'
        web.mkdir(parents=True)
        (web / 'package.json').write_text('{"original":true}\n')
        (web / 'cordis.patch.yml').write_text('original patch\n')
        (web / 'cordis.patch.yml').chmod(0o640)
        result = self.shell('setup_profiles', 'PROFILE_FAIL=1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((web / 'package.json').read_text(), '{"original":true}\n')
        self.assertEqual((web / 'cordis.patch.yml').read_text(), 'original patch\n')
        self.assertEqual((web / 'cordis.patch.yml').stat().st_mode & 0o777, 0o640)
        self.assertFalse((web.parent / 'phone/cordis.patch.yml').exists())

    def test_stop_probe_failure_keeps_maintenance_without_restoring(self):
        self.index.write_bytes(self.legacy)
        result = self.shell('restore_legacy_index; setup_profiles', 'pgrep() { return 2; }')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.index.read_bytes(), self.legacy)
        self.assertNotIn('--pristine', (self.home / 'calls').read_text())
        self.assertTrue((self.home / '.dsb-ipc/maintenance').exists())

    def test_restart_failure_cannot_report_success(self):
        (self.home / 'start-dsh.sh').write_text('exit 1\n')
        result = self.shell('setup_profiles; restart_profile_service')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('DSH 重启失败', result.stdout)
        self.assertNotIn('已受控重启', result.stdout)
        self.assertTrue((self.home / '.dsb-ipc/restart-required').read_text().startswith(
            'dsb-installer restart-required '))

    def test_foreign_maintenance_is_not_removed_or_reported_as_restarted(self):
        marker = self.home / '.dsb-ipc/maintenance'
        marker.touch()
        result = self.shell('setup_profiles; restart_profile_service')
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(marker.exists())
        self.assertNotIn('restart', (self.home / 'calls').read_text())

    def test_no_matching_shell_glob_does_not_fail_deployment(self):
        source = (REPO / 'install.sh').read_text()
        loop = re.search(r'  for script in "\$SOURCE"/dsh-web-tweaks/.*?\n  done', source, re.S).group()
        (self.root / 'dsh-web-tweaks').mkdir()
        (self.root / 'dsh-web-tweaks/restore-index.py').touch()
        (self.rf / 'root/dsh-web-tweaks').mkdir()
        result = self.shell(loop, 'SOURCE="' + str(self.root) + '"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.rf / 'root/dsh-web-tweaks/restore-index.py').exists())

    def test_failed_profile_does_not_retire_old_files_or_restart(self):
        old = self.rf / 'root/dsh-web-tweaks/apply-all.sh'
        old.parent.mkdir()
        old.touch()
        result = self.shell('setup_profiles; restart_profile_service', 'PROFILE_FAIL=1')
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(old.exists())
        self.assertNotIn('restart', (self.home / 'calls').read_text())

    def test_deployment_removes_stale_files_and_rejects_unsafe_paths(self):
        target = self.rf / 'root/dsh-plugin'
        shutil.rmtree(target)
        deploy.deploy(self.rf, REPO / 'dsh-plugin')
        (target / 'obsolete.js').touch()
        deploy.deploy(self.rf, REPO / 'dsh-plugin')
        self.assertFalse((target / 'obsolete.js').exists())
        shutil.rmtree(target)
        sentinel = self.root / 'foreign'
        sentinel.mkdir()
        target.symlink_to(sentinel)
        with self.assertRaises(ValueError):
            deploy.deploy(self.rf, REPO / 'dsh-plugin')
        self.assertTrue(sentinel.is_dir())
        with self.assertRaises(ValueError):
            deploy.deploy('/', REPO / 'dsh-plugin')
        target.unlink()
        target.mkdir()
        (target / 'package.json').write_text(json.dumps({'name': 'foreign'}))
        with self.assertRaises(ValueError):
            deploy.deploy(self.rf, REPO / 'dsh-plugin')

    def test_retirement_only_removes_known_installer_scripts(self):
        directory = self.rf / 'root/dsh-web-tweaks'
        directory.mkdir()
        keep = ['restore-index.py', 'reload-app-webview.sh', 'reload-app-webview.mjs', 'patch_custom.py']
        for name in (*deploy.LEGACY_FILES, *keep):
            (directory / name).touch()
        (directory / 'recap').mkdir()
        deploy.retire_legacy(self.rf)
        self.assertEqual(sorted(p.name for p in directory.iterdir()), sorted([*keep, 'recap']))

    def test_incomplete_source_and_copy_failure_preserve_installed_plugin(self):
        target = self.rf / 'root/dsh-plugin'
        shutil.rmtree(target)
        deploy.deploy(self.rf, REPO / 'dsh-plugin')
        before = (target / 'index.js').read_bytes()
        incomplete = self.root / 'incomplete'
        incomplete.mkdir()
        (incomplete / 'package.json').write_text('{"name":"dsh-dsb"}')
        with self.assertRaises(ValueError):
            deploy.deploy(self.rf, incomplete)
        self.assertEqual((target / 'index.js').read_bytes(), before)
        with patch.object(deploy.shutil, 'copytree', side_effect=OSError('copy failed')):
            with self.assertRaises(OSError):
                deploy.deploy(self.rf, REPO / 'dsh-plugin')
        self.assertEqual((target / 'index.js').read_bytes(), before)

    def test_interrupted_publication_preserves_old_tree(self):
        target = self.rf / 'root/dsh-plugin'
        shutil.rmtree(target)
        deploy.deploy(self.rf, REPO / 'dsh-plugin')
        (target / 'sentinel').write_text('old')
        replace = Path.replace
        def interrupt(source, destination):
            if Path(destination) == target and source != target:
                raise KeyboardInterrupt
            return replace(source, destination)
        with patch.object(Path, 'replace', interrupt), self.assertRaises(KeyboardInterrupt):
            deploy.deploy(self.rf, REPO / 'dsh-plugin')
        self.assertEqual((target / 'sentinel').read_text(), 'old')

    def test_post_rename_interrupt_and_interrupted_swap_are_recoverable(self):
        target = self.rf / 'root/dsh-plugin'
        shutil.rmtree(target)
        deploy.deploy(self.rf, REPO / 'dsh-plugin')
        (target / 'sentinel').write_text('old')
        replace = Path.replace
        def interrupt(source, destination):
            result = replace(source, destination)
            if source == target:
                raise KeyboardInterrupt
            return result
        with patch.object(Path, 'replace', interrupt), self.assertRaises(KeyboardInterrupt):
            deploy.deploy(self.rf, REPO / 'dsh-plugin')
        self.assertEqual((target / 'sentinel').read_text(), 'old')
        target.replace(target.parent / '.dsh-plugin.previous')
        deploy.deploy(self.rf, REPO / 'dsh-plugin')
        deploy.validate(target)
        self.assertFalse((target.parent / '.dsh-plugin.previous').exists())

    def test_bad_staging_copy_is_not_published(self):
        target = self.rf / 'root/dsh-plugin'
        shutil.rmtree(target)
        deploy.deploy(self.rf, REPO / 'dsh-plugin')
        (target / 'sentinel').write_text('old')
        copy = shutil.copytree
        def truncated(source, destination, *args, **kwargs):
            result = copy(source, destination, *args, **kwargs)
            if Path(source) == REPO / 'dsh-plugin':
                (Path(destination) / 'index.js').unlink()
            return result
        with patch.object(deploy.shutil, 'copytree', truncated), self.assertRaises(ValueError):
            deploy.deploy(self.rf, REPO / 'dsh-plugin')
        self.assertEqual((target / 'sentinel').read_text(), 'old')

    def test_restore_retries_after_backup_and_replace_failure_with_stale_temp(self):
        self.index.write_bytes(self.legacy)
        with patch.object(Path, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                restore.restore(self.index)
        backup = self.index.with_name('index.html.before-dsh-dsb')
        self.assertEqual(backup.read_bytes(), self.legacy)
        temporary = self.index.with_name('index.html.dsh-dsb-tmp')
        temporary.write_text('partial write from terminated process')
        self.assertTrue(restore.restore(self.index))
        self.assertEqual(self.index.read_bytes(), self.clean)
        self.assertEqual(backup.read_bytes(), self.legacy)
        self.assertFalse(temporary.exists())
        temporary.write_text('stale after completed rename')
        self.assertFalse(restore.restore(self.index))
        self.assertFalse(temporary.exists())

    def test_old_keep_and_dark_markers_are_detected(self):
        for marker in ('dsh-tobottom-keep', 'dsh-tobottom-keep-script', 'dsh-dark-enforce'):
            self.index.write_text('<script id="' + marker + '"></script>')
            self.assertTrue(restore.has_legacy(self.index))

    def test_installer_orders_restore_before_profiles_and_restart_before_boot(self):
        source = (REPO / 'install.sh').read_text()
        sequence = source[source.index('  rmdir "$PACKAGE_ADB_DIRECTORY"'):]
        self.assertLess(sequence.index('restore_legacy_index'), sequence.index('setup_profiles'))
        self.assertLess(sequence.index('restart_profile_service'), sequence.index('bash "$HOME/dsb/boot.sh"'))
        deployment = source[source.index('deploy_files()'):source.index('rollback_profile_files()')]
        self.assertLess(deployment.index('request_profile_restart'), deployment.index('plugin_deploy.py'))


if __name__ == '__main__':
    unittest.main()
