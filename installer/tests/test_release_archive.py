import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock


REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('build_release_archive', REPO / 'installer/build_release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix='release-archive-test-', dir=os.environ.get('DSB_TEST_TMPDIR'))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.cache = self.root / 'cache'
        self.apk = self.root / 'fixture.apk'
        self.apk.write_bytes(b'synthetic APK fixture\n')
        self.tracked = {
            'install.sh': b'#!/bin/sh\nexit 0\n',
            'installer/release.json': ('{"apk_sha256": "pending", "apk_certificate_sha256": "' + 'a' * 64 + '"}\n').encode(),
            'installer/tracked.py': b'RELEASE_FIXTURE = True\n',
            'hardening/tracked.txt': b'public hardening fixture\n',
            'agent/tracked.py': b'AGENT_FIXTURE = True\n',
            'bridge/tracked.py': b'BRIDGE_FIXTURE = True\n',
            'termux/tracked.sh': b'#!/bin/sh\nexit 0\n',
            'dsh-config/tracked.yml': b'fixture: true\n',
            'dsh-plugin/dsb-brand/index.js': b'export const brand = true;\n',
            'app/res/mipmap-xxxhdpi/ic_launcher_foreground.png': b'\x89PNG\r\n\x1a\n',
        }
        for index, (name, content) in enumerate(self.tracked.items()):
            self.write_source(name, content)
            os.utime(self.repo / name, (1_600_000_000 + index, 1_600_000_000 + index))
        self.status = ''
        self.untracked = []
        self.ignored = []

        hashes = {}
        notice = b'synthetic NDK license fixture\n'
        self.materials = {}
        for machine, (triple, _) in release.inputs.PINS.items():
            hashes[machine] = {}
            directory = self.cache / ('ndk-pinned-' + triple)
            directory.mkdir(parents=True)
            for name in release.inputs.SHA256[machine]:
                content = (machine + ':' + name + '\n').encode()
                (directory / name).write_bytes(content)
                hashes[machine][name] = hashlib.sha256(content).hexdigest()
                self.materials['hardening/adb-private/ndk/' + triple + '/' + name] = content
            (directory / 'NOTICE').write_bytes(notice)
            self.materials['hardening/adb-private/ndk/' + triple + '/NOTICE'] = notice

        build_temp = self.root / 'build-temp'
        build_temp.mkdir()
        for patcher in [
            mock.patch.object(release, 'REPO', self.repo),
            mock.patch.object(release.inputs, 'SHA256', hashes),
            mock.patch.object(release.inputs, 'NOTICE_SHA256', hashlib.sha256(notice).hexdigest()),
            mock.patch.object(tempfile, 'tempdir', str(build_temp)),
            mock.patch('subprocess.run', side_effect=self.git_run),
        ]:
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_source(self, name, content):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def git_run(self, command, *args, **kwargs):
        self.assertIsInstance(command, (list, tuple), 'Git queries must not use a shell')
        self.assertEqual(Path(str(command[0])).name, 'git', 'Only local Git queries are allowed')
        separator = '\0' if '-z' in command else '\n'
        code = 0
        if 'status' in command:
            stdout = self.status if '-z' in command else self.status.replace('\0', '\n')
        elif 'ls-files' in command:
            if '--others' in command:
                paths = self.ignored if '--ignored' in command else self.untracked
            else:
                paths = sorted(self.tracked)
            stdout = separator.join(paths) + (separator if paths else '')
        elif 'diff' in command or 'diff-index' in command:
            code = int(bool(self.status and not self.status.startswith('?? ')))
            stdout = ''
        elif 'archive' in command:
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode='w') as archive:
                for name in sorted(self.tracked):
                    archive.add(self.repo / name, arcname=name, recursive=False)
            stdout = buffer.getvalue()
        elif 'rev-parse' in command:
            stdout = '1' * 40 + '\n'
        elif 'show' in command or 'log' in command:
            stdout = '1600000000\n'
        else:
            self.fail('Unexpected Git query: ' + repr(command))
        text_mode = kwargs.get('text') or kwargs.get('universal_newlines') or kwargs.get('encoding')
        if not text_mode and isinstance(stdout, str):
            stdout = stdout.encode()
        if hasattr(kwargs.get('stdout'), 'write'):
            kwargs['stdout'].write(stdout)
            stdout = None
        stderr = '' if text_mode else b''
        if code and kwargs.get('check'):
            raise subprocess.CalledProcessError(code, command, output=stdout, stderr=stderr)
        return subprocess.CompletedProcess(command, code, stdout, stderr)

    def assemble(self, name='release.tar.gz'):
        output = self.root / 'outputs' / name
        with contextlib.redirect_stdout(io.StringIO()):
            release.assemble(self.apk, self.cache, output)
        return output

    def archive_files(self, output):
        with tarfile.open(output, 'r:gz') as archive:
            return {
                member.name.removeprefix('dsb-installer/'): archive.extractfile(member).read()
                for member in archive.getmembers() if member.isfile()
            }

    def test_release_stamps_apk_and_keeps_verified_materials(self):
        files = self.archive_files(self.assemble())
        manifest = json.loads(files['installer/release.json'])
        self.assertEqual(manifest['apk_sha256'], hashlib.sha256(self.apk.read_bytes()).hexdigest())
        for name, content in self.materials.items():
            with self.subTest(material=name):
                self.assertEqual(files[name], content)

    def test_only_git_tracked_sources_and_verified_materials_are_archived(self):
        self.write_source('agent/local-only.py', b'not tracked\n')
        self.ignored.append('agent/local-only.py')
        files = self.archive_files(self.assemble())
        self.assertEqual(set(files), set(self.tracked) | set(self.materials))

    def test_ignored_agent_audit_lock_and_run_artifacts_are_not_archived(self):
        paths = [
            'agent/fake-agent.py',
            'agent/safe_tap_audit.jsonl',
            'agent/.safe_tap.lock',
            'agent/xxx.runs/trace.jsonl',
            'agent/xxx.runs/x.lock',
            'agent/x.lock',
        ]
        for name in paths:
            self.write_source(name, b'ignored local runtime artifact\n')
        self.ignored.extend(paths)
        files = self.archive_files(self.assemble())
        self.assertEqual(set(files).intersection(paths), set())

    def test_dirty_tracked_sources_are_refused_before_archive_creation(self):
        for state in [' M', 'M ', ' D']:
            with self.subTest(git_state=state):
                self.status = state + ' agent/tracked.py\0'
                output = self.root / 'outputs' / ('dirty-' + state.replace(' ', '_') + '.tar.gz')
                with self.assertRaises((ValueError, RuntimeError)):
                    self.assemble(output.name)
                self.assertFalse(output.exists())

    def test_nonignored_untracked_source_is_refused(self):
        self.write_source('agent/new-source.py', b'not reviewed\n')
        self.untracked.append('agent/new-source.py')
        self.status = '?? agent/new-source.py\0'
        output = self.root / 'outputs' / 'untracked.tar.gz'
        with self.assertRaises((ValueError, RuntimeError)):
            self.assemble(output.name)
        self.assertFalse(output.exists())

    def test_archive_member_owners_do_not_expose_the_builder(self):
        with tarfile.open(self.assemble(), 'r:gz') as archive:
            owners = {(member.uid, member.gid, member.uname, member.gname)
                      for member in archive.getmembers()}
        self.assertEqual(owners, {(0, 0, '', '')})

    def test_all_archive_members_have_one_fixed_mtime(self):
        with tarfile.open(self.assemble(), 'r:gz') as archive:
            timestamps = {member.mtime for member in archive.getmembers()}
        self.assertEqual(len(timestamps), 1, 'Source and staging mtimes must be normalized')

    def test_repeated_assembly_has_identical_sha256_despite_time_and_output_name(self):
        with mock.patch('time.time', return_value=1_700_000_000):
            first = self.assemble('first.tar.gz').read_bytes()
        for name in self.tracked:
            os.utime(self.repo / name, (1_800_000_000, 1_800_000_000))
        with mock.patch('time.time', return_value=1_900_000_000):
            second = self.assemble('second.tar.gz').read_bytes()
        self.assertEqual(hashlib.sha256(first).hexdigest(), hashlib.sha256(second).hexdigest())

    def test_private_stage_content_is_refused(self):
        markers = {
            'home_path': '/' + 'Users/release-owner/private-file',
            'temporary_path': '/' + 'private/tmp/release-owner/private-file',
            'working_notes': 'scratch' + 'pad/private-notes.txt',
            'device_serial': 'R58N' + '1234ABC',
        }
        for label, marker in markers.items():
            with self.subTest(private_marker=label):
                self.write_source('agent/tracked.py', (marker + '\n').encode())
                output = self.root / 'outputs' / (label + '.tar.gz')
                with self.assertRaises((ValueError, RuntimeError)):
                    self.assemble(output.name)
                self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
