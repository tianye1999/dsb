import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from PIL import Image

from feedback_export import build_zip, redact


TASK = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
STAMP = 1700000000000


def lifecycle(task, start=STAMP - 1000, end=STAMP + 1000):
    return [{"task_id": task, "kind": "report_task_start", "ts": start / 1000},
            {"task_id": task, "kind": "report_task_end", "ts": end / 1000}]


def contents(package):
    return b"\n".join(package.read(name) for name in package.namelist()).decode(errors="replace")


class FeedbackExportTests(unittest.TestCase):
    def test_batch_metadata_survives_without_free_text(self):
        batch_id = "b" + "a" * 32
        row = {"task_id": TASK, "kind": "tap", "batch_id": batch_id,
               "batch_index": 2, "batch_size": 3, "expect": "private target",
               "reason": "private reason"}
        with zipfile.ZipFile(io.BytesIO(build_zip([row], {}, TASK, False))) as package:
            exported = json.loads(package.read("audit.jsonl"))
        self.assertEqual((exported["batch_id"], exported["batch_index"], exported["batch_size"]),
                         (batch_id, 2, 3))
        self.assertNotIn("private target", json.dumps(exported))
        self.assertNotIn("private reason", json.dumps(exported))
        invalid = redact({"batch_id": "bSECRET", "batch_index": True, "batch_size": -1})
        self.assertTrue(invalid["batch_id"]["removed"])
        self.assertTrue(invalid["batch_index"]["removed"])
        self.assertTrue(invalid["batch_size"]["removed"])
        uuid_shaped = redact({"batch_id": TASK})
        self.assertTrue(uuid_shaped["batch_id"]["removed"])
        self.assertEqual(redact({"batch_size": 0})["batch_size"], 0)
        self.assertEqual(redact({"batch_size": 7})["batch_size"], 7)

    def test_no_screenshot_cli_does_not_need_pillow(self):
        with tempfile.TemporaryDirectory() as directory:
            audit = Path(directory) / "audit.jsonl"
            audit.write_text(json.dumps({"task_id": TASK, "kind": "tap", "note": "SECRET"}) + "\n")
            result = subprocess.run([sys.executable, "-S", str(Path(__file__).with_name("feedback_export.py")),
                                     "--audit", str(audit), "--task-id", TASK, "--audit-offset", "0"],
                                    input=b"{}", capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            with zipfile.ZipFile(io.BytesIO(result.stdout)) as package:
                self.assertNotIn("SECRET", contents(package))

    def test_fail_closed_across_report_audit_and_nested_values(self):
        secrets = ["中文聊天", "订单AB123456", "+86-000-0000-0000", "https://private.example/a",
                   "tok_secret_ABC", "任务原文", "命令原文", "视觉解释原文"]
        row = {"task_id": TASK, "kind": "tap", "ts": 1.5, "allowed": False,
               "points": [[12, 34]], "reason_code": "G2B_ACK", "note": secrets[0],
               "expect": secrets[1], "reason": secrets[2], "command": secrets[6],
               "vision": {"explanation": secrets[7], "ui_reacted": True,
                          "new_field": secrets[3]}, "new_field": secrets[4],
               "task_name": secrets[5]}
        report = {"counts": {"dispatched_actions": 2, "new_field": secrets[3]},
                  "timeline": [{"action_type": "tap", "result": "refused",
                                "reason_code": "G2B_ACK", "explanation": secrets[7]}],
                  "new_field": secrets[4]}
        archive = build_zip([row, {"task_id": OTHER, "note": "OTHER_TASK_PRIVATE"}],
                            report, TASK, False)
        with zipfile.ZipFile(io.BytesIO(archive)) as package:
            self.assertEqual(set(package.namelist()), {"README.txt", "report.json", "audit.jsonl"})
            content = contents(package)
            for secret in [*secrets, "OTHER_TASK_PRIVATE"]:
                self.assertNotIn(secret, content)
            audit = json.loads(package.read("audit.jsonl"))
            self.assertEqual(audit["note"], {"removed": True, "chars": len(secrets[0])})
            self.assertEqual(audit["reason"], {"removed": True, "chars": len(secrets[2])})
            self.assertEqual(audit["points"], [[12, 34]])
            self.assertEqual(audit["vision"]["ui_reacted"], True)
            self.assertEqual(json.loads(package.read("report.json"))["counts"]["dispatched_actions"], 2)

    def test_unknown_key_name_and_value_are_not_exported(self):
        self.assertNotIn("SECRET_KEY_NAME", json.dumps(redact({"SECRET_KEY_NAME": "secret"})))
        self.assertNotIn("secret", json.dumps(redact({"SECRET_KEY_NAME": "secret"})))
        self.assertEqual(redact({"pkg": "com.example.app", "act_before": "com.example.app/.Main",
                                 "fg": "PRIVATE", "app": "com.secret.chat", "kind": "tap", "status": "PRIVATE"}),
                         {"pkg": "com.example.app", "act_before": "com.example.app/.Main",
                          "fg": {"removed": True, "chars": 7}, "kind": "tap",
                          "status": {"removed": True, "chars": 7},
                          "app": {"removed": True, "chars": len("com.secret.chat")}})
        self.assertEqual(redact({"pkg": "com.secret.chat", "act_before": "com.example.app/.Main"})["pkg"],
                         {"removed": True, "chars": len("com.secret.chat")})
        self.assertEqual(redact({"ts": 10 ** 1000})["ts"], 10 ** 1000)
        deep = {"vision": {}}
        current = deep["vision"]
        for _ in range(24):
            current["vision"] = {}
            current = current["vision"]
        leaf = redact(deep)
        for _ in range(19):
            leaf = leaf["vision"]
        self.assertEqual(leaf["vision"]["removed"], True)

    def test_screenshot_opt_in_blacks_top_and_drops_exif(self):
        with tempfile.TemporaryDirectory() as directory:
            shots = Path(directory)
            image = Image.new("RGB", (100, 100), "white")
            exif = Image.Exif()
            exif[270] = "PRIVATE_EXIF"
            shot = shots / f"before-{STAMP}.jpg"
            image.save(shot, exif=exif, icc_profile=b"PRIVATE_ICC")
            rows = lifecycle(TASK) + [{"task_id": TASK, "kind": "tap", "shot_before": str(shot)}]
            with zipfile.ZipFile(io.BytesIO(build_zip(rows, {}, TASK, True, shots))) as package:
                image_names = [name for name in package.namelist() if name.startswith("screenshots/")]
                self.assertEqual(image_names, ["screenshots/01.jpg"])
                picture = Image.open(io.BytesIO(package.read(image_names[0])))
                self.assertEqual(picture.getpixel((50, 0)), (0, 0, 0))
                self.assertEqual(picture.getexif().get(270), None)
                self.assertIsNone(picture.info.get("icc_profile"))
                self.assertNotIn("PRIVATE_ICC", contents(package))
            with zipfile.ZipFile(io.BytesIO(build_zip(rows, {}, TASK, False, shots))) as package:
                self.assertFalse(any(name.startswith("screenshots/") for name in package.namelist()))

    def test_cross_turn_and_expired_screenshots_are_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            shots = Path(directory)
            shot = shots / f"shot-{STAMP}.jpg"
            Image.new("RGB", (20, 20), "white").save(shot)
            first = lifecycle(TASK) + [{"task_id": TASK, "shot_before": str(shot)}]
            second = lifecycle(OTHER, STAMP + 2000, STAMP + 4000) + [
                {"task_id": OTHER, "shot_before": str(shot)}]
            with zipfile.ZipFile(io.BytesIO(build_zip(first + second, {}, TASK, True, shots))) as package:
                self.assertIn("screenshots/01.jpg", package.namelist())
            with zipfile.ZipFile(io.BytesIO(build_zip(first + second, {}, OTHER, True, shots))) as package:
                self.assertFalse(any(name.startswith("screenshots/") for name in package.namelist()))
                self.assertIn("跳过 1 张不属于本轮的截图", package.read("README.txt").decode())
            shot.unlink()
            with zipfile.ZipFile(io.BytesIO(build_zip(first, {}, TASK, True, shots))) as package:
                self.assertIn("1 张截图已按保留期清理", package.read("README.txt").decode())

    def test_path_and_link_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shots = root / "shots"
            shots.mkdir()
            good = shots / f"shot-{STAMP}.jpg"
            Image.new("RGB", (20, 20)).save(good)
            outside = root / f"shot-{STAMP}.jpg"
            outside.write_bytes(good.read_bytes())
            subdir = shots / "sub"
            subdir.mkdir()
            sub = subdir / good.name
            sub.write_bytes(good.read_bytes())
            symlink = shots / f"before-{STAMP}.jpg"
            symlink.symlink_to(outside)
            hardlink = shots / f"after-{STAMP}.jpg"
            os.link(good, hardlink)
            for path in (outside, sub, symlink, hardlink, shots / ".." / outside.name):
                rows = lifecycle(TASK) + [{"task_id": TASK, "shot_before": str(path)}]
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, "invalid screenshot path"):
                    build_zip(rows, {}, TASK, True, shots)

    def test_screenshot_limits_fail_instead_of_silently_truncating(self):
        with tempfile.TemporaryDirectory() as directory:
            shots = Path(directory)
            rows = []
            for index in range(41):
                path = shots / f"shot-{STAMP + index}.jpg"
                Image.new("RGB", (10, 10)).save(path)
                rows.append({"task_id": TASK, "shot_before": str(path)})
            with self.assertRaisesRegex(ValueError, "40"):
                build_zip(lifecycle(TASK) + rows, {}, TASK, True, shots)
            with self.assertRaisesRegex(ValueError, "20 MB"):
                build_zip(lifecycle(TASK) + rows[:1], {}, TASK, True, shots, max_shot_bytes=1)
            large = shots / f"after-{STAMP}.png"
            Image.new("1", (4500, 4500)).save(large)
            with self.assertRaisesRegex(ValueError, "dimensions"):
                build_zip(lifecycle(TASK) + [{"task_id": TASK, "shot_after": str(large)}],
                          {}, TASK, True, shots)


if __name__ == "__main__":
    unittest.main()
