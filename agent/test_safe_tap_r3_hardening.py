"""Offline gate-view isolation and bounded R3 evidence tests."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import safe_tap as st
import test_safe_tap_credentials as fixture


REAL_DUMP = st.dump_nodes
REAL_BUILD_ZONES = st.build_zones


class GateViews(unittest.TestCase):
    setUp = fixture.Credentials.setUp
    reset = fixture.Credentials.reset
    action = fixture.Credentials.action

    def test_description_only_xml_keeps_empty_gate_view_and_ocr_ack(self):
        xml = '<hierarchy><node text="" content-desc="Toolbar" bounds="[50,50][100,100]" /></hierarchy>'
        for rows in ([], [{"text": "删除", "x": 600, "y": 900, "w": 80, "h": 40}]):
            self.reset()
            out = io.StringIO()
            with patch.object(st, "dump_nodes", REAL_DUMP), patch.object(st, "sh", return_value=xml) as shell, \
                    patch.object(st, "grab", return_value=os.path.join(st.SHOTS, "shot-123.jpg")), \
                    patch.object(st, "build_zones", REAL_BUILD_ZONES), patch.object(st, "OCR_OK", True), \
                    patch.object(st, "pixel_recognize_result", return_value={"status": "ok", "rows": rows}) as ocr, \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(st.cmd_look(types.SimpleNamespace(note="", target="Toolbar")), 0)
            result = json.loads(out.getvalue())
            self.assertEqual((result["zone_coverage"], result["node_count"]), ("none", 0))
            self.assertEqual(result["danger_zones"], int(bool(rows)))
            self.assertEqual(result["target"]["status"], "not_found")
            ocr.assert_called_once()
            self.assertEqual(shell.call_count, 2)
            index = json.loads(Path(result["screenshot"] + ".nodes.json").read_text())
            self.assertEqual(len(index), 1)
            self.assertIn("desc_sha256", index[0])
            for ack in (False, True):
                self.assertEqual(self.action(ack=ack), 2)
            self.assertEqual(self.calls, [])

    def test_gate_views_match_original_fields_and_text_values(self):
        raw = {"nodes": [{"text": "", "desc": "Only description", "rect": [1, 2, 3, 4]},
                         {"text": " A & B ", "desc": "Other", "rect": [10, 20, 30, 40], "cls": "Button"}]}
        self.assertEqual(st._nodes_from(raw), [{"text": "A & B", "cx": 20, "cy": 30, "rect": [10, 20, 30, 40]}])
        xml = ('<hierarchy><node text=" A &amp; B " content-desc="Other" bounds="[10,20][30,40]" />'
               '<node text="" content-desc="Only description" bounds="[1,2][3,4]" /></hierarchy>')
        with patch.object(st, "sh", return_value=xml):
            self.assertEqual(st.dump_nodes_uiautomator(),
                             [{"text": "A &amp; B", "cx": 20, "cy": 30, "rect": [10, 20, 30, 40]}])


class IndexFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cred = str(Path(self.tmp.name) / "shot-123.jpg")
        self.sidecar = Path(self.cred + ".nodes.json")
        self.node = {"rect": [10, 10, 20, 20], "text": "private text"}

    def test_fifo_symlink_and_oversize_read_never_block_or_follow(self):
        for kind in ("fifo", "symlink", "oversize"):
            with self.subTest(kind=kind):
                if kind == "fifo":
                    os.mkfifo(self.sidecar)
                elif kind == "symlink":
                    self.sidecar.symlink_to("/dev/zero")
                else:
                    self.sidecar.write_bytes(b" " * (1024 * 1024 + 1))
                script = ('import safe_tap as s,sys; s.observe=lambda **k: (_ for _ in ()).throw('
                          'AssertionError("must not fetch")); print(s._r3_classify(sys.argv[1],0,(15,15)))')
                run = subprocess.run([sys.executable, "-c", script, self.cred], capture_output=True,
                                     text=True, timeout=3, cwd=Path(__file__).parent)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertIn("unclassified", run.stdout)
                self.sidecar.unlink()

    def test_writer_atomically_replaces_link_without_touching_target(self):
        target = Path(self.tmp.name) / "victim"
        target.write_text("keep me")
        self.sidecar.symlink_to(target)
        st._save_target_index(self.cred, [self.node])
        self.assertEqual(target.read_text(), "keep me")
        self.assertFalse(self.sidecar.is_symlink())
        self.assertEqual(stat.S_IMODE(self.sidecar.stat().st_mode), 0o600)
        self.assertNotIn("private text", self.sidecar.read_text())

    def test_replace_failure_preserves_index_and_removes_temporary_file(self):
        self.sidecar.write_text("previous")
        err = io.StringIO()
        with patch.object(st.os, "replace", side_effect=OSError("private failure")), contextlib.redirect_stderr(err):
            st._save_target_index(self.cred, [self.node])
        self.assertEqual(self.sidecar.read_text(), "previous")
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [self.sidecar])
        self.assertEqual(len(err.getvalue().splitlines()), 1)
        self.assertNotIn("private failure", err.getvalue())

    def test_oversize_rejected_before_reading(self):
        self.sidecar.write_bytes(b" " * (1024 * 1024 + 1))
        with patch.object(st.os, "fdopen") as reader, patch.object(st, "observe") as tree:
            self.assertEqual(st._r3_classify(self.cred, 0, (15, 15)), ("unclassified", {}, ""))
        reader.assert_not_called()
        tree.assert_not_called()

    def test_read_and_write_use_required_open_flags(self):
        real_open = os.open
        with patch.object(st.os, "open", wraps=real_open) as opener:
            st._save_target_index(self.cred, [self.node])
        flags, mode = opener.call_args.args[1:]
        self.assertEqual(flags & (os.O_EXCL | os.O_NOFOLLOW | os.O_CREAT), os.O_EXCL | os.O_NOFOLLOW | os.O_CREAT)
        self.assertEqual(mode, 0o600)
        with patch.object(st.os, "open", wraps=real_open) as opener:
            self.assertEqual(len(st._read_target_index(self.cred)), 1)
        self.assertEqual(opener.call_args.args[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)


class XmlLimits(unittest.TestCase):
    def test_invalid_xml_is_an_empty_gate_view(self):
        cases = ['<hierarchy><node text="valid" bounds="[1,2][3,4]" />',
                 '<hierarchy><node /></wrong>', '<!DOCTYPE x><hierarchy />',
                 '<!DOCTYPE x [<!ENTITY e "expanded">]><hierarchy />',
                 '<hierarchy>' + ' ' * (8 * 1024 * 1024) + '</hierarchy>',
                 '<hierarchy><node text="x" bounds="[' + '9' * 5000 + ',2][3,4]" /></hierarchy>']
        for xml in cases:
            with self.subTest(prefix=xml[:30]), patch.object(st, "sh", return_value=xml):
                self.assertEqual(st.dump_nodes_uiautomator(), [])

    def test_any_parser_exception_is_empty(self):
        with patch.object(st, "sh", return_value="<hierarchy />"), \
                patch.object(st.ET, "fromstring", side_effect=ValueError("broken parser")):
            self.assertEqual(st.dump_nodes_uiautomator(), [])


class TransportLimit(unittest.TestCase):
    def test_short_observation_caps_subprocess_not_just_app_poll(self):
        with patch.object(st, "adb_command", return_value=["synthetic-adb"]), \
                patch.object(st.subprocess, "run", side_effect=subprocess.TimeoutExpired("synthetic", 2)) as run:
            self.assertIsNone(st.observe(with_image=False, timeout=1.0, transport_timeout=2.0))
        self.assertEqual(run.call_args.kwargs["timeout"], 2.0)


if __name__ == "__main__":
    unittest.main()
