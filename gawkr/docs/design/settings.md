# Design: web-configurable settings + hot-reload

Status: proposed (roadmap #1, precedes auth)

Goal: let operators change **operational** settings from the web UI instead of
env vars, and have the bridge pick up changes **without a container restart** —
while keeping all secrets and infra endpoints env-only.

## Core model: two layers, DB overrides env

`Config` (env) stays exactly as it is: it remains the source of secrets, infra
endpoints, and the **defaults**. Add a thin override layer on top of it:

```
effective value = DB override (if the key was set in the UI)
                  else env value
                  else hardcoded default
```

Key consequence: an **empty settings table == today's behavior**. Existing
deploys upgrade with zero migration and zero seeding — nothing changes until
someone edits a setting in the UI, which writes exactly one row.

Do **not** seed the table from env on startup. Seeding makes env vestigial and
creates precedence confusion (edit an env var later, it silently doesn't take).
Sparse overrides only.

## Schema — sparse key/value

```sql
CREATE TABLE settings (
  key        TEXT PRIMARY KEY,
  value      JSONB NOT NULL,       -- preserves bool / int / list / string
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Sparse (only overridden keys exist) is what makes "DB → env → default"
precedence work. JSONB keeps types intact so `alert_cooldown` returns an int and
`alert_keywords` a list without hand-parsing.

## Security boundary: an allow-list, enforced twice

Define `OPERATIONAL_KEYS` — the only keys the DB may override:

- alert rules/thresholds/keywords/cameras/cooldown
- `transcribe_cameras`
- `identify_vehicles`
- `smart_types`
- model/timeout knobs (non-secret)

Enforce the allow-list in **both** directions:

- **On write** (`PUT /api/settings`): reject any key not in `OPERATIONAL_KEYS`.
- **On read/reload**: ignore any DB row whose key isn't in `OPERATIONAL_KEYS`.

Double enforcement means even a poisoned row can never override a secret.
**Secrets and infra endpoints stay env-only, full stop:** `UFP_PASSWORD`,
`UFP_API_KEY`, `POSTGRES_PASSWORD`, `VISION_API_KEY`, `GOTIFY_TOKEN`. They are
never writable, never surfaced in the UI, never stored where the web service can
read them back.

## Hot-reload: poll baseline + LISTEN/NOTIFY upgrade + reload-on-connect

Use polling as the reliable baseline, LISTEN/NOTIFY as the low-latency upgrade,
and always do a full reload whenever the listen connection (re)establishes.
LISTEN/NOTIFY **alone** is fragile — do not ship that.

Footgun checklist (the design must satisfy all of these):

1. **Missed NOTIFY on disconnect.** NOTIFY only reaches currently-connected
   listeners. If the bridge's listen connection drops (DB restart, blip), any
   change during the gap is lost and the bridge runs stale forever. Mitigate
   with a low-frequency poll (30–60s) as a safety net **and** a fresh full
   reload every time the listen connection (re)establishes — not just resuming
   the listener.
2. **Signal, don't ship data.** NOTIFY payloads cap at 8000 bytes and can race.
   Send a bump signal only; the bridge re-`SELECT`s the table. Never put values
   in the payload.
3. **Non-blocking listener callback.** asyncpg runs it in the connection's
   context — schedule `asyncio.create_task(settings.reload())`, never reload
   inline.
4. **Dedicated LISTEN connection** with a reconnect-and-backoff lifecycle,
   separate from the query pool.
5. **Atomic swap.** Reload builds a new overrides dict and reassigns the
   reference (`self._overrides = new`). Attribute assignment is atomic, so
   readers never see a torn dict — no locks needed.
6. **Validate on write, be defensive on read.** The `PUT` validates types/enums
   (`alert_threat_level="banana"` can't get in). On reload, skip-and-log a bad
   row and keep the prior good value — a garbage setting must never wedge the
   ingestion loop.
7. **Transaction-wrap multi-key saves** so one reload reflects a consistent set,
   not a half-applied form.

Polling-only is an acceptable v1 (a single indexed read every 10–15s is
nothing). Shipping that first and adding NOTIFY later is the sane order.

## The one real refactor: read settings live

Today processors are conditionally added at startup
(`if cfg.identify_vehicles: processors.append(...)`) and `AlertEngine` reads
`cfg` fields captured at init. Flip that:

- **Always instantiate** processors.
- Have `handles()` and `AlertEngine.evaluate()` read from the **live settings
  object**, not values copied at init.

So `VehicleProcessor.handles()` checks `settings.identify_vehicles` live;
flipping it off in the UI stops it on the next event — no restart, no rebuilding
the processor list. Do **not** tear down and rebuild the pipeline on change.

## Editable vision-prompt context (`describe_context`)

Operators want to tune what the vision model pays attention to — but the prompt
and the parser are a **contract**: `_parse()` in `vision.py` expects specific
JSON keys (`summary`, `objects`, `attributes`, `weapon`, `threat_level`, …) that
search embeddings, the alert engine, and the UI all depend on. If an operator
could rewrite the whole prompt, they could trivially drop the JSON instruction or
rename a field and silently break descriptions, alerts, and search.

So expose the tuning, not the contract:

- **`describe_context`** is one more `OPERATIONAL_KEY` — an **additive**, free-text
  snippet **injected** into the describe prompt, never a replacement for it. The
  fixed JSON-schema scaffold and safety-field definitions stay in code.
- Use cases: local domain knowledge — "rural property; livestock and farm trucks
  are normal, flag coyotes and unfamiliar vehicles", "ignore the flag on the pole".
- Read per-call in `DescriptionProcessor`, so edits take effect on the next event
  (live-reloaded like every other operational key).
- **Length-capped** (e.g. a few hundred chars) — a giant blob eats the vision
  model's token budget and can crowd out the image on a small context window.

Full-prompt override (replacing the scaffold) is a **v2**, and only behind
**validate-on-save** (run a test image, confirm the output still parses with the
required keys, reject if not) plus a **reset-to-default** button. Do not ship
free-form full-prompt editing without that guard.

## API surface (web service)

- `GET  /api/settings` → the **sparse overrides only** (the rows in the settings
  table), never secrets. See decision below.
- `PUT  /api/settings` → validate against `OPERATIONAL_KEYS` + types, upsert
  row(s) in one transaction, then `NOTIFY`.

The UI edits these; the bridge consumes them via the settings object above.

### DECIDED: GET returns sparse overrides, not resolved effective values

The web container is separate from the bridge — it has no `gawkr` package and is
not passed the operational env vars — so it **cannot** compute
"override → env → default" on its own. Options considered:

1. **Sparse overrides only (CHOSEN).** `GET` returns just what's actually set in
   the settings table. Un-set keys are shown in the UI as "inherited from env /
   using default", with no concrete value. No compose change, no second copy of
   `OPERATIONAL_KEYS`/defaults in web → **no drift** between containers.
2. Full effective value — rejected: would require passing the operational env
   vars into the web container and duplicating the allow-list + default logic in
   `web/app.py`. That two-places-for-defaults duplication is exactly what this
   whole config design exists to avoid.

**UI requirement:** render un-set keys with a clear "inherited from env / using
default" indicator (greyed placeholder or label) — never an empty field. An
empty `alert_cooldown` box must not read as "0 seconds" instead of "defaulting
to 120". This is the one place option 1 can mislead; the label fixes it.

**Future upgrade (only if operators ask to see concrete current values):** the
bridge — which *can* resolve effective values — writes a resolved snapshot on
each `reload()` (a `settings_effective` table or a read-only bridge endpoint)
that web reads. This keeps the bridge as the single source of truth with no env
duplication. Not needed for v1; do not build the bridge↔web plumbing until the
sparse-overrides loop is working.

## Auth hook (roadmap #2)

`PUT /api/settings` is a loaded gun until auth exists — unauthenticated, anyone
on the LAN can reconfigure alerting or disable detections. Land the endpoint now
as **known-unprotected**, and make #2 the immediate next step: reverse-proxy
forward-auth (if a reverse proxy is in use) or a single app password with a signed
session cookie. **Not** a rolled-from-scratch user/session system.

## Review checklist (reject a proposal that does any of these)

- Seeds the settings table from env at startup.
- Stores settings as one JSON blob instead of sparse key rows.
- Ships setting values inside the NOTIFY payload.
- Rebuilds the pipeline / processor list on change instead of reading live in
  `handles()`.
- Lets any secret become DB-overridable or surfaces one in `GET /api/settings`.
- Uses LISTEN/NOTIFY with no polling fallback and no reload-on-reconnect.
- Duplicates the operational env vars / defaults into the web container (GET must
  return sparse overrides only; un-set keys render as "using default", not blank).
