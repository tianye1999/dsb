"""Offline incident lifecycle tests using real state, look and dispatch gates."""
import contextlib
import builtins
import io
import json
import os
from pathlib import Path
import types
import unittest
from unittest.mock import patch

import safe_tap as st
import test_safe_tap_batch as fixture


class Incident(unittest.TestCase):
    setUp = fixture.Batch.setUp
    reset = fixture.Batch.reset
    run_batch = fixture.Batch.run_batch
    taps = staticmethod(fixture.Batch.taps)
    rows = fixture.Batch.rows

    @contextlib.contextmanager
    def incident_fault(self, fault):
        if fault == "corrupt":
            Path(st.STATE).write_text("{broken", encoding="utf-8")
            yield
        elif fault == "save":
            with patch.object(st, "save_state", side_effect=OSError("private failure detail")):
                yield
        else:
            real_import = builtins.__import__

            def import_module(name, *args, **kwargs):
                if name == "task_report":
                    raise ImportError("private failure detail")
                return real_import(name, *args, **kwargs)

            with patch.object(builtins, "__import__", side_effect=import_module):
                yield

    def test_refusal_audit_survives_incident_faults_on_every_entry(self):
        for fault in ("corrupt", "save", "import"):
            for entry in ("gate", "vd", "tap", "long_press", "drag", "open", "batch"):
                with self.subTest(fault=fault, entry=entry), contextlib.ExitStack() as stack:
                    self.reset()
                    count = len(self.rows())
                    events = []
                    real_record = st._record_incident

                    def audit(rec, events=events):
                        events.append("audit")
                        fixture.REAL_AUDIT(rec)

                    def record(*args, events=events, real_record=real_record, **kwargs):
                        events.append("incident")
                        return real_record(*args, **kwargs)

                    stack.enter_context(patch.object(st, "audit", side_effect=audit))
                    stack.enter_context(patch.object(st, "_record_incident", side_effect=record))
                    args = types.SimpleNamespace(shot="one", display=0, node="missing", x=400, y=900)
                    if entry == "gate":
                        stack.enter_context(patch.object(st, "check_gate", return_value=(False, "没有截图凭据", {})))
                        fn = lambda a: st.do_action(a, "tap", [(400, 900)])
                    elif entry == "vd":
                        stack.enter_context(patch.object(st, "vd_policy_block", return_value=("policy", "vd_required")))
                        fn = lambda a: st.do_action(a, "tap", [(400, 900)])
                    elif entry == "open":
                        stack.enter_context(patch.object(st, "sh", return_value="package:com.example.app"))
                        stack.enter_context(patch.object(st, "vd_policy_block", return_value=("policy", "vd_required")))
                        args = types.SimpleNamespace(app="com.example.app", allow_main=False)
                        fn = st.cmd_open
                    elif entry == "batch":
                        args = types.SimpleNamespace(display=0, steps='[{"kind":"key"}]')
                        fn = st.cmd_batch
                    else:
                        stack.enter_context(patch.object(st, "resolve_handle", return_value={"ok": False, "why": "missing"}))
                        fn = getattr(st, "cmd_" + entry)
                    stack.enter_context(self.incident_fault(fault))
                    rc, _, err = self.call(fn, args)
                    self.assertEqual(rc, 2)
                    self.assertEqual(events, ["audit", "incident"])
                    rows = self.rows()[count:]
                    self.assertEqual(len(rows), 1)
                    self.assertTrue(rows[0].get("allowed") is False or rows[0].get("status") == "refused")
                    self.assertEqual(err.count("open_incident 未保存：状态不可用"), 1)
                    self.assertNotIn("private failure detail", err)

    def test_dispatch_audit_and_rc_survive_incident_faults(self):
        for fault in ("corrupt", "save", "import"):
            for status in ("not_submitted", "unknown", "timeout"):
                with self.subTest(fault=fault, status=status):
                    self.reset()
                    count = len(self.rows())
                    with self.incident_fault(fault), patch.object(
                            st, "_do_action_locked", side_effect=st.ActionDispatchError("dispatch_failed", status)):
                        rc, out, err = self.call(lambda a: st.do_action(a, "tap", []),
                                                 types.SimpleNamespace(display=0))
                    self.assertEqual((rc, out["dispatch_status"]), (3, status))
                    self.assertEqual(self.rows()[count:][0]["dispatch_status"], status)
                    self.assertEqual(err.splitlines(), ["open_incident 未保存：状态不可用"])

    def test_success_audit_precedes_state_update_and_clear(self):
        self.action(x=0)
        self.look()
        events = []
        real_save = st.save_state

        def audit(rec):
            events.append("audit")
            fixture.REAL_AUDIT(rec)

        def save(state):
            if state.get("taps", [{}])[-1].get("outcome") == "command_completed":
                events.append("state_update" if "open_incident" in state else "clear")
            else:
                events.append("reserve")
            real_save(state)

        with patch.object(st, "audit", side_effect=audit), patch.object(st, "save_state", side_effect=save):
            rc, out, _ = self.action()
        self.assertEqual((rc, out["ui_reacted"]), (0, True))
        self.assertEqual(events, ["reserve", "audit", "state_update", "clear"])

    def test_incident_clear_failure_does_not_change_success_output(self):
        for fault in ("load", "save"):
            with self.subTest(fault=fault):
                self.reset()
                self.action(x=0)
                self.look()
                updated = []
                real_load, real_save = st.load_state, st.save_state
                count = len(self.rows())

                def load(updated=updated, fault=fault, real_load=real_load):
                    if updated and fault == "load":
                        raise ValueError("private failure detail")
                    return real_load()

                def save(state, fault=fault, real_save=real_save, updated=updated):
                    if "open_incident" not in state and fault == "save":
                        raise OSError("private failure detail")
                    real_save(state)
                    if state.get("taps", [{}])[-1].get("outcome") == "command_completed":
                        updated.append(True)

                with patch.object(st, "load_state", side_effect=load), patch.object(st, "save_state", side_effect=save):
                    rc, out, err = self.action()
                self.assertEqual((rc, out["dispatch_status"], out["ui_reacted"]), (0, "command_completed", True))
                self.assertEqual(len(self.rows()[count:]), 1)
                self.assertIn("open_incident", st.load_state())
                self.assertEqual(err.splitlines(), ["open_incident 未清除：状态不可用"])

    def test_no_reaction_incident_failure_preserves_completion_and_audit(self):
        self.react = False
        real_save = st.save_state
        count = len(self.rows())

        def save(state):
            if "open_incident" in state:
                raise OSError("private failure detail")
            real_save(state)

        with patch.object(st, "save_state", side_effect=save):
            rc, out, err = self.action()
        self.assertEqual((rc, out["dispatch_status"], out["ui_reacted"]), (0, "command_completed", False))
        self.assertEqual(self.rows()[count:][0]["dispatch_status"], "command_completed")
        self.assertEqual(err.splitlines(), ["open_incident 未保存：状态不可用"])

    def test_incident_helpers_do_not_swallow_process_control(self):
        for error in (KeyboardInterrupt, SystemExit):
            for operation in (lambda: st._record_incident({"kind": "tap", "reason_code": "OP_FAILED"}),
                              lambda: st._clear_incident()):
                with self.subTest(error=error, operation=operation), patch.object(st, "load_state", side_effect=error):
                    with self.assertRaises(error):
                        operation()

    def call(self, fn, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fn(args)
        return rc, json.loads(out.getvalue()) if out.getvalue() else None, err.getvalue()

    def look(self, **kwargs):
        return self.call(st.cmd_look, types.SimpleNamespace(note="", **kwargs))

    def action(self, x=400):
        return self.call(st.cmd_tap, types.SimpleNamespace(
            shot=st.load_state()["last_shot"]["id"], x=x, y=900,
            display=0, fast=False, ack_no_zones=False, expect=None,
            expect_activity=None))

    def test_refusals_persist_across_look_then_reaction_clears(self):
        self.assertEqual(self.action(x=0)[0], 2)
        _, out, err = self.look()
        first = out["open_incident"]
        self.assertEqual((first["kind"], first["reason_code"], first["consecutive"]),
                         ("tap", "G3_EDGE", 1))
        self.assertEqual(first["task_id"], fixture.TASK_ID)
        self.assertEqual(first["display_id"], 0)
        self.assertIn("G3_EDGE", err)
        self.assertIn("不要原样重试", err)
        self.assertEqual(self.action(x=0)[0], 2)
        _, out, _ = self.look()
        second = out["open_incident"]
        self.assertEqual(second["consecutive"], 2)
        self.assertEqual(second["first_ts"], first["first_ts"])
        self.assertGreaterEqual(second["last_ts"], first["last_ts"])
        rc, result, _ = self.action()
        self.assertEqual(rc, 0)
        self.assertTrue(result["ui_reacted"])
        self.assertIsNone(st.load_state().get("open_incident"))
        self.assertIsNone(self.look()[1]["open_incident"])

    def test_completed_without_reaction_is_failure(self):
        self.react = False
        rc, out, _ = self.action()
        self.assertEqual(rc, 0)
        self.assertFalse(out["ui_reacted"])
        self.assertEqual(self.look()[1]["open_incident"]["reason_code"], "OP_FAILED")

    def test_failed_look_keeps_incident_and_hint(self):
        self.action(x=0)
        rc, out, err = self.look(target=" ")
        self.assertEqual(rc, 3)
        self.assertEqual(out["open_incident"]["reason_code"], "G3_EDGE")
        self.assertIn("G3_EDGE", err)

    def test_dispatch_error_from_real_action_path_records(self):
        for status in ("not_submitted", "unknown"):
            with patch.object(st, "sh", side_effect=st.ActionDispatchError("adb_timeout", status)):
                self.assertEqual(self.action()[0], 3)
            self.assertEqual(self.look()[1]["open_incident"]["reason_code"], "OP_FAILED")

    def test_incident_updates_hold_one_lock_including_early_refusals(self):
        held = []
        original_save = st.save_state

        @contextlib.contextmanager
        def lock():
            self.assertFalse(held, "nested lock would deadlock")
            held.append(True)
            try:
                yield
            finally:
                held.pop()

        def save(state):
            self.assertEqual(held, [True])
            original_save(state)

        with patch.object(st, "lock", lock), patch.object(st, "save_state", save):
            self.action(x=0)
            self.look()
            with patch.object(st, "resolve_handle", return_value={"ok": False, "why": "missing"}):
                self.call(st.cmd_tap, types.SimpleNamespace(node="missing", display=0))
            self.run_batch([{"kind": "key"}])
            self.run_batch([{"kind": "tap", "x": 0, "y": 900, "expect": "edge"}])

    def test_unknown_task_does_not_inherit_known_task_incident(self):
        self.action(x=0)
        with patch.object(st.task_context, "audit_fields", return_value={"task_context": "unknown"}):
            self.assertIsNone(self.look()[1]["open_incident"])
            self.action(x=0)
            self.assertEqual(self.look()[1]["open_incident"]["consecutive"], 1)
        self.assertIsNone(self.look()[1]["open_incident"])

    def test_dispatch_errors_accumulate_across_kinds(self):
        for count, (kind, status) in enumerate(
                (("tap", "not_submitted"), ("type", "unknown"), ("key", "timeout")), 1):
            with patch.object(st, "_do_action_locked", side_effect=st.ActionDispatchError(
                    "中文拒绝句子：私密屏幕文字", status)):
                self.call(lambda a, kind=kind: st.do_action(a, kind, []), types.SimpleNamespace(display=7))
            incident = self.look()[1]["open_incident"]
            self.assertEqual((incident["kind"], incident["reason_code"], incident["consecutive"],
                              incident["display_id"]), (kind, "OP_FAILED", count, 7))
            self.assertNotIn("私密屏幕文字", Path(st.STATE).read_text())

    def test_task_and_generation_changes_hide_old_failure_and_reset_count(self):
        self.action(x=0)
        for env in ({"DSB_TASK_ID": "22222222-2222-4222-8222-222222222222"},
                    {"DSB_TASK_GENERATION": "33333333-3333-4333-8333-333333333333"}):
            with patch.dict(os.environ, env):
                self.assertIsNone(self.look()[1]["open_incident"])
                self.action(x=0)
                self.assertEqual(self.look()[1]["open_incident"]["consecutive"], 1)

    def test_refusal_text_never_enters_incident_or_later_look(self):
        reason = "落在禁区：中文拒绝句子，私密屏幕文字"
        with patch.object(st, "check_gate", return_value=(False, reason, {})):
            self.action()
        _, out, err = self.look()
        for payload in (Path(st.STATE).read_text(), json.dumps(out, ensure_ascii=False), err):
            self.assertNotIn("中文拒绝句子", payload)
            self.assertNotIn("私密屏幕文字", payload)
        self.assertEqual(st.load_state()["open_incident"]["reason_code"], "G2_DANGER_ZONE")
        self.assertEqual(set(st.load_state()["open_incident"]), {
            "kind", "reason_code", "consecutive", "first_ts", "last_ts",
            "display_id", "task_id", "task_generation"})

    def test_r3_categories_survive_look_without_private_evidence(self):
        hints = {"target_shifted": "目标可能已挪位", "target_occupied": "先处理遮挡物或等待",
                 "target_disappeared": "确认当前页面", "unclassified": "目标去向无法确认"}
        for category, hint in hints.items():
            with self.subTest(category=category):
                self.reset()
                st._record_incident({"kind": "tap", "allowed": False, "reason_code": "R3_CHANGED",
                                     "r3_category": category, "r3_evidence": {"text": "private text",
                                     "text_sha256": "0123456789abcdef"}})
                _, out, err = self.look()
                incident = out["open_incident"]
                self.assertEqual(incident["r3_category"], category)
                self.assertEqual(incident["reason_code"], "R3_CHANGED")
                self.assertIn(hint, err)
                self.assertIn("不要原样重试", err)
                self.assertNotIn("private text", json.dumps(incident))
                self.assertNotIn("0123456789abcdef", json.dumps(incident))
                self.assertNotIn("r3_evidence", incident)
                self.assertNotIn("text", incident)
                self.assertEqual(set(incident), {"kind", "reason_code", "consecutive", "first_ts", "last_ts",
                                                "display_id", "task_id", "task_generation", "r3_category"})
                self.assertEqual(self.action(x=0)[0], 2)
                self.assertNotIn("r3_category", st.load_state()["open_incident"])

    def test_incident_rejects_non_enum_r3_category(self):
        st._record_incident({"kind": "tap", "reason_code": "R3_CHANGED", "r3_category": "private text"})
        self.assertNotIn("r3_category", st.load_state()["open_incident"])

    def test_tampered_incident_category_is_removed_on_read(self):
        for category in ("private screen text", ["private screen text"], {"private": "screen text"}, 42, None):
            self.reset()
            self.action(x=0)
            state = st.load_state()
            state["open_incident"]["r3_category"] = category
            st.save_state(state)
            rc, out, err = self.look()
            self.assertEqual(rc, 0)
            self.assertNotIn("r3_category", out["open_incident"])
            self.assertNotIn("private screen text", json.dumps(out))
            self.assertNotIn("private screen text", err)

    def test_look_writes_raw_app_index_without_extra_observation(self):
        with patch.object(st, "observe", wraps=st.observe) as observe:
            rc, out, _ = self.look()
        self.assertEqual(rc, 0)
        self.assertEqual(observe.call_count, 1)
        index = Path(out["screenshot"] + ".nodes.json").read_text()
        self.assertNotIn("Ready", index)
        self.assertEqual(json.loads(index)[0]["cls"], "android.widget.TextView")
        self.assertEqual(set(json.loads(index)[0]), {"rect", "cls", "text_sha256"})

    def test_vd_policy_and_node_refusals_record_existing_codes(self):
        with patch.object(st, "vd_policy_block", return_value=("私密屏幕文字", "vd_required")):
            self.action()
        self.assertEqual(self.look()[1]["open_incident"]["reason_code"], "VD_REQUIRED")
        for fn, kind in ((st.cmd_tap, "tap"), (st.cmd_long_press, "long_press"), (st.cmd_drag, "drag")):
            with patch.object(st, "resolve_handle", return_value={"ok": False, "why": "私密屏幕文字"}):
                self.call(fn, types.SimpleNamespace(node="private-handle", display=4))
            incident = self.look()[1]["open_incident"]
            self.assertEqual((incident["kind"], incident["reason_code"], incident["display_id"]),
                             (kind, "OP_FAILED", 4))

    def test_open_policy_refusal_records(self):
        with patch.object(st, "sh", return_value="package:com.example.app"), \
                patch.object(st, "vd_policy_block", return_value=("private", "vd_required")):
            self.call(st.cmd_open, types.SimpleNamespace(app="com.example.app", allow_main=False))
        self.assertEqual(self.look()[1]["open_incident"]["kind"], "open")
        self.assertEqual(st.load_state()["open_incident"]["reason_code"], "VD_REQUIRED")

    def test_batch_stops_record_code_and_display_even_before_execution(self):
        rc, out, _ = self.run_batch([{"kind": "key"}], display=8)
        self.assertEqual(rc, 2)
        incident = self.look()[1]["open_incident"]
        self.assertEqual((incident["kind"], incident["reason_code"], incident["display_id"]),
                         ("batch", out["stop_reason_code"], 8))

    def test_batch_unwritable_state_preserves_stop_report_without_raw_error(self):
        with patch.object(st, "save_state", side_effect=OSError("私密屏幕文字")):
            rc, out, err = self.run_batch([{"kind": "key"}])
        self.assertEqual((rc, out["stop_reason_code"]), (2, "BATCH_UNSUPPORTED"))
        self.assertIn("open_incident 未保存", err)
        self.assertNotIn("私密屏幕文字", err)

    def test_batch_no_reaction_records_stop_and_internal_look_has_field(self):
        captured = []

        def look(*args, **kwargs):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rc = fixture.REAL_LOOK(*args, **kwargs)
            captured.append(json.loads(output.getvalue()))
            self.react = False
            return rc

        with patch.object(st, "_look_locked", side_effect=look):
            rc, out, _ = self.run_batch(self.taps(2))
        self.assertEqual(rc, 2)
        self.assertIsNone(captured[0]["open_incident"])
        self.assertEqual(out["stop_reason_code"], "BATCH_NO_REACTION")
        incident = st.load_state()["open_incident"]
        self.assertEqual((incident["kind"], incident["reason_code"]), ("batch", "BATCH_NO_REACTION"))


if __name__ == "__main__":
    unittest.main()
