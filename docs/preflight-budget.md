# Optional pre-call latency repair

## Scope and acceptance

The optional pre-call selector must not occupy the host's two-second hook budget waiting on skill discovery, inference or reachability. Normal selection must still work through the real host loader and real configured service. Late work must not mutate turn routing or suggestion feedback. Keep explicit tools, privacy gates, confidence thresholds and routing modes unchanged.

This changes plugin code, not the host source. The callback budget is 1.5 seconds with six admitted outstanding callbacks and no queue. ContextVars are preserved. The callback returns data only; only its timely caller may apply routing answers or emit a suggestion. Optional feedback lock contention now skips feedback instead of waiting. Already-started transport work cannot be cancelled by Python and keeps its existing transport timeout. The cap counts callbacks, not all internal transport threads. It is not a strict OS scheduling deadline or proof that every possible filesystem call outside selection is bounded.

## Exercised evidence

- `evidence/preflight-budget/regressions.json`: 1,714 package tests passed. Added eight tests cover fast/context-preserving work, held work, capacity/recovery, errors, actual hook success/late-state isolation and real feedback-lock contention. Release text scan and diff checks passed. The scanner excludes 56 pre-existing binary assets; this change adds or modifies no binary.
- `evidence/preflight-budget/negative-control.json`: the exact committed old plugin failed the same native-dispatch two-second held-work assertion. Only a disposable source copy was substituted. Canonical installed source was unchanged by the negative control.
- `evidence/preflight-budget/install.json`: guarded root-only installer completed without warnings after exact prior managed-byte comparison and backup. All 136 compared installed source files match; 47 configuration/state hashes remain unchanged. Existing profile links were not rewritten.
- `evidence/preflight-budget/installed-host.json`: fresh actual installed PluginManager and native hook dispatch exercised. Held dependency returned in 1.5966 seconds before release; late work left replacement turn state unchanged. Real configured service selected a public repository procedure and returned a suggestion in 0.2436 seconds. This was public synthetic text, not an owner message, and a small public catalog, not a fleet-scale benchmark. The host warned that the fixture's symlinked skill was outside its trusted directory; its loader still returned success. No production trust configuration was changed.

The initial missing-module red test and raw suite/negative-control logs are retained privately with the pre-install backup. The committed receipts intentionally contain no owner paths, messages, credentials or account material.

## Deployment and limits

Installed only through the canonical repository installer. No gateway restart, host source change, configuration rewrite, provider change, model swap, policy promotion or scheduled model audit was performed. Existing running plugin instances are not proven reloaded. Root installation can be visible through pre-existing shared profile links on their normal next load; it is not proof of adoption by an in-flight turn.

This is a tested and installed hook repair, not 48-hour no-timeout acceptance, an owner Telegram response-time measurement, six-agent throughput proof or completion of the broader low-waste fleet objective. Do not repeatedly call models for endurance measurement; observe existing native timing and suppression logs.

## Reused resources

The existing native PluginManager/dispatch determines actual hook registration and execution. Existing middleware tests preserve routing/protocol behavior and threshold semantics. Existing installer root-only scope and prior-byte checks preserve configuration and unrelated profiles. The installed hook-attribution report identifies the slow optional selector; it does not itself certify the repair.
