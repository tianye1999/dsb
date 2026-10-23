#!/data/data/com.termux/files/usr/bin/python3
import importlib.util
import json
import pathlib
import sys

spec = importlib.util.spec_from_file_location('entry', pathlib.Path(__file__).with_name('trusted-entry.py'))
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)
if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.exit('usage: trusted-cli.py /route < request-body.json; no automatic replay')
    request = entry.validate_request({'path': sys.argv[1], 'body': json.loads(sys.stdin.buffer.read(entry.MAX_REQUEST+1))})
    try:
        result = entry.dispatch(request, 900)
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0 if result['status'] == 200 and result['body'].get('ok') else 1)
    except Exception:
        sys.exit('channel_unavailable_or_state_unknown')
