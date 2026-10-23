import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('configure', Path(__file__).parents[1] / 'configure.py')
cfg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cfg)


class ConfigureTests(unittest.TestCase):
    def test_official_apt_urls_https_bootstrap_ca_and_rerun_preserves_custom_ca(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            directory = root / 'etc/apt/sources.list.d'
            directory.mkdir(parents=True)
            path = directory / 'ubuntu.sources'
            before = '# http://ports.ubuntu.com/comment\nURIs: http://ports.ubuntu.com/ubuntu-ports\n'
            path.write_text(before)
            ca = root / 'termux-ca'
            ca.write_bytes(b'trusted-test-ca')
            cfg.ubuntu_https(root, ca)
            self.assertIn('URIs: https://ports.ubuntu.com/ubuntu-ports', path.read_text())
            self.assertEqual(path.with_name(path.name + '.pre-dsb').read_text(), before)
            certificate = root / 'etc/ssl/certs/ca-certificates.crt'
            self.assertEqual(certificate.read_bytes(), ca.read_bytes())
            certificate.write_bytes(b'custom-existing-ca')
            once = path.read_bytes()
            cfg.ubuntu_https(root, ca)
            self.assertEqual(path.read_bytes(), once)
            self.assertEqual(certificate.read_bytes(), b'custom-existing-ca')

    def test_custom_apt_host_not_changed_or_given_ca(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'etc/apt').mkdir(parents=True)
            path = root / 'etc/apt/sources.list'
            before = 'deb http://ports.ubuntu.com.example.invalid/repo noble main\n'
            path.write_text(before)
            cfg.ubuntu_https(root, root / 'unused-ca')
            self.assertEqual(path.read_text(), before)
            self.assertFalse((root / 'etc/ssl').exists())

    def test_key_is_private_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg.save_key(root, 'sk-fake-first')
            cfg.save_key(root, 'sk-fake-second')
            key = root / '.dsh/.deepseek-key'
            self.assertEqual(key.read_text(), 'sk-fake-first\n')
            self.assertEqual(key.stat().st_mode & 0o777, 0o600)
            self.assertEqual(key.parent.stat().st_mode & 0o777, 0o700)

    def test_symlink_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / '.dsh').mkdir()
            (root / '.dsh/.deepseek-key').symlink_to(root / 'elsewhere')
            with self.assertRaises(ValueError):
                cfg.save_key(root, 'sk-fake')

    def test_directory_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / '.dsh/.deepseek-key').mkdir(parents=True)
            with self.assertRaises(ValueError):
                cfg.save_key(root, 'sk-fake')

    def test_external_apps_deduplicated_and_comments_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'termux.properties'
            p.write_text('# user preference\nallow-external-apps = false\nallow-external-apps=true\n')
            cfg.allow_external(p)
            once = p.read_text()
            cfg.allow_external(p)
            self.assertEqual(p.read_text(), once)
            self.assertEqual(once.count('allow-external-apps'), 1)
            self.assertIn('# user preference', once)

    def test_endpoint_validation(self):
        self.assertEqual(cfg.endpoint('127.0.0.1:33451'), '127.0.0.1:33451')
        for value in ['host:5555', '127.0.0.1:0', '127.0.0.1:65536', '127.0.0.1:12;id', '-s']:
            with self.assertRaises(ValueError):
                cfg.endpoint(value)

    def test_local_endpoint_does_not_accept_remote_routed_address(self):
        from unittest.mock import patch, MagicMock
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.getsockname.return_value = ('192.0.2.1', 12345)
        with patch('socket.socket', return_value=conn):
            self.assertEqual(cfg.endpoint('192.0.2.1:33221', local=True), '192.0.2.1:33221')
            with self.assertRaises(ValueError):
                cfg.endpoint('192.0.2.2:33221', local=True)


if __name__ == '__main__':
    unittest.main()
