# Design: first-run experience — a setup *doctor*, not a wizard

Status: proposed

Principle: **the first-run flow VALIDATES and GUIDES; it never WRITES secrets.**

## What NOT to build (and why)

A first-run wizard that collects *all* config — Protect password, API key, DB
password, vision key, URLs — is tempting but wrong for this app:

- **It reopens secrets-in-the-database.** Anything a wizard collects it must
  store somewhere the app reads on boot (DB or a written file). We deliberately
  keep secrets **env-only** so they never land in Postgres, backups, or anywhere
  the web UI could surface them. For a security tool, "your camera-system
  credentials now live in a table" is a strictly worse posture.
- **Chicken-and-egg lifecycle.** The bridge can't ingest without Protect creds +
  a reachable vision URL, so a wizard forces a degraded "unconfigured" boot mode
  (setup screen → accept values → bring real services up). That's a whole second
  lifecycle to build and maintain vs. "read env, validate, run".
- **The setup page is unauthenticated.** Fresh deploy, no password set yet, and a
  page is sitting on the LAN ready to *store* Protect admin creds — whoever hits
  it first wins. Same loaded-gun problem as `/api/settings`, but with
  credentials on the line.
- **It fights the deploy model.** Users deploy via Portainer/compose, where
  config-as-env-vars is the native idiom: reproducible, in their IaC,
  redeploy-identical. Wizard-stored config lives in app state, so a
  redeploy/volume-wipe loses it and automation gets harder.

## What first-run SHOULD be: a read-only doctor

Secrets and infra endpoints stay **env-only**, set via compose/Portainer. The
app refuses to start without the required ones (as `Config.validate()` does
today). The first-run experience is a **diagnostic page**, shown when required
config is missing/invalid (and reachable any time as a health/setup view). It
tells the operator *which env var to fix* — and writes nothing.

Checks it runs:

- **Database** reachable? ✓ / ✗
- **UniFi Protect** reachable + auth OK at `UFP_ADDRESS`? On failure →
  "check it's a **local** (non-SSO) user + a valid API key + that this host can
  route to the Protect subnet."
- **Vision endpoint** reachable + actually image-capable? On failure →
  "a text-only model can't see images — you probably forgot the **mmproj**."

  **The vision check must NOT require JSON output.** A correctly configured
  vision model (Qwen2.5-VL with mmproj loaded) returns plain prose to a generic
  prompt — e.g. "A person with long hair and a beige hat is seated inside a
  vehicle." Treating "not valid JSON" as "no image support" is a false negative
  that fires on *every* correctly-configured server (observed in phase-1
  testing). Success = a **non-empty text response that references image
  content**, not strict JSON. The robust probe: send a small **real** photo
  (never a 1×1 or solid color) containing something specific/unusual and check
  the reply mentions it — a text-only model can't, so it can't fake a match, and
  no JSON parsing is needed. Keep three distinct states: *unreachable*
  (connection fails) vs. *reachable-but-image-blind* (real mmproj problem) vs.
  *OK*; only show the mmproj hint for the middle one.
- **Whisper / Gotify**: optional — just show configured / not configured.

Output is guidance, not a form. Green checks + red items with the exact env var
to set. No inputs that persist anything.

## The only writable config

Operational settings (the `settings`-table keys from `settings.md`) are the only
things editable in the UI. Secrets/endpoints are never editable. This keeps the
welcoming, connection-testing first-run experience — which genuinely cuts the #1
support category (misconfigured Protect creds / wrong vision URL / missing
mmproj) — while never putting a credential in the database.

## Review checklist (reject a proposal that does any of these)

- Collects Protect / DB / vision **credentials** in any UI form.
- **Persists** any secret to the database or a written config file.
- Boots into a writable "unconfigured" mode that stores config in app state.
- Requires an unauthenticated setup page to accept secrets before auth exists.
- Turns the doctor into a wizard (inputs that write) rather than diagnostics.
