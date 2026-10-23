import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("ocr", Path(__file__).with_name("ocr.py"))
ocr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ocr)
RESULT = {"status": "ok", "engine": "rapid", "rows": [
    {"text": "sample", "confidence": .9, "source": "pixels", "x": 5, "y": 5, "w": 8, "h": 8}]}


class CacheConsistency(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.image = self.root / "image.png"
        self.image.write_bytes(b"original frame")
        self.libs = self.root / "libs"
        self.model = self.libs / "rapidocr_onnxruntime/models/test.onnx"
        self.model.parent.mkdir(parents=True)
        self.model.write_bytes(b"model one")
        for name, value in (("CACHE_FILE", str(self.root / "cache.json")), ("OCR_LIBS", str(self.libs))):
            p = patch.object(ocr, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_worker_reads_immutable_frame_and_snapshot_is_removed(self):
        paths = []
        def worker(path, *args):
            paths.append(Path(path))
            self.image.write_bytes(b"new frame")
            self.assertEqual(Path(path).read_bytes(), b"original frame")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            return RESULT
        with patch.object(ocr, "_run_worker", side_effect=worker):
            result = ocr.recognize_result(self.image)
        self.assertEqual(result["image_sha256"], hashlib.sha256(b"original frame").hexdigest())
        self.assertFalse(paths[0].exists())
        self.assertTrue(self.image.exists())

    def test_model_content_change_invalidates_even_with_same_size_and_mtime(self):
        with patch.object(ocr, "_run_worker", return_value=RESULT) as worker:
            ocr.recognize_result(self.image)
            self.assertTrue(ocr.recognize_result(self.image)["cached"])
            stat = self.model.stat()
            self.model.write_bytes(b"model two")
            os.utime(self.model, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            self.assertFalse(ocr.recognize_result(self.image)["cached"])
            self.assertEqual(worker.call_count, 2)

    def test_engine_code_change_invalidates(self):
        source = self.libs / "rapidocr_onnxruntime/main.py"
        source.write_text("old engine")
        with patch.object(ocr, "_run_worker", return_value=RESULT) as worker:
            ocr.recognize_result(self.image)
            source.write_text("new engine")
            self.assertFalse(ocr.recognize_result(self.image)["cached"])
            self.assertEqual(worker.call_count, 2)

    def test_transient_auto_fallback_not_cached(self):
        with patch.object(ocr, "_run_worker", return_value={**RESULT, "engine": "tess"}) as worker:
            ocr.recognize_result(self.image)
            self.assertFalse(ocr.recognize_result(self.image)["cached"])
            self.assertEqual(worker.call_count, 2)

    def test_cancel_after_worker_does_not_publish_cache(self):
        cancelled = [False]
        def worker(*args):
            cancelled[0] = True
            return RESULT
        with patch.object(ocr, "_run_worker", side_effect=worker):
            result = ocr.recognize_result(self.image, cancel_check=lambda: cancelled[0])
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(Path(ocr.CACHE_FILE).exists())

    def test_cache_lock_contention_does_not_block_result(self):
        ready = self.root / "ready"
        code = "import fcntl,time,pathlib; f=open(%r,'a'); fcntl.flock(f,fcntl.LOCK_EX); pathlib.Path(%r).touch(); time.sleep(2)" % (ocr.CACHE_FILE + ".lock", str(ready))
        process = subprocess.Popen([sys.executable, "-c", code])
        try:
            deadline = time.monotonic() + 2
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(ready.exists())
            with patch.object(ocr, "_run_worker", return_value=RESULT):
                start = time.monotonic()
                result = ocr.recognize_result(self.image, timeout_s=.5)
                self.assertLess(time.monotonic() - start, .5)
                self.assertEqual(result["status"], "ok")
        finally:
            process.terminate()
            process.wait()

    def test_postflight_fingerprint_cannot_return_success_after_deadline(self):
        calls = []
        def fingerprint():
            calls.append(True)
            if len(calls) == 2:
                time.sleep(.15)
            return "runtime"
        with patch.object(ocr, "_runtime_fingerprint", side_effect=fingerprint), patch.object(ocr, "_run_worker", return_value=RESULT):
            result = ocr.recognize_result(self.image, timeout_s=.1)
        self.assertEqual(result["status"], "timeout")
        self.assertFalse(Path(ocr.CACHE_FILE).exists())

    def test_no_bundle_means_no_fingerprint_even_if_pip_has_rapidocr(self):
        # 没有捆绑目录：不管本进程能不能找到 pip 装的 rapidocr、子进程用哪个解释器，都不给指纹（不用缓存）
        fake_spec = type("Spec", (), {"submodule_search_locations": [str(self.libs / "rapidocr_onnxruntime")]})()
        with patch.object(ocr, "OCR_LIBS", str(self.root / "no-bundle")), \
                patch("importlib.util.find_spec", return_value=fake_spec):
            for py312 in (str(self.root / "absent"), sys.executable):
                with patch.object(ocr, "PY312", py312):
                    self.assertIsNone(ocr._runtime_root())
                    self.assertIsNone(ocr._runtime_fingerprint())
        with patch.object(ocr.Path, "is_dir", side_effect=PermissionError("denied")):   # 路径不可访问 → None，不抛
            self.assertIsNone(ocr._runtime_root())
            self.assertIsNone(ocr._runtime_fingerprint())


if __name__ == "__main__":
    unittest.main()
