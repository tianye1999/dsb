"""Read only the existing own Canvas fixture; never launch, tap, or change settings."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import safe_tap as st


def main():
    here = Path(__file__).resolve().parent
    runs = []
    try:
        for target, expected in (("EMPTY-TREE TEST PAGE (canvas)", "found"),
                                 ("DSB-ABSENT-TARGET-9F31", "not_found")):
            assert st.foreground() == "com.dsh.emptytree/.MainActivity", "Own fixture must be foreground"
            result = subprocess.run([sys.executable, str(here / "safe_tap.py"), "look",
                                     "--target", target, "--note", "OCR-POC-own-page"],
                                    capture_output=True, text=True, timeout=30)
            out = json.loads(result.stdout)
            runs.append({"exit": result.returncode, "result": out, "stderr": result.stderr})
            assert result.returncode == 0, out
            assert out["activity"] == "com.dsh.emptytree/.MainActivity", out
            assert out["target"]["status"] == expected, out
            assert out["target"]["source"] == "pixels", out
            assert out["ocr"]["engine"] == "rapid", out
            assert out["look_forced"] and not out["shot_reused"], out
            assert out["zone_coverage"] == "none", out
            if expected == "found":
                row = out["target"]["candidates"][0]
                assert 60 <= row["x"] <= 1100 and 850 <= row["y"] <= 1030, row
        print("POC_DEVICE_PASS: found + absent; real capture/OCR; no touch; gates unchanged")
    finally:
        report = {"time": time.time(), "source_sha256": hashlib.sha256((here / "safe_tap.py").read_bytes()).hexdigest(),
                  "runs": runs}
        path = here / ("poc-device-%d.json" % time.time_ns())
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print("REPORT=" + str(path))
        st._ball("蓝")


if __name__ == "__main__":
    main()
