# Provider-manifest fix verification

Issue #36 was reproduced before the fix: the manifest forced a TypeSafe-only
requirement both in source and after a disposable OpenRouter-only installation.
Two new regressions failed, then passed after removing `requires_env`.

## Actual host behavior checked without keys

The installed Hermes source's `_missing_env_specs` function was extracted by AST
and exercised unchanged with a stub `get_env_value` representing an
OpenRouter-only environment. The old manifest reported `TYPESAFE_API_KEY`
missing; the provider-neutral manifest returned no missing install requirements.
This was the actual helper implementation, not an assumed any-of interpretation.
No real `.env`, keychain or secret-file contents were read.

## Self-review

- Listing all four keys would prompt for all individually and is not the fix.
- Removing the installer requirement does not mean inference is keyless.
- Runtime provider resolution, precedence, endpoint allowlists, private key
  setup and no-key fail-open behavior are unchanged.
- A real subprocess installation into a disposable root-only Hermes home with
  only a synthetic OpenRouter key copied the neutral manifest successfully.
- The setup skill now tells agents to use doctor metadata rather than infer
  failure from a missing TypeSafe environment variable.
- No gateway restart or live credential change was performed.

This is critical self-review, not independent review. Full suite/CI receipts are
reported on the pull request. Existing release versions remain in sync; notes
are Unreleased until the repository's release process runs.
