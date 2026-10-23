"""Real runner entry point, no device actions; synthetic audit tasks only."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent      # dsb_run.py 与本测试同目录（手机上即 /root/.dsh-phone/skills）
def main():
    results = []
    with tempfile.TemporaryDirectory(prefix='dsb-1c1-cli-') as directory:
        cases = [
            ('claim', "set_goal(True, 'synthetic CLI assertion')", 0, 'completed'),
            ('false', "set_goal(False, 'synthetic false goal')", 3, 'failed'),
            ('compile', 'if:', 3, 'failed'),
            ('source', 'import subprocess', 3, 'failed'),
            ('timeout', 'time.sleep(2)', 124, 'timed_out'),
        ]
        for name, code, expected_rc, status in cases:
            script = Path(directory) / (name + '.py')
            script.write_text(code, encoding='utf-8')
            result = subprocess.run([sys.executable, str(ROOT / 'dsb_run.py'), str(script),
                '--task-name', 'test:1C1-cli-' + name, '--timeout', '0.1' if name == 'timeout' else '3'],
                capture_output=True, text=True, timeout=10)
            summary = json.loads(result.stdout.split('=== RUN_SUMMARY ===')[-1])
            assert result.returncode == expected_rc, (name, result.returncode, summary)
            assert summary['terminal_status'] == status, (name, summary)
            assert summary['goal_verified'] is False, summary
            assert summary['actions'] == 0, summary
            rows = [json.loads(line) for line in (ROOT / 'safe_tap_audit.jsonl').read_text().splitlines()
                    if summary['task_id'] in line]
            assert len([r for r in rows if r.get('kind') == 'task_start']) == 1, rows
            assert len([r for r in rows if r.get('kind') == 'task_end']) == 1, rows
            results.append({'case': name, 'rc': result.returncode, 'terminal_status': status,
                            'task_id': summary['task_id'], 'goal_verified': False})
    print(json.dumps({'passed': len(results), 'device_actions': 0, 'results': results}, indent=2))


if __name__ == '__main__':
    main()
