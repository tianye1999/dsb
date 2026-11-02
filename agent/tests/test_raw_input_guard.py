"""Shared Bash/Python contract; every subprocess here is offline."""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

AGENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT))
import dsb_run
import check_gate

CASES = json.loads((Path(__file__).with_name("raw_input_cases.json")).read_text())
ADBQ = AGENT.parent / "termux/adbq.sh"


class RawInputGuardTests(unittest.TestCase):
    def test_bash_performance_budget(self):
        timings = {}
        locale = "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"
        for length, contains_input in ((8000, False), (2000, True)):
            prefix = "am broadcast --es cmd '"
            suffix = "'" + ("; input -d 3 tap 1 2" if contains_input else "")
            command = prefix + "x" * (length - len(prefix) - len(suffix)) + suffix
            started = time.perf_counter()
            result = subprocess.run(
                ["/bin/bash", str(ADBQ), "--raw-input-selftest", "adb", "shell", command],
                capture_output=True, text=True, timeout=10,
                env=dict(os.environ, LC_ALL=locale))
            elapsed = time.perf_counter() - started
            timings[length] = elapsed
            print("RAW_INPUT_PERF length=%d contains_input=%s elapsed_ms=%.3f" %
                  (length, contains_input, elapsed * 1000), flush=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "blocked" if contains_input else "allowed")
        # Includes process startup; 200 ms leaves generous headroom for CI jitter.
        self.assertLess(timings[8000], 0.200)

    def test_actual_adbq_guard_runs_before_transport_and_ball(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wrapper = root / "adbq.sh"
            wrapper.write_text(ADBQ.read_text())
            transport = root / "transport.sh"
            transport.write_text('printf "%s\\n" "$*" >> "$CALL_LOG"\n'
                                 'echo "mCurrentFocus=Window{com.dsb.assistant}"\n')
            (root / "adb-target.sh").write_text(
                'DSB_ADB_COMMAND=(bash "$FAKE_TRANSPORT")\nDSB_ADB_SELECTOR=(-s offline)\n'
                'sleep() { :; }\n')
            log = root / "calls"
            env = dict(os.environ, ADBQ_SELFTEST="1", SAFE_TAP_ALLOW_RAW_INPUT="0",
                       CALL_LOG=str(log), FAKE_TRANSPORT=str(transport))
            for case in CASES:
                if not case["cmd"]:
                    continue
                with self.subTest(case=case):
                    log.unlink(missing_ok=True)
                    result = subprocess.run(["bash", str(wrapper), "shell", case["cmd"]],
                                            capture_output=True, text=True, env=env, timeout=5)
                    self.assertEqual(result.returncode, 2 if case["blocked"] else 0, result.stderr)
                    self.assertEqual(log.exists(), not case["blocked"])
            for args in (("push", "input", "/sdcard/"), ("蓝",), ("橙",), ("状态",), ("done",)):
                log.unlink(missing_ok=True)
                result = subprocess.run(["bash", str(wrapper), *args], capture_output=True,
                                        text=True, env=env, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(log.exists())
            for case in CASES:
                if not case["cmd"].startswith("adb "):
                    continue
                with self.subTest(adb_args=case):
                    log.unlink(missing_ok=True)
                    result = subprocess.run(["bash", str(wrapper), *shlex.split(case["cmd"])[1:]],
                                            capture_output=True, text=True, env=env, timeout=5)
                    self.assertEqual(result.returncode, 2 if case["blocked"] else 0, result.stderr)
                    self.assertEqual(log.exists(), not case["blocked"])
            log.unlink(missing_ok=True)
            result = subprocess.run(["bash", str(wrapper), "shell", "input tap 1 2"],
                                    capture_output=True, text=True, timeout=5,
                                    env=dict(env, SAFE_TAP_ALLOW_RAW_INPUT="1"))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("WARNING", result.stderr)
            self.assertEqual(len(log.read_text().splitlines()), 2)
            for stale in ("command", "guard"):
                log.unlink()
                result = subprocess.run(["bash", str(wrapper), "shell", "dumpsys input"],
                                        capture_output=True, text=True, timeout=5,
                                        env=dict(env, ADBQ_SELFTEST=stale))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(log.read_text().splitlines()), 2)

    def test_input_is_not_read_only(self):
        for command in ("input tap 1 2", "input swipe 1 2 3 4", "input text abc", "input keyevent 4",
                        "monkey -p com.example 1"):
            self.assertFalse(any(re.search(pattern, command) for pattern in check_gate.READ_ONLY_PATTERNS))

    def test_diagnostic_override_warns_and_default_refuses(self):
        for override, code in (("0", 2), ("1", 0)):
            result = subprocess.run(
                ["bash", str(ADBQ), "--raw-input-selftest", "guard", "shell", "input", "-d", "3", "tap", "1", "2"],
                capture_output=True, text=True, timeout=5,
                env=dict(os.environ, SAFE_TAP_ALLOW_RAW_INPUT=override))
            self.assertEqual(result.returncode, code, result.stderr)
            self.assertIn("WARNING" if override == "1" else "拒绝", result.stderr)

    def test_python_vectors(self):
        from raw_input_guard import has_raw_input
        for case in CASES:
            with self.subTest(case=case):
                self.assertEqual(has_raw_input(case["cmd"]), case["blocked"])

    def test_adb_argument_vectors(self):
        for case in CASES:
            if not case["cmd"].startswith("adb "):
                continue
            with self.subTest(case=case):
                result = subprocess.run(
                    ["bash", str(ADBQ), "--raw-input-selftest", "adb", *shlex.split(case["cmd"])[1:]],
                    capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "blocked" if case["blocked"] else "allowed")

    def test_bash_vectors(self):
        for case in CASES:
            with self.subTest(case=case):
                result = subprocess.run(
                    ["bash", str(ADBQ), "--raw-input-selftest", "command", case["cmd"]],
                    capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "blocked" if case["blocked"] else "allowed")

    def test_dsb_run_constants(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "script.py"
            for case in CASES:
                with self.subTest(case=case):
                    script.write_text("cmd = " + repr(case["cmd"]) + "\n")
                    if case["blocked"] and any(c.isspace() for c in case["cmd"]):
                        with self.assertRaisesRegex(RuntimeError, "input"):
                            dsb_run._source_guard(script)
                    else:
                        dsb_run._source_guard(script)
            script.write_text('cmd = "input -d 3 tap 1 2"')
            with patch.dict(os.environ, SAFE_TAP_ALLOW_RAW_INPUT="1"), self.assertRaises(RuntimeError):
                dsb_run._source_guard(script)
            for src in ('cmd = ["input", "tap", "1", "2"]',
                        'cmd = ["/system/bin/input", "tap", "1", "2"]',
                        'cmd = "input " "-d 3 tap 1 2"',
                        'cmd = f"input -d {3} tap 1 2"'):
                script.write_text(src)
                with self.assertRaisesRegex(RuntimeError, "input"):
                    dsb_run._source_guard(script)
            script.write_text('# input tap 1 2\ncmd = "echo input"\n')
            dsb_run._source_guard(script)
            for forbidden in dsb_run._FORBIDDEN:
                for source in ("# " + forbidden, forbidden):
                    script.write_text(source)
                    with self.assertRaises(RuntimeError):
                        dsb_run._source_guard(script)

    def test_dsb_run_data_and_command_contexts(self):
        allowed = ('node.get("input")', '{"input": 1}', 'if "input" in text: pass',
                   '"""input tap is described here"""', 'st.find("input")',
                   'node["input tap"]', 'node.get("input tap")',
                   '{"input tap": 1}', 'if "input tap" == text: pass')
        blocked = ('st.sh("shell", "input", "tap", "1", "2")',
                   '["input", "-d", "3", "tap"]', '("input", "tap", "1", "2")',
                   'cmd = "input -d 3 tap 1 2"', 'cmd = f"input tap {x} {y}"')
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "script.py"
            for source in allowed + blocked:
                with self.subTest(source=source):
                    script.write_text(source)
                    if source in blocked:
                        with self.assertRaisesRegex(RuntimeError, "input"):
                            dsb_run._source_guard(script)
                    else:
                        dsb_run._source_guard(script)


if __name__ == "__main__":
    unittest.main()
