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

## Out of scope

- Attackers who need physical access to the phone, or who already have root / authorized ADB debugging / Termux execution rights (the threat model treats these parties as trusted as much as DSB itself)
- Vulnerabilities in third-party apps themselves (WeChat, Taobao, …)
- Wrong model output by itself (unless it leads to a gate bypass)

## Supported versions

Only the latest release on the `main` branch receives security fixes.
