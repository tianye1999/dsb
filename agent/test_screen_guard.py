"""screen_guard prepare/restore contract; adb and the state file are intercepted."""
import os
import tempfile
import unittest
from unittest.mock import patch
import screen_guard as sg


class ScreenGuard(unittest.TestCase):
    def setUp(self):
        self.settings = {}
        self.puts = []
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = os.path.join(directory.name, "screen_guard_state.json")
        for name, value in {"STATE": self.state, "_get": lambda ns, key: self.settings.get(key, ""),
                            "_put": lambda ns, key, value: self.puts.append((key, str(value))),
                            "sh": lambda *a, **kw: ""}.items():
            p = patch.object(sg, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(sg.subprocess, "run", side_effect=OSError("no pgrep"))
        p.start()
        self.addCleanup(p.stop)

    def test_unreadable_settings_are_not_recorded_or_changed(self):
        out = sg.prepare(20)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "settings_read_failed")
        self.assertFalse(os.path.exists(self.state))
        self.assertEqual(self.puts, [])

    def test_partial_read_is_refused(self):
        self.settings = {"screen_off_timeout": "30000"}
        self.assertFalse(sg.prepare(20)["ok"])
        self.assertFalse(os.path.exists(self.state))
        self.assertEqual(self.puts, [])

    def test_unset_key_is_refused(self):
        for key in ("screen_off_timeout", "stay_on_while_plugged_in"):
            self.settings = {"screen_off_timeout": "30000", "stay_on_while_plugged_in": "0", key: "null"}
            self.assertFalse(sg.prepare(20)["ok"])
            self.assertFalse(os.path.exists(self.state))
            self.assertEqual(self.puts, [])

    def test_prepare_then_restore_returns_original_values(self):
        self.settings = {"screen_off_timeout": "30000", "stay_on_while_plugged_in": "0"}
        self.assertTrue(sg.prepare(20)["ok"])
        self.assertIn(("screen_off_timeout", "1200000"), self.puts)
        self.assertIn(("stay_on_while_plugged_in", "7"), self.puts)
        self.puts.clear()
        self.assertTrue(sg.restore()["ok"])
        self.assertEqual(self.puts, [("screen_off_timeout", "30000"), ("stay_on_while_plugged_in", "0")])
        self.assertFalse(os.path.exists(self.state))

    def test_second_prepare_keeps_first_originals(self):
        self.settings = {"screen_off_timeout": "30000", "stay_on_while_plugged_in": "0"}
        sg.prepare(20)
        self.settings = {"screen_off_timeout": "1200000", "stay_on_while_plugged_in": "7"}
        self.assertEqual(sg.prepare(20)["original"], {"screen_off_timeout": "30000", "stay_on": "0"})


if __name__ == "__main__":
    unittest.main()
