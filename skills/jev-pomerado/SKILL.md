---
name: jev-pomerado
description: Use when a user opts into Pomerado saved integrations.
version: 1.0.0
---
# Optional Pomerado companion

Pomerado is a separate MIT-licensed integration runtime, not a Jev decision model. Leave normal browser tools and model defaults unchanged. Never auto-install Pomerado, enable this companion, mint an integration, attach an existing browser, share credentials or spend on models because this skill loaded.

Use the checkout's `companions/pomerado.mjs` to inspect a user-selected saved integration, review every source file and show the warning. The pilot only accepts declared read operations without sign-in, private answers or write effects. Metadata is not a sandbox or a guarantee that source behaves as declared. Pomerado runs code as the current account. Environment filtering does not prevent reading host files. Untrusted integrations require OS/VM isolation before execution.

After explicit user opt-in and source review, invoke the documented `config` command with the exact reviewed digest, absolute supported Node 24 runtime and reviewed Pomerado CLI. It prints a native Hermes `mcp_servers` fragment with sampling disabled. Merge only the selected entry into the user's chosen profile configuration when authorized. Never overwrite config, other servers or activate fleet-wide. An installer run only installs this skill; it does not install Pomerado or activate a server.

Use existing get_job/cancel_job to observe/cancel an upstream job; do not call the business tool again to poll. Stop for input instead of entering credentials. Revoking the MCP configuration closes that integration. If Pomerado is absent, disabled, stale, fails or needs unsupported access, explain why and offer the unchanged browser workflow, never silently switch tools or claim completion.

See `companions/POMERADO.md` for the exact opt-in commands and qualification limits.
