"""Offline execution-report fixtures; never read a device or production audit."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import task_report


ID = "11111111-1111-4111-8111-111111111111"


def start():
    return {"kind": "task_start", "task_id": ID, "ts": 100.0}


def end(status="completed", **extra):
    return {"kind": "task_end", "task_id": ID, "ts": 110.0,
            "terminal_status": status, "exit_code": 0 if status == "completed" else 3,
            "ok": status == "completed", **extra}


def action(kind="tap", allowed=True, reason="", **extra):
    return {"kind": kind, "task_id": ID, "ts": 102.0, "allowed": allowed,
            "reason": reason, "dispatch_status": "command_completed" if allowed else "not_submitted",
            "act_before": "com.example.shop/.Main", **extra}


class TaskReportTests(unittest.TestCase):
    def test_all_dispatched_but_goal_not_independently_verified(self):
        report = task_report.build_report([start(), action(), end()], {}, ID)
        self.assertEqual(report["task"]["outcome"], "completed")
        self.assertFalse(report["task"]["goal_verified"])
        self.assertEqual(report["counts"]["dispatched_actions"], 1)
        self.assertEqual(report["timeline"][0]["app"], "com.example.shop")
        self.assertIsNone(report["counts"]["command_hook_refusals"])

    def test_multiple_refusals_and_handoff(self):
        rows = [start(), action(allowed=False, reason="没有截图凭据：请先运行 look"),
                action(allowed=False, reason="空控件树且 OCR 不可用，无法确认页面有无危险按钮"),
                action(allowed=False, reason="派发前目标所在行与观测时不一致（diff=9）：页面变了"),
                {"kind": "recovery", "task_id": ID, "ts": 108,
                 "action": "stop_for_human", "situation": "login_required"}, end("refused")]
        report = task_report.build_report(rows, {}, ID)
        self.assertEqual(report["task"]["outcome"], "handoff")
        self.assertEqual(report["counts"]["gate_refusals_by_reason"],
                         {"G1_NO_SHOT": 1, "G2B_OCR_UNAVAILABLE": 1, "R3_CHANGED": 1})
        self.assertEqual(report["counts"]["handoff_points"], 1)
        self.assertIn("人工接管", task_report.render_markdown(report))

    def test_display_binding_refusal_is_g1_without_raw_details(self):
        secret = "private-session-id"
        report = task_report.build_report(
            [action(allowed=False, reason="虚拟屏会话失效：" + secret)], {}, ID)
        self.assertEqual(report["counts"]["gate_refusals_by_reason"], {"G1_OTHER": 1})
        self.assertNotIn(secret, json.dumps(report, ensure_ascii=False))

    def test_user_stopped_requires_terminal_evidence(self):
        report = task_report.build_report([start(), end("cancelled", exit_code=130)], {}, ID)
        self.assertEqual(report["task"]["outcome"], "user_stopped")
        ledger = {"version": 1, "tasks": {ID: "cancelled"}, "latest": ID,
                  "active": None, "runtimeOpen": False}
        self.assertEqual(task_report.build_report([], ledger, ID)["task"]["outcome"], "unknown")

    def test_normalized_command_hook_refusal_only(self):
        rows = [start(), {"kind": "command_gate", "task_id": ID, "ts": 103,
                          "decision": "deny", "mode": "enforce", "source": "check_gate", "category": "destructive",
                          "cmd": "private command", "reason": "private reason"}, end("failed")]
        report = task_report.build_report(rows, {}, ID)
        self.assertEqual(report["counts"]["command_hook_refusals"], 1)
        self.assertEqual(report["timeline"][0]["reason_code"], "CHECK_GATE_DESTRUCTIVE")
        self.assertEqual(report["task"]["outcome"], "error")
        self.assertNotIn("private", json.dumps(report))

    def test_shadow_hook_is_not_a_real_refusal(self):
        rows = [{"kind": "command_gate", "task_id": ID, "ts": 103,
                 "decision": "deny", "mode": "shadow", "category": "destructive"}]
        report = task_report.build_report(rows, {}, ID)
        self.assertEqual(report["counts"]["command_hook_refusals"], 0)
        self.assertEqual(report["timeline"], [])

    def test_empty_task_and_old_unlinked_rows(self):
        ledger = {"version": 1, "tasks": {ID: "reply_received"}, "latest": ID,
                  "active": None, "runtimeOpen": True}
        rows = [action(task_id=None, note="secret")]
        report = task_report.build_report(rows, ledger, ID)
        self.assertEqual(report["task"]["outcome"], "unknown")
        self.assertEqual(report["task"]["transport_state"], "reply_received")
        self.assertEqual(report["timeline"], [])
        self.assertEqual(report["counts"]["dispatched_actions"], 0)
        self.assertEqual(task_report.build_report([], {}, ID)["task"]["outcome"], "unknown")

    def test_privacy_allowlist_json_and_markdown(self):
        secret = "聊天原文_订单_OTP_746291"
        rows = [start(), action(allowed=False, reason="空控件树 + 视觉判断：" + secret + " impact=high",
                                note=secret, expect=secret, vision={"reason": secret},
                                shot_before="/private/" + secret, act_before=secret),
                end("refused", fail_reason=secret)]
        report = task_report.build_report(rows, {}, ID)
        self.assertEqual(report["timeline"][0]["reason_code"], "G2B_VISION_RISK")
        for output in (json.dumps(report, ensure_ascii=False), task_report.render_markdown(report)):
            self.assertNotIn(secret, output)
            self.assertNotIn("/private/", output)

    def test_untrusted_task_id_cannot_inject_markdown(self):
        with self.assertRaises(ValueError):
            task_report.build_report([], {}, "bad\n# leaked")

    def test_cli_writes_both_formats_from_explicit_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            audit, ledger = folder / "audit.jsonl", folder / "ledger.json"
            audit.write_text("\n".join(json.dumps(r) for r in [start(), action(), end()]) + "\n", encoding="utf-8")
            ledger.write_text(json.dumps({"version": 1, "tasks": {}}), encoding="utf-8")
            json_out, md_out = folder / "report.json", folder / "report.md"
            done = subprocess.run([sys.executable, task_report.__file__, "--audit", str(audit),
                                   "--ledger", str(ledger), "--task-id", ID,
                                   "--json-out", str(json_out), "--markdown-out", str(md_out)],
                                  capture_output=True, text=True, timeout=5)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(json.loads(json_out.read_text(encoding="utf-8"))["task"]["task_id"], ID)
            self.assertIn("执行报告", md_out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
