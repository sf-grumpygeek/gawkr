from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

log = logging.getLogger("gawkr.doctor")

RECHECK_INTERVAL = 300  # seconds; vision self-test is one inference call, keep this cheap


@dataclass
class CheckResult:
    ok: bool
    detail: str | None = None


async def check_vision(vision) -> CheckResult:
    try:
        ok = await vision.self_test()
    except Exception as e:
        return CheckResult(False, f"vision endpoint unreachable: {e}")
    if not ok:
        return CheckResult(False, "endpoint reachable but didn't correctly describe a "
                                   "test image -- you probably forgot the mmproj (a "
                                   "text-only model silently ignores images)")
    return CheckResult(True)


def check_configured(cfg) -> dict[str, CheckResult]:
    whisper_on = bool(cfg.whisper_url)
    gotify_on = bool(cfg.gotify_url and cfg.gotify_token)
    return {
        "whisper": CheckResult(whisper_on, None if whisper_on else "not configured (optional)"),
        "gotify": CheckResult(gotify_on, None if gotify_on else "not configured (optional)"),
    }


async def write_status(pool, results: dict[str, CheckResult]) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            for name, result in results.items():
                await conn.execute(
                    """
                    INSERT INTO doctor_status (name, ok, detail, checked_at)
                    VALUES ($1, $2, $3, now())
                    ON CONFLICT (name) DO UPDATE SET
                      ok = EXCLUDED.ok, detail = EXCLUDED.detail, checked_at = EXCLUDED.checked_at
                    """, name, result.ok, result.detail)


async def recheck_loop(pool, cfg, vision) -> None:
    """Keeps vision + whisper/gotify-configured status current for the web
    doctor page without a bridge restart. Protect's own status is written by
    the connect-retry loop in __main__ -- not duplicated here."""
    while True:
        try:
            results = check_configured(cfg)
            results["vision"] = await check_vision(vision)
            await write_status(pool, results)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("doctor: recheck failed")
        await asyncio.sleep(RECHECK_INTERVAL)
