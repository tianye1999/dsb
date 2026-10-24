"""Offline execution-report fixtures; never read a device or production audit."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import task_report
import safe_tap_stats
import task_status
import task_templates


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
        self.assertTrue(report["coverage"]["attribution_complete"])
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

    def test_structured_reason_code_wins_over_legacy_text(self):
        report = task_report.build_report(
            [action(allowed=False, reason="unknown text", reason_code="G3_EDGE")], {}, ID)
        self.assertEqual(report["counts"]["gate_refusals_by_reason"], {"G3_EDGE": 1})

    def test_phone_open_failure_then_success_is_not_a_gate_refusal(self):
        rows = [start(),
                {"kind": "open", "task_id": ID, "ts": 102.0, "status": "refused",
                 "dispatch_status": "not_submitted", "reason": "app_not_found",
                 "reason_code": "OTHER_REFUSAL", "note": "PRIVATE_CHAT_ORDER"},
                {"kind": "open", "task_id": ID, "ts": 103.0, "status": "completed",
                 "dispatch_status": "not_submitted", "reason": "vd_started",
                 "pkg": "com.android.calculator2"}, end()]
        report = task_report.build_report(rows, {}, ID)
        self.assertEqual(report["counts"]["dispatched_actions"], 1)
        self.assertEqual(report["counts"]["gate_refusals_by_reason"], {})
        self.assertEqual([(event["result"], event["reason_code"]) for event in report["timeline"]],
                         [("failed", "OP_APP_NOT_FOUND"), ("opened", None)])
        self.assertFalse(report["coverage"]["command_hook_linked"])
        self.assertNotIn("PRIVATE_CHAT_ORDER", json.dumps(report, ensure_ascii=False))

    def test_legacy_foreground_matched_without_dispatch_is_still_opened(self):
        row = {"kind": "open", "task_id": ID, "ts": 103.0, "status": "completed",
               "dispatch_status": "not_submitted", "reason": "foreground_matched"}
        report = task_report.build_report([row], {}, ID)
        self.assertEqual(report["counts"]["dispatched_actions"], 1)
        self.assertEqual(report["timeline"][0]["result"], "opened")

    def test_open_submitted_and_completed_is_dispatched_but_allowed_false_wins(self):
        row = {"kind": "open", "task_id": ID, "ts": 103.0, "status": "completed",
               "dispatch_status": "submitted", "reason": "foreground_matched"}
        self.assertEqual(task_report.build_report([row], {}, ID)["timeline"][0]["result"], "dispatched")
        row.update(allowed=False, reason_code="G3_EDGE")
        report = task_report.build_report([row], {}, ID)
        self.assertEqual(report["counts"]["dispatched_actions"], 0)
        self.assertEqual(report["counts"]["gate_refusals_by_reason"], {"G3_EDGE": 1})

    def test_unknown_action_failure_is_not_mislabelled_as_safety_refusal(self):
        report = task_report.build_report([action(allowed=False, reason="unexpected_error")], {}, ID)
        self.assertEqual(report["counts"]["gate_refusals_by_reason"], {})
        self.assertEqual(report["timeline"][0]["result"], "failed")
        self.assertEqual(report["timeline"][0]["reason_code"], "OP_FAILED")

    def test_unclassified_gate_refusal_still_counts_as_refusal(self):
        row = action(allowed=False, reason="新的闸门文案", points=[[10, 10]])
        self.assertEqual(task_report.reason_code_for(row), "OTHER_REFUSAL")
        report = task_report.build_report([dict(row, reason_code="OTHER_REFUSAL")], {}, ID)
        self.assertEqual(report["counts"]["gate_refusals_by_reason"], {"OTHER_REFUSAL": 1})
        self.assertEqual(report["timeline"][0]["result"], "refused")

    def test_unknown_structured_code_with_gate_points_falls_back_to_refusal(self):
        row = action(allowed=False, reason="new gate", points=[[10, 10]], reason_code="G6_NEW")
        with patch.dict(task_report.EXPLANATIONS, {"G6_NEW": "测试新闸门"}):
            report = task_report.build_report([row], {}, ID)
        self.assertEqual(report["counts"]["gate_refusals_by_reason"], {"OTHER_REFUSAL": 1})
        self.assertEqual(report["timeline"][0]["reason_code"], "OTHER_REFUSAL")

    def test_audit_failure_is_failed_even_with_gate_points(self):
        row = action(allowed=False, reason="audit_failed", points=[[10, 10]])
        report = task_report.build_report([row], {}, ID)
        self.assertEqual(report["counts"]["gate_refusals_by_reason"], {})
        self.assertEqual((report["timeline"][0]["result"], report["timeline"][0]["reason_code"]),
                         ("failed", "G5_AUDIT_FAILED"))

    def test_open_vd_policy_is_a_gate_refusal_without_points(self):
        row = {"kind": "open", "task_id": ID, "ts": 103.0, "status": "refused",
               "dispatch_status": "not_submitted", "reason": "vd_required:private", "vd_policy": "vd_required"}
        report = task_report.build_report([row], {}, ID)
        self.assertEqual(report["counts"]["gate_refusals_by_reason"], {"VD_REQUIRED": 1})

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
                 "decision": "allow", "would_decision": "deny", "mode": "shadow", "category": "destructive"},
                {"kind": "command_gate", "task_id": ID, "ts": 104,
                 "decision": "allow", "mode": "enforce", "warning": "typesafe_unavailable"}]
        report = task_report.build_report(rows, {}, ID)
        self.assertEqual(report["counts"]["command_hook_refusals"], 0)
        self.assertEqual(report["counts"]["command_hook_shadow_would_block"], 1)
        self.assertEqual(report["counts"]["typesafe_unavailable_warnings"], 1)
        self.assertEqual([event["reason_code"] for event in report["timeline"]],
                         ["SHADOW_WOULD_BLOCK", "TYPESAFE_UNAVAILABLE"])

    def test_empty_task_and_old_unlinked_rows(self):
        ledger = {"version": 1, "tasks": {ID: "reply_received"}, "latest": ID,
                  "active": None, "runtimeOpen": True}
        rows = [action(task_id=None, note="secret")]
        report = task_report.build_report(rows, ledger, ID)
        self.assertEqual(report["task"]["outcome"], "unknown")
        self.assertEqual(report["task"]["transport_state"], "reply_received")
        self.assertEqual(report["timeline"], [])
        self.assertEqual(report["counts"]["dispatched_actions"], 0)
        self.assertFalse(report["coverage"]["attribution_complete"])
        self.assertEqual(task_report.build_report([], {}, ID)["task"]["outcome"], "unknown")
        self.assertIn("归属证据不完整", task_report.render_markdown(report))

    def test_interleaved_tasks_never_use_another_tasks_rows(self):
        other = "22222222-2222-4222-8222-222222222222"
        rows = [start(), action(task_id=other, allowed=False, reason_code="G3_EDGE"),
                action(), {**end(), "task_id": other}, end()]
        report = task_report.build_report(rows, {}, ID)
        self.assertEqual(report["counts"]["dispatched_actions"], 1)
        self.assertEqual(report["counts"]["gate_refusals_by_reason"], {})

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

    def test_bridge_reply_is_not_script_completion_or_template_success(self):
        rows = [{"kind": "report_task_start", "task_id": ID, "ts": 100,
                 "evidence_kind": "bridge_admit", "audit_offset": 0},
                {"kind": "report_task_end", "task_id": ID, "ts": 110,
                 "terminal_status": "reply_received", "evidence_kind": "bridge_reply"}]
        report = task_report.build_report(rows, {}, ID)
        self.assertEqual(report["task"]["outcome"], "unknown")
        self.assertTrue(report["coverage"]["lifecycle_valid"])
        self.assertEqual(task_status.task_states(rows, "/synthetic/audit"), [])
        self.assertEqual(safe_tap_stats.aggregate_tasks(rows, [], set(), lambda _row: False), {})
        with tempfile.TemporaryDirectory() as tmp:
            audit = Path(tmp) / "audit.jsonl"
            audit.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            with patch.object(task_templates, "AUDIT", str(audit)):
                self.assertEqual(task_templates.summarize(), {})

    def test_explicit_bridge_and_web_attribution_evidence(self):
        bridge = [{"kind": "report_task_start", "task_id": ID, "ts": 100,
                   "evidence_kind": "bridge_admit"},
                  {"kind": "report_task_end", "task_id": ID, "ts": 110,
                   "terminal_status": "reply_received", "evidence_kind": "bridge_reply",
                   "attribution_complete": True}]
        report = task_report.build_report(bridge, {}, ID)
        self.assertTrue(report["coverage"]["attribution_complete"])
        self.assertEqual(report["task"]["outcome"], "unknown")
        bridge.insert(1, {"kind": "report_attribution_conflict", "task_id": ID, "ts": 105})
        self.assertFalse(task_report.build_report(bridge, {}, ID)["coverage"]["attribution_complete"])
        web = [{"kind": "report_task_start", "task_id": ID, "ts": 100,
                "evidence_kind": "web_turn_start"},
               {"kind": "report_task_end", "task_id": ID, "ts": 110,
                "terminal_status": "web_interrupted", "evidence_kind": "web_turn_end",
                "attribution_complete": True, "interrupted": True}]
        report = task_report.build_report(web, {}, ID)
        self.assertFalse(report["coverage"]["attribution_complete"])
        self.assertTrue(report["coverage"]["interrupted"])
        self.assertFalse(report["coverage"]["concurrent_turns"])

    def test_vocabulary_codes_are_a_single_allowlist(self):
        vocab = task_report.VOCABULARY
        self.assertEqual(set(task_report.EXPLANATIONS), set(vocab["reasons"]))
        self.assertTrue(task_report.GATE_REASON_CODES <= set(vocab["reasons"]))
        self.assertTrue({code for code, _, _ in task_report.REASONS} <= task_report.GATE_REASON_CODES)
        self.assertTrue(task_report.FAILURE_REASON_CODES <= set(vocab["reasons"]))
        self.assertFalse(task_report.FAILURE_REASON_CODES & task_report.GATE_REASON_CODES)
        self.assertNotIn("OP_APP_NOT_FOUND", task_report.GATE_REASON_CODES)
        self.assertEqual(set(task_report.ACTION_KINDS), set(vocab["action_kinds"]))
        self.assertEqual(set(task_report.READ_OPS), set(vocab["read_ops"]))
        self.assertTrue({code for code, _, _ in task_report.REASONS}.issubset(vocab["reasons"]))
        self.assertEqual(set(vocab["actions"]), set(vocab["action_kinds"] + vocab["read_ops"] +
                                                ["handoff", "command"]))

    def test_bridge_unverified_stop_has_no_terminal_evidence(self):
        report = task_report.build_report(
            [{"kind": "report_task_start", "task_id": ID, "ts": 100}], {}, ID)
        self.assertFalse(report["coverage"]["lifecycle_valid"])
        self.assertEqual(report["task"]["outcome"], "unknown")

    def test_bridge_confirmed_stop_is_not_script_completion(self):
        rows = [{"kind": "report_task_start", "task_id": ID, "ts": 100},
                {"kind": "report_task_end", "task_id": ID, "ts": 110,
                 "terminal_status": "cancelled", "evidence_kind": "stop_confirmed"}]
        report = task_report.build_report(rows, {}, ID)
        self.assertEqual(report["task"]["outcome"], "user_stopped")
        self.assertTrue(report["coverage"]["lifecycle_valid"])

    def test_timeline_is_bounded_but_counts_cover_all_rows(self):
        rows = [action() for _ in range(task_report.MAX_TIMELINE + 1)]
        report = task_report.build_report(rows, {}, ID)
        self.assertEqual(report["counts"]["dispatched_actions"], len(rows))
        self.assertEqual(len(report["timeline"]), task_report.MAX_TIMELINE)
        self.assertTrue(report["coverage"]["timeline_truncated"])

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

    def test_cli_offset_excludes_previous_same_id_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            audit, ledger = folder / "audit.jsonl", folder / "ledger.json"
            audit.write_text(json.dumps(action(allowed=False, reason_code="G3_EDGE")) + "\n", encoding="utf-8")
            offset = audit.stat().st_size
            with audit.open("a", encoding="utf-8") as stream:
                for row in [{"kind": "report_task_start", "task_id": ID, "ts": 100}, action(),
                            {"kind": "report_task_end", "task_id": ID, "ts": 110,
                             "terminal_status": "reply_received", "evidence_kind": "bridge_reply"}]:
                    stream.write(json.dumps(row) + "\n")
            ledger.write_text(json.dumps({"version": 1, "tasks": {ID: "reply_received"}}), encoding="utf-8")
            json_out, md_out = folder / "report.json", folder / "report.md"
            done = subprocess.run([sys.executable, task_report.__file__, "--audit", str(audit),
                                   "--ledger", str(ledger), "--task-id", ID, "--audit-offset", str(offset),
                                   "--json-out", str(json_out), "--markdown-out", str(md_out)],
                                  capture_output=True, text=True, timeout=5)
            self.assertEqual(done.returncode, 0, done.stderr)
            report = json.loads(json_out.read_text(encoding="utf-8"))
            self.assertEqual(report["counts"]["dispatched_actions"], 1)
            self.assertEqual(report["counts"]["gate_refusals_by_reason"], {})

    def test_cli_rejects_oversized_output_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            audit, ledger = folder / "audit.jsonl", folder / "ledger.json"
            audit.write_text(json.dumps(start()) + "\n", encoding="utf-8")
            ledger.write_text("{}", encoding="utf-8")
            json_out, md_out = folder / "report.json", folder / "report.md"
            args = ["--audit", str(audit), "--ledger", str(ledger), "--task-id", ID,
                    "--json-out", str(json_out), "--markdown-out", str(md_out)]
            with patch.object(task_report, "MAX_REPORT_BYTES", 8), self.assertRaises(SystemExit) as result:
                task_report.main(args)
            self.assertEqual(result.exception.code, 3)
            self.assertFalse(json_out.exists())
            self.assertFalse(md_out.exists())


if __name__ == "__main__":
    unittest.main()
