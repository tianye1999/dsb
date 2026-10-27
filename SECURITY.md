# Security Policy

**English** · [简体中文](SECURITY.zh-CN.md)

DSB's core promise is trusted delegation: the AI only does chores, and decisions stay with people. So we treat anything that can break that promise as a security issue.

## What counts

- Bypassing the execution gates (G1–G5, OCR + visual verdicts on empty-tree pages, the pre-dispatch recheck) so that the AI can tap buttons such as pay, send, delete or authorize
- Bypassing local control-plane authentication: another app on the phone can give DSB commands, inject messages into the brain's session, or impersonate the bridge service
- Task-ledger failures: the same task runs twice, or "stop" is confirmed but the task did not actually stop
- Privacy leaks: screenshots, chat content, order content, verification codes or clipboard text appearing in logs, audits, receipts or the repository
- Any mismatch between the boundaries stated in the threat model (`docs/THREAT-MODEL.md`) and real behaviour

## How to report

**Please do not open a public issue.** Use GitHub's [private vulnerability reporting](https://github.com/tianye1999/dsb/security/advisories/new) (repository page → Security → Report a vulnerability).
Include the reproduction steps, the impact, and the device and Android version involved. If you have a suggested fix or a PoC, attach it as well.

This is a personally maintained project: I will try to acknowledge your report **within 7 days**, keep you updated until the fix is released, and credit you in the release notes (unless you prefer not to be).

## Impersonated install commands

The only official way to get DSB is the bootstrap command in the [README](README.md) and [docs/INSTALL.md](docs/INSTALL.md), with release files only from [github.com/tianye1999/dsb/releases](https://github.com/tianye1999/dsb/releases). Later upgrades, repairs and reruns use the `install.sh` already on your phone, as the install guide describes. If you ran an install, upgrade or repair command, or installed an APK, from anywhere else, assume it could read and change anything Termux can reach on that phone:

1. Uninstall every app that came from that source, not only one named DSB. Check **Settings → Apps**, **Accessibility** and **Security → Device admin apps** for anything you do not recognize; if something cannot be removed or you are not sure, back up what you need and factory-reset the phone, then reinstall Termux and use the official command. Delete the fake installer files from Downloads.
2. Back up only files you know are clean (do not restore apps, app data or whole-phone backups), clear the Termux app's data — this deletes everything in Termux, not only DSB — and reinstall with the official command.
3. Revoke the DeepSeek API key you entered and create a new one. Change any other passwords, keys or tokens that were stored in Termux or in shared storage the command could read.
4. In **Settings → Developer options → Wireless debugging**, remove paired devices you do not recognize.
5. Report the post to GitHub (its ⋯ menu → **Report abuse**) and send us the link privately as described in [How to report](#how-to-report); a link and a screenshot are enough. Do not run the command again.

## Out of scope

- Attackers who need physical access to the phone, or who already have root / authorized ADB debugging / Termux execution rights (the threat model treats these parties as trusted as much as DSB itself)
- Vulnerabilities in third-party apps themselves (WeChat, Taobao, …)
- Wrong model output by itself (unless it leads to a gate bypass)

## Supported versions

Only the latest release on the `main` branch receives security fixes.
