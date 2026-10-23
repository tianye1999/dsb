"""Resolve the running DSH dist once for every patch and its verifier."""
import os
from pathlib import Path


def targets():
    override = os.environ.get('DSH_TWEAKS_DIST')
    if override:
        return [str(Path(override) / 'index.html')]
    entry = Path('/usr/local/bin/dsh')
    if entry.exists():
        active = entry.resolve().parent.parent.parent / 'dsh-web-frontend/dist/index.html'
        if active.exists():
            return [str(active)]
    return [os.path.expanduser('~/.dsh/profiles/node_modules/@deepseek-ai/dsh/node_modules/'
                               '@deepseek-ai/dsh-web-frontend/dist/index.html')]
