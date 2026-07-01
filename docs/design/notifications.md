# Design: multi-channel notifications (Apprise)

Status: proposed. Queued **after** auth (roadmap #3). Auth is the security gap;
this is a feature-add.

## Goal

Deliver alerts to whatever notification service the operator uses — Discord,
Telegram, ntfy, email, Pushbullet, Home Assistant, Slack, etc. — not just
Gotify. Do it **without** hand-writing a per-service integration for each one.

## Core decision: use Apprise, do NOT hand-roll per-service notifiers

[Apprise](https://github.com/caronc/apprise) is one library that speaks 100+
notification services through a single URL-based API (`discord://`, `tgram://`,
`ntfy://`, `mailto://`, `gotify://`, …). One dependency replaces a dozen
fiddly per-service payload formats — and, for an open project, replaces a
permanent stream of "please add service X" issues with "Apprise already supports
it."

**Do not build separate DiscordNotifier / TelegramNotifier / NtfyNotifier /
etc.** Write ONE `AppriseNotifier`. If someone asks for a new service, the answer
is an Apprise URL, not new code.

## Architecture — one notifier, unchanged rules

The existing seam already fits: `AlertEngine.evaluate()` decides **whether** to
alert; the notifier decides **how to deliver**. The alert-rules engine does not
change. Only the delivery layer generalizes.

- Replace/augment the single `GotifyNotifier` with **`AppriseNotifier`**:
  - Holds one `apprise.Apprise()` instance loaded with the operator's configured
    notification URLs at startup.
  - `async send(alert, record)`: format title + body, attach the snapshot where
    supported, then dispatch.
- Fan-out: one alert goes to **all** configured destinations. (Per-camera /
  per-severity routing is a later feature — not v1.)

### Sync-in-async

`apprise.notify()` is blocking. Run it in a thread so it never stalls the event
loop:

```python
await asyncio.to_thread(apobj.notify, title=..., body=..., attach=...)
```

Wrap with a sane timeout so a hung SMTP/host doesn't stall alert delivery.
Same pattern already used for the embedder.

## Configuration — destinations are SECRETS, so env-only

Apprise URLs carry credentials (`discord://webhook_id/webhook_token`,
`tgram://bottoken/chatid`, `mailto://user:pass@host`). Therefore, by the
project's standing rule, **the destination URLs are secrets and live in env, not
the settings DB and never surfaced in the UI.**

- **`APPRISE_URLS`** (env): one or more Apprise URLs, newline- or
  comma-separated. Blank = notifications off (same "unset = disabled + log a
  line" pattern as the current Gotify path).
- What stays UI-operational is the **alert rules** (thresholds, keywords,
  cameras, cooldown) — those are already in the settings table. The
  **destinations** are env-only. Same split as everywhere else: rules =
  operational, credentials = env.

Add `APPRISE_URLS` to `docker-compose.yml` (bridge) and `.env.example` with a
couple of commented example URLs.

## Snapshot attachment

Attach the event snapshot where the target service supports it (Discord, email,
Telegram do; SMS obviously doesn't). Apprise takes attachments; support varies
per service. **Degrade gracefully:** attach where supported, fall back to the
snapshot URL in the body (`WEB_BASE_URL`) where not. Never let an
attachment-unsupported service turn into a delivery failure.

## Failure isolation

One misconfigured or down service must not break the others or wedge the
pipeline:

- Apprise returns overall success/failure; log per-dispatch failures, don't
  throw into the event loop (same defensive stance as processors).
- A hung service is bounded by the `to_thread` timeout, not left to stall.
- A bad URL at startup is logged and skipped, not fatal.

## Gotify backward-compatibility

Gotify is just another Apprise schema (`gotify://`). To avoid breaking existing
deployments:

- **Keep the current `GOTIFY_URL` / `GOTIFY_TOKEN` working.** If they're set,
  translate them into an equivalent `gotify://` Apprise URL internally and add it
  to the Apprise instance alongside anything in `APPRISE_URLS`.
- Document `APPRISE_URLS` as the new, general way; treat the dedicated Gotify
  vars as a still-supported convenience, not a second code path to maintain.

## Dependency note

`apprise` is permissively licensed (BSD-2). Fine for the AGPL project. It pulls a
few transitive deps but nothing heavy or license-hostile — confirm at add time.

## Review checklist (reject a proposal that does any of these)

- Hand-rolls per-service notifiers (Discord/Telegram/ntfy/etc.) instead of the
  single Apprise notifier.
- Puts Apprise URLs / any destination credentials in the settings DB or surfaces
  them in the UI (they're secrets → env-only).
- Calls blocking `apprise.notify()` directly on the event loop instead of
  `to_thread` + timeout.
- Lets one failed/misconfigured destination throw into the pipeline or block the
  others.
- Breaks the existing `GOTIFY_URL`/`GOTIFY_TOKEN` setup instead of bridging it to
  a `gotify://` URL.
- Builds per-camera / per-severity routing in v1 (that's a later feature; v1 is
  fan-out to all).
