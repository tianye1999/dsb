# Injection Migration

## Frozen Reference

`tests/fixtures/pristine-0.2.1-rc.2.html` is the unmodified frontend index from
`npm ci --ignore-scripts` using `installer/dsh/package.json` and
`installer/dsh/npm-shrinkwrap.json`. SHA-256:
`3a402b94efa1253d80666438e60ed89d0b5cf1fd07d24a2f59731623e9bd8a49`.
`build.mjs` copies it and the restoration helper into published `runtime/`.
The installed `/opt/dsb-dsh` index is the restoration target, not a clean source.

`tests/fixtures/legacy-0.2.1-rc.2.html` was produced by the original `apply-all.sh`
chain at repository revision `df15f02`, against a disposable copy of that dist.
Only the verifier's temporary output directory was changed. Its 11 patch steps,
14 markers/blocks, script syntax, markup leak and resource checks passed.
SHA-256: `6b7ccf50a7939edb1ad1274774ffebd5b4aaa51100456f69a49d06cd6cd283a7`.
These two HTML files are public package/synthetic fixtures, not captured user pages. The fixed
fixtures make the equivalence test offline; it does not download npm packages or
keep an executable legacy patch chain in the repository.

## Placement Rules

The DSH `style` row inserts before the app's stylesheets and cannot preserve the
old cascade. All seven styles therefore use `html` rows at the **start of body**,
in their original document order: composer, repair, font, content, chat background,
privacy, rail. They are still after all upstream stylesheets. Head-end styles move
across only the head/body boundary; the old body-end rail style follows them just
as before. The original declarations remain byte-identical, with the five phone
button rules described below appended to rail so they override the small control.

The eight scripts use body `html` rows in phone order: rail, composer, gesture,
autohide, tabs, persistent bottom control, light theme, privacy. All script bodies
except autohide and the added bottom control remain unchanged.
DOMContentLoaded guards, mutation observers and delayed lookups accommodate the
earlier position, before the root node is parsed. Tests exercise both fresh and
saved composer state. Raw upstream markup is unchanged.
HTML comments and whitespace outside executable/style blocks are not injected.

Every injected style/script is wrapped with `dsh-dsb:ui:start/end` comments.
The official 0.2.1-rc.2 `renderIndex` renders rows first, then runs `tapIndex`.
Our tap removes these owned wrappers in a temporary copy and checks the remaining
page for any old patch ID or legacy comment. If found, it returns that copy,
preserving all old blocks and unrelated/upstream injections (including DSH's
boot-ready script), and warns once per component lifetime. Otherwise it returns
the injected page unchanged. This is response-local, not a shared request flag;
it never strips old scripts or touches disk. Even partially patched pages retain
their old UI rather than executing an additional full set. This guard requires
the supported web server rendering path, not a standalone static boot payload.

The installer detects old markers before `setup_profiles`, holds the existing
maintenance guard, stops DSH, restores the pinned bundled shell and immediately
removes its own marker on success, before any subsequent interactive/fallible
installation steps. Markers contain `dsb-installer index-restore <epoch>`; a rerun
clears such a residual marker whenever the read-only legacy check reports
`untouched`, including a newer/custom unpatched shell, without overwriting that
shell. It saves the marker plus check outcome in `index-restore-evidence.*` first.
Owned markers on legacy pages resume restoration; foreign/unlabelled markers
are never removed and block installation.
Restoration fails closed on mismatch/error with a manual command. After profile validation it retires only
known old patch script filenames, then uses `start-dsh.sh --restart` at the startup
stage. Failure leaves the maintenance marker for manual recovery; successful
restoration retains the exclusive `.before-dsh-dsb` backup. Existing backups are
never overwritten: retries use a unique suffix, and restoration cleans its stale
regular `.dsh-dsb-tmp` file before writing. Unsafe temporary symlinks are refused.

Before deployment or profile mutation, the installer durably writes
`~/.dsb-ipc/restart-required` with its own identifier. It survives interruption,
unchanged profiles and plugin-only updates. Every installer deployment requests
one final controlled restart, conservatively including byte-identical reruns.
Only disappearance of every old matched DSH PID, presence of new DSH PIDs, a
token and successful existing client-combo readiness probe clear that request.
`start-dsh.sh` exit status alone is not success; `NO_TOKEN_FOUND` is rejected.

Plugin deployment validates the source and a sibling staging copy (package name,
required entries/assets, no symlinks), then publishes the complete directory by
same-filesystem rename. A `.dsh-plugin.previous` rollback slot retains the old
tree across the rename window; failures restore it, and interrupted publication
is recovered on rerun. This is not an in-place recursive delete/copy. Both Python
and shell profile rollback preserve package.json and cordis.patch.yml, including
absence and permissions; legacy unlink intent is recorded before unlinking.

## Phone Button Baseline

The owner selected the UI of their existing 0.2.1-rc.2 phone homepage after a
headless comparison on 2026-10-29. It contains active historical button behavior
not reproduced by the frozen repository patch chain. This intentional exception
preserves that interaction instead of treating every older rule as dead code.

`tests/fixtures/phone-buttons.json` contains only two script bodies and five CSS
rules extracted from that supplied homepage, with no account/device information;
the full captured page is not included in the public repository.

- `dsh-tobottom-keep-script` is copied verbatim, inserted after hide-tabs and before
  light-enforce as on the phone. It creates the only visible 34px/0.85-opacity
  control, scrolls without showing the composer when away from the bottom, and
  toggles the composer only at the bottom. Its old introductory comment mentions
  revealing while scrolling, but the executable branch deliberately does not.
- `dsh-composer-autohide` is replaced verbatim by the phone version. The added
  check suppresses queued hiding within 1500ms of manual control activation.
- Rail gains exactly five phone rules: hide the original composer dot, style the
  persistent control, its hover and SVG size, and hide the native bottom button.
  The 61 duplicated rules, zoom and obsolete `Bao` branding are not copied.
- Gesture moves before autohide to preserve phone listener/timer registration
  order; its bytes do not change. Background keeps the chain's equivalent rules
  instead of copying a comment-only phone difference. All styles still follow
  application stylesheets, and the new rules follow the original composer style.
- Both restoration detection and the response guard also recognize standalone
  `dsh-tobottom-keep` and historical `dsh-dark-enforce` markers.

| Old chain step | Final blocks affected | New rows / reason |
| --- | --- | --- |
| `patch_sidebar.py` | rail style + script | marked body HTML; preserve overlay layers 100/120 |
| `patch_ui2.py` | rules folded into rail style | same body HTML; preserve final combined rules |
| `patch_composer.py` | composer style + script, including bottom hook | marked body HTML; same visibility/localStorage behavior |
| `patch_autohide.py` | composer-autohide script | marked body HTML; phone's 1500ms manual protection replaces this chain body |
| `patch_hidetabs.py` | hide-tabs script | marked body HTML script; same observer/hide behavior |
| `repair_index.py` | repair style, light-enforce script, refreshed rail script | marked body HTML; preserve light enforcement, thinking and conclusion styling |
| `patch_fonts.py` | font-rules style and final rail sizing | body HTML in final cascade order |
| `add_missing_rules.py` | content-rules style | body HTML after font rules |
| `patch_gesture.py` | composer-gesture script | marked body HTML script; unchanged touch thresholds and footer hooks |
| `patch_chatbg.py` | chat-bg style | body HTML after content rules |
| `patch_privacy_ui.py` | privacy-ui style + script | marked body HTML; exact clean feedback capture and product-label observer |

The unused standalone `patch_theme.py` and `patch_conclusion.py` were not in the
chain. Its effective theme/conclusion behavior came from `repair_index.py`; the
final UI forces light even when dark attributes/preferences are set. This migration
does not add dark-theme support. Historical asset-name repair is unnecessary for
response-time injection, which always starts with the installed pristine shell.

## Acceptance

`ui-injections.test.mjs` checks 12 original block bodies byte-for-byte, original
rail plus exactly five phone rules, phone autohide and added control bytes, the
explicit phone script order, syntax, prohibited closing tags, fresh/stable rows on
repeated renders, original markup equivalence and critical CSS/privacy markers.
`ui-contract.test.mjs` retains brand-slot, observer coalescing and clean feedback
assertions. With `DSH_TEST_MODULES`, `package.test.mjs` exercises the actual DSH
patch parser, bundle rejection and index renderer. Report tests retain real DSH
Connection/cookie security and export/client/turn behavior coverage.

Idempotence means repeated DSH responses render the same pristine source with a
fresh injection table and never accumulate blocks or touch dist. Passing an
already-rendered response back as a new pristine source is not DSH's rendering
contract and would duplicate upstream boot rows too.
The separate upgrade test renders `legacy + rows` through the real renderer and
registered tap, asserting unique IDs, one `openFeedback`, unchanged legacy blocks
and one warning over repeated requests. Partial-ID/comment-only pages are covered.

## Headless Browser Reproduction

`tests/browser-compare.mjs` is an opt-in runner, not a device runner. Supply
`DSB_PLAYWRIGHT_MODULE` (the absolute Playwright module path),
`DSB_BROWSER_OLD_URL`, `DSB_BROWSER_NEW_URL`, and `DSB_BROWSER_OUTPUT` (a directory
under `/tmp/dsb-t19-*`). Optionally set `DSB_BROWSER_EXECUTABLE` to a local browser.
It always launches headless, closes all contexts, and intercepts GitHub navigation
locally instead of sending a feedback request. Keep authentication URLs out of logs.
Set `DSB_BROWSER_LEGACY_PLUGIN_URL` for a third real DSH instance with legacy dist
and the new bundle enabled. All three pages must have unique patch IDs and exactly
one feedback script; the third must match the old page's states/computed styles.

For the current controlled reference, start two separate copies of the locked DSH install
with separate HOME and DSH_HOME, `web --no-open --port 0`, no API key, and the same
brand/report bundle. The old copy uses the supplied phone index and a profile
override disabling `dsb-ui`; the new copy uses pristine dist and all three rows.
Do not modify either real user profiles or a shared runtime installation.

Historical verification before adopting phone controls on 2026-10-29: both genuine DSH pages started, then headless Chrome
passed all four old/new combinations (390x844 and 1440x1000; fresh and saved-hidden
composer state). Compared sidebar/composer states and computed styles, sampled
markdown typography, loaded brand image, dark-attribute removal, observer
coalescing and the real account-menu feedback navigation. All sampled values were
equal, no page errors or failed asset/plugin responses occurred, and the new
dist's SHA-256 remained pristine. Markdown samples were synthetic DOM fixtures;
no model requests or phone operations were performed. Temporary screenshots and
runtime data are removed after acceptance; the runner preserves reproducibility.

For phone acceptance, set `DSB_BROWSER_PHONE_SESSION` to the same handwritten
offline session ID in both isolated servers. `browser-compare.mjs` delegates to
`browser-phone-compare.mjs`: 360x800, DPR 3, touch enabled, initially shown/hidden,
eight screenshots each. It checks exact button size/position/color/arrow, hidden
native/small controls, scroll-only return, subsequent reveal, gestures and queued
auto-hide protection. No prompt is submitted. Prepare the sample using DSH's
Session + sessionPersistence create/append/close and workspace.attachSession APIs,
not by invoking an Agent. Screenshots remain temporary unless explicitly retained
as user-requested comparison artifacts.

Revision-3 phone verification on 2026-10-29 started both genuine locked DSH
0.2.1-rc.2 services. Headless Chrome passed A/B-prime with both initial composer
states at 360x800 / DPR 3: all 16 paired scene measurements matched, including
the 34px / 0.85-opacity control, hidden native/small controls, scroll-only return,
arrow state, sidebar, reveal gestures and queued manual protection. Ordinary
clearing still hid the composer. The rich editor was cleared using select-all
and Backspace, and the test waited for the actual auto-hide queue before clicking.
All 32 screenshots were retained in the owner's requested comparison directory.
No page errors, failed assets or duplicate patch IDs occurred. A's disk shell
remained identical to the supplied file; B-prime's disk shell remained pristine.
The handwritten history has no bridge execution ledger, so the common report
failure notice is not a report acceptance result. Physical Android/IME behavior
and screenshot pixel-for-pixel identity are not claimed by this browser test.

Revision-1 verification also passed the full three-page matrix: legacy with UI
disabled, pristine with the new bundle, and legacy with the new bundle. The third
instance additionally had a nonexistent `BRIDGE_AGENT_DIR`; report initialization
warned without rejecting app-boot or breaking brand/UI. All 12 page scenarios
(three pages times four viewport/composer combinations) matched, with unique
patch IDs and one `openFeedback` per page. A temporary Cordis logger exporter
observed exactly one legacy warning and one report warning across requests;
the normal launcher does not print every Cordis warning to stdout. The pristine
dist remained byte-identical and all three temporary servers were stopped.

Local `dsh plugin --profile web add <absolute-package-path>` was also verified
with isolated pnpm 12.4.2 and the locked DSH launcher; the profile gained one
`link:` dependency and one `dsh-dsb` bundle. The remote GitHub form remains a
documented syntax template, not a remotely published or tested artifact.
The corresponding `remove dsh-dsb` command was verified to remove the dependency
and bundle selection while retaining the two upstream base/Web bundles.
