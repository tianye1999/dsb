"""普通传输的设备身份不能由监听 inode 代替。"""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('tcp_owner_cli', BASE / 'tcp-owner.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class OrdinaryIdentityTests(unittest.TestCase):
    def test_verified_owner_but_fake_endpoint_is_failure_without_mutation(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / 'adb-target'
            target.write_text('127.0.0.1:47123\n')
            before = target.read_bytes()
            with patch.object(helper, 'tcp_owner') as owner, \
                 patch.object(helper, 'authenticated_shell', side_effect=RuntimeError('nonce mismatch')) as identity:
                with self.assertRaisesRegex(RuntimeError, 'nonce mismatch'):
                    helper.check_saved_target(Path('/native/adb'), target)
                owner.assert_called_once()
                identity.assert_called_once_with([Path('/native/adb'), '-L', 'tcp:localhost:5037'], '127.0.0.1:47123',
                                                 record=target.with_name('adb-transport.json'), bound=True)
            self.assertEqual(target.read_bytes(), before)

    def test_verified_owner_and_identity_pass(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / 'adb-target'
            target.write_text('127.0.0.1:47123\n')
            with patch.object(helper, 'tcp_owner'), \
                 patch.object(helper, 'authenticated_shell', return_value='2000') as identity:
                helper.check_saved_target(Path('/native/adb'), target)
                identity.assert_called_once()

    def test_foreign_owner_never_receives_identity_request(self):
        with patch.object(helper, 'tcp_owner', side_effect=RuntimeError('foreign listener')), \
             patch.object(helper, 'authenticated_shell') as identity:
            with self.assertRaisesRegex(RuntimeError, 'foreign listener'):
                helper.check_saved_target(Path('/native/adb'), '/unread/target')
            identity.assert_not_called()


if __name__ == '__main__':
    unittest.main()
