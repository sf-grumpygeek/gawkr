# gawkr

Self-hosted AI for UniFi Protect. gawkr watches your Protect console for smart
detections, describes each one with a vision model you run yourself, reads
license plates and identifies vehicles, transcribes doorbell audio, indexes
everything for natural-language search, and pushes Gotify alerts — all on your
own hardware. An open, DIY alternative to expensive first-party AI add-ons.

Point it at any OpenAI-compatible vision endpoint (llama.cpp, Ollama, vLLM). No
cloud, no per-camera fees, your footage never leaves your network.

## What it does

- **Event descriptions** — a plain-language summary of every detection
- **Natural-language search** — "white truck at night", "person at the back door"
- **License plates** — OCR on `licensePlate` events
- **Vehicle ID** — make / model / color / body type
- **Audio transcription** — optional, via a whisper server
- **Safety flags** — visible-weapon / concerning-action / threat-level hints
- **Gotify alerts** — rule-based push with the snapshot attached
- **A clean web UI** — live feed, search, per-event detail

## Architecture

```
UniFi Protect ──▶ bridge ──▶ vision model (your llama.cpp/Ollama)
                    │
                    ├─▶ postgres + pgvector  (events + embeddings)
                    └─▶ web UI on :8080  (feed / search / detail)
```

Three containers: `db` (Postgres + pgvector), `bridge` (ingestion), `web` (UI).
The vision model runs wherever you already run it and is reached over HTTP.

## Requirements

- **UniFi Protect 6.0+**, a **local access user** (not a Ubiquiti SSO/cloud
  account), and a **Protect API key**.
- An OpenAI-compatible **vision model** endpoint — e.g. llama.cpp `llama-server`
  with a Qwen2.5-VL GGUF **plus its mmproj** (a text-only model won't see images).
- A Docker host that can route to both Protect and the vision endpoint.

## Quick start

```bash
cp .env.example .env      # fill in Protect creds + VISION_URL + a DB password
docker compose up -d --build
docker compose logs -f bridge
```

Open `http://<host>:8080`, trip a camera, and the event appears with a
description within a few seconds. Everything past descriptions/plates/vehicles is
optional and off by default — set `WHISPER_URL` for transcription,
`GOTIFY_URL`+`GOTIFY_TOKEN` for alerts.

## Auth

gawkr is single-tenant — one operator/household, no user accounts or roles.
When enabled, auth protects the whole app (viewing events *and* changing
settings), not select endpoints.

**Recommended: authenticate at a reverse proxy you already run.** gawkr adds
zero auth code for this path — put it behind Nginx Proxy Manager, Traefik,
Caddy, HAProxy, or a forward-auth provider (Authelia, Authentik, tinyauth) and
leave `APP_PASSWORD`/`SESSION_SECRET` unset. If you go this route:
- **Don't publish the container port to the host** — expose gawkr only on the
  proxy's internal network, so nobody on the LAN can bypass the proxy by
  hitting the container directly.
- If terminating TLS at the proxy, run uvicorn with `--proxy-headers` (and a
  trusted `--forwarded-allow-ips`) so gawkr sees the real scheme.

**Fallback: built-in single app password.** For operators without a reverse
proxy. Set both in your env (never commit real values):
- `APP_PASSWORD` — the login password. Unset = auth disabled, with a loud
  startup warning in the `web` container logs.
- `SESSION_SECRET` — signs session cookies. Required whenever `APP_PASSWORD`
  is set; the app refuses to start without it (it will not auto-generate or
  persist one). Generate with `openssl rand -hex 32`.

There's no username, no reset-password flow, and no email — a single-password
tool has nothing to reset *to*. If you forget the password or suspect it
leaked, don't try to recover it: edit `APP_PASSWORD` in your env and redeploy.
That also invalidates every existing session, which is exactly what a
password rotation should do.

## Notes & honest limits

- The `get_camera_snapshot` / `get_camera_video` calls in `bridge/gawkr/source.py`
  are the spots most sensitive to your `uiprotect` version — check them there if
  events log but images/clips don't appear.
- Vision-model accuracy: color/type/make are reliable; exact model/year are a
  best guess. The safety flags are a *heads-up from a single frame*, not a
  security-grade detector — calibrate before you trust an alert.
- Deploys great as a Portainer **Git stack** (env vars in the UI, no committed
  `.env`).

## License

AGPL-3.0-or-later — free and open for everyone, forever. See `LICENSE`.
