# Contributing

**English** · [简体中文](CONTRIBUTING.zh-CN.md)

Thanks for your interest. DSB competes on being trustworthy, not on being clever, so the rules in this guide for **changing a safety gate** are stricter than in most projects.

## Development environment

| Part | What you need | How to run the tests |
|---|---|---|
| `agent/` (execution layer, Python 3.11+) | Python only; offline tests never touch a device | `cd agent && for t in test_*.py tests/test_*.py; do python3 $t; done` |
| `bridge/` (task bridge, Node 20+) | Node | `cd bridge && node --test test-*.mjs` |
| `app/` (Android App, no Gradle) | JDK 17 + Android build-tools 35 | `cd app && DSB_KEY_PASS=... ./build.sh` (see `docs/INSTALL.md`) |
| `termux/`, `dsh-config/`, `dsh-web-tweaks/` | Termux + proot Ubuntu (real device) | Verify on a real device |

Offline tests run in CI; real-device verification cannot run in CI, so see the next sections.

New major functionality must include corresponding tests in the automated test suite.

## Local lint

Install the development dependencies in an isolated Python 3.11+ environment (this does not change the user installation's dependencies), and have Node 22, JDK 17, Android API 35 and ShellCheck available:

```bash
python3 -m venv /tmp/dsb-lint-venv
source /tmp/dsb-lint-venv/bin/activate
python3 -m pip install --require-hashes --only-binary=:all: -r requirements-dev.txt
bash scripts/lint.sh
bash app/tests/lint-java.sh
```

`scripts/lint.sh` checks every Git-tracked Python, `.sh`, `.js` and `.mjs` file and the App manifest; add new files to Git first. Ruff is pinned to 0.17.2, with wheel hashes taken from that version's PyPI JSON metadata; only the defect rules `F`, `E9` and `B006/B012/B016/B018/B023/B025/B033` are enabled, and nothing is reformatted. ShellCheck parses every `.sh` file as Bash (including Termux shebangs) and fails on `warning` or higher; CI uses the version preinstalled on `ubuntu-latest` and prints it, the local baseline is 0.10.0, and no CI binary or extra Action is downloaded.

All App / instrumentation Java sources, the offline Java tests and the release build use `--release 11 -Xlint:all -Werror`. `--release 11` keeps the Java 11 bytecode target while avoiding the toolchain warning that `-source/-target` gives when the system module path is not set; no lint category is excluded globally. The release build also fails strictly, since its JDK 17 / API 35 toolchain is fixed; Android compatibility APIs that must stay are annotated on the specific method with `@SuppressWarnings("deprecation")` and a reason, without changing runtime logic. Set `ANDROID_HOME` (or `ANDROID_SDK_ROOT`) to choose the SDK; macOS defaults to `~/Library/Android/sdk`. None of these checks runs a phone, an emulator or a browser.

## Real-device gate

Any PR touching these paths needs **real-device** evidence before merging (an audit-log excerpt / `safe_tap.py doctor` output / end-to-end timings), written in the PR description:

- `agent/safe_tap.py`, `agent/vd_session.py`, `agent/act.py`, `agent/vision_check.py`, `agent/ocr.py`
- `bridge/bridge.mjs`, `bridge/task-ledger.mjs`
- `app/src/**`

"All offline tests green" does not mean "it works": several false positives in this project's history came from exactly that.

## Changing a safety gate

The gates are the core of the product, not parameters to tweak casually. A PR that adds, relaxes or removes any gate must meet all of the following:

1. **A real incident or real need**: say which task and which page, and why the existing gate is not enough or too strict.
2. **Behaviour tests**: at least one test that fails before the change and passes after it, and one that proves "what should be refused is still refused".
3. **Independent review**: the author cannot approve their own change; the reviewer specifically hunts for new bypass paths.
4. **No enumeration guards**: do not "fix" problems by adding keywords or blocklists (every such patch in this project's history was bypassed by the next case). If you change something, change the mechanism.
5. **Off by default**: a new guard is not enabled by default unless the maintainer explicitly approves.

PRs that relax a gate (letting more actions through) are reviewed more strictly than PRs that tighten one, so allow extra time.

## Commits and PRs

- Commit messages can be in Chinese or English; the first line says what changed and why.
- One PR does one thing.
- The checkboxes in the PR template are not a formality: if you do not have the evidence, write "none" instead of ticking the box.
- Never commit screenshots, audit logs, session records, chat or order content, keystores or passwords. The secret scan in CI will catch them, but please run `gitleaks dir .` locally first.

## Code of conduct

See `CODE_OF_CONDUCT.md` (Chinese: `CODE_OF_CONDUCT.zh-CN.md`).
