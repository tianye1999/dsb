from contextlib import chdir, contextmanager, nullcontext
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'payload'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import adb_identity as identity
import adb_policy as policy


class BindingTests(unittest.TestCase):
    def test_container_executor_restores_environment_before_reading_prefix(self):
        home = Path('/fixture/files/home')
        with patch.object(identity, '__file__', str(home / 'dsb/adb_identity.py')), \
             patch.object(sys, 'argv', ['adb_identity.py', '--execute', '-t', '7', 'shell', 'true']), \
             patch.dict(os.environ, {'HOME': '/root'}, clear=True), \
             patch.object(identity, 'execute_bound', return_value=0) as execute:
            self.assertEqual(identity.main(), 0)
            execute.assert_called_once_with(home.parent / 'usr', home / 'dsb/adb-transport.json',
                                            ['-t', '7', 'shell', 'true'])
            self.assertEqual(os.environ['HOME'], str(home))

    def test_bootstrap_dispatch_uses_receipt_socket_not_tcp_or_environment(self):
        import bootstrap
        with tempfile.TemporaryDirectory() as d:
            prefix = Path(d) / 'usr'
            record = Path(d) / 'dsb/adb-transport.json'
            record.parent.mkdir(mode=0o700)
            endpoint = prefix / 'var/run/dsb-adb-bootstrap/server.sock'
            data = {'generation': {'socket': 'localfilesystem:' + str(endpoint)},
                    'serial': 'localfilesystem:/fixture/relay.sock', 'target': '127.0.0.1:45555'}
            for changed in [False, True]:
                with self.subTest(changed=changed), \
                     patch.object(identity, 'binding_lease', return_value=nullcontext()), \
                     patch.object(identity, 'read_binding', return_value=data), \
                     patch.object(identity, 'verified_id', side_effect=identity.IdentityError('changed') if changed else None,
                                  return_value=7) as verified, \
                     patch.object(policy, 'require_closed_default') as closed, \
                     patch.object(policy, 'private_dir') as private, \
                     patch.object(policy, 'socket_info') as socket_info, \
                     patch.object(bootstrap, 'tcp_owner') as tcp, \
                     patch.object(identity, 'client_run', return_value=identity.subprocess.CompletedProcess([], 0)) as run, \
                     patch.dict(os.environ, ADB_SERVER_SOCKET='tcp:foreign.invalid:5037'):
                    self.assertEqual(identity.execute_bound(prefix, record, ['-t', '7', 'shell', 'true']),
                                     78 if changed else 0)
                    tcp.assert_not_called()
                    closed.assert_called_once()
                    private.assert_called_once_with(endpoint.parent)
                    socket_info.assert_called_once_with(endpoint)
                    raw = [str(prefix / 'bin/adb'), '-L', 'localfilesystem:' + str(endpoint)]
                    verified.assert_called_once_with(raw, record)
                    if changed:
                        run.assert_not_called()
                    else:
                        self.assertEqual(run.call_args.args[0], [*raw, '-t', '7', 'shell', 'true'])

    def relay_descriptor(self, directory, name='relay'):
        state = Path(directory) / (name + '.json')
        return {'pid': 4242, 'start': '101', 'socket': str(state.with_suffix('.sock')),
                'target': '127.0.0.1:45555', 'state_file': str(state),
                'script': '/fixture/adb_relay.py'}

    def registered_relay(self, directory):
        registry = Path(directory) / 'adb-relays'
        registry.mkdir(mode=0o700)
        descriptor = self.relay_descriptor(registry, 'old')
        state = Path(descriptor['state_file'])
        state.write_text(json.dumps({**descriptor, 'accepted': True, 'dead': True}))
        state.chmod(0o600)
        return descriptor

    def test_failed_challenge_disconnects_and_removes_binding(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'transport.json'
            descriptor = self.relay_descriptor(d)
            serial = 'localfilesystem:' + descriptor['socket']
            identity.save_binding(path, descriptor['target'], 3, {'pid': 1}, descriptor)
            with patch.object(identity, 'challenge_transport', side_effect=identity.IdentityError('no shell'), create=True), \
                 patch.object(identity, 'transport_id', return_value=3, create=True), \
                 patch.object(identity, 'generation', return_value={'pid': 1}, create=True), \
                 patch.object(identity, 'relay_lease', return_value=nullcontext()), \
                 patch.dict(os.environ, ADB_SERVER_SOCKET='localfilesystem:/test.sock'), \
                 patch.object(identity, 'client_run') as run, self.assertRaises(identity.IdentityError):
                identity.authenticated_shell(['adb'], serial, record=path, relay=descriptor)
            run.assert_called_once_with(['adb', 'disconnect', serial], stdin=identity.subprocess.DEVNULL,
                                        capture_output=True, timeout=3)
            self.assertFalse(path.exists())

    def test_binding_is_private_and_id_generation_must_stay_identical(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'transport.json'
            descriptor = self.relay_descriptor(d)
            serial = 'localfilesystem:' + descriptor['socket']
            with patch.object(identity, 'challenge_transport', return_value='2000', create=True), \
                 patch.object(identity, 'transport_id', return_value=7, create=True), \
                 patch.object(identity, 'relay_lease', return_value=nullcontext()), \
                 patch.object(identity, 'relay_state', return_value={'accepted': True, 'dead': False}), \
                 patch.object(identity, 'generation', return_value={'pid': 1, 'start': '42'}, create=True):
                self.assertEqual(identity.authenticated_shell(['adb'], serial, record=path, relay=descriptor), '2000')
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                binding = identity.read_binding(path)
                self.assertEqual(binding['version'], 2)
                self.assertEqual(binding['target'], descriptor['target'])
                self.assertEqual(binding['serial'], serial)
                self.assertEqual(binding['relay'], descriptor)
                self.assertEqual(identity.verified_id(['adb'], path), 7)
                with patch.object(identity, 'transport_id', return_value=8):
                    with self.assertRaises(identity.IdentityError):
                        identity.verified_id(['adb'], path)
                with patch.object(identity, 'generation', return_value={'pid': 2, 'start': '42'}):
                    with self.assertRaises(identity.IdentityError):
                        identity.verified_id(['adb'], path)

    def test_policy_rewrites_only_matching_serial_and_rejects_other_ids(self):
        config = {'backend': 'adb', 'socket': '/p/socket', 'target_file': '/fixture/dsb/adb-target'}
        target = '127.0.0.1:45555'
        serial = 'localfilesystem:/fixture/relay.sock'
        with patch.object(identity, 'verified_id', return_value=7, create=True), \
             patch.object(identity, 'read_binding', return_value={'version': 2, 'serial': serial, 'target': target}, create=True):
            for selected in [serial, target]:
                self.assertEqual(policy.bind_command(['-s', selected, 'shell', 'true'], config),
                                 ['-t', '7', 'shell', 'true'])
            for args in [['-s', 'fake:1', 'shell', 'true'], ['-t', '8', 'shell', 'true'], ['-e', 'shell', 'true']]:
                with self.subTest(args=args), self.assertRaises(policy.PolicyError):
                    policy.bind_command(args, config)

    def test_v2_dead_relay_rejects_even_matching_transport_and_server_generation(self):
        with tempfile.TemporaryDirectory() as d, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
            descriptor = self.relay_descriptor(d)
            state = Path(descriptor['state_file'])
            row = {**descriptor, 'accepted': True, 'dead': True}
            state.write_text(json.dumps(row))
            state.chmod(0o600)
            with chdir(d):
                endpoint.bind('relay.sock')
            Path(descriptor['socket']).chmod(0o600)
            record = Path(d) / 'transport.json'
            server = {'pid': 1, 'start': '42'}
            identity.save_binding(record, descriptor['target'], 7, server, descriptor)

            proc_root = MagicMock()
            proc = proc_root.__truediv__.return_value
            proc.stat.return_value.st_uid = os.getuid()
            proc.__truediv__.return_value.read_bytes.return_value = (
                os.fsencode(descriptor['script']) + b'\0' + os.fsencode(state) + b'\0')
            def local_path(*parts):
                return proc_root if parts == ('/proc',) else Path(*parts)
            with patch.object(identity, 'Path', side_effect=local_path), \
                 patch.object(identity.adb_relay, 'process_start', return_value=descriptor['start']), \
                 patch.object(identity, 'generation', return_value=server), \
                 patch.object(identity, 'transport_id', return_value=7) as transport:
                self.assertIs(identity.relay_state(descriptor)['dead'], True)
                with self.assertRaises(identity.IdentityError):
                    identity.verified_id(['adb'], record)
                transport.assert_not_called()
                row['dead'] = False
                state.write_text(json.dumps(row))
                self.assertEqual(identity.verified_id(['adb'], record), 7)

    def test_same_transport_id_cannot_hide_changed_server_start_generation(self):
        with tempfile.TemporaryDirectory() as d:
            descriptor = self.relay_descriptor(d)
            record = Path(d) / 'transport.json'
            server = {'pid': 1, 'start': '42'}
            changed = {'pid': 1, 'start': '43'}
            identity.save_binding(record, descriptor['target'], 7, server, descriptor)
            for snapshots in [[changed], [server, changed]]:
                with self.subTest(generations=snapshots), \
                     patch.object(identity, 'relay_state', return_value={'accepted': True, 'dead': False}), \
                     patch.object(identity, 'transport_id', return_value=7), \
                     patch.object(identity, 'generation', side_effect=snapshots), \
                     self.assertRaises(identity.IdentityError):
                    identity.verified_id(['adb'], record)

    def test_registered_relay_is_retired_without_binding_before_new_launch(self):
        with tempfile.TemporaryDirectory() as d:
            record = Path(d) / 'transport.json'
            old = self.registered_relay(d)
            new = self.relay_descriptor(Path(d) / 'adb-relays', 'new')
            events = []
            def retire(descriptor):
                events.append(('retire', descriptor))
            def launch(*args):
                events.append(('launch', new))
                return new
            with patch.object(identity, 'generation', return_value={'pid': 1, 'start': '42'}), \
                 patch.object(identity, 'client_run', return_value=identity.subprocess.CompletedProcess([], 0)), \
                 patch.object(identity, 'retire_relay', side_effect=retire) as retired, \
                 patch.object(identity, 'launch_relay', side_effect=launch), \
                 patch.object(identity, 'transport_id', return_value=7), \
                 patch.object(identity, 'authenticated_shell', return_value='2000') as authenticated:
                self.assertFalse(record.exists())
                result = identity.connect_verified(['adb'], new['target'], record, Path(new['script']))
            self.assertEqual(result, '2000')
            retired.assert_called_once_with(old)
            self.assertEqual(events, [('retire', old), ('launch', new)])
            authenticated.assert_called_once_with(
                ['adb'], 'localfilesystem:' + new['socket'], record=record, relay=new)

    def test_old_disconnect_timeout_still_retires_registry_and_never_launches(self):
        with tempfile.TemporaryDirectory() as d:
            record = Path(d) / 'transport.json'
            old = self.registered_relay(d)
            with patch.object(identity, 'generation', return_value={'pid': 1, 'start': '42'}), \
                 patch.object(identity, 'client_run', side_effect=identity.subprocess.TimeoutExpired('fixture', 3)), \
                 patch.object(identity, 'retire_relay') as retired, \
                 patch.object(identity, 'launch_relay') as launch, \
                 patch.object(identity, 'authenticated_shell') as authenticated, \
                 self.assertRaises(identity.subprocess.TimeoutExpired):
                identity.connect_verified(['adb'], old['target'], record, Path(old['script']))
            retired.assert_called_once_with(old)
            launch.assert_not_called()
            authenticated.assert_not_called()
            self.assertFalse(record.exists())

    def test_failed_new_connection_retires_relay_even_when_cleanup_disconnect_times_out(self):
        with tempfile.TemporaryDirectory() as d:
            record = Path(d) / 'transport.json'
            descriptor = self.relay_descriptor(d)
            serial = 'localfilesystem:' + descriptor['socket']
            def client(command, **kwargs):
                if command[1] == 'connect':
                    return identity.subprocess.CompletedProcess(command, 1)
                if command[1:] == ['disconnect', serial]:
                    raise identity.subprocess.TimeoutExpired(command, 3)
                return identity.subprocess.CompletedProcess(command, 0)
            with patch.object(identity, 'generation', return_value={'pid': 1, 'start': '42'}), \
                 patch.object(identity, 'launch_relay', return_value=descriptor), \
                 patch.object(identity, 'client_run', side_effect=client), \
                 patch.object(identity, 'retire_relay') as retired, \
                 patch.object(identity, 'authenticated_shell') as authenticated, \
                 self.assertRaises((identity.IdentityError, identity.subprocess.TimeoutExpired)):
                identity.connect_verified(['adb'], descriptor['target'], record, Path(descriptor['script']))
            retired.assert_called_once_with(descriptor)
            authenticated.assert_not_called()
            self.assertFalse(record.exists())

    def test_reused_pid_or_zombie_is_unlinked_without_signal_and_allows_reconnect(self):
        for process_state, start in [('S', '102'), ('Z', '101')]:
            with self.subTest(process_state=process_state, start=start), \
                 tempfile.TemporaryDirectory() as d, \
                 socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
                old = self.registered_relay(d)
                state = Path(old['state_file'])
                socket_file = Path(old['socket'])
                with chdir(state.parent):
                    endpoint.bind(socket_file.name)
                socket_file.chmod(0o600)
                proc_root = MagicMock()
                proc_stat = proc_root.__truediv__.return_value.__truediv__.return_value
                proc_stat.read_text.return_value = (
                    str(old['pid']) + ' (fixture relay) ' + ' '.join([process_state] + ['0'] * 18 + [start]))
                def local_path(*parts):
                    return proc_root if parts == ('/proc',) else Path(*parts)
                with patch.object(identity, 'Path', side_effect=local_path), \
                     patch.object(identity.os, 'kill') as kill:
                    identity.retire_relay(old)
                kill.assert_not_called()
                self.assertFalse(state.exists())
                self.assertFalse(socket_file.exists())

                record = Path(d) / 'transport.json'
                new = self.relay_descriptor(state.parent, 'new')
                with patch.object(identity, 'generation', return_value={'pid': 1, 'start': '42'}), \
                     patch.object(identity, 'client_run', return_value=identity.subprocess.CompletedProcess([], 0)), \
                     patch.object(identity, 'launch_relay', return_value=new) as launch, \
                     patch.object(identity, 'retire_relay') as retire, \
                     patch.object(identity, 'transport_id', return_value=7), \
                     patch.object(identity, 'authenticated_shell', return_value='2000'):
                    self.assertEqual(identity.connect_verified(
                        ['adb'], new['target'], record, Path(new['script'])), '2000')
                launch.assert_called_once_with(new['target'], record, Path(new['script']))
                retire.assert_not_called()

    def test_failed_shared_challenge_with_disconnect_false_preserves_binding_and_relay(self):
        with tempfile.TemporaryDirectory() as d:
            record = Path(d) / 'transport.json'
            descriptor = self.relay_descriptor(d)
            server = {'pid': 1, 'start': '42'}
            identity.save_binding(record, descriptor['target'], 7, server, descriptor)
            before = record.read_bytes()
            with patch.object(identity, 'generation', return_value=server), \
                 patch.object(identity, 'relay_lease', return_value=nullcontext()), \
                 patch.object(identity, 'verified_id', return_value=7), \
                 patch.object(identity, 'challenge_transport', side_effect=identity.IdentityError('fixture failure')), \
                 patch.object(identity, 'client_run') as client, \
                 patch.object(identity, 'retire_relay') as retire, \
                 self.assertRaises(identity.IdentityError):
                identity.authenticated_shell(
                    ['adb'], descriptor['target'], record=record, bound=True, disconnect=False)
            self.assertTrue(record.exists(), 'Shared health failure must leave revocation to the exclusive lock')
            self.assertEqual(record.read_bytes(), before)
            self.assertEqual(record.stat().st_mode & 0o777, 0o600)
            client.assert_not_called()
            retire.assert_not_called()

    def test_failed_shared_health_check_defers_all_revocation_until_exclusive_lock(self):
        class ExclusiveBoundary(Exception):
            pass

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            record = root / 'transport.json'
            descriptor = self.relay_descriptor(d)
            identity.save_binding(record, descriptor['target'], 7, {'pid': 1, 'start': '42'}, descriptor)
            before = record.read_bytes()
            events = []
            @contextmanager
            def lock(directory, shared=False):
                self.assertEqual(directory, root)
                if not shared:
                    events.append('exclusive-request')
                    self.assertEqual(record.read_bytes(), before)
                    client.assert_not_called()
                    retire.assert_not_called()
                    launch.assert_not_called()
                    raise ExclusiveBoundary
                events.append('shared-enter')
                try:
                    yield
                finally:
                    events.append('shared-exit')
            def fail_health(*args, **kwargs):
                self.assertEqual(events, ['shared-enter'])
                self.assertIs(kwargs.get('disconnect'), False)
                self.assertEqual(record.read_bytes(), before)
                raise identity.IdentityError('fixture health failure')
            with patch.object(policy, 'startup_lock', side_effect=lock), \
                 patch.object(identity, 'authenticated_shell', side_effect=fail_health) as authenticated, \
                 patch.object(identity, 'client_run') as client, \
                 patch.object(identity, 'retire_relay') as retire, \
                 patch.object(identity, 'launch_relay') as launch, \
                 self.assertRaises(ExclusiveBoundary):
                identity.connect_verified(
                    ['adb'], descriptor['target'], record, Path(descriptor['script']), root=root)
            self.assertEqual(events, ['shared-enter', 'shared-exit', 'exclusive-request'])
            self.assertEqual(record.read_bytes(), before)
            authenticated.assert_called_once_with(
                ['adb'], descriptor['target'], record=record, bound=True, disconnect=False)


if __name__ == '__main__':
    unittest.main()
