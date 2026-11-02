"""Honest-mistake guard, not a shell parser or a same-UID security boundary.

Keep the state machine in sync with termux/adbq.sh and raw_input_cases.json.
Quotes delimit literal words; only substitutions and shell command arguments
are scanned recursively. No evaluation or exhaustive shell grammar parsing.
"""
import ast
import re

_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_PREFIXES = {"cmd", "exec", "nohup", "env", "command", "busybox", "toybox", "time",
             "do", "then", "else", "elif", "{", "!", "if", "while", "until"}


def _lex(text, start=0, end=""):
    tokens, word, quote = [], "", ""
    active, hit, depth, i = False, False, 0, start

    def flush():
        nonlocal word, active
        if active:
            tokens.append(("word", word))
        word, active = "", False

    while i < len(text):
        char = text[i]
        if not quote and end and char == end and depth == 0:
            i += 1
            break
        if quote == "'":
            if char == "'":
                quote = ""
            else:
                word += char
            i += 1
            continue
        if char == "\\" and i + 1 < len(text):
            following = text[i + 1]
            if not quote or following in '\\$`"\n':
                if following != "\n":
                    word += following
                    active = True
                i += 2
                continue
        if text.startswith("$(", i) or char == "`":
            sub, i, nested_hit = _lex(text, i + (2 if char == "$" else 1),
                                      ")" if char == "$" else "`")
            hit = hit or nested_hit or _blocked(sub)
            word += "${sub}"
            active = True
            continue
        if quote == '"':
            if char == '"':
                quote = ""
            else:
                word += char
            i += 1
            continue
        if char in "'\"":
            quote, active = char, True
        elif char == "#" and not active:
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        elif char in " \t\r":
            flush()
        elif char in ";&|()\n":
            flush()
            if char == "(":
                depth += 1
            elif char == ")" and depth:
                depth -= 1
            tokens.append(("boundary", char))
        elif char in "<>":
            if word.isascii() and word.isdigit():
                word, active = "", False
            flush()
            while i + 1 < len(text) and text[i + 1] in "<>&|":
                i += 1
            tokens.append(("redirect", char))
        else:
            word += char
            active = True
        i += 1
    flush()
    return tokens, i, hit


def has_raw_input(command):
    """Find basename 'input' at a command position, not in ordinary arguments."""
    # Linear fast path. Split spellings such as in\put intentionally pass:
    # adversarial obfuscation is outside this honest-mistake guard's scope.
    if "input" not in command:
        return False
    tokens, _, hit = _lex(command)
    return hit or _blocked(tokens)


def _blocked(tokens):
    state = "command"
    redirect = False
    for kind, word in tokens:
        base = word.rsplit("/", 1)[-1]
        if kind == "word" and not redirect and state == "su_command":
            if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", word):
                state = "c_arg"
                continue
            if word.startswith("-"):
                continue
            state = "command"
        if kind == "boundary":
            state = "command"
            redirect = False
        elif kind == "redirect":
            redirect = True
        elif redirect:
            redirect = False
        elif state == "adb":
            if word in ("shell", "exec-out"):
                state = "remote"
            elif word in ("push", "pull"):
                state = "args"
        elif state == "c_arg":
            if has_raw_input(word):
                return True
            state = "args"
        elif state in ("shell_c", "su"):
            if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", word):
                state = "c_arg"
            elif word.startswith("-"):
                continue
            elif base == "adbq.sh":
                state = "adb"
            else:
                state = "su_command" if state == "su" else "args"
        elif state == "timeout":
            if not word.startswith("-"):
                state = "command"
        elif state in ("command", "remote"):
            if _ASSIGNMENT.match(word) or word.startswith("-"):
                continue
            if state == "remote" and any(c.isspace() for c in word):
                if has_raw_input(word):
                    return True
                state = "args"
                continue
            if base == "input":
                return True
            if base in _PREFIXES:
                state = "command"
                continue
            if base in ("shell", "exec-out"):
                state = "remote"
            elif base in ("sh", "bash"):
                state = "shell_c"
            elif base in ("su", "timeout"):
                state = base
            elif base in ("adb", "adbq.sh"):
                state = "adb"
            else:
                state = "args"
    return False


def python_command_candidates(tree):
    """Yield (node, text) for argv groups and independent command-like literals."""
    excluded, grouped = set(), set()
    nodes = list(ast.walk(tree))
    for node in nodes:
        data = []
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.body and isinstance(node.body[0], ast.Expr):
                value = node.body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    data.append(value)
        elif isinstance(node, ast.Dict):
            data.extend(key for key in node.keys if key is not None)
        elif isinstance(node, ast.Compare):
            data.extend(x for x in [node.left, *node.comparators]
                        if isinstance(x, (ast.Constant, ast.JoinedStr)))
        elif isinstance(node, ast.Subscript):
            data.append(node.slice)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == "get" and len(node.args) == 1):
            data.append(node.args[0])
        for value in data:
            excluded.update(ast.walk(value))
    for node in nodes:
        values = node.args if isinstance(node, ast.Call) else (
            node.elts if isinstance(node, (ast.List, ast.Tuple)) else [])
        strings = [value for value in values if value not in excluded
                   and isinstance(value, ast.Constant) and isinstance(value.value, str)]
        grouped.update(strings)
        command = " ".join(value.value for value in strings)
        if len(command.split()) >= 2:
            yield node, command
    for node in nodes:
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and node not in excluded and node not in grouped
                and any(c.isspace() for c in node.value)):
            yield node, node.value
