# Installing DSB (installer v1, candidate)

**English** · [简体中文](INSTALL.zh-CN.md)

No root and no computer connection are needed. Requirements: Android 11+, 64-bit arm64 / x86_64, and we recommend keeping at least 8 GB free (the first download and unpacking also need temporary space). The only service credential you need is a **DeepSeek API key**. The wireless pairing code is Android's one-time authorization, not a second service key.

> The v1.0.3 installer is a candidate: validation so far is limited to the maintainer's device and an arm64 emulator; frozen first-install and other-ROM acceptance are still pending. The download command below pins the release tarball's SHA-256 (matching `SHA256SUMS` in the release); nothing is unpacked or run if it does not match. The APK is signed with the maintainer's existing release key, and the installer checks it against a pinned certificate SHA-256. The installer's emulator tests use a debug signature only, never the release key.

> [!WARNING]
> **Install DSB only from this project.** The official command is the one in the project README and this guide: it saves the release tarball from `github.com/tianye1999/dsb/releases/download/…` to a file, checks it against the SHA-256 shown on this page (the same value as `SHA256SUMS` in that release), and only then unpacks it. It never pipes a download into `bash` or `sh`. Do not run install, upgrade or repair commands, or open APK links, posted in issues, discussions, comments or messages, even if they say they come from a maintainer. Termux itself comes from F-Droid or the Termux GitHub releases, as the install guide says. If you already ran a command or installed an APK from somewhere else, see [what to do](../SECURITY.md#impersonated-install-commands).

## 1. Install Termux and DSB

Install the official [Termux F-Droid build](https://f-droid.org/packages/com.termux/) or the [Termux GitHub release](https://github.com/termux/termux-app/releases), version 0.118.x or later. Termux plugins must come from the same source as the main app. Installer v1 does not support the Play Store build or 32-bit systems.

DSB uses the APK from this project's pinned release, and you tap "Install" in the system installer yourself. You can install it first; if it is not installed yet, the script in step 2 downloads and opens the APK, but never taps install for you. When allowing sideloading, choose only sources you trust.

After installing, do not tap "Open" yet; first finish the script's permission walkthrough, especially the overlay permission. The installer opens the App only after it confirms the overlay permission. Opening the current App without the overlay permission can make its service fail to start.

## 2. Run one command in Termux

Open Termux, wait for the first-run setup to finish, then run:

```bash
curl -fL https://github.com/tianye1999/dsb/releases/download/v1.0.3/dsb-installer.tar.gz -o ~/dsb-installer.tar.gz && printf '%s  %s\n' 'af9d85a726f939c89261eed755ff063ae3274a732aacfa899eb46e2ba453b3c6' "$HOME/dsb-installer.tar.gz" | sha256sum -c - && tar -xzf ~/dsb-installer.tar.gz -C ~ && bash ~/dsb-installer/install.sh
```

The first run updates Termux's base libraries and installs Ubuntu 24.04, Node/dsh, Python/OCR and scrcpy, so it downloads a lot. The network must reach Termux, GitHub, Ubuntu, nodejs.org, npm and PyPI. You can interrupt with `Ctrl-C`; rerun `bash ~/dsb-installer/install.sh` to continue. Rerunning does not delete the container, sessions or key, and does not add SSH authorization twice.

> **Resolved**: Termux proot 5.1.107.99 (2026-10-23) could crash with `proot warning: signal 11 received from process -1` while installing Ubuntu packages in the container. It is fixed in proot 5.1.107.100 ([termux/proot#420](https://github.com/termux/proot/issues/420)); we re-checked the crashing install on an arm64 emulator. If you held proot to work around it, run `apt-mark unhold proot && pkg upgrade`.

The official Ubuntu repositories use HTTPS. If the minimal image lacks CA certificates, Termux's trusted CAs are used first, and Ubuntu's `ca-certificates` package takes over afterwards. TLS and APT signature checks are not turned off; the original source configuration is backed up and custom sources are kept.

The installer syncs the current public version's web tweaks and the DSB brand bundle. The brand is added through the web profile's bundles and a local package link, without reinstalling existing profile dependencies, and local customizations are kept. Session-log upload is off by default for the web, phone and headless profiles, and the installer reads the effective value back with dsh `--dump-config`. The workspace is `/root/dsb-workspaces`; `/root`, which holds the key and SSH private key, is not used as a workspace.

The installer explains and enables Termux's `allow-external-apps`. DSB needs it to start local services through the official `RUN_COMMAND` interface, and the system still asks you to grant DSB that permission separately. **Never give an untrusted app permission to run Termux commands**: with it, the app can read and write all data of the same Termux user, including the key.

## 3. Enter your DeepSeek API key

The script asks once and does not echo what you type. The key is saved to Ubuntu's `/root/.dsh/.deepseek-key` (directory 0700, file 0600). Reruns keep the existing key and do not ask again. The install log `~/dsb-install.log` records only status, never the key, and does not collect raw command output.

If you typed it wrong, run `bash ~/dsb-installer/install.sh set-key` to enter it hidden and replace it atomically; restart DSH to use the new value. Model calls happen only when you start real tasks; environment installation and doctor do not call the model.

## 4. Follow the prompts and tap a few things

Follow the script step by step; system settings pages that can be opened directly are opened for you:

1. Developer options → Wireless debugging → Pair device with pairing code. If you have no developer options yet, go to "About phone", tap the version number / build number seven times, confirm your identity as the system asks, then go back. Enter the pairing page's address and six-digit code into the script, then the **connection port** shown on the wireless debugging main page. The two ports are different. Split screen helps, so the port does not expire when the pairing page closes. If the system does not allow opening a page from the background, bring Termux to the foreground and press Enter, or open the settings page manually.
2. If DSB is not installed yet, choose "Package installer" (not Termux) to open the APK and confirm the installation in the system installer. If allowing Termux to install unknown apps only returns you to the terminal, press Enter to reopen the installer. If the system ended Termux, reopen Termux and rerun the same install command to continue; you do not need to enter the key again.
3. Grant DSB the overlay, accessibility service, notification access and notification permissions, plus the "Run Termux commands" permission. If sideloading restrictions are on, you need to confirm "Allow restricted settings" from the top-right menu of the app details page. On some systems you must first tap the disabled permission once and see the refusal message before that menu item appears; after confirming, go back and turn the permission on. The script never writes system settings to grant permissions for you.
4. In your ROM's auto-start / background management, allow Termux and DSB, and set battery usage to unrestricted. ROMs name these differently, and the installer cannot prove the allowlist has taken effect.
5. Return to Termux and press Enter; the script starts the services and runs doctor. Fix any failed items, then run `bash ~/dsb-installer/install.sh doctor`.
6. Unlock the phone, keep the screen on and open DSB; confirm the DSH Preview notice the first time. If it shows Choose workspace, pick the Default workspace the installer created; you do not need to enter a directory. Check that the web UI, the floating ball and accessibility observation work. Start with a low-risk read-only task, such as "look at what the system calculator shows; do not type or change anything". A fake key cannot complete real model tasks.

**Read doctor results separately**: `DOCTOR_FAIL` means a required item is not met. `ADB not hardened` (shown as 「ADB 未加固」 in the installer output) is a security warning for a skipped preflight or a rollback; on its own it does not make an otherwise healthy DSB fail. "Hardened but check failed" fails strictly and is never downgraded to ordinary ADB.

## Device-specific values: what needs adjusting

**With the installer you do not need to edit any device identifier or ADB selector by hand.** The following values are detected on your device or generated on the spot, never copied from the maintainer's device:

- **ADB connection target**: on first run you enter the pairing address and connection port; after verification they are saved to `~/dsb/adb-target`. Later reconnects try the saved address or an mDNS service matching this device. Both the address and the service name are only candidates: every new connection must pass this device's shell identity challenge and is dropped if it fails. If mDNS is unavailable and the port changed, rerun the installer to update the port.
- **Actual transport serial**: not the saved IP:port, but a private `localfilesystem:` channel created for each new connection. The credential is in `~/dsb/adb-transport.json` and is bound to the server generation, the transport ID and the channel. Scripts select the verified transport through a single entry point; do not type or reuse old channel names.
- **Termux UID, paths and ADB entry points**: the installer verifies this device's UID, the actual Ubuntu rootfs path and the ADB entry points on both sides, and compiles the hardening launcher with the local clang. Only the official Termux private directory is supported; arbitrary paths are not configurable, and you do not need to copy the maintainer's UID or paths.

The following are fixed defaults shared by every installation, not device identifiers. TCP services listen only on loopback, and the sockets are local private filesystem sockets:

| Item | Default and limits on changing it |
|---|---|
| SSH | `127.0.0.1:8022`; the configuration is generated by `installer/ssh_config.py`, and `termux/tsh.sh` and the installer checks also use this port |
| DSH web UI | `127.0.0.1:3080`; started by `termux/start-dsh.sh`, and the readiness check, trusted entry and App also depend on this port |
| Private sockets | ADB server: `$PREFIX/var/run/dsb-adb/server.sock`; task bridge: `~/.dsb-ipc/bridge.sock`. These paths are fixed by the deployment and calling code, and are not the random transport channel names above |

The installer currently **has no option to configure ports or socket paths**. Changing them means editing the source and keeping every caller and check in sync; changing only `sshd_config` or one start command is not enough.

`127.0.0.1:5555` is only **the default of the old manual setup without the installer**, not a port to enter for new installations. No code reads `DSB_ADB_SERIAL`, so setting it does not change the connection. Values from the maintainer's device in documents, comments or examples do not apply to other devices; do not copy them, just follow the installer's prompts on your own device.

Source: [installer](../install.sh), [reconnect script](../termux/wx_adb_connect.sh), [private transport](../hardening/adb-private/README.md), [agent ADB entry](../agent/adb_endpoint.py).

## Default ADB hardening

From the very first pairing, DSB uses a private filesystem socket, so the pairing code never goes over TCP 5037. If anything foreign or unidentifiable listens on IPv4/IPv6 5037, the installer exits in red; this is never treated as a skipped preflight. Device identity is checked with a short-lived cross-UID shell challenge, not with the public boot ID or serial number.

By default the installer tries to move the ADB server from the shared TCP 5037 to a filesystem socket in a Termux-private directory. Before installing, it probes the local UID, container path, ADB entry points, processes and device identity; if something does not fit, it leaves the entry points unchanged and shows why. After switching, it checks the private socket, key files, the entry points' shell UID, IPv4/IPv6 5037 and any TCP listeners held by ADB processes. If the switch is interrupted, it first completes the verified private deployment instead of forcing a fallback to an ordinary ADB that may no longer load.

This hardens the server transport; it does not turn Android wireless debugging or `RUN_COMMAND` into a sandbox. Parties with ADB authorization, Termux execution rights or root remain inside the trust boundary. See the [hardening notes and rollback](../hardening/adb-private/README.md).

```bash
# Run in native Termux, not inside Ubuntu
python3 ~/dsb-installer/hardening/adb-private/deploy.py check
bash ~/dsb-installer/install.sh rollback
```

Rollback restores the ordinary ADB TCP transport and keeps the key, App, container, sessions and other services. If the ordinary vendor ADB can no longer load with the upgraded public libraries, rollback refuses before stopping or changing anything; DSB says private ADB is still usable only after confirming the private service is healthy.

### Package upgrades and the private ADB runtime

- **What the installer sets up**: the hardened ADB server runs a frozen copy of `adb`. The installer works out exactly which non-system shared libraries it needs and copies them into a private directory, locked by per-file hashes. Only the ADB packages on both sides are held; no new hold is added on public shared library packages.
- **`pkg upgrade` is safe**: Termux's public libraries can be upgraded normally, without rolling back first. The private copy keeps the ADB server working.
- **Security updates need a refresh**: the private copy does not receive library security updates automatically. After updating the package index, run `bash ~/dsb-installer/install.sh refresh` (or rerun the default installer) to refresh the backend and its libraries as one generation, then run `check` / `doctor`. Refresh verifies the local service first and does not need the wireless device to be online; the existing device binding is kept and re-authenticated on the next real reconnect.
- **What you see when something is wrong**: doctor runs the full audit on the native side; inside the container it only reads a short-lived audit receipt plus a metadata quick check. If the private runtime is missing, changed or fails to load, ADB calls are refused (the guard also pauses ADB) and doctor reports the reason with the next step (for example `ADB_PRIVATE_RUNTIME_DRIFT` or `ADB_BACKEND_LOAD_FAILED`). DSB never falls back to the public libraries or TCP.

Legacy compatibility, recovery after a failed refresh and the limits of security updates are covered in the [ADB private transport notes](../hardening/adb-private/README.md).

When the wireless port changes, the saved address and the mDNS service with a matching name are only candidates; every connection must pass the shell identity challenge again and is dropped if it fails, so an address or name alone never counts as a verified device. The credential is bound to the server generation, the transport ID and the private filesystem channel. A channel forwards one real connection only, and after a disconnect a new endpoint cannot attach to the old ID. Some Termux adb builds do not support mDNS; in that case turn on wireless debugging and rerun the installer to update the connection port, without entering the key again. The candidate still needs frozen first-install and other-ROM acceptance.

Rolling back to ordinary TCP is a weaker availability fallback: doctor checks who owns the listener and fails on conflicts, but a separate check followed by a TCP connection cannot be called an atomic identity guarantee. If you need the private transport boundary, restore hardening first; do not ignore the "ADB not hardened" warning.

When Ubuntu is created for the first time, the installer holds back the web UI and task bridge and runs only the SSH and ADB daemons; it releases the first-install marker and starts the services only after the permission walkthrough. This way no task ownership is established before Android's install-source authorization might end Termux. **Once the task bridge is running**, if it crashes or its ownership becomes unclear, the original A6 protection still refuses to accept tasks again. The installer does not delete old sockets, pid files or the ledger to fake a recovery; doctor reports the actual failure, and old tasks need a manual check. This is a different acceptance layer from resuming installation steps.

## Development and testing

Run `bash install.sh` from a full repository checkout. Development overrides are not part of normal user configuration:

| Environment variable | Purpose |
|---|---|
| `DSB_APK_PATH` | Use a local APK path; emulators use only a debug signature |
| `DSB_TEST_APK_SHA256` | The measured SHA-256 of the local debug APK; if missing or mismatched, the APK is not opened |
| `DSB_TEST_APK_CERT_SHA256` | Debug signing certificate SHA-256, used only with the local APK test path; an existing package must still pass full signature verification |
| `DEEPSEEK_API_KEY` | Preset a fake key for testing; the script unexports it immediately and does not pass it to dependency installers |
| `DSB_ADB_TARGET` | A paired local IPv4:connection port for tests; device identity is still verified and authorization is not bypassed |
| `DSB_NONINTERACTIVE=1` | Do not wait for manual steps; authorization is not assumed complete, and doctor still returns real results |
| `DSB_MIGRATE_MANUAL=1` | Explicitly migrate an old manual installation (back up the scripts first); old R6-0b deployments are still refused unconditionally |

The single source of the DSH version is `installer/release.json`; both the installer and CI read it. The release package is assembled by `installer/build_release.py`, which pins the APK hash and carries pinned NDK objects/NOTICE for both architectures, so the phone does not need to reach googlesource. That domain is needed only when running development sources with an empty materials cache.

Do not type a real key on the shell command line, and do not paste an environment containing a real key into an issue. Normal users should use the hidden interactive prompt.

## Appendix: manual deployment, building and upgrading

Manual maintenance should also go through this page's entry point to complete the private bootstrap, pairing and hardening first. Do not use the fixed ports, old supervisor or `cp agent/*` from old scripts for new deployments. The installer deploys to:

| Content | Location |
|---|---|
| agent and skills | Ubuntu `/root/.dsh-phone/skills`, `/root/.dsh/skills/dsb-phone` |
| bridge / dsh-config | Ubuntu `/root/dsh-bridge`, `/root/dsh-config` |
| Native Termux scripts / container copies | `~/dsb`, Ubuntu `/root/termux-dsb` |
| Container SSH private key / Termux authorization | Ubuntu `/root/.ssh`, Termux `~/.ssh/authorized_keys` |
| Key / dedicated workspace | Ubuntu `/root/.dsh/.deepseek-key`, `/root/dsb-workspaces` |

Before updating the code, end any virtual-display session and run `check`. To upgrade the adb packages themselves, `rollback` first; the journal and backups are kept, and managed entry points must not be overwritten by hand. An old manual environment requires an explicit `DSB_MIGRATE_MANUAL=1`; the installer backs up the Termux scripts and the container's skills/bridge/dsh-config. Old R6-0b deployments still need the owner present to migrate.

Manual start and self-check:

```bash
bash ~/dsb/boot.sh
proot-distro login ubuntu -- python3 /root/.dsh-phone/skills/safe_tap.py doctor
```

Build the App from source with `app/build.sh`; to build and sign on the phone, use `app/build-phone.sh`. Official APKs use the owner's existing release key, which cannot be replaced by generating a new one; upgrades are still confirmed by the user. `sync-dsh-config.sh` syncs the running directory to the copy and cannot initialize a new environment.

Before changing SSH, the installer backs up `$PREFIX/etc/ssh/sshd_config.pre-dsb`. A running sshd does not reload automatically; you must restart it yourself and check what it listens on. To restore, stop the current sshd, restore that backup as `sshd_config`, run `sshd -t`, then start it again; consider the impact on existing remote sessions first. `~/.termux/termux.properties.pre-dsb` likewise keeps the content from before the first change.

Upgrading the public APK requires rebuilding with **the same signing key**, and the user confirms the installation; nothing is downloaded in the background and updates are never tapped for you. If `com.dsb.assistant` is already installed, the installer first verifies its full signature against the pinned certificate SHA-256; on mismatch it stops, and it never accepts or overwrites a package based on its name alone.

Swiping DSB away means quitting, and the phone-side services stop under the existing exit protocol. Uninstalling the App and deleting Termux/Ubuntu data are different operations; deleting loses the local key, tasks and sessions, so make sure you have backups before doing it.
