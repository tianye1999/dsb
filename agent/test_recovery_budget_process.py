"""Real helper processes, temporary ledger, synthetic open; no device commands."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

CHILD = r'''
import json,os,sys
from pathlib import Path
import safe_tap as st
import recovery
root=Path(sys.argv[1])
st.HERE=str(root)
st.STATE=str(root/'state.json')
st.audit=lambda row: None
def forbidden(*a,**kw):
    raise AssertionError('device access forbidden')
st.sh=forbidden
def synthetic_open(args):
    with open(root/'calls','a') as f: f.write('x')
    print(json.dumps({'launch_verified': True,'pkg':args.app}))
    return int(sys.argv[2])
st.cmd_open=synthetic_open
print(json.dumps(recovery.recover('app_dead',{'app':'synthetic.app'})))
'''


class BudgetProcesses(unittest.TestCase):
    def argv(self, root, code=0):
        return [sys.executable, "-c", CHILD, str(root), str(code)]

    def environment(self, task):
        return dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent), DSB_TASK_ID=task)

    def run_child(self, root, task, code=0):
        result = subprocess.run(self.argv(root, code), env=self.environment(task),
                                capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_persisted_budget_survives_process_exit_and_is_task_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(self.run_child(root, "one")["rc"], 0)
            self.assertEqual(self.run_child(root, "one")["rc"], 2)
            self.assertEqual(root.joinpath("calls").read_text(), "x")
            self.assertEqual(self.run_child(root, "two")["rc"], 0)
            self.assertEqual(root.joinpath("calls").read_text(), "xx")

    def test_failed_open_is_not_replayed_by_new_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(self.run_child(root, "one", 3)["rc"], 3)
            self.assertEqual(self.run_child(root, "one")["rc"], 2)
            self.assertEqual(root.joinpath("calls").read_text(), "x")

    def test_concurrent_processes_cannot_both_reserve_single_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            children = []
            try:
                for _ in range(2):
                    children.append(subprocess.Popen(self.argv(root), env=self.environment("one"),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
                results = []
                for child in children:
                    out, err = child.communicate(timeout=8)
                    self.assertEqual(child.returncode, 0, err)
                    results.append(json.loads(out)["rc"])
                self.assertEqual(sorted(results), [0, 2])
                self.assertEqual(root.joinpath("calls").read_text(), "x")
            finally:
                for child in children:
                    if child.poll() is None:
                        child.kill()
                    child.communicate(timeout=3)


if __name__ == "__main__":
    unittest.main()
