#!/data/data/com.termux/files/usr/bin/python3
"""Bounded detector; never sends kill-server to an unidentified TCP peer."""
import json
from pathlib import Path
import os
import sys
import time
import signal
import ctypes

sys.path.insert(0, str(Path(__file__).resolve().parent))
from adb_policy import ROOT, load, probe, server_processes, _status_locked, pause_reason, ensure_server_locked, publish_ready, block, startup_lock, PolicyBusy, PolicyError, digest
import socket

# Android Python 不一定导出 os/signal 的 Linux 扩展；bionic 自 API 31 提供同一 pidfd API。
_LIBC = ctypes.CDLL(None, use_errno=True)
_PIDFD_OPEN = getattr(_LIBC, 'pidfd_open', None)
_PIDFD_SEND = getattr(_LIBC, 'pidfd_send_signal', None)
if _PIDFD_OPEN is not None:
    _PIDFD_OPEN.argtypes, _PIDFD_OPEN.restype = [ctypes.c_int, ctypes.c_uint], ctypes.c_int
if _PIDFD_SEND is not None:
    _PIDFD_SEND.argtypes, _PIDFD_SEND.restype = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint], ctypes.c_int


def open_pidfd(pid):
    if callable(getattr(os, 'pidfd_open', None)):
        return os.pidfd_open(pid)
    if _PIDFD_OPEN is None:
        raise PolicyError('duplicate recovery requires pidfd; no signal sent; use owner-assisted maintenance')
    fd = _PIDFD_OPEN(pid, 0)
    if fd < 0:
        raise OSError(ctypes.get_errno(), 'pidfd_open unavailable; no signal sent')
    return fd


def send_pidfd(fd):
    if callable(getattr(signal, 'pidfd_send_signal', None)):
        signal.pidfd_send_signal(fd, signal.SIGTERM)
    elif _PIDFD_SEND is None:
        raise PolicyError('pidfd_send_signal unavailable; no signal sent')
    elif _PIDFD_SEND(fd, signal.SIGTERM, None, 0) < 0:
        raise OSError(ctypes.get_errno(), 'pidfd_send_signal failed')


def managed_identity(row, config, proc_root='/proc'):
    proc = Path(proc_root) / str(row['pid'])
    # 对枚举结果重新取样；不把 PID、路径名或同 UID 单独当作终止进程的凭据。
    before = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
    uid = next(line.split()[1:] for line in (proc / 'status').read_text().splitlines() if line.startswith('Uid:'))
    rows, unknown = server_processes(proc_root)
    current = next((item for item in rows if item['pid'] == row['pid']), None)
    expected = {'pid': row['pid'], 'exe': config['backend'], 'socket': 'localfilesystem:' + config['socket']}
    after = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
    if (unknown or any(item['exe'] != config['backend'] or item['socket'] != expected['socket'] for item in rows) or
            row != expected or current != expected or before[0] in ['Z', 'X'] or
            before[19] != after[19] or uid != [str(os.getuid())] * 4 or
            digest(proc / 'exe') != digest(config['backend'])):
        raise PolicyError('managed server identity changed; no signal sent')
    return {**expected, 'uid': os.getuid(), 'start': after[19]}


def terminate_managed(identity, config):
    pid = identity['pid']
    fd = None
    try:
        # 必须钉住内核进程对象；旧内核无法消除 PID 复用窗口时拒绝自动终止。
        fd = open_pidfd(pid)
        if managed_identity({key: identity[key] for key in ['pid', 'exe', 'socket']}, config) != identity:
            raise PolicyError('managed PID/starttime changed before TERM')
        send_pidfd(fd)
    finally:
        if fd is not None:
            os.close(fd)


def heal_duplicates(config, rows):
    # 调用者持 start.lock 独占；先完整核验集合，再只 TERM 该集合，不使用 kill-server/killall。
    identities = [managed_identity(row, config) for row in rows]
    for identity in identities:
        terminate_managed(identity, config)
    deadline = time.monotonic() + 4
    while True:
        remaining, unknown = server_processes()
        if unknown or any(row not in rows for row in remaining):
            raise PolicyError('server set changed during recovery')
        if not remaining:
            break
        if time.monotonic() >= deadline:
            raise PolicyError('managed duplicate server did not exit after TERM')
        time.sleep(.05)
    ensure_server_locked(config)


def check_locked():
    # A stopped managed server can be restarted, but an unknown transport cannot.
    if pause_reason(manual_only=True):
        report = {'ok':False, 'error':pause_reason(manual_only=True)}
    else:
        try:
            config = load()
            rows, unknown = server_processes()
            spec = 'localfilesystem:' + config['socket']
            safe = not unknown and all(p['socket']==spec and p['exe']==config['backend'] for p in rows)
            safe = safe and all(probe(f,h,5037)=='closed' for f,h in
                                [(socket.AF_INET,'127.0.0.1'),(socket.AF_INET6,'::1')])
            if not safe: raise RuntimeError('unmanaged ADB process, listening default TCP port, or unknown probe')
            (ROOT/'blocked').unlink(missing_ok=True)
            if len(rows) > 1:
                heal_duplicates(config, rows)
            elif rows:
                publish_ready(config)
            else:
                ensure_server_locked(config)
            # Reconnect must remain possible when the authenticated device is offline.
            report = _status_locked(check_inventory=False)
            if not report['ok']: raise RuntimeError(report['error'])
        except Exception as exc:
            block('watchdog integrity/transport check failed: '+str(exc))
            report = {'ok':False, 'error':pause_reason(), 'servers':server_processes()}
    report.update(pid=int(sys.argv[1]) if len(sys.argv) == 2 else os.getppid(),
                  checked_at=time.time(), generation='dsb-adb-private-v2')
    tmp = ROOT / ('health.' + str(os.getpid()) + '.tmp')
    with open(tmp, 'w') as stream:
        json.dump(report, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, ROOT / 'health.json')
    if not report['ok']:
        print('ADB_GUARD_FAIL ' + json.dumps(report, sort_keys=True), file=sys.stderr)
    return 0 if report['ok'] else 2


def main():
    try:
        with startup_lock(timeout=0):
            return check_locked()
    except PolicyBusy:
        # 长期共享会话仍在运行，本轮不修改 ready/blocked/health，更不把未检查写成成功。
        print('ADB_GUARD_DEFERRED: active client; no health result published', file=sys.stderr)
        return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print('ADB_GUARD_ERROR '+type(exc).__name__, file=sys.stderr)
        sys.exit(2)
