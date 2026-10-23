#!/usr/bin/env python3
"""Termux 原生侧的 ADB 私有传输部署。预检跳过不改入口，切换失败自动回滚。"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time

STAGE = Path(__file__).resolve().parent
sys.path.insert(0, str(STAGE))
import build_inputs
sys.path.insert(0, str(STAGE / 'payload'))
import adb_policy as policy
import adb_identity
import bootstrap

P = Path(os.environ.get('PREFIX', Path(sys.executable).parent.parent))
H = Path.home()
ROOT = P / 'var/lib/dsb-adb'
SOCKDIR = P / 'var/run/dsb-adb'
SOCK = SOCKDIR / 'server.sock'
STATE = P / 'var/lib/dsb-adb-install.json'
NATIVE = P / 'bin/adb'
SUP = H / 'wx/supervisor.sh'
TARGET = H / 'dsb/adb-target'
RF = None
SCHEMA = 'dsb-adb-private/v1'
STATES = {'prepared', 'entrypoints-installed', 'applied', 'rollback-restoring',
          'rollback-tcp', 'rollback-failed', 'rolled-back'}


class DeployError(RuntimeError):
    pass


def run(args, timeout=30):
    try:
        r = subprocess.run([str(x) for x in args], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise DeployError(f'命令不可用或超时：{Path(args[0]).name}') from e
    if r.returncode:
        # 不把任意子进程输出带进安装日志。
        raise DeployError(f'命令失败：{Path(args[0]).name} rc={r.returncode}')
    return r.stdout.strip()


def container(args):
    return run([P / 'bin/proot-distro', 'login', 'ubuntu', '--', *args], timeout=60)


def connect_ready(adb, target):
    run([*adb, 'connect', target])
    # connect 回执可能先于 transport 注册；有界等待后仍须另做身份校验。
    run([*adb, '-s', target, 'wait-for-device'], timeout=15)


def rootfs():
    candidates = [P / 'var/lib/proot-distro' / x for x in
                  ['containers/ubuntu/rootfs', 'installed-rootfs/ubuntu']]
    found = [p for p in candidates if (p / 'etc/os-release').is_file()]
    if len(found) != 1:
        raise DeployError('Ubuntu rootfs 不存在或布局不唯一')
    return found[0]


def require_native():
    if (sys.platform not in ['linux', 'android'] or os.getuid() == 0 or
            not str(sys.executable).startswith(str(P / 'bin') + '/') or
            H.stat().st_uid != os.getuid() or not Path('/system/bin/getprop').is_file()):
        raise DeployError('必须在原生 Termux 用户下运行，不能在 proot/root 中运行')
    if P != Path('/data/data/com.termux/files/usr') or H != P.parent / 'home':
        raise DeployError('只支持官方 Termux 私有目录')
    if any(line.startswith('TracerPid:') and line.split()[1] != '0'
           for line in Path('/proc/self/status').read_text().splitlines()):
        raise DeployError('检测到 tracer/proot，请退出容器后重试')


def reject_legacy():
    """在锁、状态、维护闸以及任何部署写入之前执行。未知格式也不猜测恢复。"""
    if not ROOT.exists() and not ROOT.is_symlink():
        return
    if ROOT.is_symlink() or not (ROOT / 'journal.json').is_file():
        # 只有 manifest 的旧部署仍先给出明确旧版提示。
        manifest = ROOT / 'manifest.json'
        if manifest.is_file() and json.loads(manifest.read_text()).get('schema') != SCHEMA:
            raise DeployError('旧版部署，需 owner 在场迁移；公开工具拒绝所有写操作')
        raise DeployError('部署缺少可信 journal，拒绝所有写操作')
    for name in ['journal.json', 'manifest.json']:
        path = ROOT / name
        if path.is_symlink():
            raise DeployError('部署元数据不可识别，拒绝写操作')
        if path.exists():
            data = json.loads(path.read_text())
            legacy = (data.get('schema') != SCHEMA or data.get('tool_version') != 1 or
                      data.get('state') == 'transport-applied-not-owner-accepted' or
                      any(str(row.get('destination', '')).endswith('.r60-vendor')
                          for row in data.get('diversions', [])) or
                      (name == 'manifest.json' and not {'target_file', 'container_paths'} <= data.keys()))
            if legacy:
                raise DeployError('旧版部署，需 owner 在场迁移；公开工具拒绝所有写操作')
            if name == 'journal.json' and data.get('state') not in STATES:
                raise DeployError('未知事务状态，拒绝自动恢复或写入')


def require_applied():
    reject_legacy()
    if Transaction(ROOT).data['state'] != 'applied':
        raise DeployError('仅已完成加固的 applied 状态允许 maintenance/resume')


def trusted_source():
    source = STAGE.resolve()
    if H not in source.parents:
        raise DeployError('安装源必须位于 Termux HOME 私有目录，不能使用共享存储')
    paths = [source, *list(source.parents)[:list(source.parents).index(H) + 1],
             *[p for p in source.rglob('*') if '__pycache__' not in p.parts]]
    for path in paths:
        info = path.lstat()
        if path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise DeployError('安装源含符号链接、非本人属主或组/其它用户可写项')


def atomic(path, data, mode=0o600):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix='.dsb-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def fingerprint(path):
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {'kind': 'absent'}
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode):
        return {'kind': 'symlink', 'target': os.readlink(path), 'mode': mode}
    if not stat.S_ISREG(info.st_mode):
        raise DeployError('非普通文件：' + str(path))
    return {'kind': 'file', 'sha256': policy.digest(path), 'mode': mode}


class Transaction:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.file = self.directory / 'journal.json'
        self.data = json.loads(self.file.read_text()) if self.file.exists() else {
            'schema': SCHEMA, 'tool_version': 1, 'state': 'prepared', 'files': {}, 'diversions': []}

    def save(self):
        atomic(self.file, (json.dumps(self.data, indent=2) + '\n').encode())

    def event(self, name):
        self.data['state'] = name
        self.save()

    def remember(self, path):
        name = str(path)
        if name in self.data['files']:
            return
        original = fingerprint(path)
        backup = self.directory / ('backup-' + str(len(self.data['files'])))
        if original['kind'] != 'absent':
            shutil.copy2(path, backup, follow_symlinks=False)
            if original['kind'] == 'file':
                with backup.open('rb') as stream:
                    os.fsync(stream.fileno())
        self.data['files'][name] = {'original': original, 'backup': str(backup), 'installed': None}
        self.save()

    def write(self, path, data, mode):
        self.remember(path)
        self.data['files'][str(path)]['installed'] = {
            'kind': 'file', 'sha256': hashlib.sha256(data).hexdigest(), 'mode': mode}
        self.save()
        atomic(path, data, mode)

    def check_restore(self, allow_absent=()):
        for name, row in self.data['files'].items():
            allowed = [row['original'], row['installed']]
            if name in allow_absent:
                allowed.append({'kind': 'absent'})
            if fingerprint(name) not in allowed:
                raise DeployError('回滚冲突，未覆盖后续改动：' + name)
            if row['original']['kind'] != 'absent' and fingerprint(row['backup']) != row['original']:
                raise DeployError('回滚备份被修改：' + name)

    def restore_files(self):
        self.check_restore()
        for name, row in reversed(list(self.data['files'].items())):
            path, old = Path(name), row['original']
            if fingerprint(path) == old:
                continue
            if old['kind'] == 'absent':
                path.unlink()
            elif old['kind'] == 'symlink':
                temp = path.with_name(path.name + '.dsb-link-' + str(time.time_ns()))
                temp.symlink_to(old['target'])
                os.replace(temp, path)
            else:
                atomic(path, Path(row['backup']).read_bytes(), old['mode'])
            if fingerprint(path) != old:
                raise DeployError('回滚文件校验失败：' + name)


def record_state(status, reason=''):
    atomic(STATE, json.dumps({'status': status, 'reason': reason, 'updated_at': time.time()},
                            ensure_ascii=False).encode())


def proc_identity(pid):
    proc = Path('/proc') / str(pid)
    try:
        fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] in ['Z', 'X']:
            return None
        return {'pid': int(pid), 'start': fields[19], 'exe': os.readlink(proc / 'exe')}
    except FileNotFoundError:
        return None


def stop(identity, timeout=40):
    if identity is None:
        return
    if proc_identity(identity['pid']) != identity:
        raise DeployError('进程身份已改变，不发送信号')
    os.kill(identity['pid'], signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc_identity(identity['pid']) != identity:
            return
        time.sleep(0.1)
    raise DeployError('进程未在期限内退出')


def supervisors():
    rows = []
    for p in Path('/proc').glob('[0-9]*'):
        try:
            args = (p / 'cmdline').read_bytes().split(b'\0')
            exact = args[:2] == [str(P / 'bin/bash').encode(), str(SUP).encode()]
            held = False
            try:
                held = (os.readlink(p / 'fd/9') == str(H / 'wx/supervisor.lock') and
                        (H / 'wx/supervisor.pid').read_text().strip() == p.name)
            except OSError:
                pass
            if exact or held:
                identity = proc_identity(int(p.name))
                if identity:
                    rows.append(identity)
        except (OSError, ValueError):
            continue
    return rows


def stop_original_server(original):
    # dpkg-divert 已改了磁盘文件名，/proc/exe 随之变化；PID/start/hash 仍须完全一致。
    expected = original['server']
    current = proc_identity(expected['pid'])
    if current is None:
        rows, unknown = policy.server_processes()
        if not rows and not unknown:
            return
        raise DeployError('原 server 已退出，但出现了其它/未知 ADB 进程')
    if (current['start'] != expected['start'] or
            policy.digest(f"/proc/{expected['pid']}/exe") != original['server_sha256']):
        raise DeployError('原 ADB server 身份或内容已变化，不发送信号')
    stop(current)


def wait_tcp_closed(timeout=3):
    deadline = time.monotonic() + timeout
    while True:
        probes = {h: policy.probe(f, h, 5037) for f, h in
                  [(policy.socket.AF_INET, '127.0.0.1'), (policy.socket.AF_INET6, '::1')]}
        if all(value == 'closed' for value in probes.values()):
            return
        if time.monotonic() >= deadline:
            rows, unknown = policy.server_processes()
            raise DeployError('旧 TCP 监听没有关闭：' + json.dumps({'probes': probes, 'servers': rows, 'unknown': unknown}))
        time.sleep(0.1)


def start_supervisor():
    if supervisors():
        return
    env = dict(os.environ)
    for name in ['ADB_SERVER_SOCKET', 'ANDROID_ADB_SERVER_ADDRESS', 'ANDROID_ADB_SERVER_PORT']:
        env.pop(name, None)
    with (H / 'wx/supervisor.log').open('ab') as log:
        subprocess.Popen([str(P / 'bin/bash'), str(SUP)], stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, start_new_session=True, close_fds=True, env=env)


def verify_supervisor(since):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        current = supervisors()
        if len(current) == 1:
            pid = current[0]['pid']
            try:
                report = json.loads((ROOT / 'health.json').read_text())
                if (report['ok'] is True and report['pid'] == pid and report['checked_at'] >= since and
                        (H / 'wx/supervisor.pid').read_text().strip() == str(pid) and
                        os.readlink(Path('/proc') / str(pid) / 'fd/9') == str(H / 'wx/supervisor.lock')):
                    return
            except (OSError, ValueError, KeyError):
                pass
        time.sleep(0.3)
    raise DeployError('supervisor 没有返回新的私有传输运行回执')


def packages():
    return {'termux': run([P / 'bin/dpkg-query', '-W', 'android-tools']),
            'ubuntu': container(['dpkg-query', '-W', 'adb'])}


def package_holds():
    return {'termux': 'android-tools' in run([P / 'bin/apt-mark', 'showhold']).splitlines(),
            'ubuntu': 'adb' in container(['apt-mark', 'showhold']).splitlines()}


def set_holds(values):
    run([P / 'bin/apt-mark', 'hold' if values['termux'] else 'unhold', 'android-tools'])
    container(['apt-mark', 'hold' if values['ubuntu'] else 'unhold', 'adb'])


def divert(row, operation):
    args = ['dpkg-divert', '--local', '--divert', row['destination'], '--rename', operation, row['path']]
    return container(args) if row['side'] == 'ubuntu' else run([P / 'bin' / args[0], *args[1:]])


def truename(row):
    args = ['dpkg-divert', '--truename', row['path']]
    return container(args) if row['side'] == 'ubuntu' else run([P / 'bin' / args[0], *args[1:]])


def hostpath(row):
    return Path(row['path']) if row['side'] == 'termux' else RF / row['path'].lstrip('/')


def compile_launcher():
    try:
        inputs = build_inputs.prepare(H / '.cache/dsb-adb-build')
    except build_inputs.BuildError as exc:
        raise DeployError(str(exc)) from exc
    with tempfile.TemporaryDirectory(prefix='dsb-build-', dir=P / 'tmp') as d:
        # -B 只指向本次创建的目录，缓存中的 ld/as 绝不能被 clang 执行。
        verified = Path(d) / 'inputs'
        verified.mkdir(mode=0o700)
        for name in [*build_inputs.PINS[build_inputs.platform.machine()][1], 'NOTICE']:
            shutil.copyfile(inputs / name, verified / name)
            expected = (build_inputs.NOTICE_SHA256 if name == 'NOTICE' else
                        build_inputs.SHA256[build_inputs.platform.machine()][name])
            if policy.digest(verified / name) != expected:
                raise DeployError('编译输入复制后 SHA-256 不匹配')
        binary = Path(d) / 'adb-launcher'
        defines = {'ROOT_PATH': str(ROOT), 'SOCKET_PATH': str(SOCK), 'PYTHON': str(P / 'bin/python3')}
        try:
            run([P / 'bin/clang', '-O2', '-static', '-B' + str(verified) + '/', '-L' + str(verified),
                 '-Wall', '-Wextra', '-Werror',
                 *['-D' + key + '=' + json.dumps(value) for key, value in defines.items()],
                 STAGE / 'payload/adb_launcher.c', '-o', binary], timeout=120)
        except DeployError as exc:
            raise DeployError('LAUNCHER_COMPILE_FAILED：clang 静态编译/-Werror 未通过，未改入口') from exc
        run([binary, '--policy-validate', 'devices'])
        data = binary.read_bytes()
        if data[:6] != b'\x7fELF\x02\x01' or struct.unpack_from('<H', data, 18)[0] != {'aarch64': 183, 'x86_64': 62}[build_inputs.platform.machine()]:
            raise DeployError('launcher ELF 架构不匹配')
        phoff = struct.unpack_from('<Q', data, 32)[0]
        size, count = struct.unpack_from('<HH', data, 54)
        if any(struct.unpack_from('<I', data, phoff + i * size)[0] == 3 for i in range(count)):
            raise DeployError('launcher 必须为无 PT_INTERP 的静态 ELF')
        return data


def preflight():
    global RF
    require_native()
    trusted_source()
    for name in ['adb_policy.py', 'adb_guard.py', 'adb_identity.py', 'adb_client.py', 'adb_relay.py']:
        source = (STAGE / 'payload' / name).read_bytes()
        if not source:
            raise DeployError('安装材料不完整：' + name)
        compile(source, name, 'exec')
    RF = rootfs()
    if ROOT.exists() or SOCKDIR.exists():
        raise DeployError('已有部署/备份/套接字目录，请先 check 或 rollback')
    for name in ['clang', 'python3', 'bash', 'flock', 'timeout', 'dpkg-divert', 'proot-distro']:
        if not (P / 'bin' / name).is_file():
            raise DeployError('缺少原生工具：' + name)
    if shutil.disk_usage(P).free < 64 * 1024 * 1024 or len(os.fsencode(SOCK)) >= 104:
        raise DeployError('空间不足或私有 socket 路径过长')
    if not SUP.is_file() or NATIVE.is_symlink() or not NATIVE.is_file():
        raise DeployError('缺少 supervisor 或 Termux ADB 不是普通文件')
    run([P / 'bin/bash', '-n', STAGE / 'supervisor.sh'])
    target = TARGET.read_text().strip()
    if any(policy.probe(f, h, 5037) != 'closed' for f, h in
           [(policy.socket.AF_INET, '127.0.0.1'), (policy.socket.AF_INET6, '::1')]):
        raise bootstrap.Conflict('ADB_CONFLICT：预检发现 5037 监听/未知，禁止继续')
    canonical = container(['readlink', '-f', '/usr/bin/adb'])
    if not canonical.startswith('/usr/') or '..' in Path(canonical).parts:
        raise DeployError('不支持的 Ubuntu ADB 布局')
    extra = set(container(['bash', '-c', 'type -a -p adb']).splitlines()) - {'/usr/bin/adb', '/bin/adb', canonical, str(NATIVE)}
    if extra:
        print('ADB_PREFLIGHT_WARNING：容器 PATH 中有其它 adb，请核对：' + ', '.join(sorted(extra)))
    if os.path.lexists(RF / 'usr/local/bin/adb'):
        raise DeployError('已有自定义 /usr/local/bin/adb，不能自动接管')
    rows = [{'side': side, 'path': path, 'destination': path + '.dsb-vendor'} for side, path in
            [('termux', str(NATIVE)), *[('ubuntu', x) for x in sorted({'/usr/bin/adb', canonical})]]]
    for row in rows:
        if truename(row) != row['path'] or os.path.lexists(str(hostpath(row)) + '.dsb-vendor'):
            raise DeployError('已有 dpkg diversion，不能接管')
    servers, unknown = policy.server_processes()
    expected = 'localfilesystem:' + str(P / 'var/run/dsb-adb-bootstrap/server.sock')
    if unknown or len(servers) != 1 or servers[0]['exe'] != str(NATIVE) or servers[0]['socket'] != expected:
        raise DeployError('当前 ADB 进程/端点不能完整识别')
    server_hash = policy.digest(f"/proc/{servers[0]['pid']}/exe")
    if server_hash != policy.digest(NATIVE):
        raise DeployError('磁盘 ADB 与运行中 server 不一致')
    adb = [NATIVE, '-L', expected]
    # 确认普通 client 的内核防自启过滤器可用，否则在切换前跳过。
    policy.client_run([str(NATIVE), 'version'], capture_output=True, timeout=3, check=True)
    record = TARGET.with_name('adb-transport.json')
    adb_identity.authenticated_shell(adb, target, record=record, bound=True)
    tid = adb_identity.verified_id(adb, record)
    if run([*adb, 'forward', '--list']) or run([*adb, '-t', str(tid), 'reverse', '--list']):
        raise DeployError('存在端口转发，需要先由用户处理')
    if len(supervisors()) > 1:
        raise DeployError('多个 supervisor 实例')
    return {'packages': packages(), 'holds': package_holds(), 'diversions': rows, 'server': proc_identity(servers[0]['pid']),
            'container_paths': sorted({row['path'] for row in rows if row['side'] == 'ubuntu'}),
            'server_sha256': server_hash, 'launcher': compile_launcher(), 'target': target, 'rootfs': str(RF)}


def verify_private():
    report = policy.status(ROOT)
    if not report['ok']:
        raise DeployError('私有传输检查失败：' + str(report.get('error')))
    config = policy.load(ROOT)
    if container(['sh', '-c', 'command -v adb']) != '/usr/local/bin/adb':
        raise DeployError('容器默认 adb 没有命中受管入口 /usr/local/bin/adb')
    target = TARGET.read_text().strip()
    uids = {'termux': run([NATIVE, '-s', target, 'shell', 'id', '-u'])}
    for path in config['container_paths']:
        uids[path] = container([path, '-s', target, 'shell', 'id', '-u'])
    if set(uids.values()) != {'2000'}:
        raise DeployError('入口 shell UID 不一致')
    report['entry_uids'] = uids
    return report


def cutover(original):
    # 先准备完整日志再原子发布，避免断电留下无法恢复的空部署目录。
    staging = Path(tempfile.mkdtemp(prefix='dsb-adb-stage-', dir=ROOT.parent))
    txn = Transaction(staging)
    txn.data['baseline'] = {k: v for k, v in original.items() if k != 'launcher'}
    txn.save()
    staging.rename(ROOT)
    txn = Transaction(ROOT)
    atomic(ROOT / 'maintenance', b'cutover\n')
    set_holds({'termux': True, 'ubuntu': True})
    SOCKDIR.mkdir(mode=0o700)
    for row in original['diversions']:
        txn.remember(hostpath(row))
    txn.remember(RF / 'usr/local/bin/adb')
    atomic(ROOT / 'adb.backend', NATIVE.read_bytes(), 0o700)
    for name in ['adb_policy.py', 'adb_guard.py', 'adb_identity.py', 'adb_client.py', 'adb_relay.py']:
        atomic(ROOT / name, (STAGE / 'payload' / name).read_bytes())
    for row in original['diversions']:
        txn.data['diversions'].append(row)
        txn.save()
        divert(row, '--add')
    paths = [hostpath(row) for row in original['diversions']] + [RF / 'usr/local/bin/adb']
    for path in paths:
        txn.write(path, original['launcher'], 0o700)
    # supervisor 不进入关键文件哈希锁集，正常脚本更新不应锁死 ADB。
    supervisor_data = (STAGE / 'supervisor.sh').read_bytes()
    if SUP.read_bytes() != supervisor_data or stat.S_IMODE(SUP.stat().st_mode) != 0o700:
        txn.write(SUP, supervisor_data, 0o700)
    files = {str(p): 0o700 for p in [ROOT / 'adb.backend', *paths]}
    files.update({str(ROOT / n): 0o600 for n in ['adb_policy.py', 'adb_guard.py', 'adb_identity.py', 'adb_client.py', 'adb_relay.py']})
    config = {'schema': SCHEMA, 'tool_version': 1, 'backend': str(ROOT / 'adb.backend'), 'socket': str(SOCK), 'target_file': str(TARGET),
              'vendor_hashes': {str(Path(row['destination']) if row['side'] == 'termux' else RF / row['destination'].lstrip('/')):
                                policy.digest(Path(row['destination']) if row['side'] == 'termux' else RF / row['destination'].lstrip('/'))
                                for row in original['diversions']
                                if not (Path(row['destination']) if row['side'] == 'termux' else RF / row['destination'].lstrip('/')).is_symlink()},
              'container_paths': sorted({row['path'] for row in original['diversions'] if row['side'] == 'ubuntu'} |
                                        {'/usr/local/bin/adb'}),
              'files': {p: policy.digest(p) for p in files}, 'modes': files}
    atomic(ROOT / 'manifest.json', json.dumps(config, indent=2).encode())
    txn.event('entrypoints-installed')
    since = time.time()
    try:
        for identity in supervisors():
            stop(identity)
    finally:
        start_supervisor()  # 维护态只暂停 ADB，sshd/dsh/bridge 继续。
    stop_original_server(original)
    wait_tcp_closed()
    (ROOT / 'maintenance').unlink()
    run([NATIVE, 'start-server'])
    raw = [ROOT / 'adb.backend', '-L', 'localfilesystem:' + str(SOCK)]
    adb_identity.connect_verified(raw, original['target'], TARGET.with_name('adb-transport.json'),
                                  ROOT / 'adb_relay.py', root=ROOT)
    report = verify_private()
    verify_supervisor(since)
    txn.data['report'] = report
    txn.event('applied')
    record_state('applied')
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


def rollback():
    require_native()
    reject_legacy()
    try:
        with policy.startup_lock(ROOT):
            _rollback()
    except BaseException:
        if (ROOT / 'journal.json').is_file():
            txn = Transaction(ROOT)
            txn.data['rollback_phase'] = txn.data['state']
            txn.event('rollback-failed')
            record_state('rollback-failed', '回滚未完成，阶段=' + txn.data['rollback_phase'])
        raise
    finally:
        if SUP.is_file():
            start_supervisor()


def _rollback():
    global RF
    require_native()
    reject_legacy()
    txn = Transaction(ROOT)
    if 'baseline' not in txn.data:
        raise DeployError('没有完整的回滚日志')
    RF = Path(txn.data['baseline']['rootfs'])
    if packages() != txn.data['baseline']['packages']:
        raise DeployError('ADB 包版本已改变，拒绝覆盖新版本；需人工恢复')
    missing = [str(hostpath(row)) for row in txn.data['diversions'] if truename(row) == row['destination']]
    txn.check_restore(missing)
    for row in txn.data['diversions']:
        if truename(row) == row['destination']:
            vendor = Path(row['destination']) if row['side'] == 'termux' else RF / row['destination'].lstrip('/')
            if fingerprint(vendor) != txn.data['files'][str(hostpath(row))]['original']:
                raise DeployError('vendor 备份发生后续修改，拒绝回滚覆盖：' + str(vendor))
    txn.event('rollback-restoring')
    atomic(ROOT / 'maintenance', b'rollback\n')
    for identity in supervisors():
        stop(identity)
    rows, unknown = policy.server_processes()
    if unknown:
        raise DeployError('ADB 进程不可完整识别，回滚暂停')
    for row in rows:
        if row['exe'] == str(ROOT / 'adb.backend'):
            if row['socket'] != 'localfilesystem:' + str(SOCK) or policy.digest(f"/proc/{row['pid']}/exe") != txn.data['baseline']['server_sha256']:
                raise DeployError('回滚时 backend 进程内容/端点不符')
            stop(proc_identity(row['pid']))
        elif row['pid'] == txn.data['baseline']['server']['pid']:
            stop_original_server(txn.data['baseline'])
        else:
            bootstrap.tcp_owner(NATIVE)  # 只接受本工具预绑定、持有监听 inode 的恢复 server。
    for row in reversed(txn.data['diversions']):
        actual = truename(row)
        if actual == row['destination']:
            hostpath(row).unlink(missing_ok=True)
            divert(row, '--remove')
        elif actual != row['path']:
            raise DeployError('diversion 回滚冲突')
    txn.restore_files()
    txn.event('rollback-tcp')
    # 恢复入口后先确认没有外来监听，不能把配对或 connect 发给未知 peer。
    bootstrap.tcp_owner(NATIVE, allow_empty=True)
    bootstrap.start_tcp(NATIVE)
    bootstrap.tcp_owner(NATIVE)
    target = TARGET.read_text().strip()
    raw = [NATIVE, '-L', 'tcp:localhost:5037']
    device_ok = False
    try:
        adb_identity.connect_verified(raw, target, TARGET.with_name('adb-transport.json'),
                                      H / 'dsb/adb_relay.py', locked=True)
        device_ok = True
    except (adb_identity.IdentityError, OSError, subprocess.SubprocessError):
        pass
    if device_ok:
        tid = adb_identity.verified_id(raw, TARGET.with_name('adb-transport.json'))
        for path in txn.data['baseline']['container_paths']:
            if container([path, '-L', 'tcp:localhost:5037', '-t', str(tid), 'shell', 'id', '-u']) != '2000':
                raise DeployError('回滚后容器入口 shell UID 不一致')
        if run([*raw, '-t', str(tid), 'reverse', '--list']):
            raise DeployError('回滚后 reverse 非空')
    bootstrap.tcp_owner(NATIVE)
    if run([NATIVE, '-L', 'tcp:localhost:5037', 'forward', '--list']):
        raise DeployError('回滚后 forward 非空')
    set_holds(txn.data['baseline']['holds'])
    txn.event('rolled-back')
    reason = '已恢复普通 ADB TCP 传输；其它服务不变'
    if not device_ok:
        reason += '；设备离线，请打开无线调试并重新连接'
    record_state('rolled-back', reason)
    # 保留备份作审计；移到不被 doctor 识别为已加固的位置。
    archive = ROOT.with_name('dsb-adb-rollback-' + str(time.time_ns()))
    if SOCK.exists():
        SOCK.unlink()
    if SOCKDIR.exists():
        SOCKDIR.rmdir()
    ROOT.rename(archive)
    print('ADB_ROLLBACK_OK（普通 ADB TCP 传输，未加固）' + ('' if device_ok else '：设备离线，需要重连'))


def recover():
    reject_legacy()
    # 仅回收 rename 之前的本版本空 staging；不触碰未知目录或备份。
    for directory in ROOT.parent.glob('dsb-adb-stage-*'):
        if directory.is_symlink() or directory.stat().st_uid != os.getuid():
            raise DeployError('未知 staging 残留，拒绝清理')
        data = json.loads((directory / 'journal.json').read_text())
        if (data.get('schema') != SCHEMA or data.get('tool_version') != 1 or data.get('state') != 'prepared' or
                data.get('files') or data.get('diversions') or
                {p.name for p in directory.iterdir()} != {'journal.json'}):
            raise DeployError('staging 非本版本空准备态，拒绝清理')
        (directory / 'journal.json').unlink()
        directory.rmdir()
    if (ROOT / 'journal.json').exists() and Transaction(ROOT).data['state'] != 'applied':
        rollback()
        print('ADB_INTERRUPTED_TRANSACTION_RECOVERED')


def apply():
    reject_legacy()
    if (ROOT / 'journal.json').exists():
        txn = Transaction(ROOT)
        if txn.data['state'] == 'applied':
            print(json.dumps(verify_private(), ensure_ascii=False, sort_keys=True))
            print('ADB_ALREADY_HARDENED')
            return 0
        rollback()  # 中断后先恢复完整基线，下一次运行可重新尝试。
        return 2
    try:
        original = preflight()
    except (DeployError, OSError, ValueError, adb_identity.IdentityError, policy.PolicyError, bootstrap.Conflict) as exc:
        if isinstance(exc, bootstrap.Conflict):
            record_state('conflict', str(exc))
            raise DeployError(str(exc)) from exc
        record_state('skipped', str(exc))
        print('ADB 未加固（预检跳过）：' + str(exc))
        return 0
    except Exception as exc:
        record_state('preflight-error', '预检内部异常：' + type(exc).__name__)
        raise DeployError('预检异常，禁止把陈旧状态当成成功：' + type(exc).__name__) from exc
    try:
        cutover(original)
        return 0
    except BaseException as exc:
        try:
            rollback()
        except BaseException as recovery:
            record_state('rollback-failed', f'{type(exc).__name__}; {type(recovery).__name__}')
            phase = Transaction(ROOT).data.get('state', 'unknown')
            raise DeployError('自动回滚未完成，阶段=' + phase + '；禁止继续安装/配对，请检查备份后 rollback') from recovery
        detail = str(exc)[:512] if isinstance(exc, DeployError) else type(exc).__name__
        record_state('rolled-back', '切换自检失败，已自动恢复：' + detail)
        print('ADB 自检未通过，已自动回滚；DSB 仍可继续安装：' + detail)
        return 2


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', nargs='?', default='preflight',
                        choices=['preflight', 'apply', 'check', 'rollback', 'recover', 'maintenance', 'resume', 'guard-legacy', 'ordinary'])
    args = parser.parse_args()
    require_native()
    reject_legacy()
    if args.operation == 'guard-legacy':
        return 0
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with (STATE.parent / 'dsb-adb-deploy.lock').open('a') as lock, bootstrap.control_lock(P):
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.operation == 'preflight':
            preflight()
            print('ADB_PREFLIGHT_OK')
        elif args.operation == 'apply':
            return apply()
        elif args.operation == 'rollback':
            rollback()
        elif args.operation == 'recover':
            recover()
        elif args.operation == 'check':
            print(json.dumps(verify_private(), ensure_ascii=False, sort_keys=True))
        elif args.operation == 'maintenance':
            require_applied()
            policy.load(ROOT)
            atomic(ROOT / 'maintenance', b'user maintenance\n')
        elif args.operation == 'resume':
            require_applied()
            policy.load(ROOT)
            (ROOT / 'maintenance').unlink(missing_ok=True)
            run([P / 'bin/python3', '-I', ROOT / 'adb_guard.py'])
        elif args.operation == 'ordinary':
            rows, unknown = policy.server_processes()
            if not unknown and len(rows) == 1 and (rows[0]['socket'] or '').startswith('acceptfd:'):
                bootstrap.tcp_owner(NATIVE)
            else:
                spec = 'localfilesystem:' + str(P / 'var/run/dsb-adb-bootstrap/server.sock')
                if unknown or any(r['exe'] != str(NATIVE) or r['socket'] != spec for r in rows):
                    raise DeployError('bootstrap server 无法完整识别')
                for row in rows:
                    stop(proc_identity(row['pid']))
                bootstrap.tcp_owner(NATIVE, allow_empty=True)
                bootstrap.start_tcp(NATIVE)
                bootstrap.tcp_owner(NATIVE)
            target = TARGET.read_text().strip()
            adb_identity.connect_verified([NATIVE, '-L', 'tcp:localhost:5037'], target,
                                          TARGET.with_name('adb-transport.json'), H / 'dsb/adb_relay.py')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (DeployError, OSError, ValueError, KeyError, bootstrap.Conflict,
            adb_identity.IdentityError, policy.PolicyError, subprocess.SubprocessError) as exc:
        print('\033[31mADB_DEPLOY_ERROR: ' + str(exc) + '\033[0m', file=sys.stderr)
        sys.exit(2)
