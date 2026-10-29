"""Exercise the real launcher with offline port, clock, process and probe stubs."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(os.environ.get('DSB_START_DSH_SCRIPT', Path(__file__).with_name('start-dsh.sh')))
OLD = 'http://127.0.0.1:3080/?token=offline_old'
NEW = 'http://127.0.0.1:3080/?token=offline_new'
LATEST = 'http://127.0.0.1:3080/?token=offline_latest'

STUBS = r'''
NOW=0
event() { printf '%s\n' "$*" >> "$HOME/events"; }
python3() {
  if [ -f "$HOME/started" ]; then [ "$PORT_FAIL" = 0 ]; return; fi
  [ "$INITIAL_UP" = 1 ] || return 1
  [ ! -f "$HOME/stopped" ] || [ "$NOW" -lt "$STOP_DELAY" ]
}
pkill() { event "kill:$*"; touch "$HOME/stopped"; }
setsid() {
  event "start:$NOW"
  printf '%s\n' "$NOW" > "$HOME/started"
}
sleep() {
  wait
  NOW=$((NOW + $1)); SECONDS=$NOW
  if [ -f "$HOME/started" ]; then
    started=$(cat "$HOME/started")
    if [ "$URL_DELAY" -ge 0 ] && [ "$((NOW - started))" -ge "$URL_DELAY" ] && [ ! -f "$HOME/url-written" ]; then
      printf '%s\n' "$NEW" >> "$HOME/dsh-web.log"; touch "$HOME/url-written"
    fi
    if [ "$ROTATE" = 1 ] && [ -f "$HOME/probed" ] && [ ! -f "$HOME/rotated" ]; then
      printf '%s\n' "$LATEST" >> "$HOME/dsh-web.log"; touch "$HOME/rotated"
    fi
  fi
}
source "$1" "${2:-}"
'''

PROBE = '''#!/bin/bash
printf '%s\\n' "$1" >> "$HOME/probed"
# A noisy helper must not disclose authentication URLs through the launcher.
if [ "$NOISY" = 1 ]; then printf '%s\\n' "$1"; printf '%s\\n' "$1" >&2; fi
[ "$PROBE_FAIL" = 0 ] && [ "$1" = "$EXPECTED" ]
'''


class StartDshTests(unittest.TestCase):
    def run_start(self, restart=False, initial_up=False, stop_delay=0, url_delay=6,
                  rotate=False, probe_fail=False, port_fail=False, existing_token=False, noisy=False):
        with tempfile.TemporaryDirectory(prefix='dsb-t19-start-') as directory:
            home = Path(directory)
            (home / 'dsb').mkdir()
            probe = home / 'dsb/ready-probe.sh'
            probe.write_text(PROBE)
            probe.chmod(0o700)
            (home / 'dsh-web.log').write_text(OLD + '\n')
            token = home / '.dsh-web-token'
            if existing_token:
                token.write_text('preserve-existing-token\n')
            expected = LATEST if rotate else OLD if initial_up and not restart else NEW
            env = dict(os.environ, HOME=str(home), INITIAL_UP=str(int(initial_up)),
                       STOP_DELAY=str(stop_delay), URL_DELAY=str(url_delay),
                       ROTATE=str(int(rotate)), PROBE_FAIL=str(int(probe_fail)),
                       PORT_FAIL=str(int(port_fail)), NOISY=str(int(noisy)),
                       EXPECTED=expected, NEW=NEW, LATEST=LATEST)
            result = subprocess.run(['bash', '-c', STUBS, 'offline-start', str(SCRIPT),
                                     '--restart' if restart else ''],
                                    env=env, capture_output=True, text=True, timeout=10)
            events = (home / 'events').read_text().splitlines() if (home / 'events').exists() else []
            probes = (home / 'probed').read_text().splitlines() if (home / 'probed').exists() else []
            for url in (OLD, NEW, LATEST):
                self.assertNotIn(url, result.stdout + result.stderr, 'token leaked to output')
            return result, token.read_text().strip() if token.exists() else None, events, probes

    def test_new_url_after_port_ignores_previous_log_url(self):
        result, token, events, probes = self.run_start()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(token, NEW)
        self.assertEqual(probes, [NEW])
        self.assertEqual(events, ['start:0'])

    def test_restart_waits_longer_than_three_seconds_for_old_port(self):
        result, token, events, _ = self.run_start(restart=True, initial_up=True, stop_delay=7)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(token, NEW)
        self.assertEqual(events, ['kill:-f [d]sh web', 'start:7'])
        self.assertNotIn('(already)', result.stdout)

    def test_restart_port_timeout_never_starts_or_reports_already(self):
        result, token, events, probes = self.run_start(restart=True, initial_up=True, stop_delay=100)
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(token)
        self.assertEqual(events, ['kill:-f [d]sh web'])
        self.assertEqual(probes, [])
        self.assertNotIn('(already)', result.stdout)
        self.assertIn('START_DSH_WARN', result.stderr)

    def test_url_timeout_never_publishes_old_token(self):
        result, token, _, probes = self.run_start(url_delay=-1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(token)
        self.assertEqual(probes, [])
        self.assertIn('START_DSH_WARN', result.stderr)

    def test_ready_probe_reloads_latest_url_on_each_attempt(self):
        result, token, _, probes = self.run_start(rotate=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(token, LATEST)
        self.assertEqual(probes, [NEW, LATEST])

    def test_probe_timeout_does_not_create_or_overwrite_token(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                result, token, _, probes = self.run_start(probe_fail=True, existing_token=existing)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(token, 'preserve-existing-token' if existing else None)
                self.assertGreater(len(probes), 1)
                self.assertNotIn(OLD, probes)
                self.assertNotIn('URL_READY', result.stdout)

    def test_existing_port_without_restart_uses_verified_full_log_url(self):
        result, token, events, probes = self.run_start(initial_up=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(token, OLD)
        self.assertEqual(events, [])
        self.assertEqual(probes, [OLD])
        self.assertIn('(already)', result.stdout)

    def test_existing_port_probe_failure_does_not_publish(self):
        result, token, events, _ = self.run_start(initial_up=True, probe_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(token)
        self.assertEqual(events, [])

    def test_startup_port_timeout_does_not_dump_token_bearing_log(self):
        result, token, _, probes = self.run_start(port_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(token)
        self.assertEqual(probes, [])

    def test_noisy_probe_tokens_are_not_forwarded_to_output(self):
        result, token, _, _ = self.run_start(noisy=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(token, NEW)


if __name__ == '__main__':
    unittest.main()
