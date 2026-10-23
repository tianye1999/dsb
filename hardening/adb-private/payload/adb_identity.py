"""用跨 UID 的 /proc 可见性验证本机 shell，不把答案发送给端点。"""
import os
import json
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import time
import signal
from contextlib import nullcontext, contextmanager
sys.path.insert(0, str(Path(__file__).resolve().parent))
from adb_client import client_run, active_runtime, check_runtime_maps
import adb_relay


class IdentityError(RuntimeError):
    pass


def generation(adb):
    from adb_policy import server_processes
    rows, unknown = server_processes()
    backend = str(Path(adb[0]).resolve())
    if unknown or len(rows) != 1 or rows[0]['exe'] != backend:
        # 只记录进程元数据，不记录命令输出、环境或挑战答案。
        raise IdentityError('ADB server 代次不可识别：'
                            f'servers={len(rows)}, unknown={unknown}, '
                            f'backend_match={[row["exe"] == backend for row in rows]}')
    row = rows[0]
    config = active_runtime(backend, verify=False)  # maps validation below also verifies every private file.
    if config is not None:
        check_runtime_maps(row['pid'], config)
    start = (Path('/proc') / str(row['pid']) / 'stat').read_text().rsplit(')', 1)[1].split()[19]
    return {'pid': row['pid'], 'start': start, 'exe': row['exe'], 'socket': row['socket']}


def transport_id(adb, target):
    result = client_run([*map(str, adb), 'devices', '-l'], stdin=subprocess.DEVNULL,
                            capture_output=True, timeout=3)
    found = []
    for line in result.stdout.decode('utf-8', 'strict').splitlines():
        fields = line.split()
        if len(fields) > 2 and fields[:2] == [target, 'device']:
            found += [int(field.split(':', 1)[1]) for field in fields[2:]
                      if field.startswith('transport_id:') and field.split(':', 1)[1].isdecimal()]
    if result.returncode or len(found) != 1 or found[0] <= 0:
        raise IdentityError('端点没有唯一在线 transport ID')
    return found[0]


def read_binding(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
        raise IdentityError('transport 凭据 owner/type/mode 不符')
    data = json.loads(path.read_text())
    if data.get('version') != 2 or not isinstance(data.get('transport_id'), int) or data['transport_id'] <= 0:
        raise IdentityError('transport 凭据格式不符；需重新核验连接')
    return data


def verified_id(adb, path):
    data = read_binding(path)
    relay_state(data['relay'], live=True)
    if (data['generation'] != generation(adb) or data['transport_id'] != transport_id(adb, data['serial']) or
            data['generation'] != generation(adb)):
        raise IdentityError('连接/server 代次已变化；新连接尚未通过本机身份挑战')
    relay_state(data['relay'], live=True)
    return data['transport_id']


def save_binding(path, target, tid, server, relay):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump({'version': 2, 'target': target, 'serial': 'localfilesystem:' + relay['socket'],
                       'transport_id': tid, 'generation': server, 'relay': relay}, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        (path.parent / 'adb-blocked').unlink(missing_ok=True)
    finally:
        temp.unlink(missing_ok=True)


def authenticated_shell(adb, target, inventory=False, record=None, bound=False, relay=None, disconnect=True):
    adb = list(map(str, adb))
    server = generation(adb)  # 未确认 server 归属之前，连 disconnect 也不能发。
    data = None
    try:
        data = read_binding(record) if bound else None
        serial = data['serial'] if bound else target
        descriptor = data['relay'] if bound else relay
        with relay_lease(descriptor) if descriptor else nullcontext():
            tid = verified_id(adb, record) if bound else transport_id(adb, serial)
            answer = challenge_transport(adb, tid, inventory)
            if server != generation(adb) or tid != transport_id(adb, serial):
                raise IdentityError('身份核验期间连接发生变化')
            if bound:
                verified_id(adb, record)
            elif record is not None:
                relay_state(relay, live=True)
                save_binding(record, relay['target'], tid, server, relay)
        return answer
    except (OSError, ValueError, IdentityError, subprocess.SubprocessError):
        # 失败连接不留给后续按 serial 的客户端；这里不提交任何动作。
        if record is not None and disconnect:
            Path(record).unlink(missing_ok=True)
        try:
            if disconnect and server == generation(adb):
                spec = adb[adb.index('-L') + 1] if '-L' in adb else os.environ.get('ADB_SERVER_SOCKET', '')
                if not spec.startswith('localfilesystem:'):
                    from bootstrap import tcp_owner
                    tcp_owner(Path(adb[0]))
                serial = data['serial'] if bound and data else target
                client_run([*adb, 'disconnect', serial], stdin=subprocess.DEVNULL, capture_output=True, timeout=3)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            pass  # 先失效凭据；归属不明或清理超时不能发更多请求，也不能保留旧绑定。
        raise


def relay_state(descriptor, live=False):
    """只接受本 UID 的私有状态与同一 PID/start/script；不对未知进程发信号。"""
    path = Path(descriptor['state_file'])
    for entry, mode in [(path.parent, 0o700), (path, 0o600)]:
        info = entry.lstat()
        valid_type = stat.S_ISDIR(info.st_mode) if entry == path.parent else stat.S_ISREG(info.st_mode)
        if not valid_type or stat.S_IMODE(info.st_mode) != mode or info.st_uid != os.getuid():
            raise IdentityError('连接通道状态 owner/type/mode 不符')
    row = json.loads(path.read_text())
    if any(row[key] != descriptor[key] for key in ['pid', 'start', 'socket', 'target', 'script']):
        raise IdentityError('连接通道状态代次不符')
    endpoint = Path(row['socket'])
    info = endpoint.lstat()
    if (endpoint != path.with_suffix('.sock') or not stat.S_ISSOCK(info.st_mode) or
            info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
        raise IdentityError('连接通道 socket 不可信')
    proc = Path('/proc') / str(row['pid'])
    argv = (proc / 'cmdline').read_bytes().split(b'\0')
    if (proc.stat().st_uid != os.getuid() or adb_relay.process_start(row['pid']) != row['start'] or
            os.fsencode(descriptor['script']) not in argv or os.fsencode(path) not in argv):
        raise IdentityError('连接通道进程身份不符')
    if live and (row['accepted'] is not True or row['dead'] is not False):
        raise IdentityError('已验证连接已断开；旧通道不会重连，需重新挑战')
    return row


@contextmanager
def relay_lease(descriptor):
    # 地址在本 UID 的 0700 目录内。进程意外退出时，外来 UID 也不能替换该地址。
    relay_state(descriptor)
    yield


@contextmanager
def binding_lease(path):
    data = read_binding(path)
    with relay_lease(data['relay']):
        yield data


def launch_relay(target, record, script):
    directory = Path(record).parent / 'adb-relays'
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if directory.is_symlink() or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid():
        raise IdentityError('连接通道目录不可信')
    state = directory / (secrets.token_hex(16) + '.json')
    child = subprocess.Popen([str(Path(os.environ['PREFIX']) / 'bin/python3'), '-I', str(script), target, str(state)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, close_fds=True,
                             env={k: v for k, v in os.environ.items() if not k.startswith('LD_')})
    try:
        deadline = time.monotonic() + 3
        while not state.exists():
            if child.poll() is not None or time.monotonic() > deadline:
                raise IdentityError('连接通道未就绪')
            time.sleep(.02)
        row = json.loads(state.read_text())
        descriptor = {**{key: row[key] for key in ['pid', 'start', 'socket', 'target']},
                      'state_file': str(state), 'script': str(script)}
        relay_state(descriptor)
        return descriptor
    except BaseException:
        child.terminate()
        child.wait(timeout=3)
        state.unlink(missing_ok=True)
        raise


def retire_relay(descriptor):
    # PID 复用或 zombie 是过时代次，不能发信号，但也不能永久卡住私有登记回收。
    proc = Path('/proc') / str(descriptor['pid'])
    try:
        fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
        stale = fields[19] != descriptor['start'] or fields[0] == 'Z'
    except FileNotFoundError:
        stale = True
    if stale:
        state = Path(descriptor['state_file'])
        if Path(descriptor['socket']) != state.with_suffix('.sock'):
            raise IdentityError('过期连接通道路径不符，未清理')
        for entry in [state, state.with_suffix('.sock')]:
            if entry.exists() and (entry.is_symlink() or entry.lstat().st_uid != os.getuid()):
                raise IdentityError('过期连接通道属主不符，未清理')
            entry.unlink(missing_ok=True)
        return
    try:
        relay_state(descriptor)
    except FileNotFoundError:
        # 进程已退出时只移除状态，不向可能复用的 PID 发信号。
        if not (Path('/proc') / str(descriptor['pid'])).exists():
            Path(descriptor['state_file']).unlink(missing_ok=True)
            Path(descriptor['socket']).unlink(missing_ok=True)
            return
        raise
    os.kill(descriptor['pid'], signal.SIGTERM)
    Path(descriptor['state_file']).unlink(missing_ok=True)
    Path(descriptor['socket']).unlink(missing_ok=True)


def connect_verified(adb, target, record, script, root=None, locked=False):
    """健康连接只持共享锁；建立/撤销连接必须与所有设备调用互斥。"""
    import adb_policy as policy
    adb = list(map(str, adb))
    record = Path(record)
    previous = None
    with policy.startup_lock(root, shared=True) if root and not locked else nullcontext():
        try:
            data = read_binding(record)
            previous = data
            if data['target'] == target:
                return authenticated_shell(adb, target, record=record, bound=True, disconnect=False)
        except (OSError, ValueError, KeyError, IdentityError, subprocess.SubprocessError):
            pass
    with policy.startup_lock(root) if root and not locked else nullcontext():
        generation(adb)
        # 先失效凭据，后 disconnect，最后才释放旧 relay 的端口。
        old = previous
        try:
            old = json.loads(record.read_text())
        except (OSError, ValueError):
            pass
        record.unlink(missing_ok=True)
        try:
            for serial in dict.fromkeys([target, (old or {}).get('serial', target)]):
                client_run([*adb, 'disconnect', serial], stdin=subprocess.DEVNULL, capture_output=True, timeout=3)
        finally:
            if old and old.get('relay'):
                retire_relay(old['relay'])
            # doctor 挑战失败也会清除 binding；不能因此丢失待退役通道的登记。
            directory = record.parent / 'adb-relays'
            for state in directory.glob('*.json'):
                row = json.loads(state.read_text())
                descriptor = {**{key: row[key] for key in ['pid', 'start', 'socket', 'target']},
                              'state_file': str(state), 'script': row['script']}
                # 仅回收当前脚本创建的本 UID/PID-start 匹配进程；未知记录不猜测。
                retire_relay(descriptor)
        descriptor = launch_relay(target, record, script)
        serial = 'localfilesystem:' + descriptor['socket']
        try:
            result = client_run([*adb, 'connect', serial], stdin=subprocess.DEVNULL, capture_output=True, timeout=8)
            if result.returncode:
                raise IdentityError('一次性连接建立失败')
            deadline = time.monotonic() + 5
            while True:
                try:
                    transport_id(adb, serial)
                    break
                except IdentityError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(.1)
            return authenticated_shell(adb, serial, record=record, relay=descriptor)
        except BaseException:
            record.unlink(missing_ok=True)
            try:
                client_run([*adb, 'disconnect', serial], stdin=subprocess.DEVNULL, capture_output=True, timeout=3)
            finally:
                retire_relay(descriptor)
            raise


def challenge_transport(adb, tid, inventory=False):
    prefix = Path(os.environ.get('PREFIX', '/data/data/com.termux/files/usr'))
    nonce = 'dsb-challenge-' + secrets.token_hex(32)
    child = subprocess.Popen([str(prefix / 'bin/bash'), '-c', 'read -r -t 10', 'challenge', nonce],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True,
                             env={k: v for k, v in os.environ.items() if not k.startswith('LD_')})
    try:
        deadline = time.monotonic() + 2
        while True:
            expected = (Path('/proc') / str(child.pid) / 'cmdline').read_bytes()
            if nonce.encode() in expected.split(b'\0'):
                break
            if child.poll() is not None or time.monotonic() >= deadline:
                raise IdentityError('本机身份挑战进程未就绪')
            time.sleep(0.01)
        # 只发送 PID，nonce 仅保留在本 UID 的进程中。普通 App 不能读取它。
        command = f'test "$(id -u)" = 2000 && cat /proc/{child.pid}/cmdline'
        if inventory:
            command += " && printf '\\nDSB_TCP\\n' && cat /proc/net/tcp /proc/net/tcp6"
        result = client_run([*map(str, adb), '-t', str(tid), 'shell', command],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=8)
        answer, sep, tables = result.stdout.partition(b'\nDSB_TCP\n') if inventory else (result.stdout, b'', b'')
        if result.returncode or answer != expected or (inventory and not sep):
            raise IdentityError('本机 shell 身份挑战失败（未提交后续命令）：'
                                f'rc={result.returncode}, answer_bytes={len(answer)}, '
                                f'expected_bytes={len(expected)}, separator={bool(sep)}, '
                                f'child_running={child.poll() is None}, '
                                f'stderr={result.stderr[:256]!r}')
        return tables.decode('ascii') if inventory else '2000'
    finally:
        if child.poll() is None:
            child.terminate()
        child.stdin.close()
        child.wait(timeout=2)


def execute_bound(prefix, record, args):
    import adb_policy as policy
    from bootstrap import tcp_owner
    nonce = os.environ.pop('DSB_ADB_RECEIPT_NONCE', '')
    try:
        with policy.startup_lock(record.parent, shared=True) as lock_fd, binding_lease(record):
            native = prefix / 'bin/adb'
            data = read_binding(record)
            endpoint = prefix / 'var/run/dsb-adb-bootstrap/server.sock'
            spec = 'localfilesystem:' + str(endpoint)
            if data['generation']['socket'] == spec:
                # 首装已配对但尚未切换 launcher；只接受固定私有 bootstrap，不信环境覆盖。
                policy.require_closed_default()
                policy.private_dir(endpoint.parent)
                policy.socket_info(endpoint)
            else:
                tcp_owner(native)
                spec = 'tcp:localhost:5037'
            raw = [str(native), '-L', spec]
            tid = verified_id(raw, record)
            selected, tail = policy.command_parts(args)
            if selected not in [[], ['-t', str(tid)], ['-s', data['serial']], ['-s', data['target']]]:
                raise IdentityError('selector 与已核验连接不符')
            if not tail or tail[0].startswith('-') or not policy.device_command([*selected, *tail]):
                raise IdentityError('仅允许已绑定的设备命令')
            return client_run([*raw, '-t', str(tid), *tail], close_fds=True, pass_fds=(lock_fd,)).returncode
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        print('ADB_POLICY_DENIED: ' + str(exc), file=sys.stderr)
        if len(nonce) == 64 and all(c in '0123456789abcdef' for c in nonce):
            print('ADB_POLICY_RECEIPT:' + nonce, file=sys.stderr)
        return 78


def main():
    import sys
    if sys.argv[1:2] == ['--execute']:
        # 容器调用时 HOME 是 /root；不能据此改用另一份凭据或另一条 server。
        home = Path(__file__).resolve().parent.parent
        prefix = home.parent / 'usr'
        record = home / 'dsb/adb-transport.json'
        os.environ.update(PREFIX=str(prefix), HOME=str(home))
        return execute_bound(prefix, record, sys.argv[2:])
    prefix = Path(os.environ['PREFIX'])
    root = prefix / 'var/lib/dsb-adb'
    record = Path(os.environ['HOME']) / 'dsb/adb-transport.json'
    if len(sys.argv) != 2:
        raise IdentityError('用法：adb_identity.py 本机IP:端口')
    # wx 已在 shell 层持有同一控制锁，不能再次获取。
    from bootstrap import control_lock
    with control_lock(prefix) if os.environ.get('DSB_ADB_CONNECT_LOCKED') != '1' else nullcontext():
        if root.exists():
            import adb_policy as policy
            config = policy.load(root)
            policy.require_closed_default()
            if policy.pause_reason(root) or not policy.server_ready(config):
                raise IdentityError('私有传输暂停/未就绪，未提交身份挑战')
            print(connect_verified([config['backend'], '-L', 'localfilesystem:' + config['socket']],
                                   sys.argv[1], record, root / 'adb_relay.py', root=root))
        else:
            # bootstrap 在私有 socket；普通模式须先核监听 inode，不能向陌生5037提交挑战。
            native = prefix / 'bin/adb'
            if not os.environ.get('ADB_SERVER_SOCKET', '').startswith('localfilesystem:'):
                from bootstrap import tcp_owner
                tcp_owner(native)
            print(connect_verified([native], sys.argv[1], record, Path(__file__).with_name('adb_relay.py'), root=record.parent))
    return 0


if __name__ == '__main__':
    import sys
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, IdentityError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
