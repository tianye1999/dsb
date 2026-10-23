#!/usr/bin/env python3
"""安装时的文件配置；密钥只从 stdin 读取，不输出内容。"""
import argparse
import ipaddress
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import urlsplit, urlunsplit


def atomic(path, data, mode=0o600):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('拒绝写入符号链接')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def save_key(root, value, replace=False):
    directory = Path(root) / '.dsh'
    if directory.is_symlink():
        raise ValueError('密钥目录不能是符号链接')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    path = directory / '.deepseek-key'
    if path.is_symlink():
        raise ValueError('密钥文件不能是符号链接')
    if path.exists() and not stat.S_ISREG(path.stat().st_mode):
        raise ValueError('密钥必须是普通文件')
    if path.exists() and path.stat().st_size and not replace:
        path.chmod(0o600)
        return
    if not value or len(value) > 1024 or any(c.isspace() for c in value):
        raise ValueError('Key 不能为空、含空白或超过 1024 字符')
    atomic(path, (value + '\n').encode())


def allow_external(path):
    path = Path(path)
    text = path.read_text() if path.exists() else ''
    backup = path.with_name(path.name + '.pre-dsb')
    if path.exists() and not backup.exists():
        atomic(backup, path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
    lines = [line for line in text.splitlines()
             if not re.match(r'^\s*allow-external-apps\s*=', line)]
    atomic(path, ('\n'.join(lines + ['allow-external-apps = true']) + '\n').encode())


def ubuntu_https(root, ca):
    root = Path(root).resolve()
    apt = root / 'etc/apt'
    official = False
    def upgrade(match):
        nonlocal official
        url = urlsplit(match.group())
        if url.netloc not in {'ports.ubuntu.com', 'archive.ubuntu.com', 'security.ubuntu.com'}:
            return match.group()
        official = True
        return urlunsplit(url._replace(scheme='https'))
    paths = [apt / 'sources.list', *(apt / 'sources.list.d').glob('*.list'),
             *(apt / 'sources.list.d').glob('*.sources')]
    for path in paths:
        if not path.exists():
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('APT 源目录不能越过容器根目录')
        before = path.read_text()
        after = ''.join(line if line.lstrip().startswith('#') else
                        re.sub(r'https?://[^\s]+', upgrade, line) for line in before.splitlines(keepends=True))
        if after != before:
            backup = path.with_name(path.name + '.pre-dsb')
            if not backup.exists():
                atomic(backup, before.encode(), stat.S_IMODE(path.stat().st_mode))
            atomic(path, after.encode(), stat.S_IMODE(path.stat().st_mode))
    certificate = root / 'etc/ssl/certs/ca-certificates.crt'
    if official and not certificate.exists():
        if not certificate.resolve().is_relative_to(root):
            raise ValueError('CA 路径不能越过容器根目录')
        # 最小 Ubuntu 镜像首启无 CA，先复用 Termux 的受信 CA；apt 包随后正常接管。
        atomic(certificate, Path(ca).read_bytes(), 0o644)


def endpoint(value, local=False):
    host, port = value.strip().rsplit(':', 1)
    address = ipaddress.ip_address(host)
    if address.version != 4 or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError('请输入 IPv4:端口')
    if local:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as conn:
            conn.connect((str(address), int(port)))
            if conn.getsockname()[0] != str(address):
                raise ValueError('地址不属于本机')
    return f'{address}:{int(port)}'


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['key', 'set-key', 'external', 'endpoint', 'local-endpoint', 'ubuntu-https'])
    parser.add_argument('path')
    args = parser.parse_args()
    try:
        if args.action in ['key', 'set-key']:
            import sys
            save_key(Path(args.path), sys.stdin.readline(1026).rstrip('\r\n'), replace=args.action == 'set-key')
        elif args.action == 'external':
            allow_external(args.path)
        elif args.action == 'ubuntu-https':
            ubuntu_https(args.path, Path(os.environ['PREFIX']) / 'etc/tls/cert.pem')
        else:
            print(endpoint(args.path, local=args.action == 'local-endpoint'))
    except (ValueError, OSError):
        raise SystemExit('配置未写入：输入格式或文件权限不符合要求（不打印输入）')
