"""私密备份原配置；生成受控配置而不是追加无效的后置选项。"""
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from configure import atomic

path = Path(sys.argv[1])
backup = path.with_name(path.name + '.pre-dsb')
if path.is_symlink() or backup.is_symlink():
    raise SystemExit('SSH 配置/备份不能是符号链接')
if not backup.exists():
    atomic(backup, path.read_bytes())
atomic(path, b'# DSB: original configuration in sshd_config.pre-dsb\nPort 8022\nListenAddress 127.0.0.1\nPasswordAuthentication no\nKbdInteractiveAuthentication no\nPubkeyAuthentication yes\nSubsystem sftp internal-sftp\n')
