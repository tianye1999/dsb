#!/usr/bin/env python3
"""Termux 原生侧的 ADB 私有传输部署。预检跳过不改入口，切换失败自动回滚。"""
import argparse
import fcntl
import functools
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import tarfile
import time

STAGE = Path(__file__).resolve().parent
sys.path.insert(0, str(STAGE))
import build_inputs
sys.path.insert(0, str(STAGE / 'payload'))
import adb_policy as policy
import adb_identity
import adb_guard
from adb_client import capture_system_maps, file_metadata, parent_death_setup
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
TOOL_VERSION = 3
PUBLIC_VERSIONS = [1, 2, TOOL_VERSION]
SYSTEM_DIRS = [Path('/system/lib64'), Path('/system/lib64/bootstrap'),
               Path('/vendor/lib64'), Path('/system_ext/lib64'), Path('/product/lib64'),
               Path('/odm/lib64'), *Path('/apex').glob('*/lib64'),
               *Path('/apex').glob('*/lib64/bionic')]
STATES = {'prepared', 'entrypoints-installed', 'activating', 'applied', 'rollback-restoring',
          'rollback-tcp', 'rollback-failed', 'rolled-back'}


class DeployError(RuntimeError):
    pass


class PreflightSkip(DeployError):
    """The optional private transport cannot run in this installation."""


def run(args, timeout=30):
    try:
        r = subprocess.run([str(x) for x in args], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=timeout, env=policy.backend_env())
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
        raise PreflightSkip('Ubuntu rootfs 不存在或布局不唯一')
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
            legacy = (data.get('schema') != SCHEMA or data.get('tool_version') not in PUBLIC_VERSIONS or
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
             source.parents[1] / 'agent', source.parents[1] / 'agent/adb_endpoint.py',
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
            'schema': SCHEMA, 'tool_version': TOOL_VERSION, 'state': 'prepared', 'files': {}, 'diversions': []}

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

    def remove(self, path):
        self.remember(path)
        self.data['files'][str(path)]['installed'] = {'kind': 'absent'}
        self.save()
        Path(path).unlink(missing_ok=True)

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
    env = policy.backend_env()
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


def backend_dependencies(backend, package_path=None, *, library_dir=None, package_owners=None, package_versions=None):
    """Discover the actual ELF closure, not a hand-maintained package allowlist."""
    library_dir = P / 'lib' if library_dir is None else Path(library_dir)
    system_dirs = SYSTEM_DIRS
    pending, visited, libraries, owners = [Path(backend)], set(), {}, set()

    def owner(path):
        if package_owners is not None:
            if str(path) not in package_owners:
                raise DeployError('仓库快照依赖缺少包属主：' + str(path))
            owners.add(package_owners[str(path)])
            return
        rows = run([P / 'bin/dpkg-query', '-S', path]).splitlines()
        matches = []
        for row in rows:
            name, _, owned = row.rpartition(': ')
            # dpkg-query also prints diversion metadata, which is not a package.
            if owned == str(path) and re.fullmatch(r'[a-z0-9][a-z0-9+.:\-]*', name):
                matches.append(name)
        if len(matches) != 1:
            raise DeployError('backend 依赖没有唯一软件包属主：' + str(path))
        owners.add(matches[0])

    # Existing frozen backends are unowned copies; an audited caller can supply
    # the original dpkg path after verifying its diverted vendor hash matches.
    owner(Path(backend if package_path is None else package_path))
    while pending:
        file = pending.pop().resolve(strict=True)
        if file in visited:
            continue
        visited.add(file)
        dynamic = run([P / 'bin/readelf', '--dynamic', '--wide', file])
        # Only the standard Termux search layout is supported. Never guess a custom loader path.
        for tag, value in re.findall(r'\((RPATH|RUNPATH)\)[^\[]*\[([^\]]*)\]', dynamic):
            entries = [entry.replace('${ORIGIN}', str(file.parent)).replace('$ORIGIN', str(file.parent))
                       for entry in value.split(':')]
            # Repository debs retain Termux's absolute RUNPATH even while their
            # libraries are inspected in a temporary snapshot. The published
            # backend is still admitted only after private /proc/maps auditing.
            allowed = {library_dir.resolve(), (P / 'lib').resolve()}
            if any(Path(entry).resolve() not in allowed for entry in entries):
                raise DeployError('backend 依赖含不支持的 ' + tag + '：' + str(file))
        for name in re.findall(r'\(NEEDED\)[^\[]*\[([^\]]+)\]', dynamic):
            if '/' in name or name in ['', '.', '..']:
                raise DeployError('不支持的 ELF NEEDED：' + name)
            path = library_dir / name
            if path.exists() or path.is_symlink():
                resolved = path.resolve(strict=True)
                if library_dir.resolve() not in resolved.parents or not resolved.is_file():
                    raise DeployError('backend 依赖逃出 Termux lib：' + str(path))
                owner(path)
                if resolved != path:
                    owner(resolved)
                info = resolved.stat()
                if info.st_uid != os.getuid():
                    raise DeployError('backend 依赖不属于 Termux 用户：' + str(path))
                libraries[str(path)] = {'resolved': str(resolved), 'sha256': policy.digest(resolved),
                                        'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid}
                pending.append(resolved)
            elif not any((directory / name).is_file() for directory in system_dirs):
                raise DeployError('无法解析 backend 共享库：' + name)
    versions = {}
    for name in sorted(owners):
        if package_versions is not None:
            versions[name] = package_versions[name]
            continue
        row = run([P / 'bin/dpkg-query', '-W', '-f=${db:Status-Status}\t${Version}\n', name]).split('\t')
        if len(row) != 2 or row[0] != 'installed' or not row[1]:
            raise DeployError('backend 依赖包未完整安装：' + name)
        versions[name] = row[1]
    return {'libraries': libraries, 'packages': versions}


def elf_needed(path):
    dynamic = run([P / 'bin/readelf', '--dynamic', '--wide', path])
    for tag, value in re.findall(r'\((RPATH|RUNPATH)\)[^\[]*\[([^\]]*)\]', dynamic):
        for entry in value.split(':'):
            if '$ORIGIN' in entry or '${ORIGIN}' in entry:
                resolved = Path(entry.replace('${ORIGIN}', str(path.parent)).replace('$ORIGIN', str(path.parent))).resolve()
                private = path.parent if path.name == 'adb.backend' else path.parent.parent
                if resolved != private and private not in resolved.parents:
                    raise DeployError('复制后 ' + tag + ' 的 $ORIGIN 逃出私有运行时：' + str(path))
    return re.findall(r'\(NEEDED\)[^\[]*\[([^\]]+)\]', dynamic)


def copy_runtime(root, backend, dependencies, backend_sha256=None, generation_name=None):
    """Publish one complete generation; no hardlinks to mutable package files."""
    root, backend = Path(root), Path(backend)
    directory = root / (generation_name or 'runtime-' + str(time.time_ns()))
    if (directory.parent != root or not directory.name.startswith('runtime-') or
            directory.exists() or directory.is_symlink()):
        raise DeployError('候选私有运行时路径已被占用')
    temporary = Path(tempfile.mkdtemp(prefix='.runtime-', dir=root))
    try:
        (temporary / 'lib').mkdir(mode=0o700)
        expected_backend = backend_sha256 or policy.digest(backend)
        atomic(temporary / 'adb.backend', backend.read_bytes(), 0o700)
        if policy.digest(temporary / 'adb.backend') != expected_backend:
            raise DeployError('复制期间 backend 发生变化，未发布运行时')
        libraries, needed, total = {}, {'adb.backend': elf_needed(temporary / 'adb.backend')}, 0
        for name, row in dependencies['libraries'].items():
            source = Path(row['resolved'])
            if policy.digest(source) != row['sha256']:
                raise DeployError('复制期间公共库发生变化，未发布私有运行时')
            entity, alias = source.name, Path(name).name
            if entity not in libraries:
                atomic(temporary / 'lib' / entity, source.read_bytes())
                if policy.digest(temporary / 'lib' / entity) != row['sha256']:
                    raise DeployError('私有库复制哈希不符')
                libraries[entity] = {'sha256': row['sha256'], 'mode': 0o600}
                needed[entity] = elf_needed(temporary / 'lib' / entity)
                total += (temporary / 'lib' / entity).stat().st_size
            elif libraries[entity].get('sha256') != row['sha256']:
                raise DeployError('私有库 basename 冲突')
            if alias != entity:
                expected = {'target': entity}
                if alias in libraries and libraries[alias] != expected:
                    raise DeployError('私有库 SONAME 别名冲突')
                if alias not in libraries:
                    (temporary / 'lib' / alias).symlink_to(entity)
                    libraries[alias] = expected
        all_needed = {n for names in needed.values() for n in names}
        system = {}
        for name in sorted(all_needed - libraries.keys()):
            if '/' in name or not name:
                raise DeployError('无效的系统 NEEDED')
            candidates = [d / name for d in SYSTEM_DIRS if (d / name).is_file()]
            if not candidates:
                raise DeployError('复制后的闭包不完整，拒绝公共库回落：' + name)
            # The SONAME, not an APEX versioned path, is the stable requirement.
            system[name] = name
        runtime = {'directory': str(directory), 'libraries': libraries, 'needed': needed,
                   'system_libraries': system,
                   'backend_sha256': policy.digest(temporary / 'adb.backend'),
                   'packages': dependencies['packages'], 'size_bytes': total,
                   'created_at': time.time()}
        for subdir in [temporary / 'lib', temporary]:
            fd = os.open(subdir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        temporary.rename(directory)
        runtime['metadata'] = {str(path.relative_to(directory)): file_metadata(path.lstat())
                               for path in [directory / 'adb.backend', *(directory / 'lib').iterdir()]}
        config = {'deployment_root': str(root), 'backend': str(directory / 'adb.backend'), 'runtime': runtime}
        policy.check_runtime(config)
        return config
    except BaseException:
        if directory.exists():
            shutil.rmtree(directory)
        raise
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def repository_runtime(generation_name=None):
    """Resolve a fresh repository set without installing it or touching package holds."""
    with tempfile.TemporaryDirectory(prefix='dsb-runtime-source-', dir=P / 'tmp') as tmp:
        directory = Path(tmp)
        archives = directory / 'archives'
        archives.mkdir(mode=0o700)
        # Empty dpkg status deliberately ignores the held vendor and installed
        # partial-upgrade state. APT still verifies downloads against its indexes.
        run([P / 'bin/apt-get', '-o', 'Dir::State::status=/dev/null',
             '-o', 'Dir::State::extended_states=' + str(directory / 'extended_states'),
             '-o', 'Dir::Cache::archives=' + str(archives),
             '-o', 'Dir::Cache::pkgcache=', '-o', 'Dir::Cache::srcpkgcache=',
             '-o', 'Dir::Log=' + str(directory), '-o', 'Debug::NoLocking=true',
             '--download-only', '--yes', '--no-install-recommends', 'install', 'android-tools'], timeout=600)
        source = directory / 'source'
        (source / 'lib').mkdir(parents=True, mode=0o700)
        (source / 'bin').mkdir(mode=0o700)
        owners, versions = {}, {}
        for deb in sorted(archives.glob('*.deb')):
            package = run([P / 'bin/dpkg-deb', '--field', deb, 'Package'])
            versions[package] = run([P / 'bin/dpkg-deb', '--field', deb, 'Version'])
            result = subprocess.run([str(P / 'bin/dpkg-deb'), '--fsys-tarfile', str(deb)],
                                    capture_output=True, check=True, timeout=60, env=policy.backend_env())
            with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
                for member in archive:
                    name = Path('/' + member.name.removeprefix('./').lstrip('/'))
                    if name != NATIVE and not (name.parent == P / 'lib' and '.so' in name.name):
                        continue
                    destination = source / ('bin' if name == NATIVE else 'lib') / name.name
                    if str(destination) in owners:
                        raise DeployError('仓库快照库文件存在多个包属主')
                    if member.isfile():
                        atomic(destination, archive.extractfile(member).read(), 0o700 if name == NATIVE else 0o600)
                    elif member.issym():
                        target = Path(member.linkname)
                        if target.is_absolute():
                            if target.parent != name.parent:
                                raise DeployError('仓库快照库链接逃出 lib')
                            target = Path(target.name)
                        if target.name != str(target) or str(target) in ['.', '..']:
                            raise DeployError('仓库快照包含不支持的库链接')
                        destination.symlink_to(target)
                    else:
                        raise DeployError('仓库快照库不是普通文件/链接')
                    owners[str(destination)] = package
        backend = source / 'bin/adb'
        dependencies = backend_dependencies(backend, library_dir=source / 'lib',
                                            package_owners=owners, package_versions=versions)
        config = copy_runtime(ROOT, backend, dependencies, generation_name=generation_name)
        try:
            policy.check_backend(config)
        except BaseException:
            shutil.rmtree(Path(config['runtime']['directory']))
            raise
        return config


def package_holds(names):
    held = set(run([P / 'bin/apt-mark', 'showhold']).splitlines())
    return {'termux': {name: name in held for name in sorted(names)},
            'ubuntu': 'adb' in container(['apt-mark', 'showhold']).splitlines()}


def set_holds(values):
    native = values['termux']
    # Earlier public v1 journals only recorded the two adb-package booleans.
    if isinstance(native, bool):
        native = {'android-tools': native}
    for held in [True, False]:
        names = sorted(name for name, value in native.items() if value is held)
        if names:
            run([P / 'bin/apt-mark', 'hold' if held else 'unhold', *names])
    container(['apt-mark', 'hold' if values['ubuntu'] else 'unhold', 'adb'])
    actual = package_holds(native)
    if actual != {'termux': native, 'ubuntu': values['ubuntu']}:
        raise DeployError('软件包 hold 应用/恢复未通过读回校验')


def refresh_holds(config, baseline):
    native = baseline['termux']
    native = native if isinstance(native, dict) else {'android-tools': native}
    legacy = config.get('tool_version') == 2 and not config.get('runtime')
    if legacy:
        current = package_holds(native)['termux']
        desired = {name: (held and native[name]) for name, held in current.items() if name != 'android-tools'}
    else:
        desired = {}
    return {'termux': {**desired, 'android-tools': True}, 'ubuntu': True}


def retire_dependency_holds(journal):
    baseline = journal['baseline']['holds']
    native = baseline['termux']
    if isinstance(native, dict) and set(native) - {'android-tools'}:
        journal.setdefault('legacy_dependency_holds', json.loads(json.dumps(baseline)))
        journal['baseline']['holds'] = {'termux': {'android-tools': native['android-tools']},
                                       'ubuntu': baseline['ubuntu']}


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
    for name in ['clang', 'python3', 'bash', 'flock', 'timeout', 'dpkg-divert', 'proot-distro',
                 'readelf', 'dpkg-deb', 'apt-get']:
        if not (P / 'bin' / name).is_file():
            raise DeployError('缺少原生工具：' + name)
    if shutil.disk_usage(P).free < 64 * 1024 * 1024 or len(os.fsencode(SOCK)) >= 104:
        raise DeployError('空间不足或私有 socket 路径过长')
    if not SUP.is_file() or not NATIVE.exists():
        raise PreflightSkip('缺少 supervisor 或 Termux ADB，未加固')
    if NATIVE.is_symlink() or not NATIVE.is_file():
        raise DeployError('Termux ADB 不是可信普通文件')
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
    dependencies = backend_dependencies(NATIVE)
    return {'packages': packages(), 'holds': package_holds(['android-tools']),
            'backend_dependencies': dependencies, 'diversions': rows, 'server': proc_identity(servers[0]['pid']),
            'container_paths': sorted({row['path'] for row in rows if row['side'] == 'ubuntu'}),
            'server_sha256': server_hash, 'launcher': compile_launcher(), 'target': target, 'rootfs': str(RF)}


def verify_private():
    report = policy.status(ROOT, audit=True)
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


def seal_files(config):
    config['file_metadata'] = {name: file_metadata(Path(name).lstat())
                               for name in config['files'].keys() | config.get('vendor_hashes', {}).keys()}
    return config


def activate_cutover(config):
    with policy.startup_lock(ROOT):
        (ROOT / 'maintenance').unlink()
        bind_private(config)
        local = policy._status_locked(ROOT, check_inventory=False)
        if not local['ok']:
            raise DeployError('切换后私有 server/maps 检查失败：' + str(local.get('error')))


def cutover(original):
    # 先准备完整日志再原子发布，避免断电留下无法恢复的空部署目录。
    staging = Path(tempfile.mkdtemp(prefix='dsb-adb-stage-', dir=ROOT.parent))
    txn = Transaction(staging)
    txn.save()
    txn.data['baseline'] = {k: v for k, v in original.items() if k != 'launcher'}
    txn.save()
    policy.check_dependencies(original)
    staged_runtime = copy_runtime(staging, NATIVE, original['backend_dependencies'], original['server_sha256'])
    policy.check_backend(staged_runtime)
    runtime = json.loads(json.dumps(staged_runtime))
    runtime['deployment_root'] = str(ROOT)
    runtime['runtime']['directory'] = staged_runtime['runtime']['directory'].replace(str(staging), str(ROOT), 1)
    runtime['backend'] = staged_runtime['backend'].replace(str(staging), str(ROOT), 1)
    core = ['adb_policy.py', 'adb_guard.py', 'adb_identity.py', 'adb_client.py', 'adb_relay.py']
    for name in core:
        atomic(staging / name, (STAGE / 'payload' / name).read_bytes())
    atomic(staging / 'launcher.bin', original['launcher'])
    atomic(staging / 'maintenance', b'cutover\n')
    paths = [hostpath(row) for row in original['diversions']] + [RF / 'usr/local/bin/adb']
    files = {str(p): 0o700 for p in paths}
    files.update({str(ROOT / name): 0o600 for name in core})
    launcher_hash = hashlib.sha256(original['launcher']).hexdigest()
    config = {'schema': SCHEMA, 'tool_version': TOOL_VERSION, **runtime,
              'socket': str(SOCK), 'target_file': str(TARGET),
              'vendor_hashes': {str(Path(row['destination']) if row['side'] == 'termux' else
                                    RF / row['destination'].lstrip('/')): fingerprint(hostpath(row))['sha256']
                                for row in original['diversions'] if fingerprint(hostpath(row))['kind'] == 'file'},
              'container_paths': sorted({row['path'] for row in original['diversions'] if row['side'] == 'ubuntu'} |
                                        {'/usr/local/bin/adb'}),
              'files': {name: (launcher_hash if mode == 0o700 else
                               policy.digest(staging / Path(name).name)) for name, mode in files.items()},
              'modes': files}
    atomic(staging / 'manifest.json', json.dumps(config, indent=2).encode())
    txn.data['private_config'] = config
    txn.data['launcher_sha256'] = launcher_hash
    txn.data['original_files'] = {str(path): fingerprint(path) for path in paths}
    txn.save()
    staging.rename(ROOT)
    txn = Transaction(ROOT)
    set_holds({'termux': {name: True for name in original['holds']['termux']}, 'ubuntu': True})
    SOCKDIR.mkdir(mode=0o700)
    for row in original['diversions']:
        txn.remember(hostpath(row))
    txn.remember(RF / 'usr/local/bin/adb')
    for row in original['diversions']:
        txn.data['diversions'].append(row)
        txn.save()
        divert(row, '--add')
    for path in paths:
        txn.write(path, original['launcher'], 0o700)
    # supervisor 不进入关键文件哈希锁集，正常脚本更新不应锁死 ADB。
    supervisor_data = (STAGE / 'supervisor.sh').read_bytes()
    if SUP.read_bytes() != supervisor_data or stat.S_IMODE(SUP.stat().st_mode) != 0o700:
        txn.write(SUP, supervisor_data, 0o700)
    seal_files(config)
    txn.data['private_config'] = config
    txn.save()
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
    activate_cutover(config)
    report = verify_private()
    verify_supervisor(since)
    txn = Transaction(ROOT)
    txn.data['report'] = report
    txn.event('applied')
    record_state('applied')
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


def ensure_vendor_backup(path, vendor, original):
    """Recover a diversion whose vendor was restored to its canonical path."""
    if fingerprint(vendor) == original:
        return
    if fingerprint(vendor) == {'kind': 'absent'} and fingerprint(path) == original:
        path.rename(vendor)
        return
    raise DeployError('中断恢复时 vendor 与原始备份不符，保留现场：' + str(vendor))


def _recover_private_locked(txn):
    """Finish an interrupted private cutover without trying the ordinary TCP backend."""
    config = txn.data.get('private_config')
    launcher = ROOT / 'launcher.bin'
    if not isinstance(config, dict) or not (ROOT / 'manifest.json').is_file():
        raise DeployError('中断事务缺少已验证私有运行时/manifest；未尝试普通 TCP 回滚，保留现场')
    manifest = json.loads((ROOT / 'manifest.json').read_text())
    if {k: v for k, v in manifest.items() if k != 'file_metadata'} != {
            k: v for k, v in config.items() if k != 'file_metadata'}:
        raise DeployError('中断事务 manifest 与 journal 不符，未尝试普通 TCP 回滚')
    policy.check_runtime(config)
    policy.check_backend(config)
    if not launcher.exists() and not launcher.is_symlink():
        rebuilt = compile_launcher()
        if hashlib.sha256(rebuilt).hexdigest() != txn.data.get('launcher_sha256'):
            raise DeployError('中断事务 launcher 无法按原哈希重编，保留现场')
        atomic(launcher, rebuilt)
    if launcher.is_symlink() or not launcher.is_file() or policy.digest(launcher) != txn.data.get('launcher_sha256'):
        raise DeployError('中断事务缺少已验证私有运行时/manifest/launcher；未尝试普通 TCP 回滚，保留现场')
    for name, expected in config['files'].items():
        if name.startswith(str(ROOT) + '/') and policy.digest(name) != expected:
            raise DeployError('私有策略文件漂移，未补完入口：' + name)
    for name, expected in txn.data.get('original_files', {}).items():
        if name not in txn.data['files'] and fingerprint(name) != expected:
            raise DeployError('中断后原入口已改变，未覆盖：' + name)
    missing = [str(hostpath(row)) for row in txn.data['baseline']['diversions']
               if truename(row) == row['destination']]
    txn.check_restore(missing)
    # A failed ordinary rollback may already have started our receipt-bound
    # TCP server. Stop only that attested process, before restoring diversions.
    if txn.data['state'] in ['rollback-restoring', 'rollback-tcp', 'rollback-failed']:
        probes = [policy.probe(f, h, 5037) for f, h in
                  [(policy.socket.AF_INET, '127.0.0.1'), (policy.socket.AF_INET6, '::1')]]
        if probes != ['closed', 'closed']:
            owner = bootstrap.tcp_owner(NATIVE)
            identity = proc_identity(owner['pid'])
            if identity is None:
                raise DeployError('普通恢复 server 身份已变化，保留现场')
            stop(identity)
            wait_tcp_closed()
    atomic(ROOT / 'maintenance', b'recover private\n')
    set_holds({'termux': {name: True for name in txn.data['baseline']['holds']['termux']}, 'ubuntu': True})
    SOCKDIR.mkdir(mode=0o700, exist_ok=True)
    for row in txn.data['baseline']['diversions']:
        path = hostpath(row)
        txn.remember(path)
        actual = truename(row)
        if actual == row['path']:
            divert(row, '--add')
        elif actual != row['destination']:
            raise DeployError('中断恢复发现未知 diversion，保留现场')
        vendor = Path(row['destination']) if row['side'] == 'termux' else RF / row['destination'].lstrip('/')
        ensure_vendor_backup(path, vendor, txn.data['files'][str(path)]['original'])
        if row not in txn.data['diversions']:
            txn.data['diversions'].append(row)
            txn.save()
    entry = RF / 'usr/local/bin/adb'
    txn.remember(entry)
    for path in [*[hostpath(row) for row in txn.data['baseline']['diversions']], entry]:
        if fingerprint(path) not in [txn.data['files'][str(path)]['original'],
                                     txn.data['files'][str(path)]['installed'], {'kind': 'absent'}]:
            raise DeployError('中断恢复发现入口后续改动，保留现场：' + str(path))
        txn.write(path, launcher.read_bytes(), 0o700)
    seal_files(config)
    txn.data['private_config'] = config
    txn.save()
    atomic(ROOT / 'manifest.json', json.dumps(config, indent=2).encode())
    rows, unknown = policy.server_processes()
    if unknown:
        raise DeployError('中断恢复发现未知 ADB 进程，保留现场')
    original = txn.data['baseline']
    if any(row['pid'] == original['server']['pid'] for row in rows):
        stop_original_server(original)
    elif any(row['exe'] != config['backend'] or row['socket'] != 'localfilesystem:' + config['socket']
             for row in rows):
        raise DeployError('中断恢复发现非当前私有 server，保留现场')
    wait_tcp_closed()
    (ROOT / 'maintenance').unlink(missing_ok=True)
    (ROOT / 'blocked').unlink(missing_ok=True)
    report = bind_private(config, require_device=False)
    txn.data['report'] = report
    txn.event('applied')
    record_state('applied', '中断后已验证并补完私有 ADB；旧设备 binding 保留，重连时重新认证')
    return report


def recover_private():
    require_native()
    reject_legacy()
    txn = Transaction(ROOT)
    if txn.data['state'] not in ['prepared', 'entrypoints-installed', 'activating']:
        raise DeployError('当前事务不是可补完的私有 cutover，保留现场')
    if 'baseline' not in txn.data:
        raise DeployError('中断事务缺少已验证私有运行时与基线；未尝试普通 TCP 回滚')
    global RF
    RF = Path(txn.data['baseline']['rootfs'])
    with policy.startup_lock(ROOT):
        report = _recover_private_locked(txn)
    if SUP.is_file():
        start_supervisor()
    print('ADB_INTERRUPTED_PRIVATE_RECOVERED ' + json.dumps(report, ensure_ascii=False, sort_keys=True))
    return report


def recover_private_rollback():
    require_native()
    reject_legacy()
    txn = Transaction(ROOT)
    if txn.data['state'] not in ['rollback-restoring', 'rollback-tcp', 'rollback-failed']:
        raise DeployError('没有可恢复的普通回滚事务')
    global RF
    RF = Path(txn.data['baseline']['rootfs'])
    with policy.startup_lock(ROOT):
        report = _recover_private_locked(txn)
    if SUP.is_file():
        start_supervisor()
    print('ADB_ROLLBACK_PRIVATE_REAPPLIED ' + json.dumps(report, ensure_ascii=False, sort_keys=True))
    return report


def has_private_generation(txn):
    if txn.data.get('tool_version', 1) >= 3:
        if 'private_config' not in txn.data:
            raise DeployError('v3 中断事务缺少私有配置，拒绝普通 TCP 回滚；保留现场')
        return True
    return False


def finish_rolled_back():
    """Finish the terminal rename without repeating entrypoint restoration."""
    txn = Transaction(ROOT)
    if txn.data['state'] != 'rolled-back' or 'baseline' not in txn.data:
        raise DeployError('没有可收尾的已回滚事务')
    bootstrap.tcp_owner(NATIVE, allow_empty=True)
    baseline_holds = txn.data['baseline']['holds']
    set_holds(baseline_holds)
    native_holds = baseline_holds['termux']
    if isinstance(native_holds, bool):
        native_holds = {'android-tools': native_holds}
    if package_holds(native_holds) != {'termux': native_holds, 'ubuntu': baseline_holds['ubuntu']}:
        raise DeployError('回滚 hold 未恢复到基线，保留终结态等待 recover')
    if SOCK.exists() or SOCK.is_symlink():
        if not SOCK.is_socket() or SOCK.lstat().st_uid != os.getuid():
            raise DeployError('回滚 socket 已变化，拒绝清理')
        SOCK.unlink()
    if SOCKDIR.exists():
        if SOCKDIR.is_symlink() or SOCKDIR.stat().st_uid != os.getuid():
            raise DeployError('回滚 socket 目录不可信，拒绝清理')
        SOCKDIR.rmdir()
    archive = ROOT.with_name('dsb-adb-rollback-' + str(time.time_ns()))
    ROOT.rename(archive)
    reason = txn.data.get('rollback_reason')
    if not reason:
        try:
            info = STATE.lstat()
            if (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and
                    not info.st_mode & 0o022):
                previous = json.loads(STATE.read_text())
                if (isinstance(previous, dict) and previous.get('status') == 'rolled-back' and
                        isinstance(previous.get('reason'), str)):
                    reason = previous['reason']
        except (FileNotFoundError, OSError, ValueError):
            pass
    reason = reason or '已恢复普通 ADB TCP 传输'
    record_state('rolled-back', reason + '；私有部署已归档')
    print('ADB_ROLLBACK_OK（普通 ADB TCP 传输，未加固）')


def known_pending_refresh(directory, *, initializing=False):
    try:
        info = directory.lstat()
        journal = directory / 'journal.json'
        journal_info = journal.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or
                stat.S_IMODE(info.st_mode) != 0o700 or
                not stat.S_ISREG(journal_info.st_mode) or journal_info.st_uid != os.getuid() or
                stat.S_IMODE(journal_info.st_mode) != 0o600):
            return False
        data = json.loads(journal.read_text())
        entries = list(directory.iterdir()) if initializing else ()
    except (FileNotFoundError, OSError, ValueError):
        return False
    if (not isinstance(data, dict) or data.get('schema') != SCHEMA or
            data.get('kind') != 'private-runtime-refresh' or data.get('tool_version') != TOOL_VERSION):
        return False
    if initializing:
        for path in entries:
            info = path.lstat()
            if ((path.name != 'journal.json' and not path.name.startswith('.dsb-')) or
                    not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                    stat.S_IMODE(info.st_mode) != 0o600):
                return False
        return data.get('state') == 'prepared' and not data.get('files') and not data.get('diversions')
    return data.get('state') in ['prepared', 'quiescing', 'activating', 'published', 'committed',
                                 'holds-restored', 'restored-private', 'private-restored-pending-holds']


def rollback():
    require_native()
    reject_legacy()
    refresh_paths = [ROOT / 'refresh-transaction', *ROOT.glob('.refresh-init-*')]
    pending = []
    for path in refresh_paths:
        if not os.path.lexists(path):
            continue
        if known_pending_refresh(path, initializing=path.name.startswith('.refresh-init-')):
            pending.append(path)
        else:
            print('ADB_REFRESH_WARNING：未知刷新残留，未清理：' + str(path), file=sys.stderr)
    if pending:
        raise DeployError('ADB_ROLLBACK_REFUSED：存在未完成的私有刷新；请先运行 install.sh recover，'
                          '完成私有事务恢复后再决定是否普通回滚；未改入口、日志或 hold')
    with policy.startup_lock(ROOT):
        txn = Transaction(ROOT)
        if txn.data['state'] == 'rolled-back':
            finish_rolled_back()
            return
        if txn.data['state'] in ['prepared', 'entrypoints-installed', 'activating']:
            if 'baseline' not in txn.data:
                raise DeployError('中断事务缺少已验证私有运行时与基线；未尝试普通 TCP 回滚')
            global RF
            RF = Path(txn.data['baseline']['rootfs'])
            if has_private_generation(txn):
                _recover_private_locked(txn)
                print('ADB_ROLLBACK_NOTICE：先补完并验证中断的私有部署，再预检普通 ADB')
        # Refusal is outside the mutation handler. The guard defers while an
        # ordinary vendor is tested on a separate private filesystem socket.
        rollback_preflight()
        if not has_private_generation(txn):
            try:
                _rollback()
            finally:
                if SUP.is_file():
                    start_supervisor()
            return
        try:
            _rollback()
        except BaseException as exc:
            try:
                _recover_private_locked(Transaction(ROOT))
            except BaseException as rescue:
                if (ROOT / 'journal.json').is_file():
                    txn = Transaction(ROOT)
                    txn.data['rollback_phase'] = txn.data['state']
                    txn.event('rollback-failed')
                    record_state('rollback-failed', '回滚未完成且私有重新应用失败，阶段=' + txn.data['rollback_phase'])
                raise DeployError('普通回滚及私有重新应用均未完成；保留现场，请运行 install.sh recover；'
                                  '未确认私有 ADB 可用；普通回滚错误：' + str(exc) +
                                  '；私有恢复错误：' + str(rescue)) from rescue
            raise DeployError('普通回滚失败，但私有入口/server/maps 已重新验证可用；'
                              '请检查原因后重试 rollback：' + str(exc)) from exc
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
    config = json.loads((ROOT / 'manifest.json').read_text()) if (ROOT / 'manifest.json').is_file() else {}
    for row in rows:
        if row['exe'] == config.get('backend', str(ROOT / 'adb.backend')):
            if row['socket'] != 'localfilesystem:' + str(SOCK) or policy.digest(f"/proc/{row['pid']}/exe") != policy.digest(row['exe']):
                raise DeployError('回滚时 backend 进程内容/端点不符')
            policy.check_runtime_maps(row['pid'], config)
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
    reason = '已恢复普通 ADB TCP 传输；其它服务不变'
    if not device_ok:
        reason += '；设备离线，请打开无线调试并重新连接'
    txn.data['rollback_reason'] = reason
    txn.event('rolled-back')
    record_state('rolled-back', reason)
    # 保留备份作审计；移到不被 doctor 识别为已加固的位置。
    finish_rolled_back()


def rollback_preflight():
    txn = Transaction(ROOT)
    if 'baseline' not in txn.data:
        raise DeployError('没有完整的回滚日志')
    rf = Path(txn.data['baseline']['rootfs'])
    if packages() != txn.data['baseline']['packages']:
        raise DeployError('ADB 包版本已改变，拒绝覆盖新版本；私有部署未变，请人工核验包与 journal')
    canonical = {row['path'] for row in txn.data['baseline'].get('diversions', txn.data['diversions'])
                 if row['side'] == 'ubuntu' and
                 txn.data['files'].get(str(rf / row['path'].lstrip('/')), {}).get('original', {}).get('kind') == 'file'}
    for row in txn.data['baseline'].get('diversions', txn.data['diversions']):
        path = Path(row['destination']) if row['side'] == 'termux' else rf / row['destination'].lstrip('/')
        original = txn.data['files'].get(str(Path(row['path']) if row['side'] == 'termux' else rf / row['path'].lstrip('/')))
        if original is None:
            continue  # A cutover interrupted before registering this diversion.
        if original['original']['kind'] == 'symlink':
            if fingerprint(path) != original['original']:
                raise DeployError('普通 ADB 链接已漂移，回滚未开始：' + str(path))
            if row['side'] != 'ubuntu' or rootfs_link_target(path, rf) not in canonical:
                raise DeployError('普通 ADB 链接未指向受管真实 ELF，回滚未开始：' + str(path))
            continue  # The canonical vendor ELF is probed by its own row below.
        if original['original']['kind'] != 'file':
            raise DeployError('普通 ADB 基线文件类型不支持')
        if not path.is_file():
            path = Path(original['backup'])
        if fingerprint(path) != original['original']:
            raise DeployError('普通 ADB 备份/包内容漂移，回滚未开始')
        try:
            if row['side'] == 'termux':
                policy.client_run([str(path), 'version'], env=policy.backend_env(), check=True,
                                  capture_output=True, timeout=4)
                probe_ordinary_server(path)
            else:
                executable = '/' + str(path.relative_to(rf)) if rf in path.parents else str(path)
                container(['env', '-u', 'LD_LIBRARY_PATH', '-u', 'LD_PRELOAD', executable, 'version'])
        except (OSError, subprocess.SubprocessError, DeployError) as exc:
            healthy = (txn.data['state'] == 'applied' and
                       policy._status_locked(ROOT, check_inventory=False).get('ok') is True)
            private = ('已证实私有 server/maps 健康，私有部署保持可用。' if healthy else
                       '私有 server/maps 尚未证实健康；请先运行 install.sh recover/refresh，不宣称 ADB 可用。')
            raise DeployError('ADB_ROLLBACK_REFUSED：普通 ADB 无法使用当前公共库加载；' + private +
                              '请准备与普通 android-tools/Ubuntu adb 匹配的依赖包（或由维护者更新普通 ADB 及回滚基线），'
                              '确认 vendor version 可运行后重试 install.sh rollback；不要删除私有目录或解除 ADB 包 hold') from exc


def rootfs_link_target(path, rootfs):
    current = Path(path)
    rootfs = Path(rootfs)
    seen = set()
    for _ in range(24):
        if current in seen or rootfs not in current.parents:
            raise DeployError('Ubuntu ADB 链接成环或逃出 rootfs')
        seen.add(current)
        if not current.is_symlink():
            return '/' + str(current.relative_to(rootfs))
        target = Path(os.readlink(current))
        current = Path(os.path.normpath(str(rootfs / target.relative_to('/') if target.is_absolute()
                                         else current.parent / target)))
    raise DeployError('Ubuntu ADB 链接层数过多')


def probe_ordinary_server(backend):
    with tempfile.TemporaryDirectory(prefix='dsb-rb-', dir=P / 'tmp') as tmp:
        address = Path(tmp) / 'server.sock'
        env = policy.backend_env()
        for name in ['ADB_SERVER_SOCKET', 'ANDROID_ADB_SERVER_ADDRESS', 'ANDROID_ADB_SERVER_PORT', 'DSB_ADB_RECEIPT_NONCE']:
            env.pop(name, None)
        env['ADB_EMU'] = '0'
        process = subprocess.Popen([str(backend), '-L', 'localfilesystem:' + str(address), 'server', 'nodaemon'],
                                   env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, umask=0o177,
                                   preexec_fn=functools.partial(parent_death_setup, os.getpid()))
        try:
            deadline = time.monotonic() + 4
            while process.poll() is None:
                try:
                    policy.socket_info(address)
                    policy.host_version(address)
                    return
                except (OSError, policy.PolicyError):
                    pass
                if time.monotonic() >= deadline:
                    break
                time.sleep(.05)
            raise OSError('ordinary vendor cannot start a filesystem server with public libraries')
        finally:
            if process.poll() is None:
                process.terminate()  # Only this still-unreaped child, never an enumerated/unknown PID.
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=4)


def stop_private(config, require_runtime=True):
    rows, unknown = policy.server_processes()
    if unknown or any(row['exe'] != config['backend'] or row['socket'] != 'localfilesystem:' + config['socket'] for row in rows):
        raise DeployError('私有 server 集合无法完整识别，未停止未知进程')
    for row in rows:
        identity = proc_identity(row['pid'])
        if require_runtime:
            policy.check_runtime_maps(row['pid'], config)
        expected_hash = config.get('runtime', {}).get('backend_sha256', config.get('files', {}).get(config['backend']))
        if expected_hash is None:
            raise DeployError('私有 server 缺少可信镜像哈希')
        fd = adb_guard.open_pidfd(row['pid'])
        try:
            if (identity is None or proc_identity(row['pid']) != identity or
                    policy.digest(f"/proc/{row['pid']}/exe") != expected_hash):
                raise DeployError('私有 server 镜像/PID-start 不符')
            adb_guard.send_pidfd(fd)
        finally:
            os.close(fd)
        deadline = time.monotonic() + 10
        while proc_identity(row['pid']) == identity:
            if time.monotonic() >= deadline:
                raise DeployError('私有 server 没有在期限内退出')
            time.sleep(.05)


def bind_private(config, require_device=True, txn=None):
    policy.ensure_server_locked(config, ROOT)
    if 'runtime' in config:
        rows, unknown = policy.server_processes()
        if unknown or len(rows) != 1 or rows[0]['exe'] != config['backend']:
            raise DeployError('无法核验新私有代的唯一 server 映射')
        capture_system_maps(rows[0]['pid'], config)
        policy.check_runtime_maps(rows[0]['pid'], config)
    if require_device:
        adb_identity.connect_verified([config['backend'], '-L', 'localfilesystem:' + config['socket']],
                                      TARGET.read_text().strip(), TARGET.with_name('adb-transport.json'),
                                      ROOT / 'adb_relay.py', root=ROOT, locked=True)
    report = policy._status_locked(ROOT, check_inventory=require_device)
    if not report['ok']:
        raise DeployError('换代后私有运行时检查失败：' + str(report.get('error')))
    return report


def restore_refresh(txn):
    # Never route a refresh interruption through the ordinary TCP rollback.
    current = json.loads((ROOT / 'manifest.json').read_text())
    allowed = [txn.data['old_config']['backend'], txn.data['new_config']['backend']]
    if current['backend'] not in allowed:
        raise DeployError('刷新恢复时出现未知代次，保留现场')
    stop_private(current, require_runtime=False)
    txn.restore_files()
    (ROOT / 'maintenance').unlink(missing_ok=True)
    if not txn.data.get('old_healthy', True):
        policy.block('刷新失败；旧私有代在刷新前已损坏，请检查仓库后重跑 install.sh refresh；未退 TCP', ROOT)
        txn.event('restored-private')
        return {'ok': False, 'error': '保留原先已损坏的私有代；未退 TCP'}
    # An old in-flight policy can publish a transient blocked flag after seeing
    # our maintenance gate. It must not outlive a refused/failed refresh of a
    # deployment that was healthy before the gate was installed.
    (ROOT / 'blocked').unlink(missing_ok=True)
    report = bind_private(txn.data['old_config'], require_device=False)
    txn.event('private-restored-pending-holds')
    set_holds(txn.data['holds'])
    txn.event('restored-private')
    return report


def finish_refresh(txn):
    new = Path(txn.data.get('candidate_runtime') or
               txn.data.get('new_config', {}).get('runtime', {}).get('directory', ''))
    if txn.data['state'] in ['restored-private', 'private-restored-pending-holds',
                             'prepared', 'quiescing'] and new.parent == ROOT and new.name.startswith('runtime-'):
        rows, unknown = policy.server_processes()
        if unknown or any(row['exe'] == str(new / 'adb.backend') for row in rows):
            raise DeployError('失败刷新代仍被进程使用，保留现场')
        if new.exists() and not new.is_symlink():
            shutil.rmtree(new)
    txn.directory.rename(ROOT / ('refresh-history-' + str(time.time_ns())))
    history = sorted(ROOT.glob('refresh-history-*'), key=lambda path: path.lstat().st_mtime_ns, reverse=True)
    for old in history[5:]:
        if old.is_symlink() or not (old / 'journal.json').is_file():
            print('ADB_REFRESH_WARNING：未知刷新历史，未清理：' + str(old), file=sys.stderr)
            continue
        try:
            data = json.loads((old / 'journal.json').read_text())
        except (OSError, ValueError):
            print('ADB_REFRESH_WARNING：刷新历史不可读，未清理：' + str(old), file=sys.stderr)
            continue
        if data.get('kind') != 'private-runtime-refresh' or data.get('tool_version') != TOOL_VERSION:
            print('ADB_REFRESH_WARNING：未知刷新历史格式，未清理：' + str(old), file=sys.stderr)
            continue
        shutil.rmtree(old)


def prepare_refresh(data):
    directory = Path(tempfile.mkdtemp(prefix='.refresh-init-', dir=ROOT))
    try:
        txn = Transaction(directory)
        txn.data.update(data, kind='private-runtime-refresh')
        txn.save()
        directory.rename(ROOT / 'refresh-transaction')
        fd = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return Transaction(ROOT / 'refresh-transaction')
    finally:
        if directory.exists():
            shutil.rmtree(directory)


def clean_refresh_initialization():
    # These staging directories precede publication and cannot contain backups
    # or have changed managed files. Never interpret them as active transactions.
    for directory in ROOT.glob('.refresh-init-*'):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            print('ADB_REFRESH_WARNING：未知初始化目录，未清理：' + str(directory), file=sys.stderr)
            continue
        for path in directory.iterdir():
            info = path.lstat()
            if ((path.name != 'journal.json' and not path.name.startswith('.dsb-')) or
                    not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                    stat.S_IMODE(info.st_mode) != 0o600):
                print('ADB_REFRESH_WARNING：未知初始化残留，未清理：' + str(directory), file=sys.stderr)
                break
        else:
            journal = directory / 'journal.json'
            if journal.exists():
                try:
                    data = json.loads(journal.read_text())
                except (OSError, ValueError):
                    print('ADB_REFRESH_WARNING：初始化事务不可读，未清理：' + str(directory), file=sys.stderr)
                    continue
                if (data.get('kind') != 'private-runtime-refresh' or data.get('tool_version') != TOOL_VERSION or
                        data.get('state') != 'prepared' or data.get('files') or data.get('diversions')):
                    print('ADB_REFRESH_WARNING：未知初始化事务，未清理：' + str(directory), file=sys.stderr)
                    continue
            shutil.rmtree(directory)


def recover_refresh():
    clean_refresh_initialization()
    directory = ROOT / 'refresh-transaction'
    if not directory.exists():
        return
    if directory.is_symlink():
        raise DeployError('刷新恢复目录不可信')
    txn = Transaction(directory)
    if (txn.data.get('kind') != 'private-runtime-refresh' or txn.data.get('tool_version') != TOOL_VERSION or
            txn.data['state'] not in ['prepared', 'quiescing', 'activating', 'published', 'committed',
                                      'holds-restored', 'restored-private', 'private-restored-pending-holds']):
        raise DeployError('未知刷新事务，保留现场')
    with policy.startup_lock(ROOT):
        if txn.data['state'] in ['prepared', 'quiescing']:
            txn.restore_files()  # Old server and manifest have not been touched.
            if txn.data.get('old_healthy', False):
                (ROOT / 'blocked').unlink(missing_ok=True)
            finish_refresh(txn)
        else:
            if txn.data['state'] == 'committed':
                set_holds(txn.data['desired_holds'])
            elif txn.data['state'] == 'private-restored-pending-holds':
                set_holds(txn.data['holds'])
            elif txn.data['state'] not in ['holds-restored', 'restored-private']:
                restore_refresh(txn)
            finish_refresh(txn)
    print('ADB_REFRESH_RECOVERED_PRIVATE：保留原私有代，未退回 TCP')
    if SUP.is_file():
        start_supervisor()


def drain_old_policy():
    deadline = time.monotonic() + 30
    scripts = {str(ROOT / name).encode() for name in ['adb_policy.py', 'adb_guard.py']}
    while True:
        live = []
        for proc in Path('/proc').glob('[0-9]*'):
            try:
                if proc.stat().st_uid == os.getuid() and scripts.intersection((proc / 'cmdline').read_bytes().split(b'\0')):
                    live.append(proc.name)
            except (FileNotFoundError, ProcessLookupError):
                pass
        if not live:
            return
        if time.monotonic() >= deadline:
            raise DeployError('旧策略调用尚未结束；请结束 ADB 长会话后重跑 refresh，未强制终止客户端')
        time.sleep(.05)


def retire_legacy_backend(txn, journal, config):
    legacy_backend = ROOT / 'adb.backend'
    legacy_entry = journal.data['files'].get(str(legacy_backend))
    if legacy_backend.exists() or legacy_backend.is_symlink():
        txn.remove(legacy_backend)
    if legacy_entry is not None:
        backup = Path(legacy_entry['backup'])
        if backup.parent != ROOT or not backup.name.startswith('backup-'):
            raise DeployError('旧 backend 备份路径不可信，拒绝刷新')
        if backup.exists() or backup.is_symlink():
            txn.remove(backup)
    config['files'].pop(str(legacy_backend), None)
    config['modes'].pop(str(legacy_backend), None)
    journal.data['files'].pop(str(legacy_backend), None)


def refresh():
    require_applied()
    trusted_source()
    recover_refresh()
    policy.require_closed_default()
    old = policy.load(ROOT, verify_runtime=False)
    if old.get('tool_version') == 1:
        raise DeployError('公开 v1 请先显式 rollback 再 install；历史手机 journal 不适用公开迁移')
    if policy.pause_reason(ROOT, manual_only=True):
        raise DeployError('请先核验 check/resume 解除手动维护态，再刷新私有运行时')
    try:
        policy.check_dependencies(old)
        policy.check_backend(old)
        old_healthy = policy.pause_reason(ROOT) is None
    except (OSError, policy.PolicyError):
        old_healthy = False
    baseline = Transaction(ROOT).data['baseline']['holds']
    desired_holds = refresh_holds(old, baseline)
    holds = package_holds(desired_holds['termux'])
    candidate = 'runtime-' + str(time.time_ns())
    txn = prepare_refresh({'old_config': old, 'old_healthy': old_healthy,
                           'candidate_runtime': str(ROOT / candidate),
                           'holds': holds, 'desired_holds': desired_holds})
    try:
        new_runtime = repository_runtime(generation_name=candidate)
        launcher = compile_launcher()
    except BaseException:
        with policy.startup_lock(ROOT):
            finish_refresh(txn)
        raise
    config = {**json.loads(json.dumps(old)), **new_runtime, 'tool_version': TOOL_VERSION}
    txn.data['new_config'] = config
    txn.save()
    try:
        policy.require_closed_default()
        txn.write(ROOT / 'maintenance', b'refresh private runtime\n', 0o600)
        policy.require_closed_default()
        txn.event('quiescing')
        for identity in supervisors():
            stop(identity)
        # Old public policies must leave before replacing the code they imported.
        drain_old_policy()
        with policy.startup_lock(ROOT):
            txn.event('activating')
            stop_private(old, require_runtime=False)
            for name in ['adb_policy.py', 'adb_guard.py', 'adb_identity.py', 'adb_client.py', 'adb_relay.py']:
                path = ROOT / name
                txn.write(path, (STAGE / 'payload' / name).read_bytes(), 0o600)
                config['files'][str(path)] = policy.digest(path)
                config['modes'][str(path)] = 0o600
            for name in ['adb_policy.py', 'adb_identity.py', 'adb_client.py', 'adb_relay.py', 'bootstrap.py']:
                source = STAGE / name if name == 'bootstrap.py' else STAGE / 'payload' / name
                txn.write(H / 'dsb' / name, source.read_bytes(), 0o600)
            journal = Transaction(ROOT)
            endpoint = Path(journal.data['baseline']['rootfs']) / 'root/.dsh-phone/skills/adb_endpoint.py'
            txn.write(endpoint, (STAGE.parents[1] / 'agent/adb_endpoint.py').read_bytes(), 0o600)
            if 'runtime' not in old:
                retire_legacy_backend(txn, journal, config)
            txn.write(ROOT / 'launcher.bin', launcher, 0o600)
            for name in config['files']:
                if name in journal.data['files'] and journal.data['files'][name]['installed'] is not None and name != str(SUP):
                    txn.write(Path(name), launcher, 0o700)
                    config['files'][name] = policy.digest(name)
                    journal.data['files'][name]['installed'] = fingerprint(name)
            journal.data['tool_version'] = TOOL_VERSION
            journal.data['private_config'] = config
            journal.data['launcher_sha256'] = hashlib.sha256(launcher).hexdigest()
            retire_dependency_holds(journal.data)
            txn.write(ROOT / 'journal.json', (json.dumps(journal.data, indent=2) + '\n').encode(), 0o600)
            seal_files(config)
            journal.data['private_config'] = config
            txn.write(ROOT / 'journal.json', (json.dumps(journal.data, indent=2) + '\n').encode(), 0o600)
            txn.write(ROOT / 'manifest.json', json.dumps(config, indent=2).encode(), 0o600)
            txn.event('published')
            (ROOT / 'maintenance').unlink()
            (ROOT / 'blocked').unlink(missing_ok=True)
            report = bind_private(config, require_device=False, txn=txn)
            txn.data['report'] = report
            txn.event('committed')
            # Once old shared-library holds are released, an old v2 public-lib
            # backend is no longer a safe fallback. Keep the healthy new private
            # generation if hold readback fails, and resume only hold cleanup.
            set_holds(txn.data['desired_holds'])
            txn.event('holds-restored')
            finish_refresh(txn)
    except BaseException as original_error:
        if txn.data['state'] in ['committed', 'holds-restored']:
            raise DeployError('新私有代已启用，hold 恢复尚未完成；保持当前私有服务，'
                              '请结束其它 apt/dpkg 操作后重跑 install.sh（recover）；不要回退旧公共库 backend') from None
        # Reacquire the same lock after unwinding the failed activation.
        try:
            if txn.data['state'] in ['prepared', 'quiescing']:
                with policy.startup_lock(ROOT):
                    txn.restore_files()
                    if old_healthy:
                        (ROOT / 'blocked').unlink(missing_ok=True)
                    finish_refresh(txn)
            else:
                with policy.startup_lock(ROOT):
                    restore_refresh(txn)
                    finish_refresh(txn)
        except BaseException as restore_error:
            raise DeployError('刷新原始错误：' + str(original_error) + '；私有代恢复暂停：' +
                              str(restore_error) + '；未停止未知进程，请运行 install.sh recover') from restore_error
        raise
    finally:
        if SUP.is_file():
            start_supervisor()
    print('ADB_PRIVATE_RUNTIME_REFRESHED ' + json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


def cleanup_prepublication_staging():
    # A pre-publication staging directory has never changed an entrypoint.
    for directory in ROOT.parent.glob('dsb-adb-stage-*'):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise DeployError('未知 staging 残留，拒绝清理')
        journal = directory / 'journal.json'
        if not journal.exists():
            if any(directory.iterdir()):
                raise DeployError('无 journal 的 staging 非空，拒绝清理')
            directory.rmdir()
            continue
        if journal.is_symlink() or journal.lstat().st_uid != os.getuid() or stat.S_IMODE(journal.stat().st_mode) != 0o600:
            raise DeployError('staging journal 不可信，拒绝清理')
        data = json.loads(journal.read_text())
        if (data.get('schema') != SCHEMA or data.get('tool_version') not in PUBLIC_VERSIONS or data.get('state') != 'prepared' or
                data.get('files') or data.get('diversions')):
            raise DeployError('staging 非本版本未发布准备态，拒绝清理')
        allowed = {'journal.json', 'manifest.json', 'maintenance', 'launcher.bin',
                   'adb_policy.py', 'adb_guard.py', 'adb_identity.py', 'adb_client.py', 'adb_relay.py'}
        if any(path.is_symlink() or path.lstat().st_uid != os.getuid() or
               (path.name not in allowed and not path.name.startswith(('runtime-', '.runtime-')))
               for path in directory.iterdir()):
            raise DeployError('staging 含未知文件，拒绝清理')
        shutil.rmtree(directory)


def recover():
    reject_legacy()
    recover_refresh()
    cleanup_prepublication_staging()
    if (ROOT / 'journal.json').exists():
        txn = Transaction(ROOT)
        state = txn.data['state']
        if state in ['prepared', 'entrypoints-installed', 'activating']:
            if has_private_generation(txn):
                recover_private()
            else:
                rollback()
        elif state in ['rollback-restoring', 'rollback-tcp', 'rollback-failed']:
            if has_private_generation(txn):
                recover_private_rollback()
            else:
                rollback()
        elif state == 'rolled-back':
            rollback()


def apply():
    reject_legacy()
    if (ROOT / 'journal.json').exists():
        txn = Transaction(ROOT)
        if txn.data['state'] == 'applied':
            return refresh()
        if txn.data['state'] == 'rolled-back':
            with policy.startup_lock(ROOT):
                finish_rolled_back()
            # The ordinary 5037 server is still running. The installer must
            # bootstrap a fresh private socket before a new preflight.
            return 3
        elif txn.data['state'] in ['prepared', 'entrypoints-installed', 'activating']:
            if has_private_generation(txn):
                recover_private()
                return 0
            rollback()
            return 2
        else:
            raise DeployError('未完成的普通回滚需先运行 install.sh recover，未尝试 TCP')
    try:
        original = preflight()
    except bootstrap.Conflict as exc:
        record_state('conflict', str(exc))
        raise DeployError(str(exc)) from exc
    except PreflightSkip as exc:
        record_state('skipped', str(exc))
        print('ADB 未加固（预检跳过）：' + str(exc))
        return 0
    except (DeployError, OSError, ValueError, adb_identity.IdentityError, policy.PolicyError) as exc:
        record_state('preflight-error', str(exc))
        raise DeployError('ADB 预检失败，未加固：' + str(exc)) from exc
    except Exception as exc:
        record_state('preflight-error', '预检内部异常：' + type(exc).__name__)
        raise DeployError('预检异常，禁止把陈旧状态当成成功：' + type(exc).__name__) from exc
    try:
        cutover(original)
        return 0
    except BaseException as exc:
        if (ROOT / 'journal.json').is_file():
            try:
                recover_private()
            except BaseException as recovery:
                record_state('private-recovery-failed', f'{type(exc).__name__}; {type(recovery).__name__}')
                raise DeployError('ADB 私有切换及补完失败；未尝试普通 TCP 回滚，保留现场：' + str(recovery)) from recovery
            print('ADB 私有切换中断，已补完并验证；原错误：' + type(exc).__name__)
            return 0
        cleanup_prepublication_staging()
        raise DeployError('ADB 私有切换在发布前失败；普通入口未改：' + type(exc).__name__) from exc


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', nargs='?', default='preflight',
                        choices=['preflight', 'apply', 'refresh', 'check', 'rollback', 'recover', 'maintenance', 'resume', 'guard-legacy', 'ordinary'])
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
        elif args.operation == 'refresh':
            return refresh()
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
