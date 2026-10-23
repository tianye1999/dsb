"""每个 ADB transport 只转发一次实际连接；断线后保留端口并拒绝重连。"""
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import sys


def process_start(pid):
    path = Path('/proc') / str(pid) / 'stat'
    if Path('/proc/self/stat').exists():
        return path.read_text().rsplit(')', 1)[1].split()[19]
    # 仅供非 Android 主机的离线 TCP 测试使用。
    return subprocess.check_output(['ps', '-p', str(pid), '-o', 'lstart='], text=True,
                                   env={k: v for k, v in os.environ.items() if not k.startswith('LD_')}).strip()


def write_state(path, data):
    path = Path(path)
    temp = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def serve(target, state_file):
    host, port = target.rsplit(':', 1)
    endpoint = str(Path(state_file).with_suffix('.sock'))
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(endpoint)
    os.chmod(endpoint, 0o600)
    listener.listen(8)
    row = {'pid': os.getpid(), 'start': process_start(os.getpid()),
           'socket': endpoint, 'serial': 'localfilesystem:' + endpoint,
           'target': target, 'accepted': False, 'dead': False, 'script': str(Path(__file__).resolve())}
    write_state(state_file, row)
    with listener:
        while True:
            client, _ = listener.accept()
            with client:
                if row['accepted']:
                    continue
                row['accepted'] = True
                write_state(state_file, row)
                try:
                    with socket.create_connection((host, int(port)), timeout=5) as backend:
                        backend.settimeout(None)
                        while True:
                            ready, _, _ = select.select([client, backend], [], [], 1)
                            # 后继客户端不能进入这条已核验连接。
                            for source in ready:
                                data = source.recv(65536)
                                if not data:
                                    raise EOFError
                                (backend if source is client else client).sendall(data)
                except (OSError, EOFError):
                    pass
                finally:
                    row['dead'] = True
                    write_state(state_file, row)


if __name__ == '__main__':
    for name in list(os.environ):
        if name.startswith('LD_'):
            os.environ.pop(name)
    if len(sys.argv) != 3:
        sys.exit(2)
    serve(sys.argv[1], sys.argv[2])
