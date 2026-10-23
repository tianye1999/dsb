"""新安装使用已验证的无线端点；旧手动安装保留兼容默认值。"""
import os
from pathlib import Path
import re
import json
import stat

TERMUX_HOME = Path(os.environ.get('DSB_TERMUX_HOME', '/data/data/com.termux/files/home'))
TERMUX_PREFIX = TERMUX_HOME.parent / 'usr'


class EndpointUnavailable(RuntimeError):
    pass


def adb_command(binary='adb'):
    gate = TERMUX_HOME / 'dsb/adb-blocked'
    if gate.exists() or gate.is_symlink():
        raise EndpointUnavailable('普通 ADB 冲突闸已关闭；先运行安装器 doctor 处理冲突')
    receipt = TERMUX_HOME / 'dsb/adb-transport.json'
    if receipt.exists():
        try:
            info = receipt.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                raise EndpointUnavailable('transport 凭据类型/权限不符')
            data = json.loads(receipt.read_text())
            tid = data.get('transport_id')
            if data.get('version') != 2 or not isinstance(tid, int) or tid <= 0:
                raise EndpointUnavailable('transport 凭据无效')
            # 普通模式也不能仅凭一个旧整数 ID 派发。原生 helper 再检查代次并禁止暗中自启。
            root = TERMUX_PREFIX / 'var/lib/dsb-adb'
            terminal = False
            journal = root / 'journal.json'
            if journal.is_file() and not journal.is_symlink():
                terminal = json.loads(journal.read_text()).get('state') == 'rolled-back'
            if not root.exists() or terminal:
                return [str(TERMUX_PREFIX / 'bin/python3'), '-I',
                        str(TERMUX_HOME / 'dsb/adb_identity.py'), '--execute', '-t', str(tid)]
        except (OSError, ValueError) as exc:
            raise EndpointUnavailable('transport 凭据不可读；未提交命令') from exc
        return [binary, '-t', str(tid)]
    if (TERMUX_HOME / 'dsb/adb-target').exists() or (TERMUX_HOME / '.dsb-installer-managed').exists():
        raise EndpointUnavailable('当前连接尚未通过本机身份挑战')
    return [binary, '-s', '127.0.0.1:5555']


def adb_target():
    try:
        receipt = TERMUX_HOME / 'dsb/adb-transport.json'
        if receipt.exists():
            adb_command()  # 权限与格式必须与动作入口一致。
            return json.loads(receipt.read_text())['serial']
        value = (TERMUX_HOME / 'dsb/adb-target').read_text().strip()
    except FileNotFoundError:
        return '127.0.0.1:5555'
    if not re.fullmatch(r'[0-9.]+:[0-9]{1,5}', value):
        raise ValueError('invalid saved ADB target')
    return value


def transport_check():
    """返回 ok/warning/fail；有部署痕迹时不能被 skip 标记降级成 warning。"""
    import json
    import subprocess
    root = TERMUX_PREFIX / 'var/lib/dsb-adb'
    state = TERMUX_PREFIX / 'var/lib/dsb-adb-install.json'
    native_env = {key: value for key, value in os.environ.items() if not key.startswith('LD_')}
    native_env.update(PREFIX=str(TERMUX_PREFIX), HOME=str(TERMUX_HOME))
    try:
        journal = root / 'journal.json'
        rolled_back = False
        if journal.is_file() and not journal.is_symlink():
            rolled_back = json.loads(journal.read_text()).get('state') == 'rolled-back'
        if (root.exists() or root.is_symlink()) and not rolled_back:
            result = subprocess.run([str(TERMUX_PREFIX / 'bin/python3'), '-I',
                                     str(root / 'adb_policy.py'), '--policy-check-fast'],
                                    # Native doctor/guard publishes the full-audit receipt; proot only checks metadata.
                                    capture_output=True, text=True, timeout=12, env=native_env)
            report = json.loads(result.stdout)
            if result.returncode != 0 or report.get('ok') is not True:
                return 'fail', '私有 ADB 检查失败：' + str(report.get('error', 'unknown'))
            return 'ok', '私有 socket、关键入口、进程及 TCP 检查通过'
        if state.exists() or rolled_back:
            report = json.loads(state.read_text()) if state.exists() else {'status': 'rolled-back'}
            if rolled_back:
                report = {'status': 'rolled-back', 'reason': '普通回滚已完成，等待归档私有目录'}
            if report.get('status') in ['skipped', 'rolled-back']:
                owner = subprocess.run([str(TERMUX_PREFIX / 'bin/python3'), '-I',
                                        str(TERMUX_HOME / 'dsb/adb-tcp-owner.py'), '--check'],
                                       capture_output=True, text=True, timeout=12, env=native_env)
                if owner.returncode:
                    return 'fail', '普通 ADB 归属或本机 shell 身份检查失败；未继续发送命令'
                return 'warning', 'ADB 未加固：' + str(report.get('reason', '预检跳过'))
            return 'fail', 'ADB 加固状态与部署文件不一致'
        return 'warning', 'ADB 未加固：尚未运行默认安装器'
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return 'fail', 'ADB 传输检查不可用：' + type(exc).__name__
