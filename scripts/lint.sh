#!/usr/bin/env bash
# Static checks only: never execute device, browser, or deployment entry points.
set -euo pipefail
cd -- "$(dirname -- "$0")/.."

ruff --version
git ls-files -z -- '*.py' | xargs -0 ruff check --no-cache --

shellcheck --version
shells=()
while IFS= read -r -d '' file; do
    bash -n "$file"
    shells+=("$file")
done < <(git ls-files -z -- '*.sh')
# Also check this entry point before it has been added to Git in a local worktree.
bash -n scripts/lint.sh
shellcheck --shell=bash --severity=warning "${shells[@]}" scripts/lint.sh

while IFS= read -r -d '' file; do
    node --check "$file"
done < <(git ls-files -z -- '*.js' '*.mjs')
python3 -c "import xml.dom.minidom as m; m.parse('app/AndroidManifest.xml')"
echo 'LINT_OK (Python, Bash, JavaScript, manifest)'
