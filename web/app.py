from __future__ import annotations

import asyncio
import json
import os
import re

import asyncpg
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastembed import TextEmbedding
from pgvector.asyncpg import register_vector

DSN = os.environ["DATABASE_URL"]
DATA_DIR = os.environ.get("DATA_DIR", "/data")
SNAP_DIR = os.path.join(DATA_DIR, "snapshots")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "BAAI/bge-small-en-v1.5")
ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Allow-list of DB-overridable operational settings, enforced here on write.
# Keep in sync with bridge/gawkr/settings.py OPERATIONAL_KEYS -- this list must
# never include a secret or infra endpoint (those stay env-only in bridge's Config).
_LEVELS = ("none", "low", "medium", "high")


def _v_bool(v):
    if not isinstance(v, bool):
        raise ValueError("expected bool")
    return v


def _v_str_list(v):
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ValueError("expected list of strings")
    return v


def _v_float(v):
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise ValueError("expected number")
    return float(v)


def _v_threat_level(v):
    if v not in _LEVELS:
        raise ValueError("invalid threat level")
    return v


DESCRIBE_CONTEXT_MAX_LEN = 500


def _v_describe_context(v):
    if not isinstance(v, str):
        raise ValueError("expected string")
    if len(v) > DESCRIBE_CONTEXT_MAX_LEN:
        raise ValueError(f"describe_context exceeds {DESCRIBE_CONTEXT_MAX_LEN} chars")
    return v


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


@app.get("/api/settings")
async def get_settings() -> dict:
    """Sparse DB overrides only -- keys not set here still fall back to the
    bridge's env-configured default, which this service cannot see."""
    rows = await _pool.fetch("SELECT key, value FROM settings")
    return {r["key"]: r["value"] for r in rows if r["key"] in OPERATIONAL_KEYS}


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
