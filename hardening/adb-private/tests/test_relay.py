import contextlib
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import socket
import stat
import tempfile
import threading
import time
import unittest


PAYLOAD = Path(__file__).resolve().parents[1] / 'payload'
spec = importlib.util.spec_from_file_location('adb_relay', PAYLOAD / 'adb_relay.py')
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


def run_relay(target, state_file, directory):
    os.chdir(directory)
    relay.serve(target, state_file)


class EchoTarget:
    greeting = b'local echo target ready\n'

    def __init__(self, port=0):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(('127.0.0.1', port))
        self.listener.listen(4)
        self.listener.settimeout(.05)
        self.port = self.listener.getsockname()[1]
        self.target = '127.0.0.1:' + str(self.port)
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.clients = []
        self.workers = []
        self._accepted = 0
        self.thread = threading.Thread(target=self.accept, daemon=True)
        self.thread.start()

    @property
    def accepted(self):
        with self.lock:
            return self._accepted

    def accept(self):
        while not self.stopped.is_set():
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self.lock:
                self._accepted += 1
                self.clients.append(client)
            worker = threading.Thread(target=self.echo, args=(client,), daemon=True)
            self.workers.append(worker)
            worker.start()

    def echo(self, client):
        with client:
            try:
                client.sendall(self.greeting)
                while not self.stopped.is_set():
                    data = client.recv(4096)
                    if not data:
                        return
                    client.sendall(data)
            except OSError:
                pass

    def close(self):
        self.stopped.set()
        self.listener.close()
        self.thread.join(timeout=2)
        with self.lock:
            clients = list(self.clients)
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            client.close()
        for worker in self.workers:
            worker.join(timeout=2)


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix='adb-relay-test-', dir=os.environ.get('DSB_TEST_TMPDIR') or os.environ.get('TMPDIR'))
        self.addCleanup(self.temp.cleanup)
        self.state_file = Path(self.temp.name) / 'relay.json'
        self.process = None
        self.addCleanup(self.stop_relay)

    def stop_relay(self):
        if self.process is not None:
            if self.process.is_alive():
                self.process.terminate()
            self.process.join(timeout=3)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=3)
            self.process.close()

    def echo_target(self, port=0):
        target = EchoTarget(port)
        self.addCleanup(target.close)
        return target

    def start_relay(self, target):
        self.process = multiprocessing.get_context('spawn').Process(
            target=run_relay, args=(target, self.state_file.name, self.temp.name))
        self.process.start()
        state = self.wait_state(lambda row: isinstance(row.get('socket'), str) and bool(row['socket']))
        self.assertEqual(state['pid'], self.process.pid)
        self.assertTrue(state['start'])
        self.assertEqual(state['target'], target)
        self.assertIs(state['accepted'], False)
        self.assertIs(state['dead'], False)
        self.assertEqual(stat.S_IMODE(self.state_file.stat().st_mode), 0o600)
        socket_file = self.state_file.parent / state['socket']
        self.assertEqual(socket_file, self.state_file.with_suffix('.sock'))
        self.assertTrue(stat.S_ISSOCK(socket_file.stat().st_mode))
        self.assertEqual(stat.S_IMODE(socket_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(socket_file.parent.stat().st_mode), 0o700)
        self.assertEqual(state['serial'], 'localfilesystem:' + state['socket'])
        self.endpoint = state['socket']
        return state

    def connect(self):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(1)
        try:
            # Relative paths keep long caller-selected temp roots below sockaddr_un limits.
            with contextlib.chdir(self.temp.name):
                client.connect(self.endpoint)
        except BaseException:
            client.close()
            raise
        return client

    def wait_state(self, predicate):
        deadline = time.monotonic() + 5
        last = None
        while time.monotonic() < deadline:
            self.assertTrue(self.process.is_alive(), 'Relay exited: ' + str(self.process.exitcode))
            try:
                last = json.loads(self.state_file.read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            else:
                if predicate(last):
                    self.assertEqual(stat.S_IMODE(self.state_file.stat().st_mode), 0o600)
                    return last
            time.sleep(.01)
        self.fail('Relay state did not reach the required state: ' + repr(last))

    def receive(self, client, length):
        data = bytearray()
        while len(data) < length:
            chunk = client.recv(length - len(data))
            self.assertTrue(chunk, 'Relay closed before forwarding all target bytes')
            data.extend(chunk)
        return bytes(data)

    def assert_rejected(self):
        # Connecting must still succeed: a dead relay keeps its reserved listener.
        with self.connect() as client:
            try:
                client.sendall(b'never forward a later client\n')
                data = client.recv(128)
            except (ConnectionResetError, BrokenPipeError):
                data = b''
            self.assertEqual(data, b'', 'A later client reached a live target')

    def assert_accept_count_stays(self, target, expected):
        deadline = time.monotonic() + .2
        while time.monotonic() < deadline:
            self.assertEqual(target.accepted, expected)
            time.sleep(.01)
        self.assertEqual(target.accepted, expected)

    def test_first_client_has_bidirectional_forwarding_and_private_state(self):
        target = self.echo_target()
        initial = self.start_relay(target.target)
        self.assertEqual(target.accepted, 0, 'Relay must wait for its first client before connecting')
        with self.connect() as client:
            self.assertEqual(self.receive(client, len(target.greeting)), target.greeting)
            payload = b'\x00relay request\xff\n' * 512
            client.sendall(payload)
            self.assertEqual(self.receive(client, len(payload)), payload)
            active = self.wait_state(lambda row: row.get('accepted') is True)
            self.assertIs(active['dead'], False)
            self.assertEqual(active['pid'], initial['pid'])
            self.assertEqual(active['start'], initial['start'])
            self.assertEqual(active['socket'], initial['socket'])
            self.assertEqual(active['serial'], initial['serial'])
            self.assertEqual(active['target'], target.target)
        dead = self.wait_state(lambda row: row.get('dead') is True)
        self.assertIs(dead['accepted'], True)
        self.assertEqual(target.accepted, 1)

    def test_client_eof_burns_relay_and_all_later_clients_are_rejected(self):
        target = self.echo_target()
        self.start_relay(target.target)
        with self.connect() as client:
            self.assertEqual(self.receive(client, len(target.greeting)), target.greeting)
            client.sendall(b'first connection\n')
            self.assertEqual(self.receive(client, len(b'first connection\n')), b'first connection\n')
        state = self.wait_state(lambda row: row.get('dead') is True)
        self.assertIs(state['accepted'], True)
        for _ in range(3):
            self.assert_rejected()
        self.assert_accept_count_stays(target, 1)
        self.assertTrue(self.process.is_alive(), 'Dead relay must retain its UNIX listener')

    def test_target_eof_and_replacement_on_same_port_cannot_revive_old_relay(self):
        target = self.echo_target()
        self.start_relay(target.target)
        with self.connect() as client:
            self.assertEqual(self.receive(client, len(target.greeting)), target.greeting)
            self.wait_state(lambda row: row.get('accepted') is True)
            target.close()
            try:
                eof = client.recv(1)
            except ConnectionResetError:
                eof = b''
            self.assertEqual(eof, b'')
        self.wait_state(lambda row: row.get('dead') is True)
        self.assert_rejected()
        replacement = self.echo_target(target.port)
        self.assertEqual(replacement.target, target.target)
        for _ in range(3):
            self.assert_rejected()
        self.assert_accept_count_stays(replacement, 0)
        self.assertEqual(target.accepted, 1)
        self.assertTrue(self.process.is_alive())


if __name__ == '__main__':
    unittest.main()
