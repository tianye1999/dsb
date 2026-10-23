#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bounded, on-demand OCR with explicit failure states and an eight-entry raw cache.

recognize_result returns status/rows; recognize returns rows or raises OCRFailure.
Recognition uses a child in the caller's process group, never a persistent daemon.
Pixel rectangles are observations, not authorization to bypass safe_tap gates.
"""
import argparse
import csv
import fcntl
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import signal
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(HERE, "ocr_cache.json")
OCR_LIBS = os.environ.get("DSB_OCR_LIBS", "/root/.dsh-phone/ocr-libs")   # 捆绑的 RapidOCR 运行时；没有就用 pip 装的
PY312 = "/usr/bin/python3.12"
DEFAULT_SCALE = 0.45
DEFAULT_TIMEOUT = 15.0
CACHE_VERSION = 4
_TESS = shutil.which("tesseract")
_RAPID = None
_RAPID_TRIED = False


def _prep():
    sys.path.insert(0, OCR_LIBS)
    try:
        import onnxruntime
        onnxruntime.set_default_logger_severity(4)   # 压掉 proot 下线程亲和的无害报错
    except Exception:
        pass


def _engine_rapid():
    global _RAPID, _RAPID_TRIED
    # 2026-10-01 修：非 3.12 解释器（如 3.14）绝不尝试进程内导入——
    # ocr-libs 里的 cv2 依赖 cp312 numpy，导入失败时 cv2 会把
    # 「OpenCV bindings requires numpy」打到 **stdout**，污染调用方（safe_tap look）的 JSON。
    # 3.12 直接走 _subprocess_rows 子进程（那边有完整 ocr-libs）。
    if sys.version_info[:2] != (3, 12):
        _RAPID_TRIED = True
        _RAPID = False
        return None
    if _RAPID is None and not _RAPID_TRIED:
        _RAPID_TRIED = True
        _prep()
        try:
            from rapidocr_onnxruntime import RapidOCR
            _RAPID = RapidOCR()
        except Exception:
            _RAPID = False
    return _RAPID or None


def _rapid_rows(path, min_confidence, scale):
    eng = _engine_rapid()
    if not eng:
        return None
    import cv2
    img = cv2.imread(path)
    if img is None:
        raise ValueError("Unreadable image")
    original_h, original_w = img.shape[:2]
    if scale < 1.0:
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    resized_h, resized_w = img.shape[:2]
    t0 = time.time()
    result, _elapse = eng(img)
    dt = round(time.time() - t0, 2)
    rows = []
    for item in (result or []):
        try:
            box, text, conf = item[0], item[1], float(item[2])
        except Exception:
            continue
        # Resize rounds dimensions; map float corners using the actual per-axis ratios.
        xs = [float(p[0]) * original_w / resized_w for p in box]
        ys = [float(p[1]) * original_h / resized_h for p in box]
        if not xs or not all(math.isfinite(v) for v in xs + ys):
            continue
        x1 = max(0, min(original_w, math.floor(min(xs))))
        y1 = max(0, min(original_h, math.floor(min(ys))))
        x2 = max(0, min(original_w, math.ceil(max(xs))))
        y2 = max(0, min(original_h, math.ceil(max(ys))))
        if x2 <= x1 or y2 <= y1:
            continue
        text = str(text).strip()
        if conf < min_confidence or not text:
            continue
        rows.append({"text": text, "confidence": round(conf, 3), "source": "pixels",
                     "x": (x1 + x2) // 2, "y": (y1 + y2) // 2, "w": x2 - x1, "h": y2 - y1})
    print("[ocr] %d 行 %.2fs (scale=%.2f)" % (len(rows), dt, scale), file=sys.stderr)
    return rows


class OCRFailure(RuntimeError):
    def __init__(self, result):
        self.result = result
        super().__init__("OCR " + result["status"])


def _result(status, rows=None, engine="auto", **extra):
    return {"status": status, "rows": rows or [], "engine": engine,
            "cached": False, **extra}


def _worker_command(path, scale, engine):
    py = PY312 if os.path.isfile(PY312) else sys.executable
    return [py, os.path.abspath(__file__), path, "--worker", "--scale", str(scale), "--engine", engine]


def _valid_payload(payload):
    if not isinstance(payload, dict):
        return False
    status, rows = payload.get("status"), payload.get("rows")
    if status not in ("ok", "empty", "unavailable", "error") or not isinstance(rows, list):
        return False
    if (status == "ok") != bool(rows):
        return False
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("text"), str)
                or not all(isinstance(row.get(k), (int, float)) and math.isfinite(row[k])
                           for k in ("confidence", "x", "y", "w", "h"))
                or not 0 <= row["confidence"] <= 1 or row["w"] <= 0 or row["h"] <= 0):
            return False
    return True


def _run_worker(path, scale, engine, timeout_s, cancel_check):
    deadline = time.monotonic() + timeout_s
    proc = None
    try:
        # Inherit the caller's process group: bridge cancellation must also kill OCR.
        proc = subprocess.Popen(_worker_command(path, scale, engine),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        while True:
            if cancel_check and cancel_check():
                return _result("cancelled", engine=engine)
            left = deadline - time.monotonic()
            if left <= 0:
                return _result("timeout", engine=engine)
            try:
                stdout, _ = proc.communicate(timeout=min(.1, left))
                break
            except subprocess.TimeoutExpired:
                continue
        payload = json.loads(stdout)
        if proc.returncode != 0 or not _valid_payload(payload):
            return _result("error", engine=engine)
        return payload
    except (OSError, ValueError, TypeError):
        return _result("error", engine=engine)
    finally:
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.communicate(timeout=.5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            proc.communicate()


def _tess_rows(path, min_confidence):
    if not _TESS:
        return None
    output = subprocess.run([_TESS, path, "stdout", "-l", "chi_sim+eng", "--psm", "6", "tsv"],
                            capture_output=True, timeout=DEFAULT_TIMEOUT, check=True, text=True)
    words = []
    with io.StringIO(output.stdout) as stream:
        for d in csv.DictReader(stream, delimiter="\t"):
            if d.get("level") != "5":
                continue
            try:
                conf = float(d.get("conf", "-1")) / 100.0
                left, top = int(d["left"]), int(d["top"])
                w, h = int(d["width"]), int(d["height"])
            except Exception:
                continue
            text = (d.get("text") or "").strip()
            if conf < min_confidence or not text:
                continue
            words.append({"x": left + w // 2, "y": top + h // 2,
                          "x1": left, "y1": top, "x2": left + w, "y2": top + h,
                          "confidence": round(conf, 3), "text": text})
    rows = []
    for w in sorted(words, key=lambda k: (k["y1"], k["x"])):
        if rows and abs(w["y1"] - rows[-1]["y1"]) <= max(12, w["h"] * 0.6):
            last = rows[-1]
            last["text"] = (last["text"] + " " + w["text"])[:80]
            last["x1"], last["x2"] = min(last["x1"], w["x1"]), max(last["x2"], w["x2"])
            last["y1"], last["y2"] = min(last["y1"], w["y1"]), max(last["y2"], w["y2"])
            last["x"], last["y"] = (last["x1"] + last["x2"]) // 2, (last["y1"] + last["y2"]) // 2
            last["w"], last["h"] = last["x2"] - last["x1"], last["y2"] - last["y1"]
            continue
        rows.append({"text": w["text"], "x": w["x"], "y": w["y"], "source": "pixels",
                     "x1": w["x1"], "y1": w["y1"], "x2": w["x2"], "y2": w["y2"],
                     "w": w["x2"] - w["x1"], "h": w["y2"] - w["y1"], "confidence": w["confidence"]})
    return rows


def _load_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _runtime_root():
    """只认捆绑目录；没有（或所在路径不可访问，例如 CI 里的 /root）返回 None → 不用缓存。

    不回退到 pip 安装位置：本进程看到的安装包不一定是识别子进程用的那套（子进程优先 PY312，
    venv、sys.path 也可能不同），拿它算指纹会在模型更新后复用旧坐标。"""
    root = Path(OCR_LIBS)
    try:
        return root if (root / "rapidocr_onnxruntime").is_dir() else None
    except OSError:
        return None


def _runtime_fingerprint():
    """Fingerprint the installed RapidOCR runtime, without importing it."""
    try:
        root = _runtime_root()
        if root is None:
            return None
        package = root / "rapidocr_onnxruntime"
        paths = sorted(p for p in package.rglob("*") if p.is_file()
                       and "__pycache__" not in p.parts and p.suffix != ".pyc")
        if not paths:
            return None
        paths += sorted(root.glob("*.dist-info/METADATA"))
        paths += sorted(root.glob("*.dist-info/RECORD"))
        digest = hashlib.sha256()
        for path in paths:
            digest.update(str(path.relative_to(root)).encode())
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        return digest.hexdigest()
    except OSError:
        return None  # An unreadable runtime must not reuse a possibly stale cache.


def _cache_put(key, result):
    directory = os.path.dirname(CACHE_FILE) or "."
    name = None
    try:
        with open(CACHE_FILE + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            cache = _load_cache()
            cache[key] = {"ts": time.time(), "result": result}
            cache = dict(sorted(cache.items(), key=lambda item: item[1].get("ts", 0))[-8:])
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as stream:
                name = stream.name
                json.dump(cache, stream, ensure_ascii=False)
            os.replace(name, CACHE_FILE)
    except (OSError, ValueError, TypeError, AttributeError):
        pass  # Cache availability must not decide OCR success.
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def recognize_result(path, min_confidence=.5, scale=DEFAULT_SCALE, engine=None,
                     timeout_s=DEFAULT_TIMEOUT, cancel_check=None):
    engine = engine or "auto"
    start = time.monotonic()
    if (engine not in ("auto", "rapid", "tess")
            or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (scale, min_confidence, timeout_s))
            or not 0 < scale <= 1 or not 0 <= min_confidence <= 1):
        return _result("error", engine=engine)
    if cancel_check and cancel_check():
        return _result("cancelled", engine=engine)
    if timeout_s <= 0:
        return _result("timeout", engine=engine)
    try:
        with open(path, "rb") as stream:
            image_bytes = stream.read()
        digest = hashlib.sha256(image_bytes).hexdigest()
    except (OSError, TypeError):
        return _result("error", engine=engine)
    runtime = _runtime_fingerprint() if engine != "tess" else None
    key = json.dumps([CACHE_VERSION, digest, engine, scale, runtime])
    entry = _load_cache().get(key) if runtime else None
    raw = entry.get("result") if isinstance(entry, dict) else None
    cached = _valid_payload(raw) and raw["status"] in ("ok", "empty") and raw.get("engine") == "rapid"
    deadline = start + min(timeout_s, DEFAULT_TIMEOUT)
    if time.monotonic() >= deadline:
        return _result("timeout", engine=engine)
    if not cached:
        # Hash and recognize exactly the same bytes, even if the caller replaces its file.
        try:
            with tempfile.NamedTemporaryFile(suffix=".png") as snapshot:
                snapshot.write(image_bytes)
                snapshot.flush()
                left = deadline - time.monotonic()
                raw = (_run_worker(snapshot.name, scale, engine, left, cancel_check)
                       if left > 0 else _result("timeout", engine=engine))
        except OSError:
            return _result("error", engine=engine)
    if cancel_check and cancel_check():
        return _result("cancelled", engine=engine)
    if raw["status"] in ("ok", "empty") and time.monotonic() >= deadline:
        return _result("timeout", engine=engine)
    if (not cached and runtime and raw["status"] in ("ok", "empty")
            and raw.get("engine") == "rapid" and _runtime_fingerprint() == runtime):
        if cancel_check and cancel_check():
            return _result("cancelled", engine=engine)
        if time.monotonic() < deadline:
            _cache_put(key, raw)
    if cancel_check and cancel_check():
        return _result("cancelled", engine=engine)
    if raw["status"] in ("ok", "empty") and time.monotonic() >= deadline:
        return _result("timeout", engine=engine)
    rows = [row for row in raw["rows"] if row["confidence"] >= min_confidence] if raw["status"] in ("ok", "empty") else []
    status = ("ok" if rows else "empty") if raw["status"] in ("ok", "empty") else raw["status"]
    return _result(status, rows, raw.get("engine", engine), cached=cached,
                   image_sha256=digest, runtime_sha256=runtime,
                   elapsed_ms=round((time.monotonic() - start) * 1000))


def recognize(path, min_confidence=.5, scale=DEFAULT_SCALE, engine=None, **kwargs):
    """Compatibility API: successful rows only; failure is never an empty success."""
    result = recognize_result(path, min_confidence, scale, engine, **kwargs)
    if result["status"] not in ("ok", "empty"):
        raise OCRFailure(result)
    return result["rows"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("--min", type=float, default=.5)
    parser.add_argument("--scale", type=float, default=DEFAULT_SCALE)
    parser.add_argument("--engine", choices=("auto", "rapid", "tess"), default="auto")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--result", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        # SIGTERM interrupts subprocess.run too, allowing it to reap tesseract.
        def interrupted(signum, frame):
            raise KeyboardInterrupt()
        signal.signal(signal.SIGTERM, interrupted)
        try:
            rows = _rapid_rows(args.image, 0, args.scale) if args.engine != "tess" else None
            actual = "tess" if args.engine == "tess" else "rapid"
            if rows is None and args.engine != "rapid" and _TESS:
                rows, actual = _tess_rows(args.image, 0), "tess"
            result = _result("unavailable" if rows is None else "ok" if rows else "empty", rows, actual)
        except Exception:
            result = _result("error", engine=args.engine)
    else:
        result = recognize_result(args.image, args.min, args.scale, args.engine, timeout_s=args.timeout)
    if args.worker or args.result or result["status"] not in ("ok", "empty"):
        print(json.dumps(result, ensure_ascii=False))
    elif args.json:
        print(json.dumps(result["rows"], ensure_ascii=False))
    else:
        for row in result["rows"]:
            print("%-4d %-4d %s" % (row["x"], row["y"], row["text"][:70]))
    sys.exit(0 if args.worker or result["status"] in ("ok", "empty") else 3)
