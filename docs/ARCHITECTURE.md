# Architecture

**English** · [简体中文](ARCHITECTURE.zh-CN.md)

## Three deployment layers

| Layer | Runs on | Directory | Role |
|---|---|---|---|
| **App** (`com.dsb.assistant`, shown as "逗沙包") | Android | `app/` | Eyes and hands: accessibility service (reads the view tree, dispatches gestures and input), floating ball (orange/blue), the dsh web UI in a WebView, notification listener, command entry `CmdReceiver` |
| **Termux host** | Termux app | `termux/` | sshd, adb self-connection, watchdog (`supervisor.sh`), boot chain (`boot.sh`/`shutdown.sh`), ball status light (`adbq.sh`/`ball.sh`), keep-screen-on daemon |
| **Container** | proot Ubuntu inside Termux | `agent/` `bridge/` `dsh-config/` `dsh-web-tweaks/` | Brain (dsh + DeepSeek), task bridge, execution primitives, OCR/vision |

## Life of a task

1. You submit a task in the App's web UI → the App calls the bridge's `/do` with a `task_id` (UUID).
2. **Bridge** (`bridge/bridge.mjs` + `task-ledger.mjs`): the ledger checks that the `task_id` is idempotent, no task is running and the runtime state is known → it hands your words to the dsh runtime (the `phone` profile; the `agent/check_gate.py` command hook is optional and disabled by default in installer-created profiles).
3. The **brain** reads the skill notes written for it under `docs/internal` (the release version is `agent/SKILL.md`) and calls the primitives in `agent/` through bash:
   - `safe_tap.py open --app X --vd`: create a scrcpy virtual display and open the app on it
   - `safe_tap.py look --display N`: capture a frame + view tree + OCR (when the tree is empty) → issue a **credential** (shot_id)
   - `act.py tap --target "text or description"`: tree → exact OCR text match → visual localization only if nothing matched and a region was given
   - `safe_tap.py tap/swipe/scroll/key/type --shot <id>`: every action passes the gates before dispatch
   - `vd_session.py stop`: close the display and delete the recording
4. Each action: gates → (R3 pre-dispatch recheck) → dispatched through the App's accessibility service to the chosen display → wait for the receipt → post-action screenshot → audit.
5. The brain writes a conclusion card → the bridge returns it to the App; the ledger records `reply_received`. "Stop" goes through `/cancel`, and `cancelled` is recorded only after every child process is confirmed to have exited.

## Gates (`agent/safe_tap.py`)

| Gate | What it checks |
|---|---|
| G1 | The credential is valid for 90 seconds; the foreground app has not changed; virtual-display credentials are bound to displayId / uniqueId / size / session |
| G2 | A keep-out zone around view-tree nodes containing danger words (40 px padding) |
| G2b | Empty view tree: caller confirmation + OCR danger words (full-width bands) + a visual verdict for taps; swipe-like actions are refused on the whole page when it is dangerous; keys allow only navigation and paste |
| G3 | System gesture areas (left/right edges, top, bottom navigation area) are off-limits |
| G4 | Repeated taps on the same point and a gesture budget per foreground app; the circuit breaks when exhausted |
| G5 | Before/after screenshots and an audit record (`safe_tap_audit.jsonl`) |
| R3 | Just before dispatch, capture a fresh frame and compare the target's row with the credential frame (refuse if the 48×16 grayscale mean difference is > 6) |

The chain must not be described as "every decision fails closed". On empty-tree pages, unavailable OCR refuses, and a successful vision verdict with `impact≠none` (including `unclear`) refuses. **When the vision service fails, an action can still be approved on OCR alone if OCR found no danger words, the caller confirmed the screenshot and the other gate conditions hold**; this is recorded as `ocr_clean_ack_vision_unavailable`. If OCR found danger words, the action is refused. Source: [OCR branch](../agent/safe_tap.py#L1477), [vision verdict and fallback](../agent/safe_tap.py#L1516).

When enabled, the command-level TypeSafe hook has a fail-open branch when its service is unavailable ([code](../agent/check_gate.py#L162)). The brain, the gates and the credentials run as the same Termux Linux user, so the gates cannot stop a compromised brain from rewriting or bypassing them; see how the [brain process is started](../bridge/bridge.mjs#L122).

## Virtual display (`agent/vd_session.py`)

`scrcpy --new-display=720x1600/320 --no-vd-system-decorations --no-window --no-audio` creates a display that holds only the target app. `verify()` checks in one adb round trip that the display still exists, its uniqueId, size, type and overlay setting, that scrcpy is alive, that the target app is in the foreground on that display, and the app's pid; any mismatch refuses. Third-party app actions on the main display are refused by default (`vd_policy`).

## Empty-tree pages (WeChat, Taobao-family and JD Taro pages)

When the view tree cannot be read, `look` runs OCR automatically to produce `pixel_nodes`; G2b builds danger bands from the OCR lines; before a tap, a ±240 px crop around the target with a red circle is sent to the vision model with the question "what happens if you tap here?". A successful verdict with `impact≠none` refuses; a service failure falls under the OCR fallback rule above, so not every vision failure refuses. Source: [crop and request](../agent/vision_check.py#L36), [verdict and fallback](../agent/safe_tap.py#L1516).

## Data flow and retention

- **Cropping is a per-path constraint**: the gate's visual recheck sends only a crop around the target, and visual localization sends only a crop of the given region ([recheck](../agent/vision_check.py#L36), [localization](../agent/vision_check.py#L114)). The brain as a whole does not receive only crops.
- **Full-screen questions have a separate full-frame path**: the App's "Ask DSH" passes a full screenshot, the view tree and recent notifications to `/ask`; API mode uploads the full frame directly to the default DeepSeek API, and dsh mode is instructed to `read_image` the full screenshot. dsh's internal image transport is not in this repository, so we cannot promise that only crops leave the phone. Task and page text can also reach the cloud model; running dsh on the phone does not mean inference is offline. Source: [App input](../app/src/com/dsb/assistant/OverlayService.java#L2273), [question bridge](../bridge/bridge.mjs#L548), [API upload](../bridge/bridge.mjs#L144), [dsh image entry](../bridge/bridge.mjs#L170).
- **Optional TypeSafe review**: the installer does not enable the command hook by default. When the hook is enabled and a key is configured, suspicious shell command excerpts (up to 1500 characters), the tool name and the matched features are sent to the TypeSafe cloud service; when it is unavailable the hook fails open. Source: [default installer patch](../installer/cordis.patch.yml#L2), [review input](../agent/check_gate.py#L151), [service and key](../agent/jev_client.py#L23), [unavailable branch](../agent/check_gate.py#L162).
- **Screenshots are stored and audit records are not uniformly redacted**: before/after screenshots and question screenshots stay on the phone; `audit()` serializes records directly, and `note` / `expect` / vision explanations can carry chat or order text, so we do not promise plaintext-free audits. The screen-action audit also does not cover every bash command the brain runs. Source: [action records and screenshots](../agent/safe_tap.py#L1943), [audit writer](../agent/safe_tap.py#L369), [question screenshots on disk](../bridge/bridge.mjs#L87).

## Chinese text input (`type`)

The App command `剪贴板 #写:<nonce>:<base64>` writes the clipboard → the App writes a receipt `files/clip-<nonce>.json` (operation, result, length and other metadata, without the clipboard text) → the paste key is sent only after the receipt checks out → DSB then tries to clear the clipboard and checks the clearing receipt → OCR reads the field back to verify. A failed clear is recorded as `clipboard_cleared=false`, so clearing is not guaranteed; and a plaintext-free receipt does not imply that all audits are redacted. Source: [receipt check](../agent/safe_tap.py#L2233), [paste / clear](../agent/safe_tap.py#L2252).

## Control plane

| Channel | Authentication / design | Current status and source |
|---|---|---|
| `CmdReceiver` broadcasts (ball colour, clipboard, stop, …) | Receiver permission `INJECT_EVENTS` (shell/root/system) | [Permission declared in the manifest](../app/AndroidManifest.xml#L99) |
| adb server | Termux-private filesystem socket, no TCP (R6-0) | The installer attempts private transport hardening by default, starting with a private bootstrap at first pairing; the maintainer's device moved to the same layout on 2026-10-23 and passed a reboot acceptance. If preflight is skipped or hardening is rolled back, ordinary TCP 5037 remains as a weaker fallback with an explicit warning, and other apps may then obtain the shell identity. This covers new installs: the public tool does not migrate legacy deployments, and package upgrades were accepted on an arm64 emulator (after a real `pkg upgrade` the old vendor adb could no longer load, while private ADB and doctor kept working); package-upgrade acceptance on the maintainer's device is still pending. See the [threat model](THREAT-MODEL.md#4-control-local-control-plane-authentication) |
| App ↔ bridge task channel | Termux `RUN_COMMAND` + private socket (R6-1) | [Task transport code](../app/src/com/dsb/assistant/DshClient.java#L31) and the [private listener](../bridge/private-listener.mjs#L5) are in place; the maintainer's device switched over and passed acceptance on 2026-10-22 |

Boundaries: [THREAT-MODEL.md](THREAT-MODEL.md).

## Tests

- `agent/`: offline unit tests (gates, credentials, concurrency, virtual-display binding, R3, OCR, walk, dsb_run, …); none touch a device
- `bridge/`: ledger, cancellation, restart audit
- `installer/` and `hardening/adb-private/`: offline installer and private ADB transport tests
- `app/tests/`: real-device gate scripts and notes
- Real-device gate: changes to the gates, the bridge or the App need real-device evidence (see [CONTRIBUTING.md](../CONTRIBUTING.md))
