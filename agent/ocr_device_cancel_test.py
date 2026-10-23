"""Run under an external safe_tap-gated orange-ball click; no UI actions here."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch
import ocr
import safe_tap


with tempfile.TemporaryDirectory(prefix="ocr-stop-") as tmp:
    pid_file = Path(tmp) / "worker.pid"
    command = [sys.executable, "-c", "import os,time;open(%r,'w').write(str(os.getpid()));time.sleep(30)" % str(pid_file)]
    cancelled = safe_tap._ocr_cancel_check()
    safe_tap._ball("橙")
    start = time.monotonic()
    print(json.dumps({"phase": "WAITING_ORANGE_BALL", "budget_s": 15}), flush=True)
    with patch.object(ocr, "CACHE_FILE", str(Path(tmp) / "cache.json")), \
         patch.object(ocr, "_worker_command", return_value=command):
        result = ocr.recognize_result(sys.argv[1], timeout_s=15, cancel_check=cancelled)
    alive = False
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text()), 0)
            alive = True
        except ProcessLookupError:
            pass
    ok = result["status"] == "cancelled" and pid_file.exists() and not alive
    print(json.dumps({"ok": ok, "status": result["status"], "worker_alive": alive,
                      "elapsed_s": round(time.monotonic() - start, 2)}))
    sys.exit(0 if ok else 1)
