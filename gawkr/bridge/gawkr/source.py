from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from uiprotect import EventChange, ProtectApiClient, ProtectEvent

log = logging.getLogger("gawkr.source")

CLIP_LEAD = timedelta(seconds=2)


@dataclass
class Detection:
    event_id: str
    camera_id: str
    camera_name: str
    smart_types: list[str]
    phase: str                 # "started" or "ended"
    start: datetime | None
    end: datetime | None
    snapshot: bytes | None     # set on "started"
    clip: bytes | None         # set on "ended" when transcription is enabled


class ProtectSource:
    def __init__(self, cfg, on_detection: Callable[[Detection], Awaitable[None]]):
        self.cfg = cfg
        self.on_detection = on_detection
        self._client: ProtectApiClient | None = None
        self._unsub: Callable[[], None] | None = None
        self._seen: set[str] = set()

    async def connect(self) -> None:
        self._client = ProtectApiClient(
            host=self.cfg.protect_host,
            port=self.cfg.protect_port,
            username=self.cfg.protect_username,
            password=self.cfg.protect_password,
            verify_ssl=self.cfg.protect_verify_ssl,
            api_key=self.cfg.protect_api_key or None,
        )
        await self._client.update()
        await self._client.update_public()
        log.info("connected to UniFi Protect at %s:%s", self.cfg.protect_host, self.cfg.protect_port)

    def start(self) -> None:
        assert self._client is not None
        self._unsub = self._client.subscribe_events(self._on_event)
        log.info("subscribed to Protect detection events")

    def _camera_name(self, camera_id: str) -> str:
        try:
            cam = self._client.bootstrap.cameras.get(camera_id)  # type: ignore[union-attr]
            if cam is not None:
                return cam.name
        except Exception:
            pass
        return camera_id

    @staticmethod
    def _cam_in(name: str, camera_id: str, allow: list[str]) -> bool:
        return (not allow) or name in allow or camera_id in allow

    def _on_event(self, event: ProtectEvent, change: EventChange) -> None:
        if change is EventChange.STARTED:
            phase = "started"
        elif change is EventChange.ENDED and self.cfg.whisper_url:
            phase = "ended"        # event-end = clip available (for transcription)
        else:
            return

        types = _smart_types(event)
        if not types or not any(t in self.cfg.smart_types for t in types):
            return

        key = f"{event.id}:{phase}"
        if key in self._seen:
            return
        self._seen.add(key)
        if len(self._seen) > 8000:
            self._seen = set(list(self._seen)[-3000:])

        asyncio.create_task(self._handle(event, types, phase))

    async def _handle(self, event: ProtectEvent, types: list[str], phase: str) -> None:
        camera_id = event.device_id
        camera_name = self._camera_name(camera_id)
        start = getattr(event, "start", None)
        end = getattr(event, "end", None)
        snapshot: bytes | None = None
        clip: bytes | None = None

        if phase == "started":
            try:
                # VERIFY: snapshot-fetch method name across uiprotect versions.
                snapshot = await self._client.get_camera_snapshot(camera_id)  # type: ignore[union-attr]
            except Exception as e:
                log.warning("snapshot fetch failed for camera %s: %s", camera_id, e)
        else:  # ended -> transcription
            if not self._cam_in(camera_name, camera_id, self.cfg.transcribe_cameras):
                return
            if not (start and end):
                return
            try:
                # VERIFY: clip-fetch method name/signature across uiprotect versions.
                clip = await self._client.get_camera_video(  # type: ignore[union-attr]
                    camera_id, start - CLIP_LEAD, end)
            except Exception as e:
                log.warning("clip fetch failed for camera %s: %s", camera_id, e)
            if not clip:
                return

        det = Detection(event_id=event.id, camera_id=camera_id, camera_name=camera_name,
                        smart_types=types, phase=phase, start=start, end=end,
                        snapshot=snapshot, clip=clip)
        try:
            await self.on_detection(det)
        except Exception:
            log.exception("detection handler raised")

    async def close(self) -> None:
        if self._unsub:
            try:
                self._unsub()
            except Exception:
                pass
        if self._client:
            try:
                await self._client.close_session()
            except Exception:
                pass


def _smart_types(event: ProtectEvent) -> list[str]:
    for attr in ("smart_detect_types", "smartDetectTypes"):
        v = getattr(event, attr, None)
        if v:
            return [str(x) for x in v]
    raw = getattr(event, "raw", None)
    if isinstance(raw, dict):
        v = raw.get("smartDetectTypes")
        if v:
            return [str(x) for x in v]
    else:
        v = getattr(raw, "smart_detect_types", None)
        if v:
            return [str(x) for x in v]
    return []
