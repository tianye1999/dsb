#!/data/data/com.termux/files/usr/bin/python3
import base64
import hashlib
import http.client
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit

HOME = pathlib.Path('/data/data/com.termux/files/home')
PREFIX = '/data/data/com.termux/files/usr'
READ_CHUNK, WRITE_CHUNK = 64 * 1024, 32 * 1024
MAX_REQUEST, MAX_REPLY = 40 * 1024 * 1024, 4 * 1024 * 1024
ROUTES = {'/do', '/ask', '/step', '/cancel', '/health', '/dsh-url'}


def validate_meta(m):
    if not isinstance(m, dict):
        raise ValueError('metadata')
    for key, pattern in [('request_id', r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'),
                         ('nonce', r'[0-9a-f]{64}'), ('sha256', r'[0-9a-f]{64}')]:
        if not isinstance(m.get(key), str) or not re.fullmatch(pattern, m[key]):
            raise ValueError(key)
    for key, maximum in [('size', MAX_REQUEST), ('timeout_ms', 900000)]:
        if type(m.get(key)) is not int or not 1 <= m[key] <= maximum:
            raise ValueError(key)
    return m


def validate_request(r):
    if not isinstance(r, dict) or r.get('path') not in ROUTES or not isinstance(r.get('body'), dict):
        raise ValueError('request')
    return r


def broadcast_result(stdout):
    # All values are base64, never shell/JSON fragments parsed from am diagnostics.
    match = re.search(r'^Broadcast completed: result=1(?:, data="([A-Za-z0-9+/=]*)")?\s*$', stdout, re.M)
    if not match:
        raise ValueError('protected_receiver_rejected')
    return match.group(1) or ''


def remaining(end):
    value = end - time.monotonic()
    if value <= 0:
        raise TimeoutError('transport_timeout_state_unknown')
    return value


def broadcast(meta, op, end, **extras):
    if (HOME / '.dsb-installer-managed').is_file():
        binding = json.loads((HOME / 'dsb/adb-transport.json').read_text())
        tid = str(binding['transport_id'])
        if not tid.isdecimal() or int(tid) <= 0:
            raise ValueError('invalid_adb_transport')
        if pathlib.Path(PREFIX + '/var/lib/dsb-adb').is_dir():
            command = [PREFIX + '/bin/adb', '-t', tid]
        else:
            command = [PREFIX + '/bin/python3', '-I', str(HOME / 'dsb/adb_identity.py'), '--execute', '-t', tid]
    else:
        # 保留既有手动部署；公开安装必须走已验证 transport，不按旧地址派发。
        target_file = HOME / 'dsb/adb-target'
        target = target_file.read_text().strip() if target_file.exists() else '127.0.0.1:5555'
        if not re.fullmatch(r'[0-9.]+:[0-9]{1,5}', target):
            raise ValueError('invalid_adb_target')
        command = [PREFIX + '/bin/adb', '-s', target]
    args = [*command, 'shell', 'am', 'broadcast',
            '--receiver-foreground', '-n', 'com.dsb.assistant/.CmdReceiver',
            '-a', 'com.dsb.assistant.TRANSPORT_' + op,
            '--es', 'request_id', meta['request_id'], '--es', 'nonce', meta['nonce']]
    for key, value in extras.items():
        args.extend(['--ei' if type(value) is int else '--es', key, str(value)])
    env = dict(os.environ, ADB_SERVER_SOCKET='localfilesystem:' + PREFIX + '/var/run/dsb-adb.sock')
    result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=min(5, remaining(end)), check=True)
    return broadcast_result(result.stdout)


def read_request(meta, end):
    data = bytearray()
    while len(data) < meta['size']:
        length = min(READ_CHUNK, meta['size'] - len(data))
        piece = base64.b64decode(broadcast(meta, 'READ', end, offset=len(data), length=length), validate=True)
        if len(piece) != length:
            raise ValueError('request_chunk')
        data.extend(piece)
    if hashlib.sha256(data).hexdigest() != meta['sha256']:
        raise ValueError('request_digest')
    return validate_request(json.loads(data))


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__('localhost', timeout=timeout)
        self.socket_path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def unix_request(path, route, body, timeout):
    conn = UnixHTTP(path, timeout)
    try:
        conn.request('POST' if route != '/health' else 'GET', route,
                     json.dumps(body, ensure_ascii=False).encode(), {'Content-Type': 'application/json'})
        response = conn.getresponse()
        data = response.read(MAX_REPLY + 1)
        if len(data) > MAX_REPLY:
            raise ValueError('reply_too_large_state_unknown')
        return {'status': response.status, 'body': json.loads(data)}
    finally:
        conn.close()


def dispatch(request, timeout):
    route, body = request['path'], request['body']
    if route == '/dsh-url':
        # Never fetch tokens from a loopback port. This is a Termux-private file.
        with (HOME / '.dsb-web-url').open() as stream:
            url = stream.read(8193).strip()
        parsed = urlsplit(url)
        if len(url) > 8192 or parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.port != 3080 or parsed.username or parsed.password:
            raise ValueError('invalid_private_url')
        return {'status': 200, 'body': {'ok': True, 'url': url}}
    return unix_request(HOME / '.dsb-ipc' / 'bridge.sock', route, body, timeout)


def write_reply(meta, reply, end):
    data = json.dumps(reply, ensure_ascii=False, separators=(',', ':')).encode()
    if not 1 <= len(data) <= MAX_REPLY:
        raise ValueError('reply_limit')
    digest = hashlib.sha256(data).hexdigest()
    for offset in range(0, len(data), WRITE_CHUNK):
        broadcast(meta, 'WRITE', end, offset=offset, total=len(data), sha256=digest,
                  data=base64.b64encode(data[offset:offset + WRITE_CHUNK]).decode())


def main():
    # Only fixed script/arguments and small ASCII metadata travel in the start-service Binder transaction.
    meta = validate_meta(json.loads(sys.stdin.read(4097)))
    end = time.monotonic() + meta['timeout_ms'] / 1000
    try:
        request = read_request(meta, end)
        # Check the request is still live immediately before crossing the execution boundary.
        if broadcast(meta, 'READ', end, offset=meta['size'], length=1) != '':
            raise ValueError('request_eof')
        reply = dispatch(request, remaining(end))
    except Exception as e:
        # Do not leak token-bearing exceptions, paths, payloads, or stdout to Termux logs.
        reply = {'status': 503, 'body': {'ok': False, 'error': 'channel_unavailable_or_state_unknown', 'kind': type(e).__name__}}
    try:
        write_reply(meta, reply, end)
    except Exception:
        return 1
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        sys.exit(1)
