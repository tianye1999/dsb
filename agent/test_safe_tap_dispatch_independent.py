# -*- coding: utf-8 -*-
"""独立边界验证（1B.3）：drag 组合 shell 中「所有 MOVE 成功但 UP 失败」时，
整链最终返回非零（rc 聚合不掩盖 UP 失败）；全成功返回零；MOVE 失败短路但
UP 仍尽力执行。

方法：ast 解析候选 safe_tap.py 的 _do_action_locked，提取 drag 分支构造的
seq 模板字符串；把其中的 input 命令替换为 fake_input（返回码可控、调用留痕的
shell 函数）；用 /bin/sh 真实执行。不访问 ADB、不用 safe_tap 任何状态、
不跑 do_action。
"""
import ast
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

CAND = Path(os.environ.get("DSB_CANDIDATE_DIR", str(Path(__file__).resolve().parent)))
UP_FAIL_RC = 7


def extract_drag_seq_template():
    src = (CAND / "safe_tap.py").read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(CAND / "safe_tap.py"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_do_action_locked":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign):
                    for target in sub.targets:
                        if getattr(target, "id", None) == "seq":
                            if not isinstance(sub.value, ast.BinOp) or not isinstance(sub.value.op, ast.Mod):
                                raise AssertionError("unexpected seq expression")
                            template = ast.literal_eval(sub.value.left)
                            found.append(template % (600, 900,
                                "input motionevent MOVE 600 1000 && input motionevent MOVE 600 1100",
                                600, 1200))
    if len(found) != 2:
        raise AssertionError("expected drag and scroll templates, got %d" % len(found))
    if found[0] != found[1]:
        raise AssertionError("drag/scroll templates differ; test both separately")
    return found[0]


def run_seq(seq, tmp, move_rc=0, up_rc=UP_FAIL_RC):
    calls_file = os.path.join(tmp, "calls.log")
    prelude = (
        'input() { '
        'echo "$2" >> "%s"; '
        'case "$2" in '
        'UP) return %d ;; '
        'MOVE) return %d ;; '
        '*) return 0 ;; '
        'esac; }' % (calls_file, up_rc, move_rc)
    )
    r = subprocess.run(["/bin/sh", "-c", prelude + "\n" + seq],
                       capture_output=True, text=True, timeout=3)
    calls = ""
    if os.path.isfile(calls_file):
        calls = Path(calls_file).read_text(encoding="utf-8")
    return r.returncode, calls


class DragSeqTests(unittest.TestCase):
    def setUp(self):
        self.seq = extract_drag_seq_template()
        # 结构基线：DOWN 起头、MOVE 链在 && 内、UP 在 ; 后、rc 聚合在尾部
        self.assertIn("input motionevent DOWN", self.seq)
        self.assertIn("&& input motionevent MOVE", self.seq)
        self.assertIn("; input motionevent UP", self.seq)
        self.assertIn("rc=$?", self.seq)
        self.assertIn("up_rc=$?", self.seq)
        self.assertIn("[ $rc -eq 0 ] && [ $up_rc -eq 0 ]", self.seq)
        # && 短路链内不得出现 UP（UP 是尽力清理，不是链内步骤）
        chain_part = self.seq.split("; ")[0]
        self.assertNotIn("motionevent UP", chain_part,
                         "UP must not be inside the && short-circuit chain")

    def test_all_moves_success_up_failure_returns_nonzero(self):
        with tempfile.TemporaryDirectory(prefix="dsb-1b3-up-") as tmp:
            rc, calls = run_seq(self.seq, tmp, move_rc=0, up_rc=UP_FAIL_RC)
            self.assertNotEqual(rc, 0, "UP failure must propagate nonzero")
            self.assertIn("UP", calls)
            self.assertIn("DOWN", calls)
            self.assertGreaterEqual(calls.count("MOVE"), 1)
            self.assertEqual(calls.splitlines(), ["DOWN", "MOVE", "MOVE", "UP"])
            # UP 确实被尽力执行了（调用留痕）——失败不是「没执行」

    def test_all_success_returns_zero(self):
        with tempfile.TemporaryDirectory(prefix="dsb-1b3-ok-") as tmp:
            rc, calls = run_seq(self.seq, tmp, move_rc=0, up_rc=0)
            self.assertEqual(rc, 0, "all-success chain must return zero")
            self.assertIn("UP", calls)
            self.assertIn("DOWN", calls)


    def test_move_failure_short_circuits_but_up_still_runs(self):
        with tempfile.TemporaryDirectory(prefix="dsb-1b3-mv-") as tmp:
            rc, calls = run_seq(self.seq, tmp, move_rc=5, up_rc=0)
            self.assertNotEqual(rc, 0, "MOVE failure must fail the chain")
            self.assertIn("UP", calls, "UP must still be attempted after short circuit")
            self.assertEqual(calls.splitlines(), ["DOWN", "MOVE", "UP"])


if __name__ == "__main__":
    unittest.main()
