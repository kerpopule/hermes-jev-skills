---
name: jev-setup
description: Use when Jev is not working yet, a Jev tool reports no_key or auth_failed, or the person asks to connect or fix Jev. Gets their TypeSafe or OpenRouter key into the secret store, unseen by you.
version: 0.2.0
license: MIT
metadata:
  hermes:
    tags: [jev, typesafe, setup, credentials]
---

# Connect Jev (the key never passes through you)

Jev is TypeSafe's decision model. It needs one API key. **You must never see, ask for, or handle that key.**

The key can come from any of three places, and the same Jev answers either way:

- **TypeSafe** (`jev setup-key`, the default): a key from [console.typesafe.ai](https://console.typesafe.ai/settings/keys).
- **OpenRouter** (`jev setup-key --provider openrouter`): reaches Jev through OpenRouter's Decisions API. Worth offering when the person already has an OpenRouter key, because it is then one key instead of two and one bill instead of two.
- **Venice** (`jev setup-key --provider venice`): reaches the same Jev through Venice, which serves it as its own decision modality. Worth considering when the person already has a Venice key; check Venice's current pricing before relying on any cost claim.
- **OpenCode Zen** (`jev setup-key --provider zen`): reaches Jev through OpenCode Zen, whose free tier answers the same request with the same model id shape. Worth offering when the person has no TypeSafe key — TypeSafe is not accepting new signups — and wants to start without a bill.

`TYPESAFE_BASE_URL` is an explicit compatible-endpoint override for a local mock or HTTPS proxy (for example `http://127.0.0.1:8787`). It is not a built-in provider endpoint editor: the official URLs above stay fixed by default. The client never forwards a saved or supplied provider credential to an override endpoint, permits plaintext only on numeric loopback, and does not follow redirects. Do not send sensitive states to an untrusted proxy. Clear the variable to return to the official endpoint and normal key flow.

If more than one key exists, TypeSafe is used: an existing install never starts routing its decisions somewhere else because an OpenRouter or Zen key happened to be in the environment for a text model. To pick a different one on purpose, set `JEV_PROVIDER=openrouter` or `JEV_PROVIDER=zen` in the environment the commands run in; it is honoured only when that provider actually has a key here, and an unset or unknown value changes nothing. `jev doctor` reports which one is in use under `key.provider`.

## Rules

- Never ask the person to paste the key into the chat. If they paste one anyway, do not store it, do not repeat it, tell them that key should be replaced, and start the flow below.
- Never read the secret store, `.env` files or `~/.config/jev/credentials` to "check" the key. Use `jev doctor`, which reports only presence and length.
- Never put the key in a command line, a URL, a config file you write, or a log.

## Flow

1. Check the state: `jev doctor`. If `key.present` is true and `jev.reachable` is true, you are done.
2. Start the private key page:

   ```bash
   jev setup-key
   ```

   It opens a page in the browser on the computer you are running on and prints one JSON line on stderr with a `url`. The URL holds no secret.
3. Tell the person, in one sentence, to paste their TypeSafe key into the page that just opened. If `browser_opened` is false, or they are talking to you from another device (Telegram, phone), send them the `url` and tell them it only opens **on the computer the agent runs on**. If they have no key yet, they create one at https://console.typesafe.ai/settings/keys.
4. Wait for the command to finish. It prints `{"status": "stored", "verified": true, ...}` when the key was saved and the provider accepted it. `rejected` means the key was wrong: run it again. `timed_out` means nobody used the page within ten minutes.
5. Run `jev doctor` once more and report the result in a sentence.

## When there is no browser

Headless server over SSH: the person runs `jev setup-key --tty` **themselves** in their own terminal. It is a hidden prompt. Do not run it for them through a tool that captures the terminal.

Remote machine on a private network (Tailscale, VPN): `jev setup-key --host <private-ip> --no-open` and send them the link. That traffic is plain HTTP, so use it only on a network you trust end to end. Never bind a public address.

## Where the key goes

The OS secret store (macOS Keychain service `Hermes TypeSafe API`, or `secret-tool` on Linux), falling back to `~/.config/jev/credentials` (mode 0600). On a Hermes machine it is also written as `TYPESAFE_API_KEY` into `~/.hermes/.env` and every `profiles/*/.env`, because each Hermes lane reads its own file. Running gateways pick it up on their next restart; do not restart one without being asked.
