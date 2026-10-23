"""原生客户端不能暗中启动 server；只保留线程，不允许创建本地子进程。"""
import ctypes
from contextlib import contextmanager
from contextvars import ContextVar
import errno
import os
import platform
import subprocess
import sys
import functools
import hashlib
import json
from pathlib import Path
import stat

_verified_runtime = ContextVar('verified_runtime', default=None)
SYSTEM_ROOTS = ('/system/', '/apex/', '/vendor/', '/system_ext/', '/product/', '/odm/')


@contextmanager
def runtime_verification_scope():
    token = _verified_runtime.set(set())
    try:
        yield
    finally:
        _verified_runtime.reset(token)


def file_metadata(info):
    return {'dev': info.st_dev, 'inode': info.st_ino, 'size': info.st_size,
            'mtime_ns': info.st_mtime_ns, 'ctime_ns': info.st_ctime_ns}


def file_hash(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def check_runtime(config, *, full=False):
    """Validate a complete immutable generation before letting the linker run."""
    scope = _verified_runtime.get()
    key = (config.get('backend'), full)
    if scope is not None and key in scope:
        return
    try:
        runtime = config['runtime']
        directory = Path(runtime['directory'])
        root = Path(config['deployment_root'])
        if (directory.parent != root or not directory.name.startswith('runtime-') or
                Path(config['backend']) != directory / 'adb.backend'):
            raise ValueError('generation path mismatch')
        for path in [root, directory, directory / 'lib']:
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError('private runtime directory owner/type/mode')
            if any(p.is_symlink() for p in [path, *path.parents]):
                raise ValueError('symlink in runtime directory')
        if set(p.name for p in (directory / 'lib').iterdir()) != set(runtime['libraries']):
            raise ValueError('incomplete or extra private library')
        entries = {'adb.backend': {'sha256': runtime['backend_sha256'], 'mode': 0o700},
                   **{'lib/' + k: v for k, v in runtime['libraries'].items()}}
        metadata = runtime.get('metadata', {})
        for name, expected in entries.items():
            if name != 'adb.backend' and (Path(name).parent != Path('lib') or Path(name).name in ['', '.', '..']):
                raise ValueError('invalid library name')
            path = directory / name
            info = path.lstat()
            if info.st_uid != os.getuid():
                raise ValueError('runtime owner changed')
            sealed = metadata.get(name)
            if sealed is not None and file_metadata(info) != sealed:
                raise ValueError('private runtime metadata changed: ' + name)
            if 'target' in expected:
                target = expected['target']
                if (Path(target).name != target or target not in runtime['libraries'] or
                        'target' in runtime['libraries'][target] or not path.is_symlink() or
                        os.readlink(path) != target):
                    raise ValueError('private alias changed or escaped')
            elif (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != expected['mode'] or
                  (full or sealed is None) and file_hash(path) != expected['sha256']):
                raise ValueError('private runtime content/type/mode changed')
        entities = {n for n, v in runtime['libraries'].items() if 'target' not in v}
        if set(runtime['needed']) != entities | {'adb.backend'}:
            raise ValueError('private ELF graph incomplete')
        for name, system_path in runtime['system_libraries'].items():
            if (Path(name).name != name or not isinstance(system_path, str) or
                    (system_path != name and not system_path.startswith(SYSTEM_ROOTS))):
                raise ValueError('unverified system dependency')
        for names in runtime['needed'].values():
            if any('/' in n or n not in runtime['libraries'] and n not in runtime['system_libraries'] for n in names):
                raise ValueError('private closure incomplete; refusing public fallback')
        if scope is not None:
            scope.add(key)
            if full:
                scope.add((config['backend'], False))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise OSError('ADB_PRIVATE_RUNTIME_DRIFT: 私有库缺失或已漂移；请运行 install.sh refresh 重新加固，未回落公共库/TCP：' + str(exc)) from exc


def active_runtime(backend, *, verify=True):
    path = Path(backend)
    if path.name == 'adb.backend' and path.parent.name == 'dsb-adb':
        old = json.loads((path.parent / 'manifest.json').read_text())
        if 'runtime' in old:
            raise OSError('legacy backend is inactive; retry through managed adb')
        return None
    if path.name != 'adb.backend' or not path.parent.name.startswith('runtime-'):
        return None
    root = path.parent.parent
    manifest = root / 'manifest.json'
    info = manifest.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
        raise OSError('untrusted runtime manifest')
    config = json.loads(manifest.read_text())
    if str(path) != config.get('backend') or str(root) != config.get('deployment_root'):
        raise OSError('inactive backend generation; retry through managed adb')
    if verify:
        check_runtime(config)
    return config


def check_runtime_maps(pid, config, proc_root='/proc'):
    if 'runtime' not in config:
        return  # Read-only compatibility with the older public deployments.
    check_runtime(config)
    directory = Path(config['runtime']['directory'])
    paths = [Path(config['backend']), *[directory / 'lib' / n for n, v in config['runtime']['libraries'].items() if 'target' not in v]]
    expected = {str(p): p.stat() for p in paths}
    seen = set()
    system_seen = set()
    for line in (Path(proc_root) / str(pid) / 'maps').read_text().splitlines():
        fields = line.split(None, 5)
        if len(fields) != 6 or 'x' not in fields[1] or not fields[5].startswith('/'):
            continue
        name = fields[5]
        if name.startswith(SYSTEM_ROOTS):
            system_seen.add(Path(_check_system_mapping(fields)).name)
            continue
        info = expected.get(name)
        dev = tuple(int(n, 16) for n in fields[3].split(':'))
        if info is None or dev != (os.major(info.st_dev), os.minor(info.st_dev)) or int(fields[4]) != info.st_ino:
            raise OSError('ADB_PRIVATE_RUNTIME_LOAD_PATH: 非系统映射不属于当前私有代：' + name)
        seen.add(name)
    if seen != set(expected):
        raise OSError('ADB_PRIVATE_RUNTIME_LOAD_PATH: 私有库未完整加载；拒绝公共库回落')
    required = set(config['runtime']['system_libraries'])
    if not required <= system_seen:
        raise OSError('ADB_PRIVATE_RUNTIME_LOAD_PATH: 私有后端所需系统库未映射')


def _check_system_mapping(fields):
    name = fields[5]
    try:
        if name.endswith(' (deleted)'):
            raise ValueError('deleted mapping')
        path = Path(name).resolve(strict=True)
        info = path.stat()
        dev = tuple(int(n, 16) for n in fields[3].split(':'))
        # Android's signed APEX payloads can be owned by AID_SYSTEM (1000),
        # unlike ordinary /system and vendor image files.
        trusted_owner = info.st_uid == 0 or str(path).startswith('/apex/') and info.st_uid == 1000
        if (not str(path).startswith(SYSTEM_ROOTS) or not stat.S_ISREG(info.st_mode) or
                not trusted_owner or info.st_mode & 0o022 or
                dev != (os.major(info.st_dev), os.minor(info.st_dev)) or
                int(fields[4]) != info.st_ino):
            raise ValueError('path, owner, mode or current inode mismatch')
        return str(path)
    except (OSError, ValueError) as exc:
        raise OSError('ADB_PRIVATE_RUNTIME_LOAD_PATH: 不可信系统映射：' + name) from exc


def capture_system_maps(pid, config, proc_root='/proc'):
    """Audit current system mappings without pinning boot-dependent device numbers."""
    mappings = set()
    for line in (Path(proc_root) / str(pid) / 'maps').read_text().splitlines():
        fields = line.split(None, 5)
        if len(fields) != 6 or 'x' not in fields[1] or not fields[5].startswith(SYSTEM_ROOTS):
            continue
        mappings.add(_check_system_mapping(fields))
    required = set(config['runtime']['system_libraries'])
    if not required <= {Path(path).name for path in mappings}:
        raise OSError('required system library was not mapped by private backend')
    return mappings


class Filter(ctypes.Structure):
    _fields_ = [('code', ctypes.c_ushort), ('jt', ctypes.c_ubyte), ('jf', ctypes.c_ubyte), ('k', ctypes.c_uint)]


class Program(ctypes.Structure):
    _fields_ = [('length', ctypes.c_ushort), ('filters', ctypes.POINTER(Filter))]


_PRCTL = getattr(ctypes.CDLL(None, use_errno=True), 'prctl', None)
if _PRCTL is not None:
    _PRCTL.restype = ctypes.c_int
_ARCH = {'aarch64': (0xC00000B7, 220, []), 'x86_64': (0xC000003E, 56, [57, 58])}
_SUPPORTED = sys.platform in ['linux', 'android'] and platform.machine() in _ARCH
_arch, _clone, _forks = _ARCH.get(platform.machine(), (0, 0, []))
_instructions = [(0x20, 0, 0, 4), (0x15, 1, 0, _arch), (0x06, 0, 0, 0x50000 | errno.EPERM),
                 (0x20, 0, 0, 0), (0x15, 0, 1, 435), (0x06, 0, 0, 0x50000 | errno.ENOSYS)]
for _number in _forks:
    _instructions += [(0x15, 0, 1, _number), (0x06, 0, 0, 0x50000 | errno.EPERM)]
_instructions += [(0x15, 0, 3, _clone), (0x20, 0, 0, 16), (0x45, 1, 0, 0x10000),
                  (0x06, 0, 0, 0x50000 | errno.EPERM), (0x06, 0, 0, 0x7FFF0000)]
_filters = (Filter * len(_instructions))(*(Filter(*row) for row in _instructions))
_program = Program(len(_instructions), _filters)
_pointer = ctypes.byref(_program)


def isolate_client():
    # 只供单线程原生策略进程的 preexec_fn 使用；所有对象与 libc 符号已在父进程构建。
    if not _SUPPORTED or _PRCTL is None or _PRCTL(38, 1, 0, 0, 0) or _PRCTL(22, 2, _pointer, 0, 0):
        raise OSError('ADB client process isolation unavailable')


def parent_death_setup(parent):
    # 先设父退出保护，再复查 fork 到 prctl 之间的窗口。
    if _PRCTL is None or _PRCTL(1, 9, 0, 0, 0):
        raise OSError('ADB client parent-death protection unavailable')
    if os.getppid() != parent:
        os._exit(78)


def child_setup(parent):
    parent_death_setup(parent)
    isolate_client()


def backend_env(source=None, *, backend=None, runtime=None):
    env = dict(os.environ if source is None else source)
    for name in list(env):
        if name.startswith('LD_'):
            env.pop(name)
    config = runtime if runtime is not None else active_runtime(backend, verify=False) if backend is not None else None
    if config is not None:
        if str(backend) != config['backend']:
            raise OSError('private environment requested for a different executable')
        check_runtime(config)
        env['LD_LIBRARY_PATH'] = config['runtime']['directory'] + '/lib'
    return env


def client_run(args, **kwargs):
    kwargs['env'] = backend_env(kwargs.get('env'), backend=args[0], runtime=kwargs.pop('runtime', None))
    return subprocess.run(list(map(str, args)), preexec_fn=functools.partial(child_setup, os.getpid()), **kwargs)
