"""原生客户端不能暗中启动 server；只保留线程，不允许创建本地子进程。"""
import ctypes
import errno
import os
import platform
import subprocess
import sys
import functools


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


def child_setup(parent):
    # 先设父退出保护，再复查 fork 到 prctl 之间的窗口。
    if _PRCTL is None or _PRCTL(1, 9, 0, 0, 0):
        raise OSError('ADB client parent-death protection unavailable')
    if os.getppid() != parent:
        os._exit(78)
    isolate_client()


def client_run(args, **kwargs):
    return subprocess.run(list(map(str, args)), preexec_fn=functools.partial(child_setup, os.getpid()), **kwargs)
