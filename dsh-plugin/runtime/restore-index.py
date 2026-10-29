#!/usr/bin/env python3
"""Explicit, version-pinned migration from legacy disk patches to dsh-dsb."""
import argparse
import hashlib
from pathlib import Path
import re
import uuid

PRISTINE_SHA256 = '3a402b94efa1253d80666438e60ed89d0b5cf1fd07d24a2f59731623e9bd8a49'
HERE = Path(__file__).resolve().parent
DEFAULT_PRISTINE = (HERE / 'pristine-0.2.1-rc.2.html' if HERE.name == 'runtime' else
                    HERE.parent / 'dsh-plugin/runtime/pristine-0.2.1-rc.2.html')
LEGACY = re.compile(rb'''\bid\s*=\s*["']dsh-(?:composer|repair|font|content|chat-bg|privacy-ui|rail|hide-tabs|light-enforce|dark-enforce|tobottom-keep)[\w-]*["']|<!--\s*(?:dsh-chat-bg|DSH \xe4\xbe\xa7\xe6\xa0\x8f)''', re.I)


def has_legacy(target):
    target = Path(target)
    if target.is_symlink():
        raise ValueError('index path must not be a symbolic link')
    return bool(LEGACY.search(target.read_bytes()))


def restore(target, pristine=DEFAULT_PRISTINE):
    target, pristine = Path(target), Path(pristine)
    if target.is_symlink() or pristine.is_symlink():
        raise ValueError('index paths must not be symbolic links')
    temporary = target.with_name(target.name + '.dsh-dsb-tmp')
    if temporary.is_symlink() or temporary.is_dir():
        raise ValueError('unsafe restoration temporary; inspect and remove it before retrying: ' + str(temporary))
    temporary.unlink(missing_ok=True)
    clean = pristine.read_bytes()
    if hashlib.sha256(clean).hexdigest() != PRISTINE_SHA256:
        raise ValueError('pristine index is not the pinned DSH 0.2.1-rc.2 shell')
    before = target.read_bytes()
    if before == clean:
        return False
    if not LEGACY.search(before):
        raise ValueError('no recognized legacy marker; refusing to overwrite')
    assets = lambda data: set(re.findall(rb'assets/[\w.-]+\.(?:js|css)', data))
    if assets(before) != assets(clean):
        raise ValueError('asset references differ; reinstall the matching frontend first')
    for asset in assets(clean):
        if not (target.parent / asset.decode()).is_file():
            raise ValueError('missing frontend asset: ' + asset.decode())
    backup = target.with_name(target.name + '.before-dsh-dsb')
    if backup.exists() or backup.is_symlink():
        backup = backup.with_name(backup.name + '.' + uuid.uuid4().hex)
    # Exclusive backup: never overwrite the original migration evidence.
    with backup.open('xb') as stream:
        stream.write(before)
    backup.chmod(0o600)
    created = False
    try:
        with temporary.open('xb') as stream:
            created = True
            stream.write(clean)
        temporary.chmod(target.stat().st_mode & 0o777)
        temporary.replace(target)
    finally:
        if created:
            temporary.unlink(missing_ok=True)
    return True


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index', required=True)
    parser.add_argument('--pristine', default=DEFAULT_PRISTINE)
    parser.add_argument('--check-legacy', action='store_true')
    args = parser.parse_args()
    if args.check_legacy:
        print('legacy' if has_legacy(args.index) else 'untouched')
    else:
        print('restored' if restore(args.index, args.pristine) else 'already pristine')
