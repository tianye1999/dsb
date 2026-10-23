#!/usr/bin/env python3
"""Conservative text edits for block patch lists; DSH validates the final YAML."""
import argparse
from pathlib import Path
import re
import sys

ENTRY = '- id: session-log-deepseek\n  config: {enabled: false}\n'
ID = re.compile(r'''^-\s+id:\s*(?:session-log-deepseek|'session-log-deepseek'|"session-log-deepseek")\s*(?:#.*)?$''')


def uncomment(text):
    quote = None
    i = 0
    while i < len(text):
        char = text[i]
        if quote:
            if char == '\\' and quote == '"':
                i += 2
                continue
            if char == quote:
                if quote == "'" and text[i:i + 2] == "''":
                    i += 2
                    continue
                quote = None
        elif char in "\"'":
            quote = char
        elif char == '#' and (i == 0 or text[i - 1].isspace()):
            return text[:i], text[i:]
        i += 1
    if quote:
        raise ValueError('unterminated quoted scalar')
    return text, ''


def entries(source):
    lines = source.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if re.match(r'^-\s', line)]
    end = next((i for i, line in enumerate(lines) if line.strip() == '...'), len(lines))
    return lines, [(start, next_start) for start, next_start in zip(starts, starts[1:] + [end])]


def flow_fields(body):
    """Offsets of top-level fields, retaining quoted commas and nested values."""
    quote = None
    depth = 0
    start = i = 0
    while i < len(body):
        char = body[i]
        if quote:
            if char == '\\' and quote == '"':
                i += 2
                continue
            if char == quote:
                if quote == "'" and body[i:i + 2] == "''":
                    i += 2
                    continue
                quote = None
        elif char in "\"'":
            quote = char
        elif char in '[{':
            depth += 1
        elif char in ']}':
            depth -= 1
        elif char == ',' and depth == 0:
            yield start, i
            start = i + 1
        i += 1
    if quote or depth:
        raise ValueError('unsupported inline config')
    yield start, len(body)


def update_entry(block, update):
    configs = [i for i, line in enumerate(block) if re.match(r'^  config:', line)]
    if not configs:
        if update:
            block[0] = block[0].rstrip('\r\n') + '\n'
            block.insert(1, '  config: {enabled: false}\n')
        return block, False
    if len(configs) != 1:
        raise ValueError('duplicate config fields')
    index = configs[0]
    code, comment = uncomment(block[index].rstrip('\r\n'))
    value = code[len('  config:'):].strip()
    if value:
        if not (value.startswith('{') and value.endswith('}')):
            raise ValueError('config must be an inline map or indented block')
        body = value[1:-1]
        matches = []
        for start, end in flow_fields(body):
            field = re.fullmatch(r'''\s*(?:enabled|'enabled'|"enabled")\s*:\s*(.*?)\s*''', body[start:end])
            if field:
                matches.append((start + field.start(1), start + field.end(1)))
        closed = bool(matches) and all(body[a:b] == 'false' for a, b in matches)
        if update and not closed:
            for a, b in reversed(matches):
                body = body[:a] + 'false' + body[b:]
            if not matches:
                body = 'enabled: false' + (', ' + body if body.strip() else '')
            block[index] = '  config: {' + body + '}' + (' ' + comment if comment else '') + '\n'
        return block, closed
    end = next((i for i in range(index + 1, len(block))
                if block[i].strip() and not block[i].lstrip().startswith('#')
                and re.match(r'^\S|^  \S', block[i])), len(block))
    flags = [i for i in range(index + 1, end) if re.match(r'^    enabled:', block[i])]
    closed = bool(flags) and all(uncomment(block[i].split(':', 1)[1])[0].strip() == 'false' for i in flags)
    if update and not closed:
        for i in flags:
            flag, suffix = uncomment(block[i].rstrip('\r\n'))
            if flag.split(':', 1)[1].strip().startswith(('|', '>')):
                raise ValueError('multiline enabled value is unsupported')
            block[i] = '    enabled: false' + (' ' + suffix if suffix else '') + '\n'
        if not flags:
            block[index] = block[index].rstrip('\r\n') + '\n'
            block.insert(index + 1, '    enabled: false\n')
    return block, closed


def enabled_false(source):
    try:
        lines, spans = entries(source)
        targets = [update_entry(lines[a:b], False)[1] for a, b in spans if ID.fullmatch(lines[a].strip())]
        return bool(targets) and all(targets)
    except ValueError:
        return False


def normalize(source):
    lines, spans = entries(source)
    meaningful = [(i, line.strip()) for i, line in enumerate(lines)
                  if line.strip() and not line.lstrip().startswith('#')
                  and line.strip() not in ('---', '...')]
    if len(meaningful) == 1 and re.fullmatch(r'\[\]\s*(?:#.*)?', meaningful[0][1]):
        index = meaningful[0][0]
        _, comment = uncomment(lines[index].rstrip('\r\n'))
        lines[index] = (comment + '\n' if comment else '') + ENTRY
        return ''.join(lines)
    if meaningful and not spans:
        raise ValueError('expected a block patch list or empty []')
    found = False
    for a, b in reversed(spans):
        if ID.fullmatch(lines[a].strip()):
            lines[a:b], _ = update_entry(lines[a:b], True)
            found = True
    if not found:
        end = next((i for i, line in enumerate(lines) if line.strip() == '...'), len(lines))
        prefix = ''.join(lines[:end])
        return prefix + ('' if not prefix or prefix.endswith('\n') else '\n') + ENTRY + ''.join(lines[end:])
    return ''.join(lines)


def enforce(path, destination=None):
    path = Path(path)
    output = normalize(path.read_text(encoding='utf-8'))
    target = Path(destination) if destination else path
    if not target.exists() or target.read_text(encoding='utf-8') != output:
        target.write_text(output, encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-dump', action='store_true')
    parser.add_argument('source', type=Path, nargs='?')
    parser.add_argument('destination', type=Path, nargs='?')
    args = parser.parse_args()
    if args.verify_dump:
        raise SystemExit(0 if enabled_false(sys.stdin.read()) else 1)
    if args.source is None:
        parser.error('source is required')
    enforce(args.source, args.destination)
