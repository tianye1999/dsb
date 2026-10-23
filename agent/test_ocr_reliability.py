import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("ocr", Path(__file__).with_name("ocr.py"))
ocr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ocr)
ROWS = [{"text": "low", "confidence": .6, "source": "pixels", "x": 20, "y": 20, "w": 20, "h": 10},
        {"text": "high", "confidence": .9, "source": "pixels", "x": 20, "y": 40, "w": 20, "h": 10}]


class OCRReliability(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.image = str(Path(self.tmp.name) / "image.png")
        Path(self.image).write_bytes(b"test image")
        p = patch.object(ocr, "CACHE_FILE", str(Path(self.tmp.name) / "cache.json"))
        p.start(); self.addCleanup(p.stop)
        # 缓存只认捆绑运行时：放一份假的捆绑目录，测试不依赖本机有没有 pip 装 rapidocr
        model = Path(self.tmp.name) / "libs/rapidocr_onnxruntime/models/test.onnx"
        model.parent.mkdir(parents=True)
        model.write_bytes(b"model")
        p = patch.object(ocr, "OCR_LIBS", str(Path(self.tmp.name) / "libs"))
        p.start(); self.addCleanup(p.stop)

    def engine(self, result=None):
        result = result or {"status": "ok", "rows": ROWS, "engine": "rapid"}
        p = patch.object(ocr, "_run_worker", return_value=result, create=True)
        m = p.start(); self.addCleanup(p.stop)
        p = patch.object(ocr, "_rapid_rows", side_effect=lambda path, confidence, scale: [r for r in ROWS if r["confidence"] >= confidence])
        p.start(); self.addCleanup(p.stop)
        return m

    def test_lower_threshold_recovers_rows(self):
        self.engine()
        self.assertEqual(len(ocr.recognize(self.image, .8)), 1)
        self.assertEqual(len(ocr.recognize(self.image, .5)), 2)

    def test_scale_and_engine_are_cache_identity(self):
        m = self.engine()
        ocr.recognize(self.image, scale=.45)
        ocr.recognize(self.image, scale=.9)
        ocr.recognize(self.image, scale=.9, engine="tess")
        self.assertEqual(m.call_count, 3)

    def test_failure_not_cached_and_compatibility_raises(self):
        m = self.engine({"status": "unavailable", "rows": [], "engine": "rapid"})
        with self.assertRaises(RuntimeError):
            ocr.recognize(self.image)
        m.return_value = {"status": "ok", "rows": ROWS, "engine": "rapid"}
        self.assertEqual(len(ocr.recognize(self.image)), 2)
        self.assertEqual(m.call_count, 2)

    def test_empty_is_success_and_cacheable(self):
        m = self.engine({"status": "empty", "rows": [], "engine": "rapid"})
        self.assertEqual(ocr.recognize_result(self.image)["status"], "empty")
        self.assertTrue(ocr.recognize_result(self.image)["cached"])
        self.assertEqual(m.call_count, 1)

    def test_corrupt_cache_is_ignored(self):
        self.engine()
        Path(ocr.CACHE_FILE).write_text("broken")
        self.assertEqual(len(ocr.recognize(self.image)), 2)
        json.loads(Path(ocr.CACHE_FILE).read_text())

    def sleeping_worker(self):
        pid = Path(self.tmp.name) / "pid"
        code = "import os,time;open(%r,'w').write(str(os.getpid()));time.sleep(30)" % str(pid)
        p = patch.object(ocr, "_worker_command", return_value=[sys.executable, "-c", code], create=True)
        p.start(); self.addCleanup(p.stop)
        return pid

    def assert_reaped(self, pid):
        self.assertTrue(pid.exists(), "worker did not start")
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid.read_text()), 0)

    def test_timeout_reaps_worker(self):
        pid = self.sleeping_worker()
        start = time.monotonic()
        result = ocr.recognize_result(self.image, timeout_s=.3)
        self.assertEqual(result["status"], "timeout")
        self.assertLess(time.monotonic() - start, 2)
        self.assert_reaped(pid)
        self.assertFalse(Path(ocr.CACHE_FILE).exists())

    def test_cancel_reaps_worker(self):
        pid = self.sleeping_worker()
        start = time.monotonic()
        stopped = [False]
        def cancelled():
            if stopped[0]:
                return True
            if not pid.exists():
                return False
            self.assertEqual(os.getpgid(int(pid.read_text())), os.getpgrp(), "worker escaped bridge process group")
            stopped[0] = time.monotonic() - start > .3
            return stopped[0]
        result = ocr.recognize_result(self.image, timeout_s=5, cancel_check=cancelled)
        self.assertEqual(result["status"], "cancelled")
        self.assert_reaped(pid)

    def test_cancel_does_not_reuse_cache(self):
        self.engine()
        ocr.recognize(self.image)
        self.assertEqual(ocr.recognize_result(self.image, cancel_check=lambda: True)["status"], "cancelled")

    def test_invalid_worker_payload_not_cached(self):
        command = [sys.executable, "-c", "print('not json')"]
        with patch.object(ocr, "_worker_command", return_value=command, create=True):
            self.assertEqual(ocr.recognize_result(self.image)["status"], "error")
        self.assertFalse(Path(ocr.CACHE_FILE).exists())

    def test_engine_flag_forwarded(self):
        cmd = ocr._worker_command(self.image, .75, "tess")
        self.assertEqual(cmd[cmd.index("--engine") + 1], "tess")
        self.assertIn("--worker", cmd)

    def test_nonfinite_timeout_is_rejected(self):
        self.assertEqual(ocr.recognize_result(self.image, timeout_s=float("nan"))["status"], "error")

    def test_invalid_cached_payload_is_recomputed(self):
        m = self.engine()
        ocr.recognize(self.image)
        data = json.loads(Path(ocr.CACHE_FILE).read_text())
        next(iter(data.values()))["result"]["rows"] = [None]
        Path(ocr.CACHE_FILE).write_text(json.dumps(data))
        self.assertEqual(len(ocr.recognize(self.image)), 2)
        self.assertEqual(m.call_count, 2)


if __name__ == "__main__":
    unittest.main()
