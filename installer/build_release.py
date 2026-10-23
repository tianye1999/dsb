"""发布装配，不签名/上传：固定 APK hash，随包携带已校验的两架构 NDK 材料。"""
import argparse
import gzip
import hashlib
import importlib.util
import json
import io
import re
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from privacy import check_stage

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('build_inputs', REPO / 'hardening/adb-private/build_inputs.py')
inputs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inputs)


def committed_sources():
    status = subprocess.run(['git', 'status', '--porcelain=v1', '--untracked-files=all', '-z'],
                            cwd=REPO, check=True, capture_output=True).stdout
    if status:
        raise ValueError('工作树不干净，拒绝装配；请由 owner 提交/确认源码后重试')
    commit = subprocess.run(['git', 'rev-parse', '--verify', 'HEAD'], cwd=REPO,
                            check=True, capture_output=True).stdout.decode().strip()
    result = subprocess.run(['git', 'archive', '--format=tar', commit], cwd=REPO,
                            check=True, capture_output=True)
    return result.stdout


def stage_sources(stage, data):
    prefixes = ('installer/', 'hardening/', 'agent/', 'bridge/', 'termux/', 'dsh-config/', 'dsh-web-tweaks/')
    docs = {'README.md', 'docs/INSTALL.md', 'docs/THREAT-MODEL.md', 'hardening/adb-private/README.md', 'agent/SKILL.md'}
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:') as archive:
        for member in archive.getmembers():
            name = member.name
            if name not in {'install.sh', 'app/res/mipmap-xxxhdpi/ic_launcher_foreground.png'} and not name.startswith(prefixes) and name not in docs:
                continue
            if name.endswith('.md') and name not in docs:
                continue
            if (Path(name).suffix in ['.lock', '.log', '.jsonl'] or Path(name).name.endswith('_state.json') or
                    any(p.endswith('.runs') for p in Path(name).parts)):
                continue
            if member.isdir():
                continue
            if not member.isfile() or Path(name).is_absolute() or '..' in Path(name).parts:
                raise ValueError('Git 发布树含不支持的文件类型或路径')
            destination = stage / name
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            destination.write_bytes(archive.extractfile(member).read())


def normalize(member):
    member.uid = member.gid = 0
    member.uname = member.gname = ''
    member.mtime = 0
    member.pax_headers = {}
    return member


def assemble(apk, cache, output):
    sources = committed_sources()  # 拒绝 dirty tree 必须发生在创建任何输出之前。
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='dsb-release-') as temp:
        stage = Path(temp) / 'dsb-installer'
        stage.mkdir(mode=0o700)
        stage_sources(stage, sources)
        release = stage / 'installer/release.json'
        data = json.loads(release.read_text())
        if not re.fullmatch(r'[a-f0-9]{64}', data.get('apk_certificate_sha256') or ''):
            raise ValueError('提交的 release.json 未固定发布证书 SHA-256，拒绝装配')
        data['apk_sha256'] = hashlib.sha256(Path(apk).read_bytes()).hexdigest()
        release.write_text(json.dumps(data, indent=2) + '\n')
        for machine, (triple, _) in inputs.PINS.items():
            destination = stage / 'hardening/adb-private/ndk' / triple
            destination.mkdir(parents=True, mode=0o700)
            for name, expected in {**inputs.SHA256[machine], 'NOTICE': inputs.NOTICE_SHA256}.items():
                source = Path(cache) / ('ndk-pinned-' + triple) / name
                if source.is_symlink() or hashlib.sha256(source.read_bytes()).hexdigest() != expected:
                    raise ValueError('NDK 材料未通过 SHA-256 校验：' + name)
                shutil.copyfile(source, destination / name)
        for path in stage.rglob('*'):
            path.chmod(0o700 if path.is_dir() else 0o600)
        check_stage(stage)
        # gzip 也不能带输出名/当前时间；否则相同 tar 内容仍不能复现 SHA。
        with output.open('wb') as stream, gzip.GzipFile(fileobj=stream, mode='wb', filename='', mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w', format=tarfile.USTAR_FORMAT) as archive:
                archive.add(stage, arcname='dsb-installer', filter=normalize)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    print('APK_SHA256=' + data['apk_sha256'])
    print('TARBALL_SHA256=' + digest)
    print('发布文档必须把 TARBALL_SHA256 内联到下载命令；校验通过前不得解包执行。')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apk', required=True)
    parser.add_argument('--ndk-cache', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    assemble(args.apk, args.ndk_cache, args.output)
