import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('config_security', REPO / 'installer/configure.py')
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)


class ReleaseSecurity(unittest.TestCase):
    def test_dependency_overrides_keep_dsh_pinned_and_remove_affected_packages(self):
        manifest = json.loads((REPO / 'installer/dsh/package.json').read_text())
        release = json.loads((REPO / 'installer/release.json').read_text())
        lock = json.loads((REPO / 'installer/dsh/npm-shrinkwrap.json').read_text())
        self.assertEqual(manifest['dependencies']['@deepseek-ai/dsh'], release['dsh_version'])
        fixed = {'@modelcontextprotocol/sdk': '1.32.0', '@modelcontextprotocol/client': '2.3.0',
                 'fflate': '0.8.3'}
        self.assertEqual(manifest['overrides'], fixed)
        found = set()
        for path, package in lock['packages'].items():
            name = path.rsplit('node_modules/', 1)[-1]
            if name in fixed:
                self.assertEqual(package['version'], fixed[name], path)
                found.add(name)
            if name == '@deepseek-ai/dsh':
                self.assertEqual(package['version'], release['dsh_version'])
        # The SDK is only an optional peer of genai; do not install it just to satisfy an audit.
        self.assertTrue({'@modelcontextprotocol/client', 'fflate'} <= found)

    def test_signer_certificate_is_exact_and_not_only_package_name(self):
        spec = importlib.util.spec_from_file_location('certificate', REPO / 'installer/apk_certificate.py')
        certificate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(certificate)
        expected = 'a' * 64
        certificate.verify('Signer #1 certificate SHA-256 digest: ' + expected, expected)
        for report in ['', 'Signer #1 certificate SHA-256 digest: ' + 'b' * 64,
                       'Signer #1 certificate SHA-256 digest: ' + expected + '\nSigner #2 certificate SHA-256 digest: ' + expected]:
            with self.subTest(report=report), self.assertRaises(ValueError):
                certificate.verify(report, expected)
        with self.assertRaises(ValueError):
            certificate.verify('', '')

    def test_source_trust_gate_precedes_first_secondary_python_execution(self):
        check = (REPO / 'install.sh').read_text().split('check_env() {', 1)[1].split('\ndeploy_files()', 1)[0]
        self.assertLess(check.index('安装源含符号链接'), check.index('deploy.py" guard-legacy'))

    def test_only_current_supervisor_is_deployed_and_bridge_does_not_export_key(self):
        source = (REPO / 'install.sh').read_text()
        self.assertIn('supervisor.sh|boot.sh) continue', source)
        bridge = (REPO / 'termux/ensure-private-bridge.sh').read_text()
        self.assertNotIn('cat /root/.dsh/.deepseek-key', bridge)
        self.assertIn('unset DEEPSEEK_API_KEY', bridge)
        workspace = (REPO / 'dsh-config/installer.web.patch.yml').read_text()
        self.assertIn('documentsDirectory: /root/dsb-workspaces', workspace)

    def test_missing_scrcpy_refreshes_x11_index_even_after_interrupted_package_setup(self):
        source = (REPO / 'install.sh').read_text()
        branch = source.split('scrcpy 2>/dev/null || true)', 1)[1].split('\n  fi', 1)[0]
        self.assertLess(branch.index('APT::Update::Error-Mode=any update'), branch.index('pkg install'))
        self.assertIn('Acquire::Retries=3', branch)

    def test_package_hooks_inherit_private_transport_before_any_dependency_install(self):
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, HOME=d, PREFIX=d, REPO=str(REPO),
                       ADB_SERVER_SOCKET='tcp:untrusted.invalid:5037',
                       ANDROID_ADB_SERVER_ADDRESS='untrusted.invalid', ANDROID_ADB_SERVER_PORT='5037')
            script = '''
source "$REPO/install.sh"
package_adb_transport
bash -c 'printf "%s\\n%s\\n%s" "$ADB_SERVER_SOCKET" "${ANDROID_ADB_SERVER_ADDRESS-unset}" "${ANDROID_ADB_SERVER_PORT-unset}"'
'''
            result = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            endpoint, address, port = result.stdout.splitlines()
            self.assertTrue(endpoint.startswith('localfilesystem:' + d + '/.dsb-package-adb.'))
            directory = Path(endpoint.removeprefix('localfilesystem:')).parent
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual(list(directory.iterdir()), [])
            self.assertEqual([address, port], ['unset', 'unset'])
            source = (REPO / 'install.sh').read_text().split('main_locked() {', 1)[1]
            self.assertLess(source.index('\n  package_adb_transport\n'), source.index('\n  STAGE=Termux依赖\n'))
            self.assertIn('in_ubuntu bash /root/.dsb-container-deps.sh "$DSH_VERSION" "$ADB_SERVER_SOCKET"', source)
            container = (REPO / 'installer/container-deps.sh').read_text()
            self.assertLess(container.index('export ADB_SERVER_SOCKET="${2:?}"'), container.index('\napt-get '))

    def test_automated_container_commands_cannot_consume_future_prompt_input(self):
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, HOME=d, REPO=str(REPO))
            script = '''
source "$REPO/install.sh"
proot-distro() { read -r stolen || true; printf '%s' "$stolen" > "$HOME/stolen"; }
in_ubuntu true
read -r next
printf '%s' "$next"
'''
            result = subprocess.run(['bash', '-c', script], env=env, input='NEXT_PROMPT\n', capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, 'NEXT_PROMPT')
            self.assertEqual((Path(d) / 'stolen').read_text(), '')

    def test_web_readiness_requires_live_authenticated_local_url(self):
        for url, code, expected in [('http://127.0.0.1:3080/?token=test-token', '303', 0),
                                    ('http://127.0.0.1:3080/?token=test-token', '401', 1),
                                    ('http://untrusted.invalid/?token=test-token', '200', 1)]:
            with tempfile.TemporaryDirectory() as d:
                (Path(d) / '.dsb-web-url').write_text(url + '\n')
                env = dict(os.environ, HOME=d, REPO=str(REPO), TEST_CODE=code)
                result = subprocess.run(['bash', '-c', 'source "$REPO/install.sh"; curl() { printf "%s" "$TEST_CODE"; }; web_ready'],
                                        env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, result.stderr)

    def test_noninteractive_adb_cannot_consume_future_prompt_input(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / 'adb'
            fake.write_text('#!/bin/sh\nread -r stolen || true\nprintf "%s" "$stolen" > "$HOME/stolen"\n')
            fake.chmod(0o700)
            env = dict(os.environ, HOME=d, REPO=str(REPO), PATH=d + ':' + os.environ['PATH'])
            result = subprocess.run(['bash', '-c', 'source "$REPO/install.sh"; adb shell true; read -r next; printf "%s" "$next"'],
                                    env=env, input='NEXT_PROMPT\n', capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, 'NEXT_PROMPT')
            self.assertEqual((Path(d) / 'stolen').read_text(), '')

    def test_bad_or_missing_apk_hash_never_opens_installer(self):
        for digest in ['', '0' * 64]:
            with tempfile.TemporaryDirectory() as d:
                root = Path(d)
                (root / 'debug.apk').write_bytes(b'debug fixture')
                script = '''
source "$REPO/install.sh"
touch "$LOG"
TARGET=test
adb() { return 1; }
termux-open() { touch "$HOME/opened"; }
install_apk
'''
                env = dict(os.environ, REPO=str(REPO), HOME=d, DSB_APK_PATH=str(root / 'debug.apk'), DSB_TEST_APK_SHA256=digest)
                result = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((root / 'opened').exists())

    def test_key_replacement_is_atomic_and_invalid_input_keeps_original(self):
        with tempfile.TemporaryDirectory() as d:
            config.save_key(d, 'fake-original')
            with self.assertRaises(ValueError):
                config.save_key(d, 'invalid key', replace=True)
            path = Path(d) / '.dsh/.deepseek-key'
            self.assertEqual(path.read_text(), 'fake-original\n')
            config.save_key(d, 'fake-replacement', replace=True)
            self.assertEqual(path.read_text(), 'fake-replacement\n')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_ssh_original_is_backed_up_once_and_not_appended(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'sshd_config'
            original = b'Port 2222\nPasswordAuthentication yes\nListenAddress 0.0.0.0\n'
            path.write_bytes(original)
            for _ in range(2):
                subprocess.run(['python3', str(REPO / 'installer/ssh_config.py'), str(path)], check=True)
            self.assertEqual(path.with_name('sshd_config.pre-dsb').read_bytes(), original)
            self.assertEqual(path.read_text().count('PasswordAuthentication'), 1)
            self.assertIn('PasswordAuthentication no', path.read_text())
            self.assertIn('ListenAddress 127.0.0.1', path.read_text())


if __name__ == '__main__':
    unittest.main()
