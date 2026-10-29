# dsh-dsb

English | [简体中文](README.zh-CN.md)

The installable DSH part of DSB: an independent, unofficial project built on
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness), not endorsed
by DeepSeek. This package is not the Android application or phone executor.

## Components

| Cordis entry | Purpose |
| --- | --- |
| `dsb-brand` | DSB illustration and name in the official sidebar slots |
| `dsb-report` | Authenticated, privacy-filtered execution reports and explicit ZIP exports |
| `dsb-ui` | Mobile sidebar, composer, typography, theme and clean feedback navigation |

All three entries use the same `dsh-dsb` Loader source with a `component` config.
DSH rejects multiple distinct client sources for one package; `./client` therefore
registers one lazy factory composing the two original slot clients. The bundle
also disables `ui-brand-official` (the slot otherwise conflicts) and
`ui-message-feedback`, as the existing DSB profile did. `dsh-recap` is independent
and is not installed, removed or reconfigured by this package.
For third-party users, this replaces the official brand slot and removes message
thumbs-up/thumbs-down controls and the upstream feedback form. To restore all
upstream UI, remove this bundle and restart; also remove any profile-owned
disable overrides for `ui-brand-official` / `ui-message-feedback`. To keep reports
but restore those controls, disable `dsb-brand` and `dsb-ui` and explicitly enable
the two upstream entries in the web profile's local patch, then restart.

## Compatibility

**Exactly DSH `0.2.1-rc.2`.** `peerDependencies["@deepseek-ai/dsh"]` is an exact
version, not a caret range. This is the gate enforced by app-boot at both bundle
and plugin composition boundaries; `engines.dsh` alone is not the relevant gate.
On another version, DSH skips the whole bundle and reports it in `skippedBundles`.
Do not grant a compatibility exemption: the UI selectors are version-specific.
Remove old disk patches first, or skipping the bundle cannot restore upstream UI.

## Install

Use a trusted checkout containing the built package assets, with DSH already
installed. Only the `web` profile is supported:

```sh
dsh plugin --profile web add /absolute/path/to/dsb/dsh-plugin
```

The package contains its client, illustration and report runtime dependencies;
no npm publication or build-script approval is needed. Python 3 and the DSB
Python dependencies are still required for report/export generation (Pillow for
optional screenshots; see `installer/requirements.txt` in the repository).
This does not install the phone executor, create an API key, or contact a device.
Restart DSH after installation/relinking, then reload the browser.

After a reviewed commit containing this directory is published, pnpm 12's
documented Git subdirectory form is:

```sh
dsh plugin --profile web add 'github:tianye1999/dsb#<reviewed-commit>&path:/dsh-plugin'
```

Replace `<reviewed-commit>` with that commit's hash; this is a syntax template,
not a claim that this uncommitted work is already available remotely. The quoted
`&path:` combines a revision and a subdirectory, as documented by
[pnpm package sources](https://pnpm.io/package-sources#install-from-a-git-repository-combining-different-parameters).
DSH's plugin-manager passes Git specs to `pnpm add` after its GitHub connectivity
check. Remote installation has not been performed or published by this task.

## Existing DSB Installations

The installer links `/root/dsh-plugin` as one `dsh-dsb` bundle and records its
`link:` dependency so the DSH package manager can also remove it. It recognizes old
`dsb-brand` and `dsb-report` symlinks only when they point to the sibling
`dsh-web-tweaks/<name>` deployment directories. It preserves unrelated bundles,
dependencies and `node_modules`. Foreign directories, foreign links, dependency
declarations or old bundle names without ownership evidence fail for manual
inspection. Manifest writes and validation are transactional; failed validation
restores original bytes/modes and keeps the old links, including on Python
KeyboardInterrupt/SystemExit. After successful migration, only the installer's
explicit list of old patch scripts is removed; restore/reload tools, recap and
unrecognized files remain. The owned `/root/dsh-plugin` deployment is staged and
validated before rename publication, with a recoverable previous-tree rollback
slot; a failed copy never deletes the installed version. Obsolete files are removed.

The installer checks the installed index before changing profiles. On legacy
markers it stops DSH under the existing maintenance guard, restores the bundled
pristine shell, and fails closed with a recovery command on error.
Restoration success immediately clears its own labelled maintenance marker,
before later installation steps. A rerun also clears a labelled residual marker
after checking that no legacy markers remain (including non-bundled unpatched
shells), saving evidence first; foreign markers are preserved and rejected.
Deployment/profile updates persist `~/.dsb-ipc/restart-required`, including
unchanged-profile reruns and plugin-only updates. The existing
`start-dsh.sh --restart` must replace old DSH PIDs and pass the token/client-plugin
readiness checks before that request is cleared and installation can succeed.
No legacy marker means no index write or restoration stop.
The plugin also suppresses its own UI rows on a legacy page and warns once per
component lifetime; it does not execute a second set of patch scripts.

For manual migration, **stop DSH first**. The installer's `/opt/dsb-dsh` dist is
not a pristine source: on upgraded phones it is the patched file being repaired.
Use the pinned shell shipped in this package's `runtime/`, including in npm/Git
package installations. In Ubuntu on an installer-managed phone:

```sh
python3 /root/dsh-plugin/runtime/restore-index.py \
  --index /opt/dsb-dsh/node_modules/@deepseek-ai/dsh-web-frontend/dist/index.html
```

It requires the pinned pristine SHA-256, known legacy markers, matching asset
names and existing asset files; it retains an exclusive `.before-dsh-dsb` backup
(unique suffix on retries) and removes its stale regular temporary file before
restoration. Fix the reported disk/permission/version issue and rerun the helper,
then rerun the installer; its own maintenance marker is recovered automatically.
It does not guess a DSH installation, download files or reapply patches. It
refuses unknown/mixed versions. Review any unrelated custom HTML before using
it: restoration replaces the entire shell. `--pristine` defaults to the bundled
`runtime/pristine-0.2.1-rc.2.html`; for other installations substitute the package
and target paths. Restart DSH after restoration. Normal plugin rendering never
writes dist; the installer only performs this one-time legacy restoration.

Reports import packaged bridge helpers, not a repository-relative or legacy
phone-local `task-report-store.mjs`. Existing `BRIDGE_AGENT_DIR`, `BRIDGE_PYTHON`,
`DSB_AUDIT_FILE` and `DSB_ACTIVE_TASK_FILE` retain their meanings. On a deployed
phone, keep the existing `report-paths.sh` environment and `.dsh-phone` data.
The installer sets `BRIDGE_AGENT_DIR=/root/.dsh-phone/skills`: that deployed copy
supplies `task_report.py`, `feedback_export.py` and the vocabulary, not
`runtime/agent`. Without the override, an existing `~/.dsh-phone/skills/task_report.py`
also takes precedence; otherwise bundled scripts are used read-only. Audit,
ledger and report output always defaults beneath writable `~/.dsh-phone`
(`skills/safe_tap_audit.jsonl`, `active-task.json`, `reports/`), never the package
directory. Explicit data-path overrides retain their meanings. No phone execution
evidence is manufactured. An invalid agent/vocabulary disables only the report
component with a warning; brand/UI load independently.

Report bridge helpers always come from `runtime/bridge`, not `/root/dsh-bridge`.
Updating only the plugin does not update deployed agent scripts; updating only
`/root/dsh-bridge` does not update the report helpers. Keep both at one source
revision by running `node dsh-plugin/build.mjs` in the checkout, then rerunning
the installer, which deploys `agent/` to skills and the rebuilt plugin together.
Do not copy just one Python helper or hand-edit the bundled runtime copies.

## Uninstall

```sh
dsh plugin --profile web remove dsh-dsb
```

Restart DSH and reload the page. The package does not persist edits in upstream
dist; upstream UI returns once the bundle is removed. Profile overrides remain
owned by the profile, and historical disk edits require the restoration step
above. Uninstalling does not delete reports or audit data. Do not separately
reinstall the two legacy packages alongside this bundle.

## Privacy

Feedback uses only `https://github.com/tianye1999/dsb/issues`, in the current view,
without account/device query parameters. Reports expose allowlisted fields;
exports remain authenticated and optional screenshots require an explicit export.
No extra telemetry is added by this bundle.

DSH's built-in package-inventory feature is **enabled by default**: official
DeepSeek API requests include enabled, active plugin package names and versions
in `dsh_plugin_packages`. This field contains package metadata, not conversation,
report or device content; this does **not** mean ordinary model requests contain
no conversation content. Its `enabled: false` setting disables the inventory.
See the shipped `@deepseek-ai/dsh-plugin-package-inventory-deepseek/README.md`.

## Development And Evidence

Run `node dsh-plugin/build.mjs` after editing the original clients or shared report
helpers; `--check` verifies generated client, icon and runtime copies against the
canonical repository sources. Do not hand-edit `runtime/` or `client.js`.
Run `node --test dsh-plugin/tests/*.test.mjs dsh-plugin/ui-contract.test.mjs
dsh-plugin/dsb-report/*.test.mjs dsh-plugin/dsb-report/test-*.mjs`.
Set `DSH_TEST_MODULES` to a locked installation's `node_modules` for the real
Cordis parser, compatibility, renderer and route-security tests.

[Migration mapping and fixture provenance](MIGRATION.md) describe all 11 legacy
patches, the 15 current blocks, their placement and the headless acceptance runner.
