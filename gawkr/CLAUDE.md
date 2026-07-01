# CLAUDE.md — working guide for gawkr

This file orients any AI agent (and new humans) working in this repo. Read it
before making changes.

## What gawkr is

Self-hosted AI for UniFi Protect. It watches Protect for smart detections,
sends snapshots to a vision model **the user runs themselves** (any
OpenAI-compatible endpoint — llama.cpp, Ollama, vLLM), and turns each event into
a searchable, described, alertable record. No cloud; footage never leaves the
user's network. Positioned as an open, DIY alternative to expensive first-party
AI camera add-ons.

## Project scope — all open (AGPL-3.0)

gawkr is fully open source under AGPL-3.0. There is **no commercial edition**;
everything is welcome in this repo. (An earlier draft carved out a paid tier —
that plan is dead. Treat any lingering "commercial"/"open-core" language
anywhere as a bug to fix.)

Two capabilities were built in a separate private stack and are **not yet ported
into this repo**: **face recognition** (InsightFace + enrolled gallery) and
**multi-frame behavior analysis** (temporal loitering/casing from event clips).
These are welcome additions when the owner is ready — they are **not** off-limits,
just not here yet. If asked to build them, go ahead: follow the processor pattern
and the "off by default behind an env var" rule.

The AGPL is a deliberate choice: the owner wants gawkr to stay free and open for
everyone and to prevent anyone from re-closing it (e.g. wrapping it in a
proprietary SaaS). Keep that spirit — don't add anything that undermines the
copyleft.

## Architecture

Three containers, orchestrated by `docker-compose.yml`:

- **`db`** — Postgres 16 + pgvector. Events + 384-dim description embeddings.
- **`bridge`** — the ingestion service (Python package `bridge/gawkr/`, run as
  `python -m gawkr`). The brain.
- **`web`** — FastAPI + a static single-page UI (`web/app.py`,
  `web/static/index.html`) on `:8080`. Feed, search, event detail.

The vision model is **external** — reached over HTTP at `VISION_URL`, not part
of the stack.

Data flow:
```
Protect detection ─▶ bridge (snapshot at event start / clip at event end)
                       ├─▶ processors (see below) call the vision/whisper models
                       ├─▶ store: merge onto event row, embed for search (Postgres/pgvector)
                       └─▶ alerts: evaluate rules ─▶ Gotify push
web UI ─▶ reads Postgres, serves snapshots, hybrid vector+keyword search
```

## The processor pattern (main extension point)

`bridge/gawkr/pipeline.py` defines a `Processor` protocol: `handles(det)` +
`async process(det) -> dict`. Each capability is one small class. Current
processors:

- `DescriptionProcessor` — description + single-frame safety fields (event start)
- `PlateProcessor` — license-plate OCR, gated on `licensePlate` smart type
- `VehicleProcessor` — make/model/color/type, gated on `vehicle` smart type
- `TranscriptionProcessor` — whisper on the event clip's audio (event end)

`Pipeline.run()` runs the applicable processors concurrently, merges their
output into one `record`, saves it (upsert — start and end passes merge), and
fires alerts off the merged record.

**Adding a capability = adding a processor.** New processors must be **off by
default behind an env var**, and self-gate in `handles()`.

## Repo layout

```
bridge/gawkr/         the ingestion package
  __main__.py         entrypoint; wires processors based on config
  config.py           Config dataclass — ALL settings come from env vars
  source.py           UniFi Protect connection (uiprotect); snapshot/clip fetch
  vision.py           OpenAI-compatible vision client (describe/read_plate/read_vehicle)
  pipeline.py         Processor protocol + processors + Pipeline
  store.py            Postgres + pgvector; merge-on-save; embeddings
  embeddings.py       fastembed (bge-small, 384-dim, CPU/ONNX)
  whisper.py          whisper.cpp /inference client
  audio.py            ffmpeg clip -> 16kHz mono wav
  alerts.py           rule engine over event signals
  gotify.py           Gotify push notifier
web/                  FastAPI backend + static UI
scripts/check.sh      pre-deploy connectivity probe (uses PLACEHOLDER ips)
```

## Conventions & rules

- **Secrets & PII never get committed.** No real IPs, tokens, keys, `.env`, or
  personal hostnames. `scripts/check.sh` uses placeholders (`PROTECT_IP`,
  `VISION_HOST`) on purpose — keep it that way. gitleaks runs pre-commit and in
  CI; don't defeat it.
- **New config = update three places:** `config.py` (read it), `.env.example`
  (document it), and `docker-compose.yml` bridge `environment:` (pass it). Keep
  them in sync.
- **Everything optional is off by default.** Transcription, alerts, etc. only
  activate when their env vars are set.
- **Validate before you're done:** `python3 -m py_compile bridge/gawkr/*.py web/app.py`.
  There's no test suite yet; the compile sweep + the CI workflow are the gate.
- **Keep it model-agnostic.** Don't hardcode a specific vision backend or a
  specific GPU; `VISION_URL` points anywhere OpenAI-compatible.

## Known gotchas

- **`uiprotect` version sensitivity:** `get_camera_snapshot` and
  `get_camera_video` in `source.py` are the calls most likely to differ across
  library versions (flagged `VERIFY`). Symptom: events log but snapshots/clips
  don't appear. Requires a **local** Protect access user (not Ubiquiti SSO) +
  an API key.
- **Vision needs the mmproj:** a text-only GGUF silently ignores images. Not our
  code's problem, but the #1 user setup failure — keep the README note.
- **JSONB round-trips as dict** only because `store.py`/`app.py` register a jsonb
  codec on the asyncpg pool. Don't remove it.
- **Embedding dim is 384** (bge-small). If the embed model changes, `EMBED_DIM`
  in `embeddings.py` and the `VECTOR(...)` column must match.
- **Protect may be on a different subnet** than the Docker host; connectivity is
  a common deploy failure (see `scripts/check.sh`).

## Roadmap & design decisions

### 1. Web-configurable settings (do this before auth)
Today `Config` reads env at startup. Goal: make **operational** settings
editable in the web UI. Plan:
- Store operational settings in a Postgres `settings` table; web UI writes them;
  bridge reads them and **reloads without a container restart** (poll the table
  or use Postgres `LISTEN/NOTIFY`).
- **Secrets stay in env** — `UFP_PASSWORD`, `UFP_API_KEY`, `POSTGRES_PASSWORD`,
  `VISION_API_KEY`. Never surface secrets in a UI or store them where the web
  service can display them; this is a security tool.
- UI-editable = alert rules/thresholds/keywords/cooldown, transcribe cameras,
  `identify_vehicles`, smart types. Not credentials or endpoints-with-keys.

### 2. Auth (depends on #1)
Once settings are browser-editable, an unauthenticated UI lets anyone on the LAN
change rules and read every event. **Don't build a user system.** Preferred, in
order: (a) reverse-proxy forward-auth (user already runs Nginx Proxy Manager),
(b) a single app password with a signed session cookie. Rolling your own
session/password handling is how a security tool earns a CVE — avoid it.

## Dev environment (Windows + VS Code, current)

- Edit in VS Code; run commands in the integrated terminal (PowerShell).
- Quick validation without Docker: `python3 -m py_compile bridge/gawkr/*.py web/app.py`.
- Full run needs Docker Desktop **and** reachable Protect + vision endpoints, so
  end-to-end testing happens against the user's real homelab, not the laptop.
  Expect to iterate on code here, then deploy to the Linux Docker host
  (Portainer Git stack) to actually exercise it.
- Watch path separators in any new scripts (`/` in-container vs `\` on Windows);
  keep container-facing paths POSIX.
- CI lives in `.github/workflows/` for GitHub. (A `.gitea/workflows/` copy exists
  for the user's Gitea mirror — keep both in sync if you touch CI.)

## Before publishing / housekeeping TODOs

- Replace `security@example.com` in `SECURITY.md`.
- Add a copyright/attribution line (source headers or a NOTICE) if attribution
  matters.
- No test suite yet — a few unit tests around `_parse` (vision JSON), the alert
  rule engine, and `_embed_text` would be high-value first additions.
