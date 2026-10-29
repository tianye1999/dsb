"""Transactionally migrate installer-owned links to the single DSB bundle."""
import json
import os
from pathlib import Path
import sys
import subprocess
import uuid

TEMPLATES = {
    'web': ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-web-app'],
    'phone': ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-sdk-app'],
    'headless': ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-headless'],
}


def configure(root, bundle, verify=None):
    root, bundle = Path(root), Path(bundle).resolve()
    packages = {'dsh-dsb': bundle}
    legacy = {}
    for name in ('dsb-brand', 'dsb-report'):
        link = root / 'web/node_modules' / name
        target = bundle.parent / 'dsh-web-tweaks' / name
        if os.path.lexists(link):
            if not link.is_symlink() or link.resolve() != target.resolve():
                raise ValueError(f'已有非受管 {name}；未替换，请先人工核对')
            legacy[name] = (link, os.readlink(link))
    for name, target in packages.items():
        link = root / 'web/node_modules' / name
        if os.path.lexists(link) and (not link.is_symlink() or link.resolve() != target):
            raise ValueError(f'已有非受管 {name}；未替换，请先人工核对')
        if not target.is_dir():
            raise ValueError(f'{name} 目标目录不存在；未修改 profile')
    patch_paths = [root / name / 'cordis.patch.yml' for name in TEMPLATES]
    patches_before = [path.read_bytes() if path.exists() else None for path in patch_paths]
    patch_modes = [path.stat().st_mode & 0o777 if path.exists() else None for path in patch_paths]
    plans = []
    for name, defaults in TEMPLATES.items():
        directory = root / name
        path = directory / 'package.json'
        if path.is_symlink():
            raise ValueError('profile package.json 不能是符号链接')
        data = json.loads(path.read_text()) if path.exists() else {
            'name': 'dsh-profile-' + name, 'private': True, 'dependencies': {},
            'dsh': {'profile': {'bundles': defaults.copy()}},
        }
        bundles = data['dsh']['profile']['bundles']
        if not isinstance(bundles, list) or any(not isinstance(item, str) for item in bundles):
            raise ValueError('无法识别现有 profile bundles；未重装依赖')
        if name == 'web':
            dependencies = data.setdefault('dependencies', {})
            if not isinstance(dependencies, dict):
                raise ValueError('无法识别现有 profile dependencies；未修改 profile')
            local_spec = 'link:' + str(bundle)
            if 'dsh-dsb' in dependencies and dependencies['dsh-dsb'] != local_spec:
                raise ValueError('已有非受管 dsh-dsb dependency；请先人工核对')
            for old in ('dsb-brand', 'dsb-report'):
                if old in bundles or old in dependencies:
                    if old not in legacy:
                        raise ValueError(f'已有非受管 {old}；未替换，请先人工核对')
                    if old in dependencies:
                        # Old installer links never created dependency entries.
                        raise ValueError(f'已有非受管 {old} dependency；请先人工核对')
            bundles[:] = [item for item in bundles if item not in legacy]
            dependencies['dsh-dsb'] = local_spec
            for package in packages:
                if package not in bundles:
                    bundles.append(package)
        plans.append((path, json.dumps(data, indent=2, ensure_ascii=False) + '\n',
                      path.read_bytes() if path.exists() else None,
                      path.stat().st_mode & 0o777 if path.exists() else None))
    created = []
    touched = []
    temporaries = []
    removed = []
    try:
        for name, target in packages.items():
            link = root / 'web/node_modules' / name
            link.parent.mkdir(parents=True, exist_ok=True)
            if not link.is_symlink():
                link.symlink_to(target, target_is_directory=True)
                created.append(link)
            if not link.is_symlink() or link.resolve() != target or not link.is_dir():
                raise ValueError(f'{name} 链接校验失败')
        for path, content, original, mode in plans:
            path.parent.mkdir(parents=True, exist_ok=True)
            if original is None or original.decode('utf-8') != content:
                temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.dsb-tmp')
                with temporary.open('x', encoding='utf-8') as stream:
                    temporaries.append(temporary)
                    stream.write(content)
                temporary.chmod(mode if mode is not None else 0o600)
                temporary.replace(path)
                touched.append((path, original, mode))
        if verify:
            subprocess.run(verify, check=True)
        for link, target in legacy.values():
            removed.append((link, target))
            link.unlink()
    except BaseException:
        for link, target in reversed(removed):
            if not os.path.lexists(link):
                link.symlink_to(target, target_is_directory=True)
        for path, original, mode in zip(patch_paths, patches_before, patch_modes):
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(original)
                path.chmod(mode)
        for temporary in temporaries:
            temporary.unlink(missing_ok=True)
        for path, original, mode in reversed(touched):
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(original)
                path.chmod(mode)
        for link in reversed(created):
            link.unlink(missing_ok=True)
        raise
    patches_after = [path.read_bytes() if path.exists() else None for path in patch_paths]
    return bool(created or touched or removed or patches_before != patches_after)


if __name__ == '__main__':
    changed = configure(sys.argv[1], sys.argv[2], sys.argv[3:] or None)
    print('DSB_PROFILES_CHANGED' if changed else 'DSB_PROFILES_UNCHANGED')
