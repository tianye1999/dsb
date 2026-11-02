"""Repository-wide literal/command scan, not dynamic data-flow analysis.

Python uses AST (including argv lists and f-string constant fragments). Shell
commands and shell/JS/Java quoted literals use a small lexical scan. Computed
command names, expansion and obfuscation are outside the honest-mistake model.
"""
import ast
from fnmatch import fnmatch
from pathlib import Path
import re
import shlex
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent"))
from raw_input_guard import has_raw_input, python_command_candidates

ALLOWED_FILES = {
    "agent/safe_tap.py": "Regular gated dispatch and fixed lifecycle operations",
    "agent/safe_tap_smoke.py": "Existing device-only smoke test setup/cleanup",
}
ALLOWED_COMMANDS = {
    ("agent/vd_session.py", "input -d %d keyevent 224"): "Fixed internal virtual-display WAKEUP",
    ("agent/screen_guard.py", "shell input keyevent 224"):
        "prepare() 里固定的主屏 WAKEUP（亮屏，不解锁），先于 look，属生命周期操作",
    ("app/src/com/dsb/assistant/OverlayService.java", "input keyevent 279"):
        "App-process clipboard paste",
    ("termux/adbq.sh", "input) return 0 ;;"): "Case label in the guard, not a dispatch",
}
ALLOWED_PATTERNS = {
    "docs/*": "Documentation and documentation assets",
    "*/tests/*": "Test fixtures (never production entry points)",
    "agent/test_*.py": "Offline regression fixtures",
    "agent/*_test.py": "Existing test fixtures",
}
# These exact data literals are not executable commands. Do not exempt their
# whole files: a new dispatch in the same file must still fail this test.
NON_COMMAND_LITERALS = {
    ("agent/check_gate.py", "input keyevent 26"),  # SUSPECT_HINTS, not a whitelist
    ("agent/dsb_run.py", "input (command word)"),  # refusal diagnostic
    ("agent/task_report.py", "input and output paths must be distinct"),
    ("dsh-plugin/runtime/agent/task_report.py", "input and output paths must be distinct"),
}
SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", "build", "dist", ".ruff_cache", ".pytest_cache"}
SUFFIXES = {".py", ".sh", ".mjs", ".java"}
_QUOTED = r'''(?P<quote>["'`])(?P<body>(?:\\.|(?!(?P=quote))[\s\S])*)(?P=quote)'''
_C_LITERALS = re.compile(r"//[^\n]*|/\*[\s\S]*?\*/|" + _QUOTED)
_SH_ASSIGNMENT = re.compile(r"^[ \t]*[A-Za-z_][A-Za-z0-9_]*=" + _QUOTED, re.M)


def candidates(path):
    source = path.read_text(encoding="utf-8")
    if path.suffix == ".py":
        tree = ast.parse(source, filename=str(path))
        for node, command in python_command_candidates(tree):
            yield node.lineno, command
    elif path.suffix == ".sh":
        # Retain quotes: shlex.split + join would turn quoted regexes into code.
        pending, start = "", 1
        for number, line in enumerate(source.splitlines(keepends=True), 1):
            if not pending:
                start = number
            pending += line
            try:
                shlex.split(pending, comments=True)  # Only check quote balance.
            except ValueError:
                continue
            yield start, pending.strip()
            pending = ""
        if pending:
            yield start, pending.strip()
        for match in _SH_ASSIGNMENT.finditer(source):
            yield source.count("\n", 0, match.start()) + 1, match.group("body")
    else:
        for match in _C_LITERALS.finditer(source):
            if match.group("body") is not None:
                yield source.count("\n", 0, match.start()) + 1, match.group("body")


def scan(root):
    violations = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if (not path.is_file() or path.suffix not in SUFFIXES
                or any(part in SKIP_DIRS for part in path.relative_to(root).parts)):
            continue
        if relative in ALLOWED_FILES or any(fnmatch(relative, p) for p in ALLOWED_PATTERNS):
            continue
        for line, value in candidates(path):
            if ((relative, value) not in NON_COMMAND_LITERALS
                    and (relative, value) not in ALLOWED_COMMANDS and has_raw_input(value)):
                violations.append("%s:%d: %r" % (relative, line, value))
    return violations


class RawInputArchitectureTests(unittest.TestCase):
    def test_repository_has_no_unregistered_dispatch(self):
        self.assertEqual(scan(ROOT), [], "Register an owner-reviewed dispatch exception or use safe_tap")

    def test_new_unregistered_calls_fail_for_each_source_type(self):
        examples = {
            "new.py": 'command = "input -d 3 tap 1 2"\n',
            "argv.py": 'command = ["input", "-d", "3", "tap", "1", "2"]\n',
            "new.sh": 'input -d 3 tap 1 2\n',
            "quoted.sh": 'command="input -d 3 tap 1 2"\n',
            "loop.sh": 'for i in 1 2; do input tap 1 2; done\n',
            "conditional.sh": 'if true; then input tap 1 2; fi\n',
            "new.mjs": 'const command = "input -d 3 tap 1 2";\n',
            "New.java": 'class New { String command = "input -d 3 tap 1 2"; }\n',
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, source in examples.items():
                (root / name).write_text(source)
            found = {entry.split(":", 1)[0] for entry in scan(root)}
            self.assertEqual(found, set(examples))

    def test_read_only_arguments_and_comments_are_not_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "query.py").write_text('"""input tap is prohibited"""\ncmd = "dumpsys input"\n')
            (root / "query.sh").write_text(
                '# input tap 1 2\necho input\nls /dev/input\necho "input tap"\n'
                "dumpsys window | grep -E '(input|focus)'\necho 'literal\ninput tap 1 2\n'\n")
            (root / "query.mjs").write_text('// input tap 1 2\nconst command = "echo input";\n')
            self.assertEqual(scan(root), [])

    def test_lifecycle_exceptions_do_not_allow_new_commands(self):
        examples = {
            "agent/vd_session.py": 'remote = "input -d %d keyevent 224"\n',
            "agent/screen_guard.py": 'sh("shell", "input", "keyevent", "224")\n',
            "app/src/com/dsb/assistant/OverlayService.java": 'String command = "input keyevent 279";\n',
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, source in examples.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(source)
            self.assertEqual(scan(root), [])
            for name, source in examples.items():
                path = root / name
                path.write_text(source + '\ncommand = "input -d 3 tap 1 2"\n')
                self.assertTrue(any(entry.startswith(name + ":") for entry in scan(root)))
                path.write_text(source)

    def test_generated_directories_are_not_scanned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("build", "dist", ".ruff_cache", ".pytest_cache"):
                path = root / name
                path.mkdir()
                (path / "generated.py").write_text('cmd = "input tap 1 2"\n')
            self.assertEqual(scan(root), [])


if __name__ == "__main__":
    unittest.main()
