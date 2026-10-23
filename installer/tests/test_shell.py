import os
import fcntl
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]


class InstallerShellTests(unittest.TestCase):
    def test_overlay_must_be_confirmed_before_enabling_accessibility(self):
        with tempfile.TemporaryDirectory() as d:
            script = '''
source "$REPO/install.sh"
touch "$LOG"
open_setting() { printf 'OPEN:%s\n' "$1" >> "$HOME/steps"; }
pause() { :; }
device_adb() {
  echo CHECK_OVERLAY >> "$HOME/steps"
  if [ -f "$HOME/checked" ]; then echo 'SYSTEM_ALERT_WINDOW: allow'
  else touch "$HOME/checked"; echo 'SYSTEM_ALERT_WINDOW: default'; fi
}
guide_permissions
'''
            env = dict(HOME=d, PATH=os.environ['PATH'], REPO=str(REPO))
            result = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            steps = (Path(d) / 'steps').read_text().splitlines()
            checks = [i for i, value in enumerate(steps) if value == 'CHECK_OVERLAY']
            self.assertEqual(len(checks), 2)
            accessibility = 'OPEN:android.settings.ACCESSIBILITY_SETTINGS'
            self.assertIn(accessibility, steps[checks[0] + 1:checks[1]])
            self.assertIn(accessibility, steps[checks[1] + 1:])

    def device_dispatch_fixture(self, directory, private, receipt=True):
        home = Path(directory)
        prefix = home / 'usr'
        (prefix / 'bin').mkdir(parents=True)
        native = prefix / 'bin/python3'
        native.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$HOME/native-calls"\n')
        native.chmod(0o700)
        (home / 'dsb').mkdir(mode=0o700)
        if private:
            managed = prefix / 'var/lib/dsb-adb'
            managed.mkdir(parents=True)
            (managed / 'manifest.json').write_text('{}\n')
        if receipt:
            target = '127.0.0.1:45555'
            descriptor = {'pid': 4242, 'start': '101', 'socket': str(home / 'relay.sock'),
                          'state_file': str(home / 'relay.json'), 'target': target,
                          'script': str(home / 'dsb/adb_relay.py')}
            path = home / 'dsb/adb-transport.json'
            path.write_text(json.dumps({'version': 2, 'target': target,
                                        'serial': 'localfilesystem:' + descriptor['socket'],
                                        'transport_id': 7, 'generation': {'pid': 1, 'start': '42'},
                                        'relay': descriptor}))
            path.chmod(0o600)
        return dict(HOME=directory, PREFIX=str(prefix), PATH=os.environ['PATH'], REPO=str(REPO))

    def test_device_adb_private_policy_selects_recorded_transport_id_without_serial(self):
        with tempfile.TemporaryDirectory() as d:
            env = self.device_dispatch_fixture(d, private=True)
            script = '''
source "$REPO/install.sh"
touch "$LOG"
TARGET=127.0.0.1:45555
adb() { printf '%s\n' "$@" > "$HOME/adb-calls"; }
device_adb shell printf 'value with spaces'
'''
            result = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            calls = (Path(d) / 'adb-calls').read_text().splitlines()
            self.assertEqual(calls, ['-t', '7', 'shell', 'printf', 'value with spaces'])
            self.assertNotIn('-s', calls)
            self.assertFalse((Path(d) / 'native-calls').exists())

    def test_device_adb_ordinary_mode_uses_native_isolated_bound_executor(self):
        with tempfile.TemporaryDirectory() as d:
            env = self.device_dispatch_fixture(d, private=False)
            script = '''
source "$REPO/install.sh"
touch "$LOG"
TARGET=127.0.0.1:45555
adb() { printf '%s\n' "$@" > "$HOME/adb-calls"; }
device_adb shell printf 'value with spaces'
'''
            result = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            calls = (Path(d) / 'native-calls').read_text().splitlines()
            self.assertEqual(calls, ['-I', str(Path(d) / 'dsb/adb_identity.py'), '--execute',
                                     '-t', '7', 'shell', 'printf', 'value with spaces'])
            self.assertNotIn('-s', calls)
            self.assertFalse((Path(d) / 'adb-calls').exists())

    def test_device_adb_missing_receipt_never_submits_private_or_ordinary_command(self):
        for private in [True, False]:
            with self.subTest(private_policy=private), tempfile.TemporaryDirectory() as d:
                env = self.device_dispatch_fixture(d, private=private, receipt=False)
                script = '''
source "$REPO/install.sh"
touch "$LOG"
TARGET=127.0.0.1:45555
adb() { printf '%s\n' "$@" > "$HOME/adb-calls"; }
device_adb shell id -u
'''
                result = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((Path(d) / 'adb-calls').exists())
                self.assertFalse((Path(d) / 'native-calls').exists())

    def test_first_install_defers_services_but_not_adb_supervision(self):
        for pending in (True, False):
            with self.subTest(pending=pending), tempfile.TemporaryDirectory() as d:
                home = Path(d)
                (home / 'usr/bin').mkdir(parents=True)
                (home / 'usr/bin/bash').symlink_to('/bin/bash')
                (home / 'dsb').mkdir()
                for name in ('dsh-up', 'ensure-private-bridge'):
                    (home / ('dsb/' + name + '.sh')).write_text('echo ' + name + ' >> "$HOME/services"\n')
                if pending:
                    (home / '.dsb-install-pending').touch()
                script = '''
flock() { return 0; }
pgrep() { return 0; }
timeout() { echo adb >> "$HOME/adb-supervision"; return 0; }
sleep() { wait; exit 0; }
source "$REPO/hardening/adb-private/supervisor.sh"
'''
                env = dict(HOME=d, PREFIX=str(home / 'usr'), PATH=os.environ['PATH'], REPO=str(REPO))
                r = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True, timeout=5)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertTrue((home / 'adb-supervision').exists())
                calls = (home / 'services').read_text().splitlines() if (home / 'services').exists() else []
                self.assertEqual(sorted(calls), [] if pending else ['dsh-up', 'ensure-private-bridge'])

    def test_boot_releases_first_install_gate_without_erasing_bridge_state(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / '.dsb-install-pending').touch()
            (home / '.dsb-ipc').mkdir()
            state = home / '.dsb-ipc/bridge-launch.pid'
            state.write_text('unknown-owner')
            script = '''
setsid() { return 0; }
bash() { return 0; }
source "$REPO/installer/boot.sh"
'''
            env = dict(HOME=d, PREFIX=d, PATH=os.environ['PATH'], REPO=str(REPO))
            r = subprocess.run(['/bin/bash', '-c', script], env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse((home / '.dsb-install-pending').exists())
            self.assertEqual(state.read_text(), 'unknown-owner')

    def test_paired_device_port_change_does_not_require_new_pair_code(self):
        with tempfile.TemporaryDirectory() as d:
            script = '''
source "$REPO/install.sh"
device_adb() { adb "$@"; }
mkdir -p "$HOME/wx" "$HOME/dsb"
python3() {
  case "$1" in
    *bootstrap.py) return 0 ;;
    *adb_identity.py) printf '{"transport_id":7}\n' > "$HOME/dsb/adb-transport.json"; echo 2000; return 0 ;;
  esac
  command python3 "$@"
}
echo 'exit 1' > "$HOME/wx/wx_adb_connect.sh"
echo testserial > "$HOME/dsb/adb-device-id"
touch "$LOG"
pause() { echo "$*"; }
am() { return 0; }
timeout() { shift 3; "$@"; }
cat() {
  if [ "$1" = /proc/sys/kernel/random/boot_id ]; then echo testboot; else command cat "$@"; fi
}
adb() {
  echo "$*" >> "$HOME/adb-calls"
  case "$*" in
    *'shell id -u') echo 2000 ;;
    *'shell cat /proc/sys/kernel/random/boot_id') echo testboot ;;
    *'shell getprop ro.serialno') echo testserial ;;
  esac
}
setup_adb
'''
            env = dict(HOME=d, PREFIX=d, PATH=os.environ['PATH'], REPO=str(REPO))
            r = subprocess.run(['bash', '-c', script], env=env, input='127.0.0.1:43210\n',
                               capture_output=True, text=True, timeout=5)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertNotIn('pair ', (Path(d) / 'adb-calls').read_text())
            self.assertIn('shell getprop ro.serialno', (Path(d) / 'adb-calls').read_text())
            self.assertEqual((Path(d) / 'dsb/adb-target').read_text(), '127.0.0.1:43210\n')

    def test_apk_failed_download_never_opens_old_apk(self):
        with tempfile.TemporaryDirectory() as d:
            script = '''
source "$REPO/install.sh"
device_adb() { adb "$@"; }
touch "$LOG"
mkdir -p "$HOME/.cache/dsb-installer"
touch "$HOME/.cache/dsb-installer/DSB.apk"
TARGET=test
adb() { return 1; }
curl() { return 22; }
termux-open() { touch "$HOME/opened"; }
install_apk
'''
            env = dict(HOME=d, PATH=os.environ['PATH'], REPO=str(REPO))
            r = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertFalse((Path(d) / 'opened').exists())
            self.assertIn('APK 下载失败', r.stdout)

    def test_apk_noninteractive_only_opens_once_and_reports_pending(self):
        with tempfile.TemporaryDirectory() as d:
            script = '''
source "$REPO/install.sh"
device_adb() { adb "$@"; }
touch "$LOG" "$HOME/debug.apk"
DSB_APK_PATH="$HOME/debug.apk"
DSB_TEST_APK_SHA256=e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
TARGET=test
adb() { return 1; }
termux-open() { echo opened >> "$HOME/opens"; }
verify_apk_file() { return 0; }
install_apk
'''
            env = dict(HOME=d, PATH=os.environ['PATH'], REPO=str(REPO), DSB_NONINTERACTIVE='1')
            r = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual((Path(d) / 'opens').read_text().splitlines(), ['opened'])
            self.assertIn('doctor 将报告实际状态', r.stdout)

    def test_apk_source_permission_roundtrip_reopens_until_installed(self):
        with tempfile.TemporaryDirectory() as d:
            script = '''
source "$REPO/install.sh"
device_adb() { adb "$@"; }
touch "$LOG" "$HOME/debug.apk"
DSB_APK_PATH="$HOME/debug.apk"
DSB_TEST_APK_SHA256=e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
TARGET=test
adb() { [ -e "$HOME/installed" ] && echo 'package:/test.apk'; }
pause() { return 0; }
termux-open() {
  echo opened >> "$HOME/opens"
  [ "$(wc -l < "$HOME/opens")" -lt 2 ] || touch "$HOME/installed"
}
verify_installed_apk() { echo verified >> "$HOME/verified"; }
verify_apk_file() { return 0; }
install_apk
'''
            env = dict(HOME=d, PATH=os.environ['PATH'], REPO=str(REPO))
            r = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual((Path(d) / 'opens').read_text().splitlines(), ['opened', 'opened'])
            self.assertEqual((Path(d) / 'verified').read_text().splitlines(), ['verified'])

    def test_doctor_blocked_adb_remains_failure_without_more_commands(self):
        with tempfile.TemporaryDirectory() as d:
            script = '''
source "$REPO/install.sh"
device_adb() { adb "$@"; }
touch "$LOG"
TARGET=test
in_ubuntu() { return 2; }
adb() { echo called >> "$HOME/adb-calls"; return 78; }
doctor
'''
            env = dict(HOME=d, PATH=os.environ['PATH'], REPO=str(REPO))
            r = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertFalse((Path(d) / 'adb-calls').exists())

    def test_doctor_unhealthy_bridge_is_not_success(self):
        with tempfile.TemporaryDirectory() as d:
            script = '''
source "$REPO/install.sh"
device_adb() { adb "$@"; }
touch "$HOME/.dsb-web-url" "$LOG"
echo ready > "$HOME/.dsb-web-url"
TARGET=test
in_ubuntu() { return 0; }
adb() {
  printf '%s\n' 'com.termux.permission.RUN_COMMAND: granted=true' \
    'android.permission.POST_NOTIFICATIONS: granted=true' \
    'SYSTEM_ALERT_WINDOW: allow' 'com.dsb.assistant/NotificationService'
}
curl() { return 22; }
doctor
'''
            env = dict(HOME=d, PATH=os.environ['PATH'], REPO=str(REPO))
            r = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn('私有任务桥未就绪', r.stdout)

    def test_background_daemon_cannot_inherit_install_lock(self):
        with tempfile.TemporaryDirectory() as d:
            script = '''
source "$REPO/install.sh"
check_env() {
  exec 8>"$HOME/lock"
  python3 -c 'import fcntl; fcntl.flock(8, fcntl.LOCK_EX)'
}
main_locked() { sleep 30 >/dev/null 2>&1 & echo $!; }
main install
'''
            env = dict(HOME=d, PATH=os.environ['PATH'], REPO=str(REPO))
            r = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(r.returncode, 0, r.stderr)
            pid = int(r.stdout.strip())
            try:
                with (Path(d) / 'lock').open('a') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.kill(pid, 15)

    def test_one_key_prompt_and_no_log_or_environment_leak(self):
        with tempfile.TemporaryDirectory() as d:
            fake = 'sk-fake-DO-NOT-LOG-123456789'
            script = '''
source "$REPO/install.sh"
RF="$HOME/rootfs"
mkdir -p "$RF/root"
touch "$LOG"
setup_key
setup_key
env
'''
            env = dict(HOME=d, PATH=os.environ['PATH'], REPO=str(REPO),
                       DEEPSEEK_API_KEY=fake, INSTALL_KEY='previously-exported', DSB_NONINTERACTIVE='1')
            r = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn(fake, r.stdout + r.stderr + (Path(d) / 'dsb-install.log').read_text())
            self.assertEqual((Path(d) / 'rootfs/root/.dsh/.deepseek-key').read_text(), fake + '\n')

    def test_refuses_non_termux_before_mutation(self):
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, HOME=d)
            env.pop('PREFIX', None)
            r = subprocess.run(['bash', str(REPO / 'install.sh')], env=env, capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertFalse(list(Path(d).iterdir()))


if __name__ == '__main__':
    unittest.main()
