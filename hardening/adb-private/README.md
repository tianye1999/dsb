# ADB private transport

**English** · [简体中文](README.zh-CN.md)

## What it is

It moves the managed ADB server from the shared TCP 5037 to a **0700 private directory and a 0600 filesystem socket** inside Termux, and unifies the managed entry points on the Termux and Ubuntu sides. The launcher is compiled with Termux clang at install time, and paths and UIDs are detected on the device.

For how device identifiers and connection targets differ from the fixed local defaults, see [Install: device-specific values](../../docs/INSTALL.md#device-specific-values-what-needs-adjusting).

This is not an Android sandbox. Parties that already have the Termux UID, RUN_COMMAND, ADB authorization or root remain inside the trust boundary; the wireless debugging port still needs its authorization kept safe and should be off when unused. See the [threat model](../../docs/THREAT-MODEL.md) for the exact boundaries.

## Why it is attempted by default

A TCP server port that ordinary apps can reach and an app-private directory are different boundaries; APK command authentication cannot replace server isolation. The installer uses the private socket from the very first start/pair/connect, and the pairing code goes only through stdin. A foreign or unidentifiable listener on IPv4/IPv6 5037 is a red conflict and must never be downgraded to a skip.

If preflight does not pass, the entry points are not replaced, and doctor shows the reason together with an "ADB not hardened" warning; if a hardened deployment fails its check, it fails strictly. The candidate's connection identity and parallel sessions still need acceptance, so a skipped preflight or limited testing must not be advertised as working hardening. The old tool was verified on one physical device, and the general version only on an arm64 emulator; other ROMs, reboots, mDNS and x86_64 Android still need real-device acceptance.

A device address is only a candidate, not an identity. Each new connection goes through the private filesystem channel and a cross-UID shell challenge, after which the credential is bound to the server generation, the transport ID and the channel process. A channel carries one real connection only and refuses reuse after a disconnect; a failed new connection is dropped and its credential invalidated. Ordinary clients share an execution lock; start, inventory and rollback take it exclusively. End long-running virtual-display sessions before a full inventory check or a rollback. When the guard cannot get the exclusive lock, it only renews the file-audit receipt and never records an unexecuted process check as healthy.

Cold starts double-check inside the exclusive lock, so several first calls share one server. The start environment forces `ADB_EMU=0`, turning off local emulator scanning, so the local connection is never registered as an emulator alias. Only after fully verifying the exe, socket, UID, start time and running image does the guard terminate a duplicate managed server through pidfd and start one; unknown processes, or old kernels without pidfd support, are refused automatic termination and need manual maintenance. It never kills blindly by numeric PID.

## How to roll back

```bash
# Run in native Termux, not inside the Ubuntu container
bash ~/dsb-installer/install.sh check
bash ~/dsb-installer/install.sh rollback
```

Once a private switch has published a complete transaction, an interruption or failed self-check first completes the private start from the verified manifest, runtime and launcher, instead of forcing a fallback to TCP; a failure before publication leaves the ordinary entry points unchanged. Ordinary rollback verifies the same-UID server, listener inode, closed IPv6, the forwarding table and the online device identity. If its second half fails, it tries to re-apply the private generation; if that also fails, it keeps the current state and reports the real phase, and never claims ADB is usable. A successful rollback restores ordinary TCP, which is only a weaker availability fallback; the App, key, web UI and task bridge are not deleted.

If a rollback reached its terminal state but was interrupted before finishing, and after a reboot you ran ordinary `adb` first, that new server has no 5037 credential from this tool, so `recover` refuses to archive. In native Termux, run `adb kill-server`, make sure nothing else uses 5037, then run `bash ~/dsb-installer/install.sh recover`. Do not delete the journal to bypass the ownership check.

Old R6-0b journals/diversions or unknown formats are refused before any write, with the message 「旧版部署，需 owner 在场迁移」 ("legacy deployment, needs the owner present to migrate"); the public tool cannot roll back such a deployment. Do not delete the journal to bypass the check.

## Shared library upgrades and private generations

**Public libraries can be upgraded normally with `pkg upgrade`, without rolling back first.** At install time, `readelf --dynamic` recursively computes the backend's NEEDED closure, and `dpkg-query -S` records the owning package and version of every non-system library and link target; there is no enumerated list of library or package names. The files and the necessary SONAME aliases are copied to `R/runtime-<generation>/lib` (directory 0700, files 0600), with the backend in the same generation directory. Per-file SHA-256, permissions, owner and links are locked, and the manifest is published only after the whole generation is ready.

Only the diverted Termux `android-tools` and Ubuntu `adb` are held; the original holds are recorded and restored after an ordinary rollback. No new hold is added on public shared libraries, and unrelated or user-set holds are not changed. The system bionic and `/system`/APEX libraries are not copied. `libc++_shared.so` is made private together with the full closure and is used only inside the separate backend process.

The static launcher clears the caller's `LD_*` before starting Python. Ordinary clients, identity challenges, version probes and server start all set a fixed private `LD_LIBRARY_PATH` only for the verified backend, never for Python, proot or system commands. Deployment locks per-file hashes plus dev/inode/size/mtime_ns/ctime_ns. Everyday calls use the metadata quick check, and any change is re-checked and refused; native doctor and the periodic guard do the full hash and version probe. The server's `/proc/PID/maps` is checked against the private mappings; system libraries are checked by the trusted root directory, owner, permissions and live dev/inode of the current mapping, without locking APEX version paths or boot-time device numbers. Failures are reported clearly as `ADB_PRIVATE_RUNTIME_DRIFT`, `ADB_PRIVATE_RUNTIME_LOAD_PATH` or `ADB_BACKEND_LOAD_FAILED`, and never downgrade to TCP.

**The private copy does not automatically receive security updates through `pkg upgrade`.** Regularly update the installer source and the package index, end long-running ADB sessions, then refresh the whole generation:

```bash
pkg update
bash ~/dsb-installer/install.sh refresh
bash ~/dsb-installer/install.sh recover  # only if the previous step says a transaction is unfinished
bash ~/dsb-installer/install.sh check
bash ~/dsb-installer/install.sh doctor
```

Rerunning the default `install.sh` also refreshes a deployed private generation. Refresh resolves, through APT's current package index, a complete package set and downloads and verifies it (it does not install the packages, run package scripts or change public libraries). It computes the closure from the staged packages, verifies that the backend loads, then switches the whole generation; it never pairs an old backend with new libraries and re-records the hashes. If the repository itself is inconsistent, refresh refuses and the existing private generation stays. If you use third-party or local repositories, you must keep them consistent yourself. Without network access the existing generation stays, but you cannot claim to have received security updates. After a refresh, the device-identity stage of `check` needs the wireless debugging device online or reconnected; while offline you can run `doctor`'s local audit first, but the old binding must not be treated as a new connection.

ROOT, the HOME helpers, the four entry points and the single `adb_endpoint.py` inside the container are updated in one transaction. doctor first does a full audit in native Termux and publishes a short-lived receipt; the proot side only reads that receipt and runs the metadata quick check plus the actual process/mapping checks, without hashing every library again. A missing or expired receipt fails strictly. If you are told a refresh is unfinished, run `bash ~/dsb-installer/install.sh recover` to recover the private transaction first, then retry the refresh or an ordinary rollback.

Refresh has its own recoverable transaction and never borrows the ordinary TCP rollback. It first switches and self-checks the local server/runtime offline, so an offline wireless device does not block it; the old binding file is kept, but the new server generation does not treat it as an authenticated connection, and it is invalidated and rebuilt only on an actual reconnect. If activation fails or power is lost, the old private generation is restored. If, after the new private generation has been committed healthy, restoring the holds is blocked by another apt/dpkg operation, the new generation is kept and you are told to rerun the installer to finish. A failed new generation is cleaned up after recovery, refresh history keeps at most five backups, and the running generation is never cleaned up.

Before stopping the server or changing entry points, journals or holds, ordinary `rollback` first verifies with the public libraries that the original vendor ELFs on both sides can load. If the public libraries were upgraded and the old vendor cannot load, it reports `ADB_ROLLBACK_REFUSED`; it says the private service is still usable only when the journal is applied and the local private server/maps are healthy. Prepare dependency packages that match the ordinary ADB, or have the maintainer review an update of the ordinary ADB package and rollback baseline, then retry. Do not delete the private directory, edit hashes or force-remove the ADB holds. Refreshing the private generation does not update the ordinary vendor package, so an ordinary rollback is not guaranteed to complete immediately.

New journals/manifests use `tool_version=3`. An old public v2 can be migrated with an explicit `refresh`: ROOT, the HOME helpers, the entry points and the manifest are updated in one transaction, and once the new generation is healthy, the shared-library holds added by the old version are restored once to each package's original state. The historical holds are saved separately for audit; later refreshes and ordinary rollbacks manage only the ADB packages on both sides and do not replay the shared-library hold cleanup. An old public v1 still supports an explicit rollback followed by a fresh install; there is no implicit v1 rewrite. Historical R6/phone-private journals are still refused before any public write and must use a separately reviewed private incremental transaction; adding a schema to bypass this is not allowed.

If an interruption happened in the middle of an old v1/v2 switch, before a complete private generation existed, `recover` first runs the ordinary rollback preflight and then tries to restore the old entry points. If ordinary `android-tools` has been upgraded to a version different from the journal baseline, preflight refuses and does not force-overwrite the new package. Have the maintainer restore the packages and public libraries that match the journal first, then rerun `recover`; do not delete the journal by hand or remove the holds. A v3 rollback that reached its terminal state can be archived idempotently by `recover`.

Android OTAs, damage to Python/Termux itself, and existing same-UID/root control remain outside what private library isolation guarantees. Maintenance mode only pauses new ADB calls; it does not undo committed actions and does not stop the web UI, the bridge or SSH:

```bash
bash ~/dsb-installer/install.sh maintenance
bash ~/dsb-installer/install.sh resume
```
