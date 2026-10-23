#!/usr/bin/env python3
"""Read-only import diagnostic. Never activates hooks or contacts a device."""
import argparse
from pathlib import Path
import subprocess


def check_profiles(root, timeout=10):
    rows = []
    for profile in sorted(Path(root).iterdir()):
        if not profile.is_dir() or not any((profile / name).is_file()
                                          for name in ('package.json', 'cordis.patch.yml')):
            continue
        try:
            result = subprocess.run(
                ['node', '-e', "import('@deepseek-ai/dsh-hooks-claude-code').catch(e => {"
                 "console.error(e.name + ': ' + e.message); process.exit(1); })"],
                cwd=profile, capture_output=True, text=True, timeout=timeout)
            error = '' if result.returncode == 0 else (result.stderr.strip() or 'import failed')[:800]
        except (OSError, subprocess.TimeoutExpired) as exc:
            error = type(exc).__name__
        rows.append({'profile': profile.name, 'ok': not error, 'error': error})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profiles-root', type=Path, default=Path.home() / '.dsh/profiles')
    args = parser.parse_args()
    try:
        rows = check_profiles(args.profiles_root)
    except OSError as exc:
        print('HOOKS_IMPORT_FAIL: ' + type(exc).__name__)
        return 1
    for row in rows:
        print('%s %s hooks-cc %s' % ('PASS' if row['ok'] else 'FAIL', row['profile'], row['error']))
    if not rows:
        print('HOOKS_IMPORT_FAIL: no configured profiles')
    return 0 if rows and all(row['ok'] for row in rows) else 1


if __name__ == '__main__':
    raise SystemExit(main())
