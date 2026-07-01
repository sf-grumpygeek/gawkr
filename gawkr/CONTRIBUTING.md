# Contributing to gawkr

Thanks for taking a look. gawkr is fully open source (AGPL-3.0) — the whole
thing, no paid tier. Contributions of any kind are welcome, including new
processors like face recognition or richer behavior analysis. The only ask is
that additions keep the project open (AGPL) and follow the conventions below.

## Ground rules

- **Never commit secrets or your network layout.** No IPs, tokens, API keys, or
  `.env` files. Install the secret-scan hook before you start:
  ```bash
  pipx install pre-commit
  pre-commit install
  ```
  CI also runs gitleaks on every push, so a leaked value will fail the build.
- **Redact when you open an issue.** Logs and screenshots contain your camera
  names and IPs — scrub them.

## Dev setup

The whole thing runs from `docker compose`. For quick iteration on the bridge:

```bash
cp .env.example .env      # fill in your Protect creds + VISION_URL
docker compose up -d --build
docker compose logs -f bridge
```

Byte-compile before you push (CI does this too):

```bash
python3 -m py_compile bridge/gawkr/*.py web/app.py
```

## Architecture in one paragraph

The `bridge` subscribes to UniFi Protect events, grabs a snapshot (and, for
transcription, the clip), and runs a set of **processors** over it — each
capability is one small class implementing `handles()` + `process()` in
`bridge/gawkr/pipeline.py`. Results are merged onto the event, embedded for
search, and stored in Postgres/pgvector. The `web` service is a thin FastAPI +
static UI over that table. Adding a capability = adding a processor; that's the
main extension point.

## PRs

Keep them focused, describe what you tested, and don't include generated files
or your local config. New processors should be off by default behind an env var.

## License

By contributing you agree your contributions are licensed under AGPL-3.0-or-later.
