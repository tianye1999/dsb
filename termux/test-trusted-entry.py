import base64
import hashlib
import importlib.util
import io
import json
import math
import os
import pathlib
import subprocess
import types
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(os.environ.get('DSB_TEST_SOURCE_ROOT', pathlib.Path(__file__).resolve().parents[1]))
READ_CHUNK, WRITE_CHUNK = 64 * 1024, 32 * 1024


def load_entry(relative='termux/trusted-entry.py'):
    spec = importlib.util.spec_from_file_location('entry', ROOT / relative)
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    return entry


def request_bytes(size):
    prefix, suffix = b'{"path":"/ask","body":{"image":"', b'"}}'
    return prefix + b'A' * (size - len(prefix) - len(suffix)) + suffix


def metadata(data):
    try:
        route = json.loads(data).get('path', '/ask')
    except (ValueError, AttributeError):
        route = '/ask'
    return {'request_id': '12345678-1234-1234-1234-123456789abc', 'nonce': 'a' * 64,
            'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'timeout_ms': 30000,
            'route': route}


class SpoolBroadcast:
    def __init__(self, data):
        self.data, self.calls, self.writes = data, [], []

    def __call__(self, meta, op, end, **extras):
        self.calls.append((op, extras))
        if op == 'READ':
            offset, length = extras['offset'], extras.get('length', 12288)
            return base64.b64encode(self.data[offset:offset + length]).decode()
        if op == 'WRITE':
            self.writes.append(extras)
            return ''
        raise AssertionError('unexpected operation')

    def reply(self):
        data = bytearray()
        for chunk in self.writes:
            if chunk['offset'] != len(data):
                raise AssertionError('reply gap')
            data.extend(base64.b64decode(chunk['data'], validate=True))
        if not self.writes:
            raise AssertionError('missing reply')
        digest = hashlib.sha256(data).hexdigest()
        if any(c['total'] != len(data) or c['sha256'] != digest for c in self.writes):
            raise AssertionError('reply metadata changed')
        return json.loads(data)


class Routes(unittest.TestCase):
    def test_only_used_task_routes_remain(self):
        for filename in ['termux/trusted-entry.py', 'app/termux/trusted-entry.py']:
            entry = load_entry(filename)
            self.assertEqual(entry.ROUTES, {'/do', '/ask', '/step', '/cancel', '/health', '/dsh-url', '/report'})
            for route in ['/recap.inject', '/recap.generate', '/inject', '/generate']:
                with self.assertRaises(ValueError):
                    entry.validate_request({'path': route, 'body': {}})
            self.assertEqual(entry.validate_request({'path': '/health', 'body': {}})['path'], '/health')

    def test_report_is_read_only_and_token_bound(self):
        for filename in ['termux/trusted-entry.py', 'app/termux/trusted-entry.py']:
            entry = load_entry(filename)
            body = {'task_id': '11111111-1111-4111-8111-111111111111', 'report_token': 'a' * 64}
            self.assertEqual(entry.validate_request({'path': '/report', 'body': body})['body'], body)
            self.assertEqual(entry.validate_request({'path': '/report', 'body': dict(body, format='markdown')})['path'], '/report')
            for invalid in [{}, {'task_id': body['task_id']}, dict(body, task_id='bad'),
                            dict(body, report_token='secret'), dict(body, format='write'),
                            dict(body, command='cat /private/data')]:
                with self.subTest(filename=filename, invalid=invalid), self.assertRaises(ValueError):
                    entry.validate_request({'path': '/report', 'body': invalid})
            with self.assertRaises(ValueError):
                entry.validate_request({'path': '/report', 'body': body, 'command': 'ignored'})
            with self.assertRaises(ValueError):
                entry.validate_request({'path': '/report', 'body': dict(body, padding='x' * 9000)})

    def test_report_size_hint_and_actual_route_must_agree(self):
        entry = load_entry()
        report = {'path': '/report', 'body': {'task_id': '11111111-1111-4111-8111-111111111111',
                                             'report_token': 'a' * 64}}
        data = json.dumps(report).encode()
        self.assertEqual(entry.validate_meta(dict(metadata(data), route='/report'))['route'], '/report')
        with self.assertRaises(ValueError):
            entry.validate_meta(dict(metadata(data), route='/report', size=8193))
        with self.assertRaises(ValueError):
            entry.validate_meta(dict(metadata(data), route=None, size=8193))
        broadcast = SpoolBroadcast(data)
        with patch.object(entry, 'broadcast', broadcast), \
                patch.object(entry.sys, 'stdin', io.StringIO(json.dumps(
                    dict(metadata(data), route='/report', size=8193)))), \
                self.assertRaisesRegex(ValueError, 'report_request_size'):
            entry.main()
        self.assertEqual(broadcast.calls, [])
        with self.assertRaises(ValueError):
            entry.validate_meta(dict(metadata(data), route='/not-allowed'))
        oversized = json.dumps({'path': '/report', 'body': dict(report['body'],
                               format='json' + ' ' * 9000)}).encode()
        with self.assertRaises(ValueError):
            entry.validate_request(json.loads(oversized))
        broadcast = SpoolBroadcast(data)
        with patch.object(entry, 'broadcast', broadcast), self.assertRaisesRegex(ValueError, 'route_mismatch'):
            entry.read_request(dict(metadata(data), route='/ask'), 1e30)

    def test_packaged_receiver_is_identical(self):
        self.assertEqual((ROOT / 'termux/trusted-entry.py').read_bytes(),
                         (ROOT / 'app/termux/trusted-entry.py').read_bytes())


class Transport(unittest.TestCase):
    def setUp(self):
        self.entry = load_entry()

    def test_directional_chunk_contract(self):
        self.assertEqual(getattr(self.entry, 'READ_CHUNK', None), READ_CHUNK)
        self.assertEqual(getattr(self.entry, 'WRITE_CHUNK', None), WRITE_CHUNK)
        self.assertEqual(self.entry.MAX_REQUEST, 40 * 1024 * 1024)
        self.assertEqual(self.entry.MAX_REPLY, 4 * 1024 * 1024)

    def test_one_mib_request_needs_exactly_sixteen_data_reads(self):
        self.check_read_count(1024 * 1024, 16)

    def test_phone_regression_request_needs_exactly_nineteen_data_reads(self):
        self.check_read_count(1224867, 19)

    def check_read_count(self, size, expected):
        data = request_bytes(size)
        broadcast = SpoolBroadcast(data)
        with patch.object(self.entry, 'broadcast', broadcast):
            result = self.entry.read_request(metadata(data), 1e30)
        self.assertEqual(result, json.loads(data))
        self.assertEqual(len(broadcast.calls), expected)
        self.assertEqual(expected, math.ceil(size / READ_CHUNK))
        for index, (op, extras) in enumerate(broadcast.calls):
            self.assertEqual(op, 'READ')
            self.assertEqual(extras, {'offset': index * READ_CHUNK,
                                     'length': min(READ_CHUNK, size - index * READ_CHUNK)})

    def test_full_main_counts_include_one_expiry_read_and_one_small_reply(self):
        for size, expected in [(1024 * 1024, 18), (1224867, 21)]:
            with self.subTest(size=size):
                data = request_bytes(size)
                broadcast = SpoolBroadcast(data)
                reply = {'status': 200, 'body': {'ok': True}}
                with patch.object(self.entry, 'broadcast', broadcast), \
                        patch.object(self.entry, 'dispatch', return_value=reply) as dispatch, \
                        patch.object(self.entry.sys, 'stdin', io.StringIO(json.dumps(metadata(data)))):
                    self.assertEqual(self.entry.main(), 0)
                dispatch.assert_called_once()
                self.assertEqual(len(broadcast.calls), expected)
                self.assertEqual(broadcast.calls[-2], ('READ', {'offset': size, 'length': 1}))
                self.assertEqual(broadcast.reply(), reply)

    def test_one_mib_jpeg_base64_request_has_at_most_twenty_four_calls(self):
        data = json.dumps({'path': '/step', 'body': {
            'screenshot': base64.b64encode(bytes(1024 * 1024)).decode()}}, separators=(',', ':')).encode()
        broadcast = SpoolBroadcast(data)
        with patch.object(self.entry, 'broadcast', broadcast), \
                patch.object(self.entry, 'dispatch', return_value={'status': 200, 'body': {}}), \
                patch.object(self.entry.sys, 'stdin', io.StringIO(json.dumps(metadata(data)))):
            self.assertEqual(self.entry.main(), 0)
        self.assertLessEqual(len(broadcast.calls), 24)
        self.assertEqual(len(broadcast.calls), math.ceil(len(data) / READ_CHUNK) + 2)

    def test_short_or_oversized_chunk_fails_without_topping_up(self):
        data = request_bytes(READ_CHUNK + 10)
        for size in [0, 1, 12288, READ_CHUNK - 1, READ_CHUNK + 1]:
            with self.subTest(size=size):
                value = base64.b64encode(b'A' * size).decode()
                with patch.object(self.entry, 'broadcast', return_value=value) as broadcast:
                    with self.assertRaisesRegex(ValueError, 'request_chunk'):
                        self.entry.read_request(metadata(data), 1e30)
                self.assertEqual(broadcast.call_count, 1)

    def test_final_piece_must_match_exact_remaining_length(self):
        data = request_bytes(READ_CHUNK + 10)
        for size in [9, 11]:
            with self.subTest(size=size):
                values = [base64.b64encode(data[:READ_CHUNK]).decode(), base64.b64encode(b'A' * size).decode()]
                with patch.object(self.entry, 'broadcast', side_effect=values) as broadcast:
                    with self.assertRaisesRegex(ValueError, 'request_chunk'):
                        self.entry.read_request(metadata(data), 1e30)
                self.assertEqual(broadcast.call_count, 2)

    def test_request_digest_and_base64_corruption_are_rejected(self):
        data = request_bytes(600)
        with patch.object(self.entry, 'broadcast', SpoolBroadcast(data)):
            with self.assertRaisesRegex(ValueError, 'request_digest'):
                self.entry.read_request(dict(metadata(data), sha256='0' * 64), 1e30)
        with patch.object(self.entry, 'broadcast', return_value='not base64!'):
            with self.assertRaises(ValueError):
                self.entry.read_request(metadata(data), 1e30)

    def test_expiry_probe_nonempty_or_rejected_must_not_dispatch(self):
        data = request_bytes(600)
        for mode in ['nonempty', 'rejected']:
            with self.subTest(mode=mode):
                spool = SpoolBroadcast(data)

                def broadcast(meta, op, end, mode=mode, spool=spool, **extras):
                    if op == 'READ' and extras['offset'] == len(data):
                        if mode == 'rejected':
                            raise ValueError('protected_receiver_rejected')
                        return base64.b64encode(b'x').decode()
                    return spool(meta, op, end, **extras)

                with patch.object(self.entry, 'broadcast', side_effect=broadcast), \
                        patch.object(self.entry, 'dispatch') as dispatch, \
                        patch.object(self.entry.sys, 'stdin', io.StringIO(json.dumps(metadata(data)))):
                    self.assertEqual(self.entry.main(), 0)
                dispatch.assert_not_called()
                self.assertEqual(spool.reply()['status'], 503)

    def test_write_reassembles_exact_bytes_and_stable_digest_in_32k_chunks(self):
        reply = {'status': 200, 'body': {'text': '\u4e2d\u6587' * 40000}}
        encoded = json.dumps(reply, ensure_ascii=False, separators=(',', ':')).encode()
        spool = SpoolBroadcast(b'')
        with patch.object(self.entry, 'broadcast', spool):
            self.entry.write_reply(metadata(b'{}'), reply, 1e30)
        self.assertEqual(spool.reply(), reply)
        self.assertEqual(len(spool.calls), math.ceil(len(encoded) / WRITE_CHUNK))
        for index, chunk in enumerate(spool.writes):
            self.assertEqual(chunk['offset'], index * WRITE_CHUNK)
            self.assertEqual(base64.b64decode(chunk['data']), encoded[index * WRITE_CHUNK:(index + 1) * WRITE_CHUNK])

    def test_write_size_bounds_reject_without_broadcast(self):
        with patch.object(self.entry, 'broadcast') as broadcast:
            with self.assertRaisesRegex(ValueError, 'reply_limit'):
                self.entry.write_reply(metadata(b'{}'), {'text': 'x' * self.entry.MAX_REPLY}, 1e30)
        broadcast.assert_not_called()

    def test_32k_write_am_argv_is_below_phone_56k_and_not_shell_code(self):
        raw = b'A' * WRITE_CHUNK
        encoded = base64.b64encode(raw).decode()
        self.assertEqual(len(encoded), 43692)
        complete = types.SimpleNamespace(stdout='Broadcast completed: result=1\n')
        with patch.object(self.entry.subprocess, 'run', return_value=complete) as run:
            self.entry.broadcast(metadata(b'{}'), 'WRITE', self.entry.time.monotonic() + 30,
                                 offset=0, total=len(raw), sha256=hashlib.sha256(raw).hexdigest(), data=encoded)
        args, kwargs = run.call_args.args[0], run.call_args.kwargs
        self.assertIsInstance(args, list)
        self.assertLess(sum(len(a.encode()) + 1 for a in args), 56 * 1024)
        self.assertNotIn('shell', kwargs)
        self.assertNotIn('-c', args)
        self.assertNotIn('sh', args)
        self.assertEqual(args[:7], [self.entry.PREFIX + '/bin/adb', '-s', '127.0.0.1:5555', 'shell', 'am', 'broadcast', '--receiver-foreground'])
        self.assertEqual(kwargs['env']['ADB_SERVER_SOCKET'], 'localfilesystem:' + self.entry.PREFIX + '/var/run/dsb-adb.sock')
        self.assertTrue(kwargs['check'])
        self.assertTrue(kwargs['capture_output'])
        self.assertLessEqual(kwargs['timeout'], 5)

    def test_broadcast_timeout_is_bounded_and_expired_deadline_does_not_spawn(self):
        complete = types.SimpleNamespace(stdout='Broadcast completed: result=1\n')
        with patch.object(self.entry.time, 'monotonic', return_value=100), \
                patch.object(self.entry.subprocess, 'run', return_value=complete) as run:
            self.entry.broadcast(metadata(b'{}'), 'READ', 101.25, offset=0, length=64)
            self.assertEqual(run.call_args.kwargs['timeout'], 1.25)
            run.reset_mock()
            with self.assertRaises(TimeoutError):
                self.entry.broadcast(metadata(b'{}'), 'READ', 100, offset=0, length=64)
            run.assert_not_called()
        with patch.object(self.entry.subprocess, 'run', side_effect=subprocess.TimeoutExpired('fixture', 1)):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.entry.broadcast(metadata(b'{}'), 'READ', self.entry.time.monotonic() + 30, offset=0, length=64)

    def test_receiver_output_parser_is_exact_and_rejects_diagnostic_fragments(self):
        self.assertEqual(self.entry.broadcast_result('Broadcast completed: result=1, data="QQ=="\n'), 'QQ==')
        self.assertEqual(self.entry.broadcast_result('Broadcast completed: result=1\n'), '')
        for stdout in ['Broadcast completed: result=0\n', 'Broadcast completed: result=1, data="$(id)"\n', 'prefix Broadcast completed: result=1\n']:
            with self.subTest(stdout=stdout):
                with self.assertRaises(ValueError):
                    self.entry.broadcast_result(stdout)


if __name__ == '__main__':
    unittest.main()
