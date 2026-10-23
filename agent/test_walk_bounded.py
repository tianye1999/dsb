import contextlib
import io
import json
import sys
import unittest
from unittest.mock import patch
import walk


def frame(text="row", **extra):
    return {"nodes": [{"text": text, "rect": [100, 300, 600, 400]}],
            "shot_id": "s", "screenshot": "/test/s.jpg", **extra}


class BoundedWalk(unittest.TestCase):
    def run_walk(self, args=(), observations=None, scroll_rc=0):
        output = io.StringIO()
        observations = observations or [(frame(str(i)), 0) for i in range(40)]
        with patch.object(sys, "argv", ["walk.py", "--json", *args]), \
                patch.object(walk, "_look", side_effect=observations) as look, \
                patch.object(walk, "_scroll", return_value=(scroll_rc, {})) as scroll, \
                patch.object(walk.st, "load_state", return_value={"last_shot": {"id": "s", "node_count": 1}}), \
                contextlib.redirect_stdout(output):
            rc = walk.main()
        return rc, json.loads(output.getvalue()), look.call_count, scroll.call_count

    def test_default_stops_after_three_scrolls(self):
        rc, result, looks, scrolls = self.run_walk()
        self.assertEqual((looks, scrolls), (4, 3))
        self.assertEqual(result["stop"], "max-scrolls")
        self.assertFalse(result["complete"])

    def test_initial_substring_match_does_not_scroll(self):
        rc, result, looks, scrolls = self.run_walk(["--until-text", "夕"], [(frame("今夕何夕"), 0)])
        self.assertEqual((rc, looks, scrolls), (0, 1, 0))
        self.assertEqual(result["stop"], "target-found")
        self.assertEqual(result["matches"][0]["text"], "今夕何夕")
        self.assertEqual(result["matches"][0]["source"], "tree")

    def test_later_match_stops_before_next_scroll(self):
        rc, result, looks, scrolls = self.run_walk(["--until-text", "夕"], [(frame("first"), 0), (frame("夕"), 0)])
        self.assertEqual((rc, looks, scrolls), (0, 2, 1))
        self.assertEqual(result["stop"], "target-found")

    def test_pixel_match_and_duplicate_candidates_preserved(self):
        data = frame("other", pixel_nodes=[{"text": "夕", "x": 200, "y": 500, "w": 60, "h": 40},
                                            {"text": "夕", "x": 200, "y": 900, "w": 60, "h": 40}])
        rc, result, _, scrolls = self.run_walk(["--until-text", "夕", "--ocr"], [(data, 0)])
        self.assertEqual((rc, scrolls), (0, 0))
        self.assertEqual(len(result["matches"]), 2)
        self.assertEqual(result["matches"][0]["source"], "pixels")
        self.assertEqual(result["screenshot"], "/test/s.jpg")

    def test_gate_refusal_nonzero_and_no_retry(self):
        rc, result, looks, scrolls = self.run_walk(scroll_rc=2)
        self.assertEqual((rc, looks, scrolls), (2, 1, 1))
        self.assertEqual(result["stop"], "gate-refused")
        self.assertFalse(result["complete"])

    def test_failed_observation_not_end_or_new_scroll(self):
        rc, result, looks, scrolls = self.run_walk(observations=[(frame(), 0), (None, 3)])
        self.assertEqual((rc, looks, scrolls), (3, 2, 1))
        self.assertEqual(result["stop"], "look-failed")

    def test_nonzero_look_cannot_supply_match(self):
        rc, result, looks, scrolls = self.run_walk(["--until-text", "夕"], [(frame("夕"), 3)])
        self.assertEqual((rc, scrolls), (3, 0))
        self.assertEqual(result["stop"], "look-failed")

    def test_stable_text_is_not_proof_of_end(self):
        rc, result, _, scrolls = self.run_walk(observations=[(frame(), 0)] * 4)
        self.assertEqual(result["stop"], "no-new-text")
        self.assertFalse(result["complete"])
        self.assertEqual(scrolls, 3)

    def test_no_match_is_partial_not_absence(self):
        rc, result, _, scrolls = self.run_walk(["--until-text", "夕", "--max", "0"])
        self.assertEqual((rc, scrolls), (4, 0))
        self.assertFalse(result["complete"])
        self.assertEqual(result["matches"], [])

    def test_black_or_sleeping_screen_cannot_match(self):
        for extra in ({"shot_black": True}, {"interactive": False}):
            rc, result, _, scrolls = self.run_walk(["--until-text", "夕"], [(frame("夕", **extra), 0)])
            self.assertEqual((rc, scrolls), (3, 0))
            self.assertEqual(result["matches"], [])

    def test_target_near_bottom_not_silently_excluded(self):
        data = frame()
        data["nodes"] = [{"text": "夕", "rect": [100, 2650, 500, 2720]}]
        rc, result, _, scrolls = self.run_walk(["--until-text", "夕"], [(data, 0)])
        self.assertEqual((rc, scrolls), (0, 0))

    def test_look_does_not_borrow_nodes_from_another_frame(self):
        def cmd(ns):
            print(json.dumps({"shot_id": "s1"}))
            return 0
        with patch.object(walk.st, "cmd_look", side_effect=cmd), patch.object(walk.st, "load_state", return_value={"last_shot": {"id": "s2", "nodes": [{"text": "夕"}]}}):
            data, rc = walk._look("test")
        self.assertIsNone(data)
        self.assertEqual(rc, 3)


if __name__ == "__main__":
    unittest.main()
