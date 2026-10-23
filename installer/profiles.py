"""只修改 profile 清单并链接品牌包，不重装用户的 node_modules。"""
import json
import os
from pathlib import Path
import sys

TEMPLATES = {
    'web': ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-web-app'],
    'phone': ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-sdk-app'],
    'headless': ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-headless'],
}


def configure(root, brand):
    root, brand = Path(root), Path(brand).resolve()
    link = root / 'web/node_modules/dsb-brand'
    if os.path.lexists(link) and (not link.is_symlink() or link.resolve() != brand):
        raise ValueError('已有非受管 dsb-brand；未替换，请先人工核对')
    for name, defaults in TEMPLATES.items():
        directory = root / name
        directory.mkdir(parents=True, exist_ok=True)
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
        if name == 'web' and 'dsb-brand' not in bundles:
            bundles.append('dsb-brand')
        content = json.dumps(data, indent=2, ensure_ascii=False) + '\n'
        if not path.exists() or path.read_text() != content:
            temporary = path.with_suffix('.json.dsb-tmp')
            temporary.write_text(content)
            temporary.chmod(0o600)
            temporary.replace(path)
    link.parent.mkdir(parents=True, exist_ok=True)
    if not link.is_symlink():
        link.symlink_to(brand, target_is_directory=True)


if __name__ == '__main__':
    configure(sys.argv[1], sys.argv[2])
