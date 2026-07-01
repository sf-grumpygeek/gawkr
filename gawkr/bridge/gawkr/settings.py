from __future__ import annotations

import asyncio
import logging

import asyncpg

from .alerts import _LEVELS

log = logging.getLogger("gawkr.settings")

CHANNEL = "gawkr_settings"
POLL_INTERVAL = 45
_RECONNECT_DELAY = 5


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


# The only keys the DB may override. Enforced on write (web PUT) and on
# reload (here) -- secrets and infra endpoints must never appear in this dict.
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


class LiveSettings:
    """Proxies attribute reads to a DB override (if set) else the env Config.

    Drop-in replacement for `cfg` anywhere code reads `cfg.<attr>` at call
    time rather than capturing it at construction -- reads stay live across
    reloads with no other change at the call site.
    """

    def __init__(self, cfg, pool: asyncpg.Pool):
        self.cfg = cfg
        self._pool = pool
        self._overrides: dict = {}
        self._tasks: list[asyncio.Task] = []

    def __getattr__(self, name):
        if name in OPERATIONAL_KEYS and name in self._overrides:
            return self._overrides[name]
        return getattr(self.cfg, name)

    async def reload(self) -> None:
        rows = await self._pool.fetch("SELECT key, value FROM settings")
        new: dict = {}
        for r in rows:
            key, value = r["key"], r["value"]
            validator = OPERATIONAL_KEYS.get(key)
            if validator is None:
                continue
            try:
                new[key] = validator(value)
            except (ValueError, TypeError):
                log.warning("settings: bad value for %r, keeping prior", key)
                if key in self._overrides:
                    new[key] = self._overrides[key]
        self._overrides = new

    async def start(self) -> None:
        await self.reload()
        self._tasks = [
            asyncio.create_task(self._poll_loop()),
            asyncio.create_task(self._listen_loop()),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                await self.reload()
            except Exception:
                log.exception("settings: poll reload failed")

    async def _listen_loop(self) -> None:
        while True:
            conn: asyncpg.Connection | None = None
            try:
                conn = await asyncpg.connect(self.cfg.database_url)
                await conn.add_listener(CHANNEL, self._on_notify)
                # Reload on every (re)connect -- catches changes made while
                # this listener was down (missed NOTIFYs are otherwise lost).
                await self.reload()
                log.info("settings: listening for changes on %r", CHANNEL)
                while not conn.is_closed():
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("settings: listen connection error, retrying")
            finally:
                if conn is not None:
                    await conn.close()
            await asyncio.sleep(_RECONNECT_DELAY)

    def _on_notify(self, connection, pid, channel, payload) -> None:
        asyncio.create_task(self.reload())
