"""公开材料的隐私检查；只报告相对路径和规则，不回显命中内容。"""
import re
from pathlib import Path

RULES = [
    re.compile(r'/Users/[A-Za-z0-9_.-]+/'),
    re.compile(r'192\.168\.[0-9]+\.[0-9]+'),
    re.compile(r'u0_a[0-9]{3}@'),
    re.compile(r'/private[/]tmp(?:/|\b)'),
    re.compile(r'scratch' r'pad(?:/|\b)', re.I),
    re.compile(r'\bR58[A-Z0-9]{7,16}\b'),
    re.compile(r'\b(?:serial(?:no)?|device_serial|android_serial|adb_serial)[\'\"]?\s*[:=]\s*[\'\"]?[A-Za-z0-9]{10,24}\b(?![A-Za-z0-9_:])', re.I),
]

# 正式图标/文档插图与运行截图区分；不能按目录放行任意图片。
PUBLIC_IMAGES = {
    'app/res/mipmap-' + density + '/ic_launcher_' + layer + '.png'
    for density in ['mdpi', 'hdpi', 'xhdpi', 'xxhdpi', 'xxxhdpi']
    for layer in ['foreground', 'monochrome']
} | {'docs/assets/' + name + '.png' for name in [
    'hero-a', 'hero-a-bg', 'hero-b-bg', 'hero-b.en',
    'social-preview-a-bg', 'social-preview-a.bilingual',
]}


def check_stage(stage, paths=None):
    stage = Path(stage)
    for path in sorted(stage.rglob('*') if paths is None else paths):
        if path.is_symlink():
            raise ValueError('公开材料禁止符号链接：' + str(path.relative_to(stage)))
        if not path.is_file():
            continue
        relative = path.relative_to(stage)
        if ((path.suffix in ['.lock', '.log', '.jsonl', '.png', '.jpg', '.mkv', '.mp4', '.keystore', '.jks'] and
             relative.as_posix() not in PUBLIC_IMAGES) or
                any(part.endswith('.runs') for part in relative.parts) or
                path.name in ['.env', 'MEMORY.md', 'HANDOVER.md']):
            raise ValueError('公开材料含运行产物：' + str(relative))
        text = path.read_bytes().decode('utf-8', 'replace')
        for i, rule in enumerate(RULES, 1):
            if rule.search(text):
                raise ValueError(f'公开材料隐私检查失败：{relative} (rule {i})')


if __name__ == '__main__':
    import sys
    if sys.argv[2:] in [['--tracked'], ['--sources']]:
        import subprocess
        root = Path(sys.argv[1])
        args = ['git', 'ls-files', '-z']
        if sys.argv[2:] == ['--sources']:
            args += ['--cached', '--others', '--exclude-standard']
        names = subprocess.check_output(args, cwd=root).split(b'\0')
        check_stage(root, [root / name.decode() for name in names if name])
    else:
        check_stage(sys.argv[1])
