#!/data/data/com.termux/files/usr/bin/python3
"""Managed ADB transport policy. Unknown checks never mean success."""
import errno
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import tempfile
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parent))
import adb_identity
from contextlib import nullcontext
from adb_client import backend_env, client_run, check_runtime, check_runtime_maps, file_metadata, runtime_verification_scope

# 部署后策略与 manifest 同目录；不相信调用者传入的环境路径。
ROOT = Path(__file__).resolve().parent


class PolicyError(RuntimeError):
    pass


class PolicyBusy(PolicyError):
    pass


def pause_reason(root=ROOT, manual_only=False):
    for name in ['maintenance'] if manual_only else ['maintenance', 'blocked']:
        path = Path(root)/name
        try: path.lstat()
        except FileNotFoundError: continue
        except OSError as exc: return 'ADB_POLICY_PAUSED: '+name+' gate: state unreadable: '+str(exc)
        try:
            fd=os.open(path,os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW)
            try: reason=os.read(fd,512).decode('utf-8','replace').splitlines()[0]
            finally: os.close(fd)
        except (OSError,IndexError): reason='pause reason unavailable; run doctor'
        return 'ADB_POLICY_PAUSED: '+name+' gate: '+(reason or 'pause reason unavailable; run doctor')
    return None


def write_state(name, value, root=ROOT):
    root = Path(root)
    tmp = root/(name+'.'+str(os.getpid())+'.tmp')
    with open(tmp, 'w') as stream:
        os.chmod(tmp, 0o600)
        stream.write(value+'\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, root/name)


def block(reason, root=ROOT):
    write_state('blocked', reason, root)
    (Path(root)/'ready').unlink(missing_ok=True)


def publish_ready(config, root=ROOT):
    deadline = time.monotonic() + 2
    while True:
        rows, unknown = server_processes()
        private = [row for row in rows if row['exe']==config['backend'] and row['socket']=='localfilesystem:'+config['socket']]
        if unknown or len(private) != len(rows):
            raise PolicyError('unmanaged/unknown server during readiness')
        if len(private) == 1:
            break
        if time.monotonic() >= deadline:
            raise PolicyError('cannot identify one managed server for readiness: ' + json.dumps(rows, sort_keys=True))
        time.sleep(.05)
    pid = private[0]['pid']
    check_runtime_maps(pid, config)
    start = (Path('/proc')/str(pid)/'stat').read_text().rsplit(')',1)[1].split()[19]
    write_state('ready', str(pid)+' '+start, root)


@contextmanager
def startup_lock(root=ROOT, shared=False, timeout=30):
    # 普通调用共享锁覆盖生命周期；改 server/清单/回滚必须独占，不能让长会话阻塞其它客户端。
    fd = os.open(Path(root) / 'start.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise PolicyError('startup lock owner/type/mode mismatch')
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise PolicyBusy('private server busy; command not submitted')
                time.sleep(.02)
        yield fd
    finally:
        os.close(fd)


def require_closed_default():
    if any(probe(f, h, 5037) != 'closed' for f, h in
           [(socket.AF_INET, '127.0.0.1'), (socket.AF_INET6, '::1')]):
        raise PolicyError('ADB_CONFLICT: default TCP port is listening or unknown')


def ensure_server_locked(config, root=ROOT):
    require_closed_default()
    command, env = prepare(['start-server'], config, root)
    subprocess.run(command, stdin=subprocess.DEVNULL, timeout=4, check=True, capture_output=True, env=env)
    publish_ready(config, root)


def ensure_server(config, root=ROOT):
    with startup_lock(root):
        ensure_server_locked(config, root)


def server_ready(config):
    rows, unknown = server_processes()
    expected = 'localfilesystem:' + config['socket']
    if unknown or any(p['exe'] != config['backend'] or p['socket'] != expected for p in rows) or len(rows) > 1:
        raise PolicyError('unmanaged/unknown ADB server')
    if not rows:
        return False
    check_runtime_maps(rows[0]['pid'], config)
    socket_info(config['socket'])
    host_version(config['socket'])
    return True


def command_parts(args):
    i = 0
    while i < len(args):
        if args[i] in ['-s', '-t', '--one-device']:
            i += 2
        elif args[i] in ['-d', '-e', '--exit-on-write-error'] or args[i].startswith('-s'):
            i += 1
        else:
            break
    return args[:i], args[i:]


def device_command(args):
    prefix, tail = command_parts(args)
    # 这些是 server 的协议命令，没有设备 OPEN；设备命令必须绑定本次已验证 transport。
    if not prefix and (tail[:1] in [[x] for x in ['devices', 'connect', 'disconnect', 'pair', 'mdns',
                                                       'version', 'help', 'start-server', 'kill-server', 'server', 'nodaemon']] or
                       tail == ['forward', '--list']):
        return False
    return True


def bind_command(args, config):
    if not device_command(args):
        return args
    prefix, tail = command_parts(args)
    path = Path(config['target_file']).with_name('adb-transport.json')
    adb = [config['backend'], '-L', 'localfilesystem:' + config['socket']]
    try:
        data = adb_identity.read_binding(path)
        tid = adb_identity.verified_id(adb, path)
    except (OSError, ValueError, KeyError, adb_identity.IdentityError, subprocess.SubprocessError) as exc:
        raise PolicyBusy('ADB transport 未核验，命令未提交：' + str(exc)) from None
    selectors = [['-t', str(tid)]]
    for serial in {data['serial'], data.get('target', data['serial'])}:
        selectors.extend([['-s', serial], ['-s' + serial]])
    if prefix and prefix not in selectors:
        raise PolicyBusy('ADB selector 与已验证 transport 不符，命令未提交')
    return ['-t', str(tid), *tail]


def digest(path):
    result = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def private_dir(path):
    path = Path(path)
    for parent in [path, *path.parents]:
        if parent.is_symlink():
            raise PolicyError('symlink in private directory')
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        raise PolicyError('private directory mode must be 0700')
    if info.st_uid != path.parent.stat().st_uid:
        raise PolicyError('private directory owner mismatch')


def validate(args, sock):
    """Parse only the global prefix; shell/push payload is not host syntax."""
    args = list(args)
    prefix = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith(('-H', '-P', '-a')):
            raise PolicyError('server host/port/all-interface overrides are disabled')
        if arg == '-L':
            if i + 1 >= len(args) or args[i + 1] != 'localfilesystem:' + sock:
                raise PolicyError('only the managed filesystem socket is supported')
            i += 2
            continue
        if arg.startswith('-L') or arg in ['fork-server', '--reply-fd']:
            raise PolicyError('unsupported internal or endpoint option')
        if arg in ['-s', '-t', '--one-device']:
            if i + 1 >= len(args) or not args[i + 1] or args[i + 1].startswith('-'):
                raise PolicyError('missing global option value')
            prefix += args[i:i + 2]
            i += 2
            continue
        if arg in ['server', 'nodaemon', '-d', '-e', '--exit-on-write-error'] or (arg.startswith('-s') and len(arg) > 2):
            prefix.append(arg)
            i += 1
            continue
        if arg.startswith('-'):
            raise PolicyError('unknown global option')
        break
    tail = args[i:]
    if tail and tail[0] == 'raw':
        raise PolicyError('raw host-service API is not a managed entry point')
    if tail and tail[0] in ['forward', 'reverse']:
        for endpoint in tail[1:]:
            if endpoint.startswith('localfilesystem:'):
                candidate = os.path.realpath(endpoint.split(':', 1)[1])
                private = os.path.realpath(os.path.dirname(sock))
                aliases = False
                for left, right in [(candidate, sock), (candidate, private), (os.path.dirname(candidate), private)]:
                    try: aliases = aliases or os.path.samefile(left, right)
                    except OSError: continue
                if aliases or candidate == private or candidate.startswith(private + os.sep):
                    raise PolicyError('private ADB directory cannot be bridged')
    return prefix + tail


def probe(family, host, port):
    try:
        with socket.socket(family, socket.SOCK_STREAM) as conn:
            conn.settimeout(0.8)
            rc = conn.connect_ex((host, port))
        return 'listening' if rc == 0 else 'closed' if rc == errno.ECONNREFUSED else 'unknown'
    except OSError:
        return 'unknown'


def socket_info(path):
    info = Path(path).lstat()
    if not stat.S_ISSOCK(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise PolicyError('socket type/mode must be filesystem/0600')
    if info.st_uid != Path(path).parent.stat().st_uid:
        raise PolicyError('socket owner mismatch')


def host_version(path):
    """Read-only smart-socket query, bounded even for an unresponsive peer."""
    def exact(conn, length):
        result = b''
        while len(result) < length:
            block = conn.recv(length - len(result))
            if not block:
                raise PolicyError('short smart-socket response')
            result += block
        return result
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(1)
        conn.connect(str(path))
        conn.sendall(b'000chost:version')
        if exact(conn, 4) != b'OKAY':
            raise PolicyError('smart-socket query rejected')
        length = int(exact(conn, 4), 16)
        if length > 64:
            raise PolicyError('unexpected smart-socket response length')
        return exact(conn, length).decode('ascii')


def load(root=ROOT, verify_runtime=True, *, audit=False):
    root = Path(root)
    private_dir(root)
    manifest = (root / 'manifest.json').lstat()
    if not stat.S_ISREG(manifest.st_mode) or stat.S_IMODE(manifest.st_mode) != 0o600 or manifest.st_uid != root.stat().st_uid:
        raise PolicyError('manifest type, owner or mode mismatch')
    config = json.loads((root / 'manifest.json').read_text())
    if verify_runtime:
        check_dependencies(config, full=audit)
    sealed = config.get('file_metadata', {})
    for path, expected in config.get('vendor_hashes', {}).items():
        info = Path(path).lstat()
        if path in sealed and file_metadata(info) != sealed[path]:
            raise PolicyError('ADB_PACKAGE_VENDOR_DRIFT: vendor metadata changed: ' + path)
        if (audit or path not in sealed) and digest(path) != expected:
            raise PolicyError('ADB_PACKAGE_VENDOR_DRIFT: vendor/backend 已漂移；禁止覆盖，请维护态下人工核验包再迁移')
    for path, expected in config['files'].items():
        file = Path(path)
        if file.is_symlink() or not file.is_file():
            raise PolicyError('managed file changed: ' + path)
        info = file.stat()
        if path in sealed and file_metadata(info) != sealed[path]:
            raise PolicyError('managed file metadata changed: ' + path)
        if (audit or path not in sealed) and digest(file) != expected:
            raise PolicyError('managed file changed: ' + path)
        if info.st_uid != root.stat().st_uid or stat.S_IMODE(info.st_mode) != config['modes'][path]:
            raise PolicyError('managed file owner or mode changed: ' + path)
    private_dir(Path(config['socket']).parent)
    if len(os.fsencode(config['socket'])) >= 104:
        raise PolicyError('filesystem socket path too long')
    if os.path.lexists(config['socket']):
        socket_info(config['socket'])
    if verify_runtime and audit:
        check_backend(config)
    return config


REHARDEN = ('软件包升级可能影响了 ADB 后端；请先恢复原依赖包，再运行 install.sh rollback，'
            '然后 pkg upgrade 并运行 install.sh 重新加固（不自动降级为 TCP）')


def check_dependencies(config, *, full=False):
    if 'runtime' in config:
        try:
            check_runtime(config, full=full)
        except OSError as exc:
            raise PolicyError(str(exc)) from exc
        return
    dependencies = config.get('backend_dependencies')
    if not dependencies or not dependencies.get('packages') or not dependencies.get('libraries'):
        raise PolicyError('ADB_BACKEND_DEPENDENCY_UNTRACKED: 缺少依赖基线；请运行 install.sh rollback 后重新加固')
    for name, expected in dependencies['libraries'].items():
        try:
            path = Path(name)
            if str(path.resolve(strict=True)) != expected['resolved'] or digest(path) != expected['sha256']:
                raise ValueError('content or link target changed')
            info = path.stat()
            # proot maps both the Termux owner and getuid to 0. Match the
            # execution identity, as private_dir does, not the native audit UID.
            if ('mode' in expected and stat.S_IMODE(info.st_mode) != expected['mode'] or
                    'uid' in expected and info.st_uid != os.getuid()):
                raise ValueError('owner or mode changed')
        except (OSError, ValueError) as exc:
            raise PolicyError('ADB_BACKEND_DEPENDENCY_DRIFT: ' + name + '；' + REHARDEN) from exc


def check_backend(config):
    try:
        # version never contacts a server; isolate it just like ordinary clients.
        result = client_run([config['backend'], 'version'], stdin=subprocess.DEVNULL,
                            capture_output=True, timeout=4, **({'runtime': config} if 'runtime' in config else {}))
        if result.returncode:
            raise OSError('backend version rc=' + str(result.returncode))
    except (OSError, subprocess.SubprocessError) as exc:
        hint = ('私有 ADB 后端无法加载；请运行 install.sh refresh 重新加固完整私有代，未退回 TCP'
                if 'runtime' in config else REHARDEN)
        raise PolicyError('ADB_BACKEND_LOAD_FAILED: ' + hint) from exc


def prepare(args, config, root=ROOT):
    reason = pause_reason(root)
    if reason:
        raise PolicyError(reason)
    clean = validate(args, config['socket'])
    env = backend_env(backend=config['backend'], runtime=config if 'runtime' in config else None)
    for name in ['ADB_SERVER_SOCKET', 'ANDROID_ADB_SERVER_ADDRESS', 'ANDROID_ADB_SERVER_PORT', 'DSB_ADB_RECEIPT_NONCE']:
        env.pop(name, None)
    env['ADB_SERVER_SOCKET'] = 'localfilesystem:' + config['socket']
    # ADB 35.0.2 client/main.cpp：在线程创建前关闭本地模拟器扫描，避免本机 adbd 被注册为别名。
    env['ADB_EMU'] = '0'
    # Servers inherit this umask, including backend fork/exec children.
    os.umask(0o177)
    return [config['backend'], '-L', env['ADB_SERVER_SOCKET'], *clean], env


def server_processes(proc_root='/proc'):
    rows = []
    unknown = []
    real_uid = next(line.split()[1] for line in (Path(proc_root) / 'self/status').read_text().splitlines()
                    if line.startswith('Uid:'))
    proc_uid = (Path(proc_root) / 'self').stat().st_uid
    for proc in Path(proc_root).glob('[0-9]*'):
        try:
            # Android 隐藏其它 UID 的 status；先按内核 proc 属主排除，不能吞掉本 UID 的读取失败。
            if proc.stat().st_uid != proc_uid:
                continue
            uid = next(line.split()[1] for line in (proc / 'status').read_text().splitlines()
                       if line.startswith('Uid:'))
            if uid != real_uid:
                continue
            data = (proc / 'cmdline').read_bytes()
            args = [x.decode(errors='replace') for x in data.split(b'\0') if x]
            if not args:
                continue
            # 按 server 全局参数语法识别，不依赖可任意改名的 argv[0]。
            # server 只能是命令位置，不能是 -s/--one-device 等选项的值。
            spec = None
            n = 1
            markers = []
            while n < len(args):
                arg = args[n]
                if arg == '-L':
                    if n + 1 >= len(args): break
                    spec = args[n + 1]
                    n += 2
                elif arg in ['-s', '-t', '-H', '-P', '--one-device', '--reply-fd']:
                    n += 2
                elif arg in ['fork-server', 'server', 'nodaemon']:
                    markers.append(arg)
                    n += 1
                elif arg in ['-d', '-e', '-a', '--exit-on-write-error'] or arg.startswith(('-s', '-L', '-H', '-P')):
                    n += 1
                else:
                    break
            if 'server' not in markers or not any(marker in markers for marker in ['fork-server', 'nodaemon']):
                continue
            rows.append({'pid': int(proc.name), 'socket': spec,
                         'exe': os.readlink(proc / 'exe')})
        except (FileNotFoundError, ProcessLookupError):
            # /proc 枚举后短命进程退出：Linux 既可能报 ENOENT，也可能报 ESRCH。
            continue
        except (OSError, ValueError):
            unknown.append(proc.name)
    return rows, unknown


def tcp_inventory(config, servers):
    """Join authenticated kernel TCP tables to ADB-owned socket inodes."""
    with startup_lock(config.get('deployment_root', Path(config['backend']).parent)):
        servers, unknown = server_processes()
        if unknown:
            raise PolicyError('process inspection incomplete')
        return _tcp_inventory_locked(config, servers)


def _tcp_inventory_locked(config, servers):
    target = Path(config['target_file']).read_text().strip()
    require_closed_default()
    tables = adb_identity.authenticated_shell(
        [config['backend'], '-L', 'localfilesystem:'+config['socket']], target, inventory=True,
        record=Path(config['target_file']).with_name('adb-transport.json'), bound=True)
    headers = 0
    listeners = {}
    for line in tables.splitlines():
        fields = line.split()
        if not fields: continue
        if fields[0] == 'sl':
            headers += 1
            continue
        if len(fields) < 10 or not fields[0].endswith(':'):
            raise PolicyError('unexpected kernel TCP inventory format')
        if fields[3] == '0A':
            listeners[fields[9]] = {'family': headers, 'address': fields[1]}
    if headers != 2:
        raise PolicyError('both IPv4 and IPv6 TCP inventories are required')
    found = []
    for server in servers:
        for fd in (Path('/proc')/str(server['pid'])/'fd').iterdir():
            try: target = os.readlink(fd)
            except FileNotFoundError: continue
            if target.startswith('socket:[') and target.endswith(']'):
                inode = target[8:-1]
                if inode in listeners:
                    found.append({'pid': server['pid'], 'inode': inode, **listeners[inode]})
    return found


def write_audit_receipt(root=ROOT):
    root = Path(root)
    data = json.dumps({'manifest_sha256': digest(root / 'manifest.json'), 'checked_at': time.time()}).encode()
    fd, temporary = tempfile.mkstemp(prefix='.runtime-audit-', dir=root)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / 'runtime-audit.json')
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def require_recent_audit(root=ROOT, *, max_age=300):
    path = Path(root) / 'runtime-audit.json'
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise PolicyError('原生私有运行时审计回执不可信')
    record = json.loads(path.read_text())
    if (record['manifest_sha256'] != digest(Path(root) / 'manifest.json') or
            not 0 <= time.time() - record['checked_at'] <= max_age):
        raise PolicyError('原生私有运行时审计已过期；请运行 install.sh doctor 或等待 guard 复核')


def status(root=ROOT, check_default=True, check_inventory=True, *, audit=False):
    try:
        with startup_lock(root), runtime_verification_scope():
            result = _status_locked(root, check_default, check_inventory, audit=audit)
            if result.pop('local_audit_ok', False) and audit:
                write_audit_receipt(root)
            return result
    except (OSError, PolicyError) as exc:
        return {'ok': False, 'error': str(exc)}


def _status_locked(root=ROOT, check_default=True, check_inventory=True, *, audit=False):
    result = {'ok': False, 'coverage': 'managed-files+same-uid-adb-processes+default-port',
              'all_tcp_inventory': 'requires external USB-shell acceptance'}
    try:
        reason = pause_reason(root)
        if reason: raise PolicyError(reason)
        config = load(root, audit=audit)
        socket_info(config['socket'])
        result['socket_version'] = host_version(config['socket'])
        result['servers'], unknown = server_processes()
        expected = 'localfilesystem:' + config['socket']
        if unknown:
            raise PolicyError('process inspection incomplete')
        if len(result['servers']) != 1 or not any(p['socket'] == expected and p['exe'] == config['backend'] for p in result['servers']):
            raise PolicyError('managed server process not found')
        if any(p['socket'] != expected or p['exe'] != config['backend'] for p in result['servers']):
            raise PolicyError('unmanaged ADB server found')
        check_runtime_maps(result['servers'][0]['pid'], config)
        if 'runtime' in config:
            result['private_runtime'] = {'generation': Path(config['runtime']['directory']).name,
                                        'bytes': config['runtime']['size_bytes'],
                                        'security_updates': '私有副本不随 pkg upgrade 更新；运行 install.sh refresh 刷新整代'}
        if check_default:
            result['tcp5037'] = {host: probe(family, host, 5037) for family, host in
                                 [(socket.AF_INET, '127.0.0.1'), (socket.AF_INET6, '::1')]}
            if any(v != 'closed' for v in result['tcp5037'].values()):
                raise PolicyError('default TCP port is listening or unknown')
        result['local_audit_ok'] = True
        if check_inventory:
            result['adb_owned_tcp_listeners'] = _tcp_inventory_locked(config, result['servers'])
            result['all_tcp_inventory'] = 'authenticated IPv4+IPv6 tables joined to ADB socket inodes'
            result['coverage'] += '+authenticated-all-port-socket-inode-join'
            if result['adb_owned_tcp_listeners']:
                raise PolicyError('ADB process owns a TCP listener (forwarding needs separate approval)')
        result['ok'] = True
    except (OSError, ValueError, KeyError, PolicyError, adb_identity.IdentityError, subprocess.TimeoutExpired) as exc:
        result['error'] = str(exc)
    return result


def start(args, root=ROOT):
    reason = pause_reason(root)
    if reason: raise PolicyError(reason)
    try:
        config = load(root)
        _, tail = command_parts(validate(args, config['socket']))
        if tail[:1] in [['start-server'], ['kill-server'], ['server'], ['nodaemon']]:
            with startup_lock(root), runtime_verification_scope():
                config = load(root)
                command, env = prepare(args, config, root)
                require_closed_default()
                result = subprocess.run(command, env=env, close_fds=True).returncode
                if not result and tail == ['start-server']:
                    publish_ready(config, root)
                return result
        while True:
            with startup_lock(root, shared=True) as lock_fd, runtime_verification_scope():
                config = load(root)
                command, env = prepare(args, config, root)
                require_closed_default()
                if tail[:1] in [['version'], ['help']] or server_ready(config):
                    parsed = validate(args, config['socket'])
                    lease = adb_identity.binding_lease(Path(config['target_file']).with_name('adb-transport.json')) if device_command(parsed) else nullcontext()
                    try:
                        with lease:
                            clean = bind_command(parsed, config)
                            command, env = prepare(clean, config, root)
                            # client 自持 SH 到退出；内核过滤器禁止它派生 server 继承此锁。
                            return client_run(command, env=env, close_fds=True,
                                              pass_fds=(lock_fd,) if lock_fd is not None else ()).returncode
                    except (OSError, ValueError, KeyError, adb_identity.IdentityError) as exc:
                        raise PolicyBusy('ADB transport 未核验，命令未提交：' + str(exc)) from None
            # 不能把共享锁原地升级：释放后独占，再双检；其它客户端不会被串行化。
            with startup_lock(root), runtime_verification_scope():
                config = load(root)
                prepare(args, config, root)
                if not server_ready(config):
                    ensure_server_locked(config, root)
    except PolicyBusy:
        raise
    except (OSError, ValueError, KeyError, PolicyError, subprocess.SubprocessError) as exc:
        block('startup integrity/transport check failed: '+str(exc), root)
        raise PolicyError(pause_reason(root)) from None


def main():
    receipt = os.environ.pop('DSB_ADB_RECEIPT_NONCE', '')
    try:
        if sys.argv[1:2] == ['--policy-check']:
            report = status(audit=True)
            print(json.dumps(report, sort_keys=True))
            return 0 if report['ok'] else 2
        if sys.argv[1:2] == ['--policy-check-fast']:
            require_recent_audit()
            report = status()
            print(json.dumps(report, sort_keys=True))
            return 0 if report['ok'] else 2
        args = sys.argv[2:] if sys.argv[1:2]==['--policy-start'] else sys.argv[1:]
        return start(args)
    except (OSError, ValueError, KeyError, TypeError, PolicyError) as exc:
        print(str(exc) if str(exc).startswith('ADB_POLICY_PAUSED:') else 'ADB_POLICY_DENIED: '+str(exc), file=sys.stderr)
        if len(receipt) == 64 and all(c in '0123456789abcdef' for c in receipt):
            print('ADB_POLICY_RECEIPT:' + receipt, file=sys.stderr)
        return 78


if __name__ == '__main__':
    sys.exit(main())
