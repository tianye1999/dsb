"""Replace only the installer-owned plugin tree; retire an explicit legacy list."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

LEGACY_FILES = (
    'apply-all.sh', 'patch_sidebar.py', 'patch_ui2.py', 'patch_composer.py',
    'patch_autohide.py', 'patch_hidetabs.py', 'repair_index.py', 'patch_fonts.py',
    'add_missing_rules.py', 'patch_gesture.py', 'patch_chatbg.py', 'patch_privacy_ui.py',
    'patch_theme.py', 'patch_conclusion.py', 'tweak_paths.py',
)
REQUIRED_FILES = (
    'index.js', 'client.js', 'cordis.patch.yml', 'LICENSE',
    'dsb-brand/index.js', 'dsb-brand/client.js', 'dsb-report/index.js',
    'dsb-report/client.js', 'dsb-report/web-turns.mjs', 'dsb-ui/index.js', 'dsb-ui/blocks.json',
    'runtime/icon.json', 'runtime/pristine-0.2.1-rc.2.html', 'runtime/restore-index.py',
    'runtime/agent/task_report.py', 'runtime/agent/task_status.py',
    'runtime/agent/feedback_export.py', 'runtime/agent/report_vocabulary.json',
    'runtime/bridge/task-report-store.mjs', 'runtime/bridge/active-task.mjs',
)


def validate(package, complete=True):
    if package.is_symlink() or json.loads((package / 'package.json').read_text()).get('name') != 'dsh-dsb':
        raise ValueError('refusing to replace a foreign package')
    if complete:
        if any(p.is_symlink() for p in package.rglob('*')):
            raise ValueError('plugin tree must not contain symbolic links')
        for name in REQUIRED_FILES:
            if not (package / name).is_file() or not (package / name).stat().st_size:
                raise ValueError('incomplete plugin: ' + name)


def deployment_root(rootfs):
    rootfs = Path(rootfs)
    if not rootfs.is_absolute() or rootfs.resolve() == Path('/') or not (rootfs / 'etc/os-release').is_file():
        raise ValueError('expected an installed container rootfs, not the host root')
    root = rootfs / 'root'
    if root.is_symlink() or not root.is_dir():
        raise ValueError('container /root must be a real directory')
    return root


def deploy(rootfs, source):
    root = deployment_root(rootfs)
    source = Path(source).resolve()
    target = root / 'dsh-plugin'
    if target.is_symlink() or target.resolve().parent != root.resolve():
        raise ValueError('unsafe plugin deployment path')
    validate(source)
    if target.exists():
        validate(target, complete=False)
    if source == target.resolve() or target.resolve() in source.parents:
        raise ValueError('source must be outside the deployment tree')
    previous = root / '.dsh-plugin.previous'
    # A durable rollback slot also recovers interruption between the two renames.
    if previous.exists() or previous.is_symlink():
        validate(previous)
        if not target.exists():
            os.replace(previous, target)
        else:
            validate(target)
            shutil.rmtree(previous)
    stage = Path(tempfile.mkdtemp(prefix='.dsh-plugin-stage-', dir=root))
    try:
        shutil.copytree(source, stage, dirs_exist_ok=True)
        validate(stage)
        try:
            if target.exists():
                target.replace(previous)
            stage.replace(target)
        except BaseException:
            if previous.exists():
                if target.exists():
                    shutil.rmtree(target)
                os.replace(previous, target)
            raise
        if previous.exists():
            shutil.rmtree(previous)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def retire_legacy(rootfs):
    directory = deployment_root(rootfs) / 'dsh-web-tweaks'
    if directory.is_symlink():
        raise ValueError('legacy deployment directory must not be a symbolic link')
    for name in LEGACY_FILES:
        file = directory / name
        if file.is_file() or file.is_symlink():
            file.unlink()


if __name__ == '__main__':
    if sys.argv[2] == '--retire-legacy':
        retire_legacy(sys.argv[1])
    else:
        deploy(sys.argv[1], sys.argv[2])
