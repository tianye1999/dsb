"""缺省 Termux 缺静态 CRT/libc；只取固定官方 NDK 对象并校验 Git blob。"""
import base64
import hashlib
import os
from pathlib import Path
import platform
import tempfile
import time
import urllib.error
import urllib.request

LIB_COMMIT = '0e26c931f59bf2a77e53d0550964e683c2365cd2'
CRT_COMMIT = '77eba0d553f8f58557f99fa98f327eb5f46e0c8c'
LIB_BASE = f'https://android.googlesource.com/platform/prebuilts/ndk/+/{LIB_COMMIT}/platform/sysroot/usr/lib'
CRT_BASE = f'https://android.googlesource.com/toolchain/prebuilts/ndk/r27/+/{CRT_COMMIT}'
PINS = {
    'aarch64': ('aarch64-linux-android', {
        'libc.a': '8540d0d434113d61b250cbf524746037ae7f461d',
        'libdl.a': '19e4d7cb6851b1c73e8e5ba1b674e2aa23f0d2bb',
        'libm.a': '1d155a5de05dabfd5a3adfacb81506ae0ac3322d',
        'crtbegin_static.o': 'c34c407f7d75324c6164bbd2c06e85ddcfb9e434'}),
    'x86_64': ('x86_64-linux-android', {
        'libc.a': '08303315cfd57bb1d1d1e70eb03df942d8855b35',
        'libdl.a': '2ce09bf249efe8d96d559bcf6dbee3a5cc6ee560',
        'libm.a': 'e90a7499a06f002c39fda8dd354c8a946942d85f',
        'crtbegin_static.o': 'ccea6cd3bb44b38c13c6a3eb9df3527024a6af14'}),
}
SHA256 = {
    'aarch64': {
        'libc.a': 'de7c0e37b3b16392937ca3bec76a7f4acbe3df65585b0bdbce4d1616b6d085e8',
        'libdl.a': '767160513d20f1a88e77b798c6f7437ec46d834815a62b09c51240a8ade4d28d',
        'libm.a': 'dd99450402f54803dd03cef5f4c5243d07154e392c1991c95377f50681ee36cd',
        'crtbegin_static.o': '27ce7bd74c94e323494036018b9b8521af98ad9148a684d9077aa25af6fcfa25'},
    'x86_64': {
        'libc.a': 'be982fead4075f4bfc5c545696e551d8c19a838a6ed5697f350169362be302aa',
        'libdl.a': 'd91f1a873cb80c55f1cc328dfd4488848fe0dfeb210c3b1385e3c2e9c7f0eae4',
        'libm.a': '738000a19883f89e0e6767090625532e89d876487414eeb77f7d454d1dbe5d65',
        'crtbegin_static.o': '53d19fd0111bd4508df4e139f7f1101d0a0a3cae058c97f11ba4e876c44ebef6'},
}
NOTICE_SHA256 = '4d5224d1c0b54ffa88b0dc0088191638fdbb7227835095dfb98f7f66b416a323'


class BuildError(RuntimeError):
    pass


def blob(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def fetch(path, url, expected, sha256=False):
    def matches(data):
        return (hashlib.sha256(data).hexdigest() if sha256 else blob(data)) == expected
    path = Path(path)
    if path.is_file() and not path.is_symlink() and matches(path.read_bytes()):
        return
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url + '?format=TEXT', timeout=60) as response:
                data = base64.b64decode(response.read(24 * 1024 * 1024))
            if not matches(data):
                raise BuildError('官方构建材料校验不匹配')
            fd, tmp = tempfile.mkstemp(dir=path.parent)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            return
        except (OSError, ValueError, urllib.error.URLError, BuildError):
            if attempt == 2:
                raise BuildError('FETCH_INPUT_UNAVAILABLE：NDK 材料下载/校验失败，未修改 ADB 入口') from None
            time.sleep(2)


def prepare(cache):
    machine = platform.machine()
    try:
        triple, pins = PINS[machine]
    except KeyError:
        raise BuildError('暂不支持此 CPU 的静态 launcher') from None
    directory = Path(cache) / ('ndk-pinned-' + triple)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink():
        raise BuildError('构建缓存不能是符号链接')
    directory.chmod(0o700)
    bundled = Path(__file__).resolve().parent / 'ndk' / triple
    for name, expected in pins.items():
        if (bundled / name).is_file() and not (bundled / name).is_symlink():
            data = (bundled / name).read_bytes()
            if hashlib.sha256(data).hexdigest() != SHA256[machine][name]:
                raise BuildError('BUNDLED_INPUT_INVALID：发布包 NDK SHA-256 不匹配')
            (directory / name).write_bytes(data)
            (directory / name).chmod(0o600)
            continue
        url = (f'{CRT_BASE}/toolchains/llvm/prebuilt/linux-x86_64/sysroot/usr/lib/{triple}/24/{name}'
               if name.endswith('.o') else f'{LIB_BASE}/{triple}/{name}')
        fetch(directory / name, url, expected)
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != SHA256[machine][name]:
            raise BuildError('FETCH_INPUT_INVALID：NDK SHA-256 不匹配')
    if (bundled / 'NOTICE').is_file():
        data = (bundled / 'NOTICE').read_bytes()
        if hashlib.sha256(data).hexdigest() != NOTICE_SHA256:
            raise BuildError('发布包 NOTICE SHA-256 不匹配')
        (directory / 'NOTICE').write_bytes(data)
    else:
        fetch(directory / 'NOTICE', CRT_BASE + '/NOTICE', NOTICE_SHA256, sha256=True)
    return directory
