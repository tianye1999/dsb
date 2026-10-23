from pathlib import Path
import subprocess
import tempfile
import unittest


class LauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name).resolve()
        (cls.root / 'run').mkdir()
        cls.binary = cls.root / 'adb'
        source = Path(__file__).parents[1] / 'payload/adb_launcher.c'
        subprocess.run(['cc', '-O2', '-Wall', '-Wextra', '-Werror', '-std=c11',
                        '-DROOT_PATH="' + str(cls.root) + '"',
                        '-DSOCKET_PATH="' + str(cls.root / 'run/server.sock') + '"',
                        '-DPYTHON="/not-executed"', str(source), '-o', str(cls.binary)],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_prefix_parser_and_payload_boundary(self):
        for args in [['-P1'], ['-H', 'x'], ['server', '-P1'], ['-L', 'invalid'],
                     ['fork-server'], ['-a'], ['-s'], ['--unknown'], ['raw', 'host:version']]:
            r = subprocess.run([str(self.binary), '--policy-validate', *args], capture_output=True)
            self.assertEqual(r.returncode, 78, args)
        for args in [['shell', 'printf', '-P1'], ['-s', 'x', 'exec-out', 'printf', '-L'],
                     ['devices', '-l'], ['disconnect'], ['forward', '--list'], ['pair', '127.0.0.1:12345']]:
            r = subprocess.run([str(self.binary), '--policy-validate', *args], capture_output=True)
            self.assertEqual(r.returncode, 0, args)

    def test_pause_precedes_backend(self):
        gate = self.root / 'maintenance'
        try:
            gate.write_text('user maintenance\n')
            r = subprocess.run([str(self.binary), 'devices'], capture_output=True, text=True)
            self.assertEqual(r.returncode, 78)
            self.assertIn('ADB_POLICY_PAUSED:', r.stderr)
            self.assertIn('user maintenance', r.stderr)
        finally:
            gate.unlink()

    def test_private_directory_alias_rejected(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.root / 'run', target_is_directory=True)
        try:
            for endpoint in [str(alias / 'server.sock'), 'server.sock']:
                r = subprocess.run([str(self.binary), '--policy-validate', 'reverse', 'tcp:0',
                                    'localfilesystem:' + endpoint], cwd=self.root / 'run', capture_output=True)
                self.assertEqual(r.returncode, 78)
        finally:
            alias.unlink()


if __name__ == '__main__':
    unittest.main()
