"""首次配对专用私有 server；任何不明 5037 监听都是冲突，不是预检跳过。"""
import os
import json
import fcntl
from contextlib import contextmanager
import stat
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent / 'payload'))
import adb_policy as policy


class Conflict(RuntimeError):
    pass


def start_tcp(native):
    native = Path(native)
    tcp_owner(native, allow_empty=True)
    rows, _ = policy.server_processes()
    if rows:
        return
    env = policy.backend_env()
    env['ADB_EMU'] = '0'
    for name in ['ADB_SERVER_SOCKET', 'ANDROID_ADB_SERVER_ADDRESS', 'ANDROID_ADB_SERVER_PORT']:
        env.pop(name, None)
    # 由我们持有 bind 结果并把同一个监听 inode 交给 server，不向未知 TCP peer 探测协议。
    receipt = native.parent.parent / 'var/run/dsb-adb-tcp.json'
    receipt.parent.mkdir(parents=True, exist_ok=True)
    with policy.socket.socket(policy.socket.AF_INET, policy.socket.SOCK_STREAM) as listener:
        listener.setsockopt(policy.socket.SOL_SOCKET, policy.socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', 5037))
        listener.listen(16)
        fd = listener.fileno()
        child = subprocess.Popen([str(native), '-L', 'acceptfd:' + str(fd), 'server', 'nodaemon'],
                                 pass_fds=(fd,), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True, env=env)
        try:
            deadline = time.monotonic() + 5
            while True:
                rows, unknown = policy.server_processes()
                if len(rows) == 1 and rows[0]['pid'] == child.pid and not unknown:
                    break
                if child.poll() is not None or time.monotonic() >= deadline:
                    raise Conflict('socket activation 原生 ADB 未就绪')
                time.sleep(.05)
            data = {'schema': 'dsb-tcp-listener/v1', 'pid': child.pid, 'fd': fd,
                    'start': process_start(child.pid), 'inode': os.fstat(fd).st_ino,
                    'exe_sha256': policy.digest(native), 'address': ['127.0.0.1', 5037]}
            policy.write_state(receipt.name, json.dumps(data), receipt.parent)
        except BaseException:
            child.terminate()
            child.wait(timeout=5)
            raise


def process_start(pid):
    return (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()[19]


def holds_inode(pid, inode):
    for path in (Path('/proc') / str(pid) / 'fd').iterdir():
        try:
            if os.readlink(path) == f'socket:[{inode}]':
                return True
        except FileNotFoundError:
            continue
    return False


def tcp_candidate(native):
    rows, unknown = policy.server_processes()
    if (unknown or len(rows) != 1 or rows[0]['exe'] != str(native) or
            rows[0]['socket'] is None or not rows[0]['socket'].startswith(('tcp:', 'acceptfd:')) or
            policy.digest(f"/proc/{rows[0]['pid']}/exe") != policy.digest(native)):
        raise Conflict('ADB_CONFLICT：server 进程身份不符；未配对/连接')
    return rows[0]


def tcp_owner(native, allow_empty=False):
    probes = [policy.probe(f, h, 5037) for f, h in
              [(policy.socket.AF_INET, '127.0.0.1'), (policy.socket.AF_INET6, '::1')]]
    rows, unknown = policy.server_processes()
    if allow_empty and probes == ['closed', 'closed'] and not rows and not unknown:
        return None
    if probes != ['listening', 'closed']:
        raise Conflict('ADB_CONFLICT：5037 归属不明、外来监听或进程身份不符；未配对/连接')
    row = tcp_candidate(native)
    receipt = Path(native).parent.parent / 'var/run/dsb-adb-tcp.json'
    try:
        fd = os.open(receipt, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError('listener receipt owner/type/mode mismatch')
            data = json.loads(os.read(fd, 4096))
        finally:
            os.close(fd)
        if (data['schema'] != 'dsb-tcp-listener/v1' or data['address'] != ['127.0.0.1', 5037] or
                row['pid'] != data['pid'] or row['socket'] != 'acceptfd:' + str(data['fd']) or
                process_start(row['pid']) != data['start'] or data['exe_sha256'] != policy.digest(native) or
                not holds_inode(row['pid'], data['inode'])):
            raise ValueError('listener inode/PID/start/executable mismatch')
    except (OSError, ValueError, KeyError) as exc:
        raise Conflict('ADB_CONFLICT：5037 缺少可验证监听 inode 的本版本凭据；未配对/连接') from exc
    return row


def deployed_gate(prefix):
    root = prefix / 'var/lib/dsb-adb'
    if root.is_symlink():
        raise Conflict('旧版部署或非可信目录，需 owner 在场迁移；未写入')
    journal = json.loads((root / 'journal.json').read_text())
    manifest = json.loads((root / 'manifest.json').read_text())
    if journal.get('tool_version') == 1 and journal.get('state') == 'applied':
        raise Conflict('公开 v1 已加固：请先运行 bash install.sh rollback，再运行 bash install.sh；未改入口')
    if (journal.get('schema') != 'dsb-adb-private/v1' or journal.get('tool_version') not in [1, 2, 3] or
            manifest.get('schema') != 'dsb-adb-private/v1' or manifest.get('tool_version') not in [1, 2, 3] or
            journal.get('state') != 'applied' or not {'target_file', 'container_paths'} <= manifest.keys() or
            any(str(row.get('destination', '')).endswith('.r60-vendor')
                for data in [journal, manifest] for row in data.get('diversions', []))):
        raise Conflict('旧版部署或非 applied 状态，需 owner 在场迁移/恢复；未写入')


def _prepare(prefix):
    native = prefix / 'bin/adb'
    root = prefix / 'var/lib/dsb-adb'
    if root.exists() or root.is_symlink():
        # 系统授权页可能结束整个 Termux UID。先用有界 guard 恢复 server，
        # 后续 wx 做 shell 挑战与重连；不能要求离线设备预先通过完整 inventory。
        deployed_gate(prefix)
        subprocess.run([str(prefix / 'bin/python3'), '-I', str(root / 'adb_guard.py')], check=True, timeout=12,
                       env=policy.backend_env())
        return
    directory = prefix / 'var/run/dsb-adb-bootstrap'
    spec = 'localfilesystem:' + str(directory / 'server.sock')
    rows, unknown = policy.server_processes()
    probes = [policy.probe(f, h, 5037) for f, h in
              [(policy.socket.AF_INET, '127.0.0.1'), (policy.socket.AF_INET6, '::1')]]
    if probes != ['closed', 'closed']:
        row = tcp_candidate(native)
        # 只给已校验的本 UID PID 发信号，不向 TCP peer 发 kill-server。
        start = process_start(row['pid'])
        if (policy.digest(f"/proc/{row['pid']}/exe") != policy.digest(native) or
                process_start(row['pid']) != start):
            raise Conflict('ADB_CONFLICT：候选 server 在发送信号前发生变化')
        os.kill(row['pid'], 15)
        for _ in range(400):
            if all(policy.probe(f, h, 5037) == 'closed' for f, h in
                   [(policy.socket.AF_INET, '127.0.0.1'), (policy.socket.AF_INET6, '::1')]):
                break
            time.sleep(.1)
        else:
            raise Conflict('ADB_CONFLICT：原 server 未退出')
        rows, unknown = policy.server_processes()
    if unknown or any(r['exe'] != str(native) or r['socket'] != spec for r in rows):
        raise Conflict('ADB_CONFLICT：无法识别现有 server；未配对/连接。请先检查本机其它 ADB/Termux 会话并停止冲突服务，再重跑安装器；不要向未知5037发送 kill-server，也不要删除旧部署日志绕过检查')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    policy.private_dir(directory)
    os.umask(0o177)
    subprocess.run([str(native), '-L', spec, 'start-server'], check=True, timeout=10,
                   env={**policy.backend_env(), 'ADB_EMU': '0'})
    policy.socket_info(directory / 'server.sock')


@contextmanager
def control_lock(prefix):
    control = prefix / 'var/run/dsb-adb-bootstrap.lock'
    control.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(control, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise Conflict('bootstrap 控制锁属主/类型/权限不符')
        deadline = time.monotonic() + 30
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Conflict('ADB 重连尚未完成，bootstrap 未取得控制锁')
                time.sleep(.05)
        yield
    finally:
        os.close(fd)


def prepare(prefix):
    root = prefix / 'var/lib/dsb-adb'
    if root.exists() or root.is_symlink():
        deployed_gate(prefix)  # 先拒绝旧部署，不能先创建锁文件；锁内还要重新检查。
    with control_lock(prefix):
        _prepare(prefix)


if __name__ == '__main__':
    try:
        prepare(Path(os.environ['PREFIX']))
    except (OSError, ValueError, Conflict, policy.PolicyError, subprocess.SubprocessError) as exc:
        print('\033[31m' + str(exc) + '\033[0m', file=sys.stderr)
        sys.exit(2)
