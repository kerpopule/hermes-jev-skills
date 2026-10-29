# Optional Pomerado companion (pilot)

Pomerado turns website workflows into saved integrations. This companion runs a separately installed, reviewed **Pomerado 0.4.0** runtime. It is **off by default**. It never replaces ordinary browser tools or Jev, and the Jev installer does not install Pomerado, start a server, edit MCP configuration or make model calls.

## Trust and scope

Pomerado executes source as your current account. **This is not a sandbox.** A declared read effect is not enforced filesystem/network confinement. Removing environment keys does not prevent trusted code from reading files. Only use reviewed, trusted source; evaluate untrusted code in a disposable VM with no personal files or credentials.

The pilot refuses declared writes, sign-in, private-answer questions, artifact symlinks, path traversal, oversized packages and changed source fingerprints. It never attaches a saved browser profile or mints an integration. Running a saved integration needs no model key; creating one uses paid models and is a separate user decision. Host paths and integration inputs are private; do not commit generated config or live artifacts.

## Setup

Install upstream separately using its published instructions. Upstream currently requires **Node >=24.21.0 and <25**. The source pinned for private qualification was `Pomerado/pomerado@17d2394b74391343a83e2619c46e42b4f7f17043`, MIT. The registry did not offer version 0.4.0 during qualification, so a reviewed source build was used. Do not recommend an unversioned `npx` download as a substitute.

From this repository, with real absolute paths in these shell variables:

```sh
# Inspection never executes the integration or downloads anything.
node companions/pomerado.mjs inspect --artifact "$ARTIFACT"

# Read every source file, then supply the exact digest returned above.
# NODE must be the supported Node 24 executable. CLI is the reviewed package's
# dist/typescript/src/standalone/mcp-cli.js, not an arbitrary shell command.
node companions/pomerado.mjs config --artifact "$ARTIFACT" \
  --node "$NODE" --cli "$CLI" --approved "$REVIEWED_SHA256" \
  --enabled --acknowledge-host-code
```

This prints a native Hermes `mcp_servers` fragment, with sampling disabled. Merge **only** the selected server entry into your chosen profile config after explicit opt-in. Do not replace other servers. No file is edited by the command. For a client requiring `mcpServers` instead, adapt the outer key using that client's documentation, not by copying the Hermes fragment verbatim.

The server launches only `serve --artifact`, with no shell, model keys or attach endpoint. Both enabling and acknowledging host-code risk are required on every start. Review again after artifact files, metadata or directory change. Dependencies and the upstream runtime are trusted separately; the artifact digest does not authenticate the interpreter or dependency tree. Do not modify runtime files while a session is active.

Use upstream `get_job` and `cancel_job` for the returned job ID; never invoke the operation again to poll. Inputs requiring credentials are unsupported in this pilot. To revoke, remove this server entry from the chosen client and close/reload its connection. Existing browser tools remain available, but failures never cause an automatic fallback or reattempt.

## Qualification

```sh
node --test companions/pomerado.test.mjs
python3 -m unittest discover -s tests
python3 scripts/check_release.py
```

`fixtures/pomerado/read-heading` is an explicitly **hand-authored** integration, not a successful AI mint. Private qualification substituted a nonce-bearing loopback website and used the real upstream browser runtime through paired MCP and Co-Agent built-in call paths. No paid inference, private account, credential, model-quality, production or security-sandbox claim follows. The companion's standard offline tests do not require Pomerado or launch a browser.

## Co-Agent

The companion is also integrated as a user-controlled setting in the separately reviewed Co-Agent candidate. Its pilot selects one reviewed integration, keeps caller sessions separate and offers bounded run/job/cancel tools. It does not auto-enable this Hermes MCP entry or install dependencies. Shipping the Co-Agent candidate remains a separate release action.
