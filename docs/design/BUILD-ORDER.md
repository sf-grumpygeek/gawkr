# Build order & how to work these features

Design docs live beside this file: `first-run.md`, `settings.md`, `auth.md`,
`notifications.md`.
Each ends with a **review checklist** — treat those as acceptance criteria.

## How to work (every feature)

1. **Read the relevant design doc first.** Build to the spec, not from memory.
2. **Plan before code.** Propose the approach, wait for an OK, then implement.
3. **Check the plan against that doc's review checklist** before writing code —
   the checklists exist to catch the tempting-but-wrong version.
4. **One feature per branch** (`git checkout -b <feature>`); small, reviewable
   commits. Review the diff before each commit.
5. **Verify before claiming done:** `pre-commit run --all-files` (gitleaks +
   checks) and `python -m py_compile bridge/gawkr/*.py web/app.py`; run tests if
   they exist.
6. **Never commit secrets.** No IPs, tokens, keys, `.env`. The pre-commit hook
   is the seatbelt.

## Step 0 — Inventory (do this first, no code)

Some of this may already exist (see `git log` — settings hot-reload and an auth
pass have commits). Before building anything: compare what's in the repo against
each design doc and produce a **gap list**. Don't rebuild what's already there.

## Order

Build in this sequence — each is a separate, planned, branched task.

### 1. First-run doctor  (`first-run.md`) — done/merged
Earliest because it's **read-only** (diagnoses env/connectivity, writes nothing)
and low-risk. A setup doctor that reports "Protect reachable ✓ / vision endpoint
not image-capable ✗ — you forgot the mmproj" cuts the #1 support category and
helps every later step. Reject: any wizard that collects or stores secrets.

### 2. Finish settings  (`settings.md`) — done/merged
Sparse overrides (no seeding from env), secrets never DB-overridable,
LISTEN/NOTIFY **with** a polling fallback and reload-on-reconnect, processors
read settings **live in `handles()`** (don't rebuild the pipeline). `GET
/api/settings` returns sparse overrides only; UI shows un-set keys as "using
default". Includes `describe_context` (additive injected prompt text, not a
full-prompt replacement).

### 3. Auth  (`auth.md`) — done/merged
Depends on #2: the browser-editable settings endpoint is what makes an
unauthenticated UI dangerous. App-password path: constant-time compare,
HttpOnly+SameSite signed cookie, no hardcoded `SESSION_SECRET`, password change
invalidates existing sessions, throttle failed logins. **No** user system, roles,
reset form, or reset tokens. Forward-auth (any reverse proxy) is the zero-code
recommended path. **Get the login/session/cookie code reviewed before deploying.**

### 4. Multi-channel notifications (`notifications.md`) — after auth, in progress
Delivery layer only — the alert *rules* don't change. ONE Apprise notifier,
never per-service integrations. `APPRISE_URLS` is credential-bearing →
**env-only**, never in the settings DB or UI. Fan-out to all configured
services in v1 (per-camera/severity routing is later). Attach the snapshot
where supported, degrade to a URL where not. `to_thread` + timeout for the sync
library. Isolate per-destination failures. Keep existing GOTIFY_URL/GOTIFY_TOKEN
working by folding it into a `gotify://` URL — one notifier, not two paths.
Reject: any hand-rolled Discord/Telegram/ntfy notifier, or Apprise URLs in the
settings DB.

## Reminder

Launch Claude Code from the repo root (`~/gawkr`) — the project is flat at the
root now, not under a nested `gawkr/` subdir. Running from the wrong level is
what caused the earlier file-placement mess.
