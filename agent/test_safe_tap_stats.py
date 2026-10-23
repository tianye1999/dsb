"""Synthetic audit statistics only. No production audit or device access."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import safe_tap_stats as stats


class TaskStats(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audit = Path(self.tmp.name) / "audit.jsonl"

    def rows(self, rows):
        self.audit.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def query(self, *args, text=False):
        out = io.StringIO()
        with patch.object(sys, "argv", ["stats", "--audit", str(self.audit),
                "--by-task", *([] if text else ["--json"]), *args]), contextlib.redirect_stdout(out):
            rc = stats.main()
        return rc, out.getvalue() if text else json.loads(out.getvalue())

    def start(self, tid="one", **kw):
        return dict(kind="task_start", task_id=tid, task_name="owned", **kw)

    def end(self, tid="one", status="completed", **kw):
        return dict(kind="task_end", task_id=tid, task_name="owned", terminal_status=status,
                    exit_code=0 if status == "completed" else 3, ok=status == "completed", **kw)

    def test_missing_end_counts_unknown_in_denominator(self):
        self.rows([self.start()])
        rc, report = self.query()
        self.assertEqual(rc, 0)
        t = report["by_task"]["owned"]
        self.assertEqual((t["n"], t["done"], t["statuses"]["unknown"]), (1, 0, 1))
        self.assertEqual(t["duration_samples"], 0)
        self.assertIsNone(t["avg_secs"])

    def test_legacy_ok_and_achieved_never_verify_goal(self):
        for old in ({"ok": True}, {"result": "achieved", "ok": True}):
            with self.subTest(old=old):
                self.rows([self.start(), dict(kind="task_end", task_id="one", task_name="owned", **old)])
                rc, report = self.query()
                t = report["by_task"]["owned"]
                self.assertEqual((t["achieved"], t["done"]), (0, 0))
                self.assertEqual(t["script_asserted"], int(old.get("result") == "achieved"))
                self.assertEqual(t["legacy_ok"], int("result" not in old))
                self.assertFalse(report["goal_verification_available"])

    def test_caller_verified_flag_is_not_trusted(self):
        self.rows([self.start(), self.end(goal_verified=True, goal_asserted=True)])
        _, report = self.query()
        t = report["by_task"]["owned"]
        self.assertEqual((t["achieved"], t["done"], t["script_asserted"]), (0, 1, 1))
        self.assertEqual(report["task_schema_version"], 2)

    def test_all_terminal_states_and_unknowns_remain_distinct(self):
        rows = []
        for status in ("completed", "failed", "refused", "cancelled", "timed_out", "audit_failed"):
            rows += [self.start(status), self.end(status, status)]
        rows.append(self.start("unknown"))
        self.rows(rows)
        _, report = self.query()
        t = report["by_task"]["owned"]
        self.assertEqual(t["n"], 7)
        self.assertEqual(t["known_terminal"], 6)
        self.assertEqual(t["done"], 1)
        self.assertEqual(t["rate_done"], 1 / 7)
        self.assertEqual(sum(t["statuses"].values()), 7)

    def test_duplicate_or_out_of_order_or_orphan_ends_are_unknown(self):
        for rows in ([self.start(), self.end(), self.end()], [self.end(), self.start()], [self.end()]):
            with self.subTest(rows=rows):
                self.rows(rows)
                _, report = self.query()
                t = report["by_task"]["owned"]
                self.assertEqual((t["n"], t["done"], t["statuses"]["unknown"]), (1, 0, 1))

    def test_inconsistent_terminal_is_unknown(self):
        self.rows([self.start(), dict(self.end(), exit_code=3)])
        _, report = self.query()
        self.assertEqual(report["by_task"]["owned"]["statuses"]["unknown"], 1)

    def test_live_lease_then_released_lease_changes_only_liveness(self):
        tid = "t123-" + "a" * 32
        self.rows([self.start(tid, lease_version=1)])
        leases = Path(str(self.audit) + ".runs")
        leases.mkdir()
        code = 'import fcntl,sys; f=open(sys.argv[1],"w"); fcntl.flock(f,fcntl.LOCK_EX); print("ready",flush=True); sys.stdin.read()'
        p = subprocess.Popen([sys.executable, "-c", code, str(leases / (tid + ".lock"))],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(p.stdout.readline().strip(), "ready")
            _, report = self.query()
            self.assertEqual(report["by_task"]["owned"]["statuses"]["running"], 1)
        finally:
            p.communicate(timeout=5)
        _, report = self.query()
        self.assertEqual(report["by_task"]["owned"]["statuses"]["unknown"], 1)

    def test_bad_json_nonobject_missing_identity_and_invalid_utf8_fail_explicitly(self):
        for data in (b'{bad}\n', b'[]\n', b'{"kind":"task_end"}\n', b'\xff\n'):
            with self.subTest(data=data):
                self.audit.write_bytes(data)
                rc, report = self.query()
                self.assertEqual(rc, 3)
                self.assertFalse(report["ok"])
                self.assertFalse(report["data_complete"])
                self.assertNotIn("by_task", report)

    def test_missing_audit_is_explicit_error(self):
        rc, report = self.query()
        self.assertEqual(rc, 3)
        self.assertFalse(report["data_complete"])

    def test_since_selects_whole_lifecycle_without_false_orphan(self):
        self.rows([self.start(ts_local="2026-10-01"), self.end(ts_local="2026-10-03")])
        _, report = self.query("--since", "2026-10-02")
        self.assertEqual(report["by_task"]["owned"]["done"], 1)

    def test_test_filter_excludes_entire_lifecycle_and_default_keeps_it(self):
        self.rows([dict(self.start(), task_name="batch-owned"), dict(self.end(), task_name="batch-owned")])
        _, default = self.query()
        self.assertEqual(default["by_task"]["batch-owned"]["n"], 1)
        _, filtered = self.query("--exclude-test")
        self.assertEqual(filtered["by_task"], {})

    def test_activity_filter_excludes_entire_lifecycle(self):
        self.rows([self.start(act_before="owned.fixture.Main"), self.end()])
        _, report = self.query("--exclude-activity", "owned.fixture")
        self.assertEqual(report["by_task"], {})

    def test_activity_filter_uses_linked_action_rows_not_only_task_start_end(self):
        self.rows([self.start("a"),
                   {"kind":"tap", "task_id":"a", "act_before":"owned.fixture.Main", "allowed":False},
                   self.end("a"), self.start("b"), self.end("b")])
        _, report = self.query("--exclude-activity", "owned.fixture")
        self.assertEqual(report["by_task"]["owned"]["n"], 1)
        self.assertEqual(report["by_task"]["owned"]["done"], 1)

    def test_linked_test_action_before_since_excludes_whole_task(self):
        self.rows([self.start("a", ts_local="2026-10-01"),
                   {"kind":"tap", "task_id":"a", "expect":"batch-owned", "ts_local":"2026-10-01"},
                   self.end("a", ts_local="2026-10-03")])
        _, report = self.query("--since", "2026-10-02", "--exclude-test")
        self.assertEqual(report["by_task"], {})

    def test_unlinked_action_cannot_exclude_another_task(self):
        self.rows([self.start(), {"kind":"tap", "act_before":"owned.fixture.Main"}, self.end()])
        _, report = self.query("--exclude-activity", "owned.fixture")
        self.assertEqual(report["by_task"]["owned"]["n"], 1)

    def test_duration_and_steps_use_known_valid_samples_not_unknown_zeroes(self):
        self.rows([self.start("a"), self.end("a", elapsed_s=4, steps=2), self.start("b"),
                   self.start("c"), self.end("c", elapsed_s=-2, steps="wrong")])
        _, report = self.query()
        t = report["by_task"]["owned"]
        self.assertEqual((t["n"], t["avg_secs"], t["avg_steps"], t["duration_samples"], t["step_samples"]), (3, 4, 2, 1, 1))

    def test_human_output_does_not_call_assertion_achievement(self):
        self.rows([self.start(), self.end(goal_asserted=True)])
        rc, report = self.query(text=True)
        self.assertEqual(rc, 0)
        self.assertNotIn("达成=显式断言", report)
        self.assertIn("目标未核验", report)

    def test_empty_audit_is_valid_zero_tasks(self):
        self.rows([])
        rc, report = self.query()
        self.assertEqual(rc, 0)
        self.assertTrue(report["data_complete"])
        self.assertEqual(report["by_task"], {})

    def test_modern_ok_without_assertion_is_not_script_assertion(self):
        self.rows([self.start(), self.end(result="completed", goal_asserted=None)])
        _, report = self.query()
        self.assertEqual(report["by_task"]["owned"]["script_asserted"], 0)

    def test_nonfinite_json_literals_fail_explicitly(self):
        for literal in ("NaN", "Infinity", "-Infinity"):
            self.audit.write_text('{"kind":"unrelated","value":' + literal + '}\n')
            rc, report = self.query()
            self.assertEqual(rc, 3)
            self.assertFalse(report["data_complete"])

    def test_large_finite_duration_average_never_overflows(self):
        self.rows([self.start("a"), self.end("a", elapsed_s=1e308),
                   self.start("b"), self.end("b", elapsed_s=1e308)])
        _, report = self.query()
        self.assertEqual(report["by_task"]["owned"]["avg_secs"], 1e308)

    def test_absent_measurements_have_no_hidden_zero_totals(self):
        self.rows([self.start()])
        _, report = self.query()
        t = report["by_task"]["owned"]
        self.assertNotIn("secs", t)
        self.assertNotIn("steps", t)

    def test_non_task_action_metrics_remain_unchanged(self):
        self.rows([{"kind":"tap", "allowed":True, "ui_reacted":True},
                   {"kind":"tap", "allowed":True, "ui_reacted":False}])
        _, report = self.query()
        self.assertEqual((report["samples"], report["noop"], report["noop_rate"]), (2, 1, 0.5))
        self.assertEqual(report["by_task"], {})

    def test_cli_returns_actual_failure_for_bad_audit(self):
        self.audit.write_text('{broken}\n')
        p = subprocess.run([sys.executable, stats.__file__, "--audit", str(self.audit), "--json"],
                           capture_output=True, text=True, timeout=5)
        self.assertEqual(p.returncode, 3)
        self.assertFalse(json.loads(p.stdout)["data_complete"])
        self.assertNotIn("{broken}", p.stdout)

    def test_cli_success_is_query_success_not_goal_success(self):
        self.rows([self.start()])
        before = self.audit.read_bytes()
        p = subprocess.run([sys.executable, stats.__file__, "--audit", str(self.audit), "--json", "--by-task"],
                           capture_output=True, text=True, timeout=5)
        self.assertEqual(p.returncode, 0)
        report = json.loads(p.stdout)
        self.assertEqual(report["by_task"]["owned"]["statuses"]["unknown"], 1)
        self.assertFalse(report["goal_verification_available"])
        self.assertEqual(self.audit.read_bytes(), before)
        self.assertFalse(Path(str(self.audit) + ".runs").exists())

    def test_runner_and_stats_classify_same_evidence_identically(self):
        import dsb_run as runner
        rows = [self.start("a"), self.end("a"), self.start("b"),
                self.start("c"), self.end("c", "refused"), self.end("orphan")]
        self.rows(rows)
        output = io.StringIO()
        with patch.object(runner.st, "AUDIT", str(self.audit)), contextlib.redirect_stdout(output):
            rc = runner._status_query(None)
        self.assertEqual(rc, 0)
        observed = {}
        for task in json.loads(output.getvalue())["tasks"]:
            key = task["terminal_status"]
            observed[key] = observed.get(key, 0) + 1
            self.assertFalse(task["goal_verified"])
        _, report = self.query()
        counts = report["by_task"]["owned"]["statuses"]
        self.assertEqual({k:v for k,v in counts.items() if v}, observed)


if __name__ == "__main__":
    unittest.main()
