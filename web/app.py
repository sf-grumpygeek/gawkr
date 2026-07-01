from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time

import asyncpg
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastembed import TextEmbedding
from itsdangerous import URLSafeTimedSerializer
from pgvector.asyncpg import register_vector

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("gawkr.web")

DSN = os.environ["DATABASE_URL"]
DATA_DIR = os.environ.get("DATA_DIR", "/data")
SNAP_DIR = os.path.join(DATA_DIR, "snapshots")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "BAAI/bge-small-en-v1.5")
ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# --- Auth: single app password (see docs/design/auth.md). Unset = disabled. ---
# APP_PASSWORD and SESSION_SECRET are secrets -- env-only, like every other
# secret in this project. Never stored in the settings table or surfaced in the UI.
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
AUTH_ENABLED = bool(APP_PASSWORD)
SESSION_COOKIE = "gawkr_session"
SESSION_MAX_AGE = 7 * 24 * 3600  # 7 days

if AUTH_ENABLED and not SESSION_SECRET:
    raise SystemExit(
        "APP_PASSWORD is set but SESSION_SECRET is not. Refusing to start: a "
        "session-signing key is required to issue cookies, and gawkr will not "
        "auto-generate or persist one (this container's /data mount is read-only). "
        "Set SESSION_SECRET to a long random value, e.g. `openssl rand -hex 32`."
    )

if not AUTH_ENABLED:
    log.warning(
        "running without auth -- anyone who can reach this can view events and "
        "change settings; set APP_PASSWORD or put gawkr behind an authenticating proxy."
    )

# Mixing the password into the signing key means rotating APP_PASSWORD (and
# redeploying) changes the key, which invalidates every existing session --
# password rotation is the whole "log everyone out" story, with no extra state.
_serializer: URLSafeTimedSerializer | None = None
if AUTH_ENABLED:
    signing_key = SESSION_SECRET + hashlib.sha256(APP_PASSWORD.encode()).hexdigest()
    _serializer = URLSafeTimedSerializer(signing_key, salt="gawkr-session")

# Per-IP login throttle (in-memory; single-process container). Not persisted --
# a restart resets it, which is fine, this only needs to slow down brute force.
_login_state: dict[str, dict] = {}
LOGIN_MAX_FAILURES = 10
LOGIN_LOCKOUT_SECONDS = 300

# Allow-list of DB-overridable operational settings, enforced here on write.
# Keep in sync with bridge/gawkr/settings.py OPERATIONAL_KEYS -- this list must
# never include a secret or infra endpoint (those stay env-only in bridge's Config).
_LEVELS = ("none", "low", "medium", "high")


def _v_bool(v):
    if not isinstance(v, bool):
        raise ValueError("expected bool")
    return v


_v_bool.meta = {"type": "bool"}


def _v_str_list(v):
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ValueError("expected list of strings")
    return v


_v_str_list.meta = {"type": "str_list"}


def _v_float(v):
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise ValueError("expected number")
    return float(v)


_v_float.meta = {"type": "float"}


def _v_threat_level(v):
    if v not in _LEVELS:
        raise ValueError("invalid threat level")
    return v


_v_threat_level.meta = {"type": "enum", "options": list(_LEVELS)}

DESCRIBE_CONTEXT_MAX_LEN = 500


def _v_describe_context(v):
    if not isinstance(v, str):
        raise ValueError("expected string")
    if len(v) > DESCRIBE_CONTEXT_MAX_LEN:
        raise ValueError(f"describe_context exceeds {DESCRIBE_CONTEXT_MAX_LEN} chars")
    return v


_v_describe_context.meta = {"type": "text", "max_len": DESCRIBE_CONTEXT_MAX_LEN}


OPERATIONAL_KEYS = {
    "identify_vehicles": _v_bool,
    "transcribe_cameras": _v_str_list,
    "smart_types": _v_str_list,
    "alert_on_weapon": _v_bool,
    "alert_threat_level": _v_threat_level,
    "alert_keywords": _v_str_list,
    "alert_cameras": _v_str_list,
    "alert_cooldown": _v_float,
    "describe_context": _v_describe_context,
}

app = FastAPI(title="gawkr")
_pool: asyncpg.Pool | None = None
_embed: TextEmbedding | None = None

# Paths reachable without a session. Everything else is gated when AUTH_ENABLED.
_AUTH_EXEMPT = {"/login", "/logout", "/healthz"}


def _is_authenticated(request: Request) -> bool:
    if not AUTH_ENABLED:
        return True
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return False
    try:
        _serializer.loads(token, max_age=SESSION_MAX_AGE)  # type: ignore[union-attr]
        return True
    except Exception:
        # Fail closed: any malformed cookie or unexpected itsdangerous error
        # (not just BadSignature/SignatureExpired) must never propagate out of
        # the auth check and must never be treated as authenticated.
        return False


def _set_session_cookie(response, request: Request) -> None:
    token = _serializer.dumps({"auth": True})  # type: ignore[union-attr]
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=SESSION_MAX_AGE, httponly=True, samesite="lax",
        secure=(request.url.scheme == "https"),
    )


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _throttle_check(key: str) -> float:
    """Returns seconds to wait before processing this login attempt (0 if none)."""
    st = _login_state.get(key)
    if not st:
        return 0.0
    if st["failures"] >= LOGIN_MAX_FAILURES:
        remaining = st["locked_until"] - time.monotonic()
        if remaining > 0:
            return remaining
        _login_state.pop(key, None)
    return 0.0


def _throttle_fail(key: str) -> None:
    st = _login_state.setdefault(key, {"failures": 0, "locked_until": 0.0})
    st["failures"] += 1
    if st["failures"] >= LOGIN_MAX_FAILURES:
        st["locked_until"] = time.monotonic() + LOGIN_LOCKOUT_SECONDS


def _throttle_reset(key: str) -> None:
    _login_state.pop(key, None)


_LOGIN_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>gawkr — sign in</title>
<style>
  :root{{--bg:#0c1014;--surface:#141a21;--line:#283139;--text:#d6dee6;
    --muted:#7f8b97;--accent:#2f81f7;--bad:#f85149;--radius:10px;
    --ui:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    --mono:ui-monospace,"SF Mono","JetBrains Mono","Cascadia Code",Menlo,Consolas,monospace}}
  *{{box-sizing:border-box}}
  html,body{{margin:0;height:100%}}
  body{{background:var(--bg);color:var(--text);font-family:var(--ui);
    display:flex;align-items:center;justify-content:center}}
  form{{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);
    padding:28px;width:280px;display:flex;flex-direction:column;gap:14px}}
  h1{{font-family:var(--mono);font-size:14px;letter-spacing:1.5px;text-transform:uppercase;
    margin:0 0 4px}}
  input{{background:#0c1014;border:1px solid var(--line);border-radius:8px;
    color:var(--text);padding:10px 12px;font:inherit;outline:none}}
  input:focus{{border-color:var(--accent)}}
  button{{background:var(--accent);border:none;border-radius:8px;color:#fff;
    padding:10px 12px;font:inherit;font-weight:600;cursor:pointer}}
  .err{{color:var(--bad);font-size:13px;margin:0}}
</style>
</head>
<body>
<form method="post" action="/login">
  <h1>gawkr</h1>
  {error}
  <input type="password" name="password" placeholder="password" autofocus required />
  <button type="submit">sign in</button>
</form>
</body>
</html>"""


@app.get("/login", include_in_schema=False, response_model=None)
async def login_page(request: Request) -> HTMLResponse:
    if _is_authenticated(request):
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(_LOGIN_PAGE.format(error=""))


@app.post("/login", include_in_schema=False, response_model=None)
async def login_submit(request: Request) -> HTMLResponse | RedirectResponse:
    if not AUTH_ENABLED:
        return RedirectResponse("/", status_code=303)
    key = _client_key(request)
    if _throttle_check(key) > 0:
        # Locked out: reject outright, no sleep. The password is never checked
        # here, so a delay buys no security -- it would only tie up a request
        # slot for up to LOGIN_LOCKOUT_SECONDS for every locked-out retry.
        return HTMLResponse(
            _LOGIN_PAGE.format(error='<p class="err">too many attempts, try again later</p>'),
            status_code=429)

    form = await request.form()
    password = form.get("password", "")
    if not isinstance(password, str):
        password = ""

    if hmac.compare_digest(password.encode(), APP_PASSWORD.encode()):
        _throttle_reset(key)
        resp = RedirectResponse("/", status_code=303)
        _set_session_cookie(resp, request)
        return resp

    st = _login_state.get(key, {"failures": 0})
    await asyncio.sleep(min(2 ** st["failures"], 20))
    _throttle_fail(key)
    return HTMLResponse(
        _LOGIN_PAGE.format(error='<p class="err">wrong password</p>'), status_code=401)


@app.post("/logout", include_in_schema=False, response_model=None)
async def logout() -> RedirectResponse:
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict:
    """Unauthenticated liveness probe for the container healthcheck -- no data,
    no DB round-trip, so it can't be used to bypass the login gate for anything."""
    return {"ok": True}


@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    if AUTH_ENABLED and request.url.path not in _AUTH_EXEMPT and not _is_authenticated(request):
        if request.url.path.startswith("/api"):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


async def _init_conn(conn):
    await register_vector(conn)
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads,
                              schema="pg_catalog")


@app.on_event("startup")
async def _startup() -> None:
    global _pool, _embed
    for _ in range(10):
        try:
            _pool = await asyncpg.create_pool(DSN, min_size=1, max_size=5, init=_init_conn)
            break
        except Exception:
            await asyncio.sleep(2)
    if _pool is None:
        raise RuntimeError("could not connect to database")
    # Defensive: on a fresh deploy this service can start before bridge has
    # run once and created the table itself.
    await _pool.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
          key        TEXT PRIMARY KEY,
          value      JSONB NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # Defensive here too: the doctor page can be the first thing hit on a
    # fresh deploy, before bridge has run once and created the table itself.
    await _pool.execute(
        """
        CREATE TABLE IF NOT EXISTS doctor_status (
          name       TEXT PRIMARY KEY,
          ok         BOOLEAN NOT NULL,
          detail     TEXT,
          checked_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    _embed = TextEmbedding(model_name=EMBED_MODEL)


def _embed_query(q: str) -> list[float]:
    vec = next(iter(_embed.embed([q])))  # type: ignore[union-attr]
    return [float(x) for x in vec]


def _row(r: asyncpg.Record) -> dict:
    rec = r["record"] or {}
    desc = rec.get("description") or {}
    return {
        "event_id": r["event_id"],
        "ts": r["ts"],
        "camera": r["camera"],
        "smart_types": list(r["smart_types"] or []),
        "summary": r["summary"] or desc.get("summary", ""),
        "notable": r["notable"],
        "objects": desc.get("objects", []),
        "attributes": desc.get("attributes", []),
        "plate": rec.get("plate"),
        "vehicle": rec.get("vehicle"),
        "transcript": rec.get("transcription"),
        "threat_level": desc.get("threat_level"),
        "weapon": desc.get("weapon"),
        "weapon_detail": desc.get("weapon_detail"),
        "concerning_detail": desc.get("concerning_detail"),
        "has_snapshot": bool(r["snapshot"]),
    }


@app.get("/api/cameras")
async def cameras() -> list[str]:
    rows = await _pool.fetch("SELECT DISTINCT camera FROM events ORDER BY camera")
    return [r["camera"] for r in rows]


@app.get("/api/events")
async def events(camera: str | None = None,
                 type: str | None = Query(None),
                 limit: int = 60) -> list[dict]:
    limit = max(1, min(limit, 200))
    where, args = [], []
    if camera:
        args.append(camera)
        where.append(f"camera = ${len(args)}")
    if type:
        args.append(type)
        where.append(f"${len(args)} = ANY(smart_types)")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    args.append(limit)
    rows = await _pool.fetch(
        f"SELECT event_id, ts, camera, smart_types, summary, notable, snapshot, record "
        f"FROM events {clause} ORDER BY ts DESC LIMIT ${len(args)}", *args)
    return [_row(r) for r in rows]


@app.get("/api/search")
async def search(q: str, limit: int = 60) -> list[dict]:
    limit = max(1, min(limit, 200))
    emb = await asyncio.to_thread(_embed_query, q)
    like = f"%{q}%"
    rows = await _pool.fetch(
        """
        SELECT event_id, ts, camera, smart_types, summary, notable, snapshot, record
        FROM events
        ORDER BY
          ((record->'plate'->>'plate' ILIKE $2)
            OR (record->'transcription'->>'text' ILIKE $2)
            OR (summary ILIKE $2)) DESC,
          embedding <=> $1 ASC
        LIMIT $3
        """, emb, like, limit)
    return [_row(r) for r in rows]


@app.get("/api/event/{eid}")
async def event(eid: str) -> dict:
    if not ID_RE.match(eid):
        raise HTTPException(400, "bad id")
    r = await _pool.fetchrow(
        "SELECT event_id, ts, camera, smart_types, summary, notable, snapshot, record "
        "FROM events WHERE event_id = $1", eid)
    if not r:
        raise HTTPException(404, "not found")
    out = _row(r)
    out["record"] = r["record"]
    return out


@app.get("/api/settings/schema")
async def settings_schema() -> dict:
    """Metadata (type + constraints) for every operational key, so the UI can
    render the full editable set -- including keys with no override row yet --
    without keeping its own copy of OPERATIONAL_KEYS or its constraints."""
    return {key: fn.meta for key, fn in OPERATIONAL_KEYS.items()}


@app.get("/api/settings")
async def get_settings() -> dict:
    """Sparse DB overrides only -- keys not set here still fall back to the
    bridge's env-configured default, which this service cannot see."""
    rows = await _pool.fetch("SELECT key, value FROM settings")
    return {r["key"]: r["value"] for r in rows if r["key"] in OPERATIONAL_KEYS}


@app.delete("/api/settings/{key}")
async def delete_setting(key: str) -> dict:
    """Clear an override so the key falls back to its env/default value."""
    if key not in OPERATIONAL_KEYS:
        raise HTTPException(400, f"not overridable: {key}")
    async with _pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM settings WHERE key = $1", key)
            await conn.execute("NOTIFY gawkr_settings")
    return {"deleted": key}


@app.put("/api/settings")
async def put_settings(body: dict = Body(...)) -> dict:
    unknown = [k for k in body if k not in OPERATIONAL_KEYS]
    if unknown:
        raise HTTPException(400, f"not overridable: {', '.join(unknown)}")

    validated: dict = {}
    for key, value in body.items():
        try:
            validated[key] = OPERATIONAL_KEYS[key](value)
        except (ValueError, TypeError) as e:
            raise HTTPException(400, f"{key}: {e}")

    async with _pool.acquire() as conn:
        async with conn.transaction():
            for key, value in validated.items():
                await conn.execute(
                    """
                    INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, now())
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
                    """, key, value)
            await conn.execute("NOTIFY gawkr_settings")
    return validated


@app.get("/api/doctor")
async def doctor_status() -> dict:
    """Read-only diagnostic view: reachability/config status for Protect,
    vision, and optional whisper/gotify, as last written by the bridge.
    Never accepts input -- this is the doctor, not a setup wizard."""
    try:
        rows = await _pool.fetch("SELECT name, ok, detail, checked_at FROM doctor_status")
    except Exception as e:
        return {"database": {"ok": False, "detail": str(e)}}
    out = {r["name"]: {"ok": r["ok"], "detail": r["detail"], "checked_at": r["checked_at"]}
           for r in rows}
    out["database"] = {"ok": True, "detail": None, "checked_at": None}
    return out


@app.get("/api/snapshot/{eid}")
async def snapshot(eid: str) -> FileResponse:
    if not ID_RE.match(eid):
        raise HTTPException(400, "bad id")
    path = os.path.join(SNAP_DIR, f"{eid}.jpg")
    if not os.path.isfile(path):
        raise HTTPException(404, "no snapshot")
    return FileResponse(path, media_type="image/jpeg")


app.mount("/", StaticFiles(directory="static", html=True), name="static")
