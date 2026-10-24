"""只修改 profile 清单并链接品牌包，不重装用户的 node_modules。"""
import json
import os
from pathlib import Path
import sys
import uuid

TEMPLATES = {
    'web': ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-web-app'],
    'phone': ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-sdk-app'],
    'headless': ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-headless'],
}


def configure(root, brand, report=None):
    root, brand = Path(root), Path(brand).resolve()
    packages = {'dsb-brand': brand}
    if report is not None:
        packages['dsb-report'] = Path(report).resolve()
    for name, target in packages.items():
        link = root / 'web/node_modules' / name
        if os.path.lexists(link) and (not link.is_symlink() or link.resolve() != target):
            raise ValueError(f'已有非受管 {name}；未替换，请先人工核对')
        if not target.is_dir():
            raise ValueError(f'{name} 目标目录不存在；未修改 profile')
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
            for package in packages:
                if package not in bundles:
                    bundles.append(package)
        plans.append((path, json.dumps(data, indent=2, ensure_ascii=False) + '\n',
                      path.read_bytes() if path.exists() else None,
                      path.stat().st_mode & 0o777 if path.exists() else None))
    created = []
    touched = []
    temporaries = []
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
    except Exception:
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


if __name__ == '__main__':
    configure(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
