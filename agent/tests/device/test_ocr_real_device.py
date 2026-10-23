"""Phone-only checks using installed RapidOCR models, never a fake recognizer."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont
import ocr


def main():
    assert Path("/system/fonts/NotoSansCJK-Regular.ttc").is_file(), "Run on Android phone"
    font = ImageFont.truetype("/system/fonts/NotoSansCJK-Regular.ttc", 48)
    reports = []
    with tempfile.TemporaryDirectory(prefix="ocr-real-") as temp:
        root = Path(temp)
        ocr.CACHE_FILE = str(root / "cache.json")
        for dark in (False, True):
            image = Image.new("RGB", (1007, 803), "#202020" if dark else "white")
            draw = ImageDraw.Draw(image)
            labels = [("中文识别验证", (80, 70)), ("重复目标", (80, 250)),
                      ("重复目标", (80, 430)), ("删除", (80, 610))]
            truth = []
            for text, xy in labels:
                draw.text(xy, text, font=font, fill="white" if dark else "black")
                truth.append((text, draw.textbbox(xy, text, font=font)))
            path = root / ("dark.png" if dark else "light.png")
            image.save(path)
            for scale in (.45, .75, 1.0):
                result = ocr.recognize_result(str(path), engine="rapid", scale=scale)
                reports.append({"dark": dark, "scale": scale, "result": result})
                assert result["status"] == "ok" and result["engine"] == "rapid", result
                assert not result["cached"], result
                rows = result["rows"]
                for text, rect in truth:
                    found = [r for r in rows if r["text"].replace(" ", "") == text
                             and rect[0] - 5 <= r["x"] <= rect[2] + 5
                             and rect[1] - 5 <= r["y"] <= rect[3] + 5]
                    assert len(found) == 1, (text, rect, rows)
                assert len([r for r in rows if r["text"].replace(" ", "") == "重复目标"]) == 2, rows
                assert not any(r["text"] == "不存在的目标" for r in rows)
                for row in rows:
                    assert 0 <= row["x"] < 1007 and 0 <= row["y"] < 803, row
                    assert 0 < row["w"] <= 1007 and 0 < row["h"] <= 803, row
                cached = ocr.recognize_result(str(path), engine="rapid", scale=scale)
                assert cached["cached"] and cached["rows"] == rows, cached
        # Real worker lifecycle, isolated cache. No sleeping/fake recognition worker.
        ocr.CACHE_FILE = str(root / "cancel-cache.json")
        real_popen = ocr.subprocess.Popen
        for operation in ("cancelled", "timeout"):
            processes = []
            def recorded_popen(*args, processes=processes, **kwargs):
                process = real_popen(*args, **kwargs)
                assert os.getpgid(process.pid) == os.getpgrp()
                processes.append(process)
                return process
            started = time.monotonic()
            options = ({"cancel_check": lambda processes=processes, started=started: bool(processes) and time.monotonic() - started > .3}
                       if operation == "cancelled" else {"timeout_s": .5})
            with patch.object(ocr.subprocess, "Popen", side_effect=recorded_popen):
                result = ocr.recognize_result(str(path), engine="rapid", **options)
            elapsed = time.monotonic() - started
            assert result["status"] == operation, result
            assert len(processes) == 1, "Real OCR worker was not launched"
            process = processes[0]
            assert process.returncode is not None
            try:
                os.kill(process.pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise AssertionError("OCR worker still exists: %s" % process.pid)
            assert elapsed < 2, elapsed
            assert not Path(ocr.CACHE_FILE).exists()
            reports.append({operation: result, "wall_ms": round(elapsed * 1000),
                            "worker_pid": process.pid, "worker_reaped": True,
                            "worker_returncode": process.returncode})
        broken = root / "broken.png"
        broken.write_bytes(b"not an image")
        result = ocr.recognize_result(str(broken), engine="rapid")
        assert result["status"] == "error", result
        reports.append({"corrupt_image": result})
    report = {"source_sha256": hashlib.sha256(Path(ocr.__file__).read_bytes()).hexdigest(),
              "pid": os.getpid(), "runs": reports}
    dest = Path(__file__).with_name("real-device-%d.json" % time.time_ns())
    dest.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print("REAL_ENGINE_PASS: 6 image/scale cases + cache + cancel + timeout + corrupt image")
    print("REPORT=" + str(dest))


if __name__ == "__main__":
    main()
