import contextlib
import copy
import io
import json
import tempfile
import types
import unittest
from unittest.mock import patch
import safe_tap as st


class SafeOCR(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.state = {"last_shot": {"id": "old"}}
        def save(state): self.state = copy.deepcopy(state)
        replacements = {"SHOTS": tmp.name, "load_state": lambda: copy.deepcopy(self.state),
            "save_state": save, "observe": lambda **kw: None, "grab": lambda *a, **kw: "/test.png",
            "dump_nodes": lambda: [], "foreground": lambda: "test/.Page",
            "build_zones": lambda *a: [], "_ball": lambda *a: None, "audit": lambda *a: None,
            "wakefulness": lambda: "Awake", "screen_size": lambda: (1260, 2800),
            "cfg_get": lambda k, d=None: d, "OCR_OK": True,
            "_ocr_cancel_check": lambda: lambda: False}
        for key, value in replacements.items():
            p = patch.object(st, key, value, create=True); p.start(); self.addCleanup(p.stop)

    def call(self, fn, args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            rc = fn(args)
        return rc, json.loads(output.getvalue())

    def test_fallback_without_atomic_observation_does_not_crash(self):
        with patch.object(st, "dump_nodes", return_value=[{"text": "Settings"}]):
            rc, out = self.call(st.cmd_look, types.SimpleNamespace(note="test"))
        self.assertEqual(rc, 0)
        self.assertEqual(out["hints"]["already_active"], [])

    def test_failed_ocr_invalidates_old_credential(self):
        with patch.object(st, "pixel_recognize_result", return_value={"status": "timeout", "rows": []}, create=True), \
             patch.object(st, "pixel_recognize", side_effect=RuntimeError("timeout"), create=True):
            rc, out = self.call(st.cmd_look, types.SimpleNamespace(note="test"))
        self.assertEqual(rc, 3)
        self.assertFalse(out["ok"])
        self.assertNotIn("shot_id", out)
        self.assertNotIn("last_shot", self.state)

    def test_successful_empty_does_not_grant_zone_coverage(self):
        with patch.object(st, "pixel_recognize_result", return_value={"status": "empty", "rows": []}, create=True):
            rc, out = self.call(st.cmd_look, types.SimpleNamespace(note="test"))
        self.assertEqual(rc, 0)
        self.assertEqual(out["zone_coverage"], "none")
        self.assertEqual(out["ocr"]["status"], "empty")

    def test_explicit_ocr_covers_partial_tree(self):
        with patch.object(st,"dump_nodes",return_value=[{"text":"Toolbar"}]), \
             patch.object(st,"pixel_recognize_result",return_value={"status":"ok","rows":[]}) as mock:
            rc,out=self.call(st.cmd_look,types.SimpleNamespace(note="test",ocr=True))
        self.assertEqual(rc,0)
        mock.assert_called_once()
        self.assertEqual(out["ocr"]["status"],"ok")

    def test_wait_uses_remaining_budget_and_reports_failure(self):
        args = types.SimpleNamespace(stable=False, text="missing", timeout=.2, interval=.01)
        with patch.object(st, "pixel_recognize_result", return_value={"status": "timeout", "rows": []}, create=True) as mock:
            rc, out = self.call(st.cmd_wait, args)
        self.assertEqual(rc, 3)
        self.assertEqual(out["ocr"]["status"], "timeout")
        self.assertLessEqual(mock.call_args.kwargs["timeout_s"], .2)

    def target(self, text="POC READY", **kwargs):
        return self.call(st.cmd_look, types.SimpleNamespace(note="poc", target=text, **kwargs))

    def test_target_missing_from_partial_tree_runs_ocr_once(self):
        row = {"text": "POC READY", "x": 300, "y": 400, "w": 100, "h": 40,
               "confidence": .99, "source": "pixels"}
        with patch.object(st, "dump_nodes", return_value=[{"text": "Toolbar"}]), \
             patch.object(st, "observe", return_value=None) as observe, \
             patch.object(st, "pixel_recognize_result", return_value={"status": "ok", "rows": [row]}) as ocr:
            rc, out = self.target()
        self.assertEqual(rc, 0)
        ocr.assert_called_once()
        self.assertTrue(observe.call_args.kwargs["force"])
        self.assertEqual(out["target"], {"text": "POC READY", "status": "found",
                         "source": "pixels", "match": "casefold_no_whitespace", "candidates": [row]})

    def test_ocr_spacing_and_case_preserve_raw_evidence(self):
        row = {"text": "EMPTY-TREETESTPAGE(CanVaS)", "x": 480, "y": 960, "w": 850, "h": 63}
        with patch.object(st, "pixel_recognize_result", return_value={"status": "ok", "rows": [row]}):
            rc, out = self.target("EMPTY-TREE TEST PAGE (canvas)")
        self.assertEqual(out["target"]["status"], "found")
        self.assertEqual(out["target"]["candidates"][0]["text"], row["text"])

    def test_target_in_tree_skips_ocr(self):
        node = {"text": "POC READY", "rect": [10, 20, 100, 50]}
        with patch.object(st, "dump_nodes", return_value=[node]), \
             patch.object(st, "pixel_recognize_result") as ocr:
            rc, out = self.target(" POC READY ")
        ocr.assert_not_called()
        self.assertEqual(out["target"]["source"], "tree")
        self.assertEqual(out["target"]["status"], "found")

    def test_duplicate_pixel_targets_are_ambiguous_not_first_match(self):
        rows = [{"text": "POC READY", "x": x, "y": 400, "w": 80, "h": 30} for x in (100, 500)]
        with patch.object(st, "pixel_recognize_result", return_value={"status": "ok", "rows": rows}):
            rc, out = self.target()
        self.assertEqual(out["target"]["status"], "ambiguous")
        self.assertEqual(len(out["target"]["candidates"]), 2)
        self.assertEqual(out["zone_coverage"], "none")

    def test_missing_target_is_not_found_not_successful_match(self):
        with patch.object(st, "pixel_recognize_result", return_value={"status": "empty", "rows": []}):
            rc, out = self.target()
        self.assertEqual(out["target"]["status"], "not_found")

    def test_disabled_ocr_is_unknown_even_with_partial_tree(self):
        with patch.object(st, "dump_nodes", return_value=[{"text": "Toolbar"}]), \
             patch.object(st, "cfg_get", side_effect=lambda k, d=None: False if k == "ocr.enabled" else d):
            rc, out = self.target()
        self.assertEqual(out["target"]["status"], "unknown")
        self.assertEqual(out["ocr"]["status"], "disabled")

    def test_target_ocr_timeout_invalidates_credential(self):
        with patch.object(st, "dump_nodes", return_value=[{"text": "Toolbar"}]), \
             patch.object(st, "pixel_recognize_result", return_value={"status": "timeout", "rows": []}):
            rc, out = self.target()
        self.assertEqual(rc, 3)
        self.assertNotIn("last_shot", self.state)

    def test_target_requires_exact_text_not_substring(self):
        with patch.object(st, "dump_nodes", return_value=[{"text": "POC READY EXTRA"}]), \
             patch.object(st, "pixel_recognize_result", return_value={"status": "empty", "rows": []}) as ocr:
            rc, out = self.target()
        ocr.assert_called_once()
        self.assertEqual(out["target"]["status"], "not_found")

    def test_sleeping_screen_does_not_claim_target_found(self):
        with patch.object(st, "dump_nodes", return_value=[{"text": "POC READY"}]), \
             patch.object(st, "wakefulness", return_value="Asleep"):
            rc, out = self.target()
        self.assertEqual(out["target"]["status"], "unknown")

    def test_empty_target_is_rejected_before_capture(self):
        with patch.object(st, "observe") as observe:
            rc, out = self.target("  ")
        self.assertEqual(rc, 3)
        observe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
