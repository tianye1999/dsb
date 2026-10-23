# Roadmap

**English** · [简体中文](ROADMAP.zh-CN.md)

> Everything here is **planned, not implemented**. For what already works, see "What it can do today" in the README.

## Vision

A phone AI assistant built on DSH (DeepSeek model by default) that needs no new phone and no root, and puts being **trustworthy** ahead of being clever. DSB's own value is on the execution side: the virtual display, execution gates, task ledger and audit.

## Milestones

| Stage | Scope | Trust requirements |
|---|---|---|
| **M0 Release** | Plan B: keep Termux and ship a one-click installer; build the packaging pipeline the all-in-one APK will need along the way (rootfs trimming, version manifest, download channel) | Finish local control-plane authentication (R6) and secret/privacy scanning before release |
| **M1 Voice entry** | On-device wake word + streaming ASR + TTS (the existing system TTS that reads conclusion cards can be reused) | The wake word is recognized on the device and **no audio is uploaded**; recording is clearly indicated; one tap turns it off |
| **M2 Trust trio** | Execution report (a readable "what it did, what it refused" for every task); accessibility self-healing (when the system turns the service off, prompt and guide recovery, **without clicking grants on your behalf**); sensitive input `type_secret` (password-like input never goes through the model and is never written to disk) | |
| **M3 All-in-one APK (plan A)** | Ship node/proot inside the APK, download the rootfs on first launch, upgrade through versioned directories plus a pointer switch. Triggers (do it only when two hold): installation friction becomes a frequent issue; the execution layer is stable; someone is willing to own long-term maintenance | Known risks: shipping executables this way is not officially guaranteed; 16 KB page alignment on Android 15+; proot is GPL and needs a source notice |
| **M4 Drop the rootfs** | Gradually move skills onto native dsh/Node capabilities so the container keeps shrinking | |
| **Security work in progress** | Move final authorization to a boundary the brain cannot change (for example the App side), so a compromised brain still cannot tap "pay" | See the first item of "Explicitly not defended" in the threat model |

## Trust hardening (delivering stronger promises)

Everything below is **planned, not implemented**, and is not a privacy or security guarantee of the current version. For current behaviour and the code behind it, see the [threat model's data-flow section](THREAT-MODEL.md#2-privacy-what-leaves-the-phone-and-what-stays) and the [architecture](ARCHITECTURE.md#data-flow-and-retention).

- [ ] **Make full-screen questions crop-first, or allow turning off full-frame uploads**: this must cover "Ask DSH" and the model-call paths, not just the gate's cropping logic; the current full-frame path is in [`bridge/bridge.mjs`](../bridge/bridge.mjs#L548).
- [ ] **Refuse empty-tree taps when the vision service fails**: remove the fallback that approves when OCR found no danger words; the current branch is in [`agent/safe_tap.py`](../agent/safe_tap.py#L1516).
- [ ] **Redact free-text audit fields**: cover `note` / `expect` / vision explanations and similar fields, then verify that chat, order and other sensitive text is not carried in; records are currently serialized directly by [`audit()`](../agent/safe_tap.py#L369).

## Other ideas

- Home-screen widget: submit a task with one tap, see task status and recent refusals
- **Pluggable models**: connect the brain through the providers/models available in DSH and pass end-to-end acceptance; switching the separate gate vision model also requires re-accepting visual recheck and localization (gate safety depends on the quality of visual judgement). The existing `BRIDGE_BRAIN=api` only lets `/step` and `/ask` call the model directly, bypassing DSH on those two paths; it does not replace the DSH runtime for `/do`
- Familiar-route mode: reuse a path that worked last time to cut brain steps (today 60–80% of each task's time is spent waiting for the model)

## Explicitly not planned

- Play Store distribution (executable restrictions + review risk; releases ship directly on GitHub)
- "Replacing the phone": the phone is only the AI's execution organ
- Enumeration guards: "fixing" safety problems with keyword lists
