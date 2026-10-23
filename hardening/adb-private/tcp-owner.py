"""普通传输的 fail-closed 归属检查，禁止给外来 5037 发 ADB 请求。"""
import os
import ipaddress
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bootstrap import tcp_owner, start_tcp
from adb_identity import authenticated_shell


def gate_path():
    return Path(os.environ['HOME']) / 'dsb/adb-blocked'


def check_saved_target(native, target_file):
    tcp_owner(native)
    target = Path(target_file).read_text().strip()
    host, port = target.rsplit(':', 1)
    ipaddress.IPv4Address(host)
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError('invalid saved ADB target')
    # 监听归属不等于设备身份；doctor 不重连、不改端点，只发无答案的挑战。
    authenticated_shell([native, '-L', 'tcp:localhost:5037'], target,
                        record=Path(target_file).with_name('adb-transport.json'), bound=True)


def main():
    if sys.argv[1:] not in [[], ['--check']]:
        raise ValueError('usage: adb-tcp-owner.py [--check]')
    native = Path(os.environ['PREFIX']) / 'bin/adb'
    if sys.argv[1:] == ['--check']:
        check_saved_target(native, Path(os.environ['HOME']) / 'dsb/adb-target')
        gate_path().unlink(missing_ok=True)
    else:
        tcp_owner(native, allow_empty=True)
        start_tcp(native)
        tcp_owner(native)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        gate = gate_path()
        gate.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(gate, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write('ADB_CONFLICT: run installer doctor; no commands submitted\n')
        print('ADB_CONFLICT: ' + str(exc), file=sys.stderr)
        sys.exit(2)
