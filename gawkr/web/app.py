from __future__ import annotations

import asyncio
import json
import os
import re

import asyncpg
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastembed import TextEmbedding
from pgvector.asyncpg import register_vector

DSN = os.environ["DATABASE_URL"]
DATA_DIR = os.environ.get("DATA_DIR", "/data")
SNAP_DIR = os.path.join(DATA_DIR, "snapshots")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "BAAI/bge-small-en-v1.5")
ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

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


@app.get("/api/snapshot/{eid}")
async def snapshot(eid: str) -> FileResponse:
    if not ID_RE.match(eid):
        raise HTTPException(400, "bad id")
    path = os.path.join(SNAP_DIR, f"{eid}.jpg")
    if not os.path.isfile(path):
        raise HTTPException(404, "no snapshot")
    return FileResponse(path, media_type="image/jpeg")


app.mount("/", StaticFiles(directory="static", html=True), name="static")
