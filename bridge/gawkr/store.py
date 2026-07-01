from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import asyncpg
from pgvector.asyncpg import register_vector

from .embeddings import EMBED_DIM

log = logging.getLogger("gawkr.store")

SCHEMA = f"""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS events (
  event_id     TEXT PRIMARY KEY,
  ts           BIGINT,
  camera_id    TEXT,
  camera       TEXT,
  smart_types  TEXT[],
  summary      TEXT,
  notable      BOOLEAN DEFAULT FALSE,
  snapshot     TEXT,
  record       JSONB,
  embedding    VECTOR({EMBED_DIM})
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_camera ON events (camera);
CREATE INDEX IF NOT EXISTS idx_events_embedding
  ON events USING hnsw (embedding vector_cosine_ops);
CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value      JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS doctor_status (
  name       TEXT PRIMARY KEY,
  ok         BOOLEAN NOT NULL,
  detail     TEXT,
  checked_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


async def _init_conn(conn):
    await register_vector(conn)
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads,
                              schema="pg_catalog")


class Store:
    def __init__(self, cfg, embedder):
        self.cfg = cfg
        self.embedder = embedder
        self.media_dir = os.path.join(cfg.data_dir, "snapshots")
        os.makedirs(self.media_dir, exist_ok=True)
        self._pool: asyncpg.Pool | None = None

    @property
    def pool(self) -> asyncpg.Pool:
        assert self._pool is not None
        return self._pool

    async def open(self) -> None:
        last = None
        for _ in range(10):
            try:
                bootstrap = await asyncpg.connect(self.cfg.database_url)
                await bootstrap.execute("CREATE EXTENSION IF NOT EXISTS vector;")
                await bootstrap.close()
                break
            except Exception as e:  # noqa: BLE001
                last = e
                await asyncio.sleep(2)
        else:
            raise SystemExit(f"could not reach database: {last}")

        self._pool = await asyncpg.create_pool(
            self.cfg.database_url, min_size=1, max_size=4, init=_init_conn)
        async with self._pool.acquire() as conn:
            await conn.execute(SCHEMA)
        log.info("store ready (postgres + pgvector)")

    async def save(self, det, record: dict) -> dict:
        assert self._pool is not None
        new_snap: str | None = None
        if det.snapshot:
            path = os.path.join(self.media_dir, f"{det.event_id}.jpg")
            with open(path, "wb") as f:
                f.write(det.snapshot)
            new_snap = os.path.basename(path)

        async with self._pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT record, snapshot FROM events WHERE event_id = $1", det.event_id)
            base = dict(existing["record"]) if existing and existing["record"] else {}
            merged = {**base, **record}
            snap = new_snap if new_snap is not None else (existing["snapshot"] if existing else None)
            merged["snapshot_path"] = snap or ""

            desc = merged.get("description") or {}
            summary = desc.get("summary", "")
            notable = bool(desc.get("notable", False))
            embedding = await asyncio.to_thread(self.embedder.embed, _embed_text(det, merged))

            await conn.execute(
                """
                INSERT INTO events
                  (event_id, ts, camera_id, camera, smart_types, summary, notable, snapshot, record, embedding)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                ON CONFLICT (event_id) DO UPDATE SET
                  summary = EXCLUDED.summary, notable = EXCLUDED.notable,
                  snapshot = EXCLUDED.snapshot, record = EXCLUDED.record,
                  embedding = EXCLUDED.embedding
                """,
                det.event_id, int(time.time()), det.camera_id, det.camera_name,
                det.smart_types, summary, notable, snap, merged, embedding,
            )
        return merged

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()


def _embed_text(det, record: dict) -> str:
    desc = record.get("description") or {}
    plate = (record.get("plate") or {}).get("plate")
    veh = record.get("vehicle") or {}
    vehicle = " ".join(str(veh.get(k)) for k in ("color", "make", "model", "body_type") if veh.get(k))
    transcript = (record.get("transcription") or {}).get("text")
    parts = [
        det.camera_name,
        " ".join(det.smart_types),
        desc.get("summary", ""),
        " ".join(desc.get("objects", []) or []),
        " ".join(desc.get("attributes", []) or []),
    ]
    if plate:
        parts.append(f"plate {plate}")
    if vehicle:
        parts.append(f"vehicle {vehicle}")
    if transcript:
        parts.append(f"said: {transcript}")
    return " | ".join(p for p in parts if p)
