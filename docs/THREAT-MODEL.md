# Threat Model

**English** · [简体中文](THREAT-MODEL.zh-CN.md)

This document describes **what DSB's current defences can constrain and what they explicitly do not defend against**. It is not an absolute security guarantee. A project that competes on trust has to state its boundaries clearly; if you find that real behaviour differs from what is written here, report it as described in [SECURITY.md](../SECURITY.md).

## What we protect

1. **Irreversible actions** in third-party apps on your phone: paying, transferring money, sending, deleting, closing accounts, granting authorization.
2. Your **privacy**: chats, orders, verification codes and clipboard contents on the screen.
3. Your **attention**: the main screen is not taken over or interrupted.
4. **Control** over DSB: only you (through the App) can give it tasks.

## Trust boundaries

| Party | Trust level | Notes |
|---|---|---|
| You, giving tasks through the DSB App | Fully trusted | |
| The brain (dsh + model) | **Its judgement is not trusted; its environment is** | It makes mistakes and can be misled by text on a page, which is why the gates exist. But it runs as the **same Termux Linux user** as the gates and credentials, so the gates cannot stop "a compromised brain rewriting the gate code" |
| Other apps on the phone | Not trusted | By design they get no control; the current permission bar and the ADB exposure risk on self-installed devices are described under "Control" below |
| Parties with root, authorized ADB, or Termux execution rights (RUN_COMMAND) | **Trusted as much as DSB itself** | They can already do everything DSB can; DSB does not try to defend against them |
| Third-party apps themselves | Their content is not trusted | Page text is data, not instructions |

## Existing defences and their limits

### 1. Brain mistakes: execution gates

Screen actions on the normal execution path go through `agent/safe_tap.py`. It is not strongly isolated from the brain and cannot stop a compromised brain from bypassing it. The checks and evidence are:

- **G1 credential**: an action must be based on a `look` from the last 90 seconds, and the foreground app must not have changed; virtual-display actions are also bound to the display ID, uniqueId, size and session
- **G2 danger zone**: no taps around view-tree nodes containing danger words (pay, delete, send, authorize, …)
- **G2b empty view-tree pages**: the caller must explicitly confirm it has looked at the screenshot; unavailable OCR refuses, and danger words found by OCR create keep-out zones; a successful visual verdict for tap / long press must be `impact=none`. **When the vision service fails, an action can still be approved on OCR alone if OCR found no danger words and the other conditions hold**; if OCR found danger words, the action is refused. Non-tap actions such as swipes are refused on the whole page when it has danger words; navigation keys are exempt, and the only other key allowed is paste. Source: [OCR and danger bands](../agent/safe_tap.py#L1477), [vision verdict and fallback](../agent/safe_tap.py#L1516)
- **G3 edge bands**: no taps in system gesture areas
- **G4 circuit breaker**: stop after repeated failures on the same point or with the same gesture
- **G5 evidence**: before/after screenshots + audit
- **R3 pre-dispatch recheck**: just before dispatch, capture a fresh frame and compare the target's row with the credential frame; refuse if it changed

These checks are not a guarantee that "the whole system fails closed". When enabled, the command-level TypeSafe hook has a fail-open branch when its service is unavailable; see the data flow below. Rules for changing a gate are in [CONTRIBUTING.md](../CONTRIBUTING.md).

### 2. Privacy: what leaves the phone and what stays

- **Gate recheck and visual localization**: the recheck sends only a crop around the target, and localization sends only a crop of the given region; this is not a crop-only guarantee for the brain as a whole. Source: [recheck crop](../agent/vision_check.py#L36), [localization crop](../agent/vision_check.py#L114).
- **Full-screen questions and the model API**: "Ask DSH" passes a full screenshot, the view tree and recent notifications to the brain; API mode uploads the full frame directly to the default DeepSeek API, and the dsh path is instructed to read the full screenshot with `read_image`. dsh's internal image transport is not in this repository, so we cannot promise that only crops leave the phone; task and page text can also reach the cloud model. Source: [App question input](../app/src/com/dsb/assistant/OverlayService.java#L2273), [question bridge](../bridge/bridge.mjs#L548), [API upload](../bridge/bridge.mjs#L144), [dsh image entry](../bridge/bridge.mjs#L170).
- **DSH full session logs**: the patches for all three public profiles turn off `session-log-deepseek.enabled` (upstream default true; when on, full session events are uploaded with requests to the official DeepSeek API). Turning it back on uploads the events accumulated while it was off, so temporarily disabling it is not data deletion. The patch takes precedence over the web form; installation and copy sync both keep it false. Separately, `session-telemetry-otel` defaults to `FEEDBACK_ONLY`: only an explicit thumbs-up/down on a message or session feedback authorizes uploading that session's log, and an existing feedback authorization can be replayed on a later load. The DSB web UI disables the corresponding UI but does not block the underlying feedback API, and it does not change the data flow of normal model requests. The feedback item in the More menu opens a clean GitHub Issues URL; anything you post publicly still needs manual redaction. `desktop-product-telemetry` is enabled only for a profile named desktop; the phone web / phone / headless profiles do not enable it. Check the effective values with `dsh --profile <p> --dump-config`.
- **TypeSafe cloud review**: when the optional command hook is enabled and a key is configured, shell command excerpts that match suspicious features (up to 1500 characters), the tool name and the matched features are sent to TypeSafe; when the service is unavailable the hook fails open, and it does not replace the screen gates. The installer does not enable the command hook (`hooks-cc` / `check_gate.py`) by default; the repository ships optional templates such as `dsh-config/cordis.patch.web.yml`. Under proot, an enabled hook needs a working sandbox or service-level full access (for example, service environment `DSH_PERMISSION_MODE=danger-full-access`, which also changes the service-level approval default); otherwise it does not run. Source: [installer profile copy](../install.sh#L143), [default installer patch](../installer/cordis.patch.yml#L2), [optional web template](../dsh-config/cordis.patch.web.yml#L35), [review input](../agent/check_gate.py#L151), [service address and key loading](../agent/jev_client.py#L23), [fail-open when unavailable](../agent/check_gate.py#L162).
- **Clipboard receipts**: write receipts record only metadata such as the operation, success and length, not the clipboard text; after pasting, DSB tries to clear the clipboard and checks the clearing receipt, but the attempt is not guaranteed to succeed. Source: [receipt check](../agent/safe_tap.py#L2233), [paste and clear](../agent/safe_tap.py#L2252).
- **Screenshots and audits are written to disk**: before/after screenshots and question screenshots stay on the phone, so we cannot claim "nothing is written to disk"; audit writes are not uniformly redacted, and `note` / `expect` / vision explanations can carry chat or order text, so we cannot promise plaintext-free logs. Check for sensitive content before sharing. Source: [screenshots and action records](../agent/safe_tap.py#L1943), [audit serialized directly](../agent/safe_tap.py#L369), [question screenshots on disk](../bridge/bridge.mjs#L87).
- Familiar-route notes **should** record only paths and page features; this is a usage convention, not automatic redaction, and the notes should be handled as sensitive local files.

### 3. Attention: virtual display enforced

Third-party apps are always operated on a scrcpy virtual display; main-display actions are refused by default; while the main display's foreground is changing, creating the virtual display waits.

### 4. Control: local control-plane authentication

- The App's command entry (`CmdReceiver`) requires the sender to hold the system permission `INJECT_EVENTS`: only shell (ADB), root, the system and platform-signed apps can send; ordinary apps cannot, and the permission cannot be granted to them. The `token` parameter in commands is only kept for compatibility with old scripts and **is not a secret**. Source: [entry permission](../app/AndroidManifest.xml#L99).
- The ADB server's lockdown design uses a filesystem socket in a Termux-private directory and no TCP port. The maintainer's device moved to the same private transport layout as the installer on 2026-10-23 and passed an acceptance run that included a reboot: after the reboot only one managed server started, identity was automatically re-verified and bound, and doctor, the background virtual display and real tasks all passed; package upgrades were accepted on an arm64 emulator (after a real `pkg upgrade` the old vendor adb could no longer load, while private ADB and doctor kept working); package-upgrade acceptance on the maintainer's device is still pending. The public tool still does not migrate legacy deployments. The installer tries the repository's [general deployment tool](../hardening/adb-private/README.md) by default: the first pairing already uses the private bootstrap, and a foreign listener on 5037 is refused in red; when preflight is skipped or a strict rollback succeeds, an "ADB not hardened" warning is shown, and ordinary TCP is only a weaker fallback. If the hardened state fails its check, DSB refuses strictly and does not hide it behind a skip flag. Other devices, and package upgrades on real devices, still need acceptance; limited testing does not mean every device is locked down.
- The **code** for the task channel between the App and the brain uses Termux's `RUN_COMMAND` (the system checks the caller's permission), and a private filesystem socket inside Termux. **Status (R6-1): the code has been rewritten this way (the bridge listens only on the private socket, and 3097 no longer hands out a token); the maintainer's device switched over and passed acceptance on 2026-10-22**: real tasks over the private channel; closing and reopening gives a new instance with the ledger unchanged; after an idle bridge exits on SIGTERM, the restart locks down automatically with the ledger unchanged (in-flight tasks, SIGKILL and power loss were not covered); an independent negative-test app (ordinary UID) was refused on 3096, got 404 when fetching the token, and all four protected broadcasts were rejected by the system for lacking `INJECT_EVENTS`. Self-installed devices need the same switch-over. Source: [App task transport](../app/src/com/dsb/assistant/DshClient.java#L31), [private socket permissions](../bridge/private-listener.mjs#L5).
- Public App lockdown (A6): only one accessibility service, registered under its real class name; no self-update, no clicking install/allow/authorize buttons on your behalf, no writing the accessibility switch back automatically; the boot broadcast receiver is not exported and checks the action first; release builds do not open the WebView debugging port; logcat does not record URLs or task/screen/notification text (the panel and local files may still contain it)

## Explicitly not defended (residual risks)

| Risk | Why it is not defended / status |
|---|---|
| **A compromised brain bypassing the gates** | The brain, gates and credentials share one Linux user; file permissions and prompts are not enforced isolation. Defending this requires moving final authorization somewhere the brain cannot change (for example the App side); it is on the roadmap |
| OCR misreads, icon-only dangerous buttons | Empty-tree pages depend on OCR / vision, and both can misjudge; a successful `unclear` verdict refuses, but there is still an OCR fallback that approves when the vision service fails (see [G2b](../agent/safe_tap.py#L1516)) |
| Top and bottom edges of very tall dangerous buttons | Bands cover only 40–70 px above and below a text line; taps at the edges rely on vision alone |
| Authorized ADB wireless debugging (5555) | Anyone on the same Wi-Fi can send a connection request; **never tap "Allow USB debugging" for an unfamiliar device on the phone** |
| Text on a page steering the brain (prompt injection) | The brain can be misled by page content; the gates constrain execution but do not stop the steering itself, and cannot guarantee that every irreversible action is recognized and blocked ([visual misjudgement / fallback limits](../agent/safe_tap.py#L1516), plus the same-user bypass risk) |
| Third-party apps' anti-automation | Platforms may detect and restrict automated access; that is at your own risk |
| Late tools from an ended task | A background process that starts a tool only after the next turn begins can be attributed to that newer turn; the single active-task slot cannot distinguish its origin |

## Rules for users

1. Do not authorize USB/wireless debugging for unfamiliar devices.
2. Do not switch the gates to `warn`/`off` mode (the config allows it, but then it is no longer DSB).
3. On login pages, captchas and payment confirmations, stop and take over manually; do not treat "it will stop on its own" as an absolute guarantee, and **do not** help it bypass the gates just to get a run through.
4. The audit log (`agent/safe_tap_audit.jsonl`) is yours; check now and then what it refused.

Further trust hardening is [planned](ROADMAP.md#trust-hardening-delivering-stronger-promises).
