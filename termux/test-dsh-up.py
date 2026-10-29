"""Run the real startup scripts with offline curl/port/process substitutes."""

import fcntl
import json
import os
import pathlib
import select
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(os.environ.get('DSB_TEST_SOURCE_ROOT', pathlib.Path(__file__).resolve().parents[1]))
SCRIPTS = ('termux/dsh-up.sh', 'app/termux/dsh-up.sh')
OLD_URL = 'http://127.0.0.1:3080/?token=offline_existing'
NEW_URL = 'http://127.0.0.1:3080/?token=offline_restarted'

FAKE_FLOCK = '''import fcntl
import sys

if sys.argv[1:] != ['-n', '9']:
    raise SystemExit('unexpected flock arguments')
try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit(1)
'''


def install_fake_flock(binary):
    fake = binary / 'flock'
    fake.write_text('#!' + sys.executable + '\n' + FAKE_FLOCK)
    fake.chmod(0o755)


def lock_is_free(lock):
    if not lock.exists():
        return True
    with lock.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(handle, fcntl.LOCK_UN)
    return True

FAKE_COMMAND = r'''import json
import os
import pathlib
import sys
import time

root = pathlib.Path(os.environ['DSB_UP_FIXTURE'])
scenario = json.loads((root / 'scenario.json').read_text())
name = pathlib.Path(sys.argv[0]).name
events_file = root / 'events.jsonl'
events = [json.loads(line) for line in events_file.read_text().splitlines()] if events_file.exists() else []
event = json.dumps({'command': name, 'args': sys.argv[1:]}) + '\n'
fd = os.open(events_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
os.write(fd, event.encode())
os.close(fd)

if name == 'python3':
    # dsh-up invokes python3 only for its socket-connect probe.
    opened = scenario['port_open'] or (root / 'started').exists()
    sys.exit(0 if opened and not (root / 'stopped').exists() else 1)
elif name == 'curl':
    url = sys.argv[-1]
    if url.endswith('offline_restarted'):
        status, exit_code = '303', 0
    else:
        previous = sum(e['command'] == 'curl' and e['args'][-1] == url for e in events)
        plan = scenario['responses']
        status, exit_code = plan[min(previous, len(plan) - 1)]
    sys.stdout.write(status)
    sys.exit(exit_code)
elif name == 'pkill':
    (root / 'stopped').touch()
elif name == 'setsid':
    (root / 'stopped').unlink(missing_ok=True)
    (root / 'started').touch()
    if scenario.get('delayed_new_url'):
        time.sleep(0.06)
    print('http://127.0.0.1:3080/?token=offline_restarted', flush=True)
elif name == 'sleep':
    # Give the fake background starter a scheduling opportunity without waiting seconds.
    time.sleep(0.005)
    if scenario.get('external_new_after_sleeps'):
        sleeps = sum(e['command'] == 'sleep' for e in events) + 1
        if sleeps >= 2 and not (root / 'external_url_written').exists():
            (root / 'home' / 'dsh-web.log').open('a').write(
                'http://127.0.0.1:3080/?token=offline_restarted\n')
            (root / 'external_url_written').touch()
elif name == 'ready-probe.sh':
    pass
else:
    raise SystemExit('unexpected fake command: ' + name)
'''


class StartupChecks(unittest.TestCase):
    def run_startup(self, relative, responses, port_open=True, stale_lock=False,
                    **scenario_options):
        with tempfile.TemporaryDirectory(prefix='dsb-up-offline-') as directory:
            root = pathlib.Path(directory)
            home, binary = root / 'home', root / 'bin'
            (home / 'dsb').mkdir(parents=True)
            binary.mkdir()
            (root / 'scenario.json').write_text(json.dumps({
                'port_open': port_open,
                'responses': responses,
                **scenario_options,
            }))
            (home / 'dsh-web.log').write_text(OLD_URL + '\n')
            if stale_lock:
                (home / 'dsb/.dsh-up.lock').write_text(str(os.getpid()) + '\n')
            (home / '.dsb-web-url').write_text(OLD_URL + '\n')
            (home / '.dsh-web-token').write_text(OLD_URL + '\n')
            fake = binary / 'fake-command'
            fake.write_text('#!' + sys.executable + '\n' + FAKE_COMMAND)
            fake.chmod(0o755)
            install_fake_flock(binary)
            for command in ('python3', 'curl', 'pkill', 'setsid', 'sleep'):
                (binary / command).symlink_to(fake)
            (home / 'dsb' / 'ready-probe.sh').symlink_to(fake)
            for command in ('grep', 'tail', 'wc'):
                (binary / command).symlink_to(shutil.which(command))

            # Preserve the production logic; redirect only its fixed Termux paths.
            source = (ROOT / relative).read_text()
            for original, replacement in (
                    ('P=/data/data/com.termux/files/usr/bin', 'P=' + str(binary)),
                    ('H=/data/data/com.termux/files/home', 'H=' + str(home))):
                self.assertEqual(source.count(original), 1, 'unexpected startup path assignment')
                source = source.replace(original, replacement)
            script = root / 'dsh-up.sh'
            script.write_text(source)
            env = dict(os.environ, DSB_UP_FIXTURE=str(root), PATH=str(binary) + os.pathsep + os.environ['PATH'])
            result = subprocess.run(['bash', str(script)], env=env, capture_output=True, text=True, timeout=15)
            events_file = root / 'events.jsonl'
            events = ([json.loads(line) for line in events_file.read_text().splitlines()]
                      if events_file.exists() else [])
            lock = home / 'dsb/.dsh-up.lock'
            return {
                'result': result,
                'events': events,
                'url': (home / '.dsb-web-url').read_text().strip(),
                'token': (home / '.dsh-web-token').read_text().strip(),
                'lock_free': lock_is_free(lock),
                'log': (home / 'dsh-web.log').read_text(),
            }

    def count(self, outcome, command):
        return sum(event['command'] == command for event in outcome['events'])

    def assert_kept_live_web(self, outcome, min_checks=1):
        result = outcome['result']
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), OLD_URL)
        self.assertEqual(outcome['url'], OLD_URL)
        self.assertEqual(outcome['token'], OLD_URL)
        self.assertEqual(self.count(outcome, 'pkill'), 0, 'a live web process must not be killed')
        self.assertEqual(self.count(outcome, 'setsid'), 0, 'a live web process must not be restarted')
        self.assertGreaterEqual(self.count(outcome, 'curl'), min_checks)
        self.assertLessEqual(self.count(outcome, 'curl'), 6, 'URL checks must stay bounded')
        self.assertTrue(outcome['lock_free'])

    def test_existing_valid_url_does_not_restart(self):
        for relative in SCRIPTS:
            for status in ('200', '303'):
                with self.subTest(script=relative, status=status):
                    self.assert_kept_live_web(self.run_startup(relative, [[status, 0]]))

    def test_one_timeout_retries_without_changing_token(self):
        for relative in SCRIPTS:
            with self.subTest(script=relative):
                outcome = self.run_startup(relative, [['000', 28], ['303', 0]])
                self.assert_kept_live_web(outcome, min_checks=2)

    def test_two_transient_failures_can_recover_without_restart(self):
        for relative in SCRIPTS:
            with self.subTest(script=relative):
                outcome = self.run_startup(relative, [['', 7], ['503', 0], ['200', 0]])
                self.assert_kept_live_web(outcome, min_checks=3)

    def test_persistent_unknown_failure_is_bounded_and_preserves_live_web(self):
        for relative in SCRIPTS:
            for response in (['000', 28], ['503', 0]):
                with self.subTest(script=relative, response=response):
                    outcome = self.run_startup(relative, [response])
                    result = outcome['result']
                    self.assertNotEqual(result.returncode, 0, 'unknown health must not be reported as healthy')
                    self.assertIn('DSH_UP_FAILED', result.stderr)
                    self.assertEqual(result.stdout, '')
                    self.assertEqual(outcome['url'], OLD_URL)
                    self.assertEqual(outcome['token'], OLD_URL)
                    self.assertEqual(self.count(outcome, 'pkill'), 0, 'probe failure is not proof the server is stale')
                    self.assertEqual(self.count(outcome, 'setsid'), 0)
                    self.assertGreaterEqual(self.count(outcome, 'curl'), 2)
                    self.assertLessEqual(self.count(outcome, 'curl'), 6)
                    self.assertTrue(outcome['lock_free'])

    def test_confirmed_unauthorized_url_restarts_once_and_publishes_new_token(self):
        for relative in SCRIPTS:
            with self.subTest(script=relative):
                outcome = self.run_startup(relative, [['401', 0]])
                result = outcome['result']
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), NEW_URL)
                self.assertEqual(outcome['url'], NEW_URL)
                self.assertEqual(outcome['token'], NEW_URL)
                self.assertEqual(self.count(outcome, 'pkill'), 1)
                self.assertEqual(self.count(outcome, 'setsid'), 1)
                self.assertTrue(outcome['lock_free'])

    def test_closed_port_starts_web_and_publishes_verified_token(self):
        for relative in SCRIPTS:
            with self.subTest(script=relative):
                outcome = self.run_startup(relative, [['000', 7]], port_open=False)
                result = outcome['result']
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), NEW_URL)
                self.assertEqual(outcome['url'], NEW_URL)
                self.assertEqual(outcome['token'], NEW_URL)
                self.assertEqual(self.count(outcome, 'setsid'), 1)
                self.assertTrue(outcome['lock_free'])

    def test_closed_port_ignores_old_url_until_new_start_writes_one(self):
        for relative in SCRIPTS:
            with self.subTest(script=relative):
                outcome = self.run_startup(relative, [['401', 0]], port_open=False,
                                           delayed_new_url=True)
                self.assertEqual(outcome['result'].returncode, 0, outcome['result'].stderr)
                self.assertEqual(outcome['result'].stdout.strip(), NEW_URL)
                self.assertEqual(outcome['url'], NEW_URL)
                self.assertEqual(outcome['token'], NEW_URL)
                self.assertEqual(outcome['log'], OLD_URL + '\n' + NEW_URL + '\n')
                self.assertEqual(self.count(outcome, 'pkill'), 0)
                self.assertEqual(self.count(outcome, 'setsid'), 1)
                self.assertTrue(outcome['lock_free'])

    def test_stale_lock_with_reused_pid_does_not_skip_startup(self):
        for relative in SCRIPTS:
            with self.subTest(script=relative):
                outcome = self.run_startup(relative, [['000', 7]], port_open=False,
                                           stale_lock=True)
                self.assertEqual(outcome['result'].returncode, 0, outcome['result'].stderr)
                self.assertEqual(outcome['result'].stdout.strip(), NEW_URL)
                self.assertEqual(outcome['url'], NEW_URL)
                self.assertEqual(self.count(outcome, 'setsid'), 1)
                self.assertTrue(outcome['lock_free'])

    def test_existing_listener_waits_for_new_url_after_old_token_is_rejected(self):
        for relative in SCRIPTS:
            with self.subTest(script=relative):
                outcome = self.run_startup(relative, [['401', 0]],
                                           external_new_after_sleeps=True)
                self.assertEqual(outcome['result'].returncode, 0, outcome['result'].stderr)
                self.assertEqual(outcome['result'].stdout.strip(), NEW_URL)
                self.assertEqual(outcome['log'], OLD_URL + '\n' + NEW_URL + '\n')
                self.assertEqual(self.count(outcome, 'pkill'), 0)
                self.assertEqual(self.count(outcome, 'setsid'), 0)


class SupervisorChecks(unittest.TestCase):
    def run_one_iteration(self, lock_state):
        with tempfile.TemporaryDirectory(prefix='dsb-supervisor-offline-') as directory:
            home = pathlib.Path(directory)
            binary = home / 'bin'
            (home / 'wx').mkdir()
            (home / 'dsb').mkdir()
            binary.mkdir()
            install_fake_flock(binary)
            lock = home / 'dsb/.dsh-up.lock'
            holder = None
            if lock_state == 'stale':
                lock.write_text(str(os.getpid()) + '\n')
            elif lock_state == 'held':
                fake_dsh_up = home / 'fake-dsh-up.py'
                fake_dsh_up.write_text(
                    'import fcntl, os, pathlib, sys\n'
                    'with pathlib.Path(sys.argv[1]).open("a") as lock:\n'
                    '    fcntl.flock(lock, fcntl.LOCK_EX)\n'
                    '    lock.seek(0)\n'
                    '    lock.truncate()\n'
                    '    lock.write(str(os.getpid()) + "\\n")\n'
                    '    lock.flush()\n'
                    '    print("ready", flush=True)\n'
                    '    sys.stdin.read()\n')
                holder = subprocess.Popen([sys.executable, str(fake_dsh_up), str(lock)],
                                          stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, text=True)
                ready, _, _ = select.select([holder.stdout], [], [], 5)
                self.assertTrue(ready, 'fake dsh-up did not acquire its flock')
                self.assertEqual(holder.stdout.readline().strip(), 'ready')
            (home / 'wx/wx_adb_connect.sh').write_text('#!/bin/bash\nexit 0\n')
            script = ROOT / 'termux/supervisor.sh'
            try:
                result = subprocess.run([
                    'bash', '-c',
                    'pgrep() { return 0; }; '
                    'python3() { return 1; }; '
                    'setsid() { printf "%s\\n" "$*" >> "$HOME/starts"; }; '
                    'sleep() { wait; exit 0; }; '
                    'source "$1"',
                    'offline-supervisor', str(script),
                ], env=dict(os.environ, HOME=str(home),
                            PATH=str(binary) + os.pathsep + os.environ['PATH']),
                    capture_output=True, text=True, timeout=5)
            finally:
                if holder is not None:
                    holder.communicate(timeout=5)  # closes stdin, which ends the fake holder
            starts = home / 'starts'
            return result, starts.read_text().splitlines() if starts.exists() else []

    def test_dsh_up_live_lock_prevents_duplicate_web_start(self):
        result, starts = self.run_one_iteration(lock_state='held')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(starts, [])

    def test_no_dsh_up_lock_allows_web_start_when_port_is_closed(self):
        result, starts = self.run_one_iteration(lock_state='missing')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(starts), 1, starts)
        self.assertIn('/wx/start-dsh.sh', starts[0])

    def test_stale_lock_with_reused_pid_allows_web_start(self):
        result, starts = self.run_one_iteration(lock_state='stale')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(starts), 1, starts)
        self.assertIn('/wx/start-dsh.sh', starts[0])


class ManualBootAdbChecks(unittest.TestCase):
    def run_boot(self, managed=None, selector='verified'):
        with tempfile.TemporaryDirectory(prefix='dsb-boot-offline-') as directory:
            root = pathlib.Path(directory)
            home, binary = root / 'home', root / 'bin'
            (home / 'dsb').mkdir(parents=True)
            (home / 'wx').mkdir()
            binary.mkdir()
            recorder = '#!/bin/bash\nprintf "%s\\n" "$*" >> "$HOME/adb-calls"\n'
            adb = binary / 'adb'
            adb.write_text(recorder)
            adb.chmod(0o700)
            (binary / 'python3').write_text('#!/bin/bash\nexit 0\n')
            (binary / 'python3').chmod(0o700)
            (binary / 'bash').symlink_to('/bin/bash')
            for name in ('dsh-up.sh', 'ensure-private-bridge.sh'):
                (home / 'dsb' / name).write_text('#!/bin/bash\nexit 0\n')
                (home / 'dsb' / name).chmod(0o700)
            reconnect = home / 'wx/wx_adb_connect.sh'
            # Failure must never trigger a fallback connection to the legacy endpoint.
            reconnect.write_text('echo reconnect >> "$HOME/adb-calls"\nexit 1\n')
            if managed:
                (home / managed).touch()
            if selector == 'blocked':
                (home / 'dsb/adb-blocked').touch()
                (root / 'adb-target.sh').write_text((ROOT / 'termux/adb-target.sh').read_text().replace(
                    '/data/data/com.termux/files/home', str(home)))
            elif selector != 'missing':
                (root / 'adb-target.sh').write_text(
                    'exit 78\n' if selector == 'denied' else
                    'DSB_ADB_COMMAND=("' + str(adb) + '" "bound executor")\n'
                    'DSB_ADB_SELECTOR=(-t 7)\n')
            (home / 'dsb/usb-mode.sh').touch()
            rootfs = root / 'var/lib/proot-distro/containers/ubuntu/rootfs/root'
            for relative in ('dsh-config/auto-commit.sh', '.dsh-phone/back2dsb_daemon.py'):
                daemon = rootfs / relative
                daemon.parent.mkdir(parents=True, exist_ok=True)
                daemon.touch()
            source = (ROOT / 'termux/boot.sh').read_text()
            source = source.replace('/data/data/com.termux/files/usr', str(root))
            source = source.replace('/data/data/com.termux/files/home', str(home))
            script = root / 'boot.sh'
            script.write_text(source)
            env = dict(os.environ, HOME=str(home))
            result = subprocess.run(['/bin/bash', '-c',
                                     'setsid() { printf "%s\\n" "$*" >> "$HOME/services"; }; '
                                     'pgrep() { return 1; }; trap wait EXIT; source "$1"',
                                     'offline-boot', str(script)],
                                    env=env, capture_output=True, text=True, timeout=5)
            calls = home / 'adb-calls'
            services = home / 'services'
            return (result, calls.read_text().splitlines() if calls.exists() else [],
                    services.read_text().splitlines() if services.exists() else [])

    def assert_followup_daemons(self, services):
        self.assertEqual(len(services), 3, services)
        for expected in ('/dsb/usb-mode.sh', '/root/dsh-config/auto-commit.sh --watch',
                         '/root/.dsh-phone/back2dsb_daemon.py --watch'):
            self.assertEqual(sum(expected in call for call in services), 1, services)

    def assert_selector_failure_is_isolated(self, selector):
        result, calls, services = self.run_boot('dsb/adb-target', selector=selector)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ['reconnect'])
        self.assertEqual(result.stderr.splitlines(), [
            'boot: ADB 选择器不可用，跳过三项 ADB 设置；其它服务继续启动'])
        self.assert_followup_daemons(services)

    def test_manual_boot_uses_selector_arrays_and_limits_legacy_connect(self):
        expected = [
            'bound executor -t 7 shell settings put global settings_enable_monitor_phantom_procs false',
            'bound executor -t 7 shell device_config put activity_manager max_phantom_processes 2147483647',
            'bound executor -t 7 shell dumpsys deviceidle whitelist +com.termux',
        ]
        for managed in (None, 'dsb/adb-target', '.dsb-installer-managed'):
            with self.subTest(managed=managed):
                result, calls, services = self.run_boot(managed)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(calls, [
                    'reconnect' if managed else 'connect 127.0.0.1:5555', *expected])
                self.assert_followup_daemons(services)

    def test_manual_boot_denied_selector_never_submits_shell_commands(self):
        self.assert_selector_failure_is_isolated('denied')

    def test_manual_boot_conflict_gate_does_not_block_other_daemons(self):
        self.assert_selector_failure_is_isolated('blocked')

    def test_manual_boot_missing_selector_does_not_block_other_daemons(self):
        self.assert_selector_failure_is_isolated('missing')


if __name__ == '__main__':
    unittest.main()
