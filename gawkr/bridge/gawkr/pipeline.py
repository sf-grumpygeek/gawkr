from __future__ import annotations

import asyncio
import logging
from typing import Protocol, runtime_checkable

from .audio import extract_wav
from .source import Detection

log = logging.getLogger("gawkr.pipeline")


def _cam_ok(det: Detection, allow: list[str]) -> bool:
    return (not allow) or det.camera_name in allow or det.camera_id in allow


@runtime_checkable
class Processor(Protocol):
    name: str

    def handles(self, det: Detection) -> bool:
        ...

    async def process(self, det: Detection) -> dict:
        ...


class DescriptionProcessor:
    """Description + single-frame safety assessment (one vision call)."""
    name = "description"

    def __init__(self, vision):
        self.vision = vision

    def handles(self, det: Detection) -> bool:
        return det.snapshot is not None

    async def process(self, det: Detection) -> dict:
        return await self.vision.describe(det.snapshot, det.smart_types, det.camera_name)


class PlateProcessor:
    name = "plate"

    def __init__(self, vision):
        self.vision = vision

    def handles(self, det: Detection) -> bool:
        return det.snapshot is not None and "licensePlate" in det.smart_types

    async def process(self, det: Detection) -> dict:
        return await self.vision.read_plate(det.snapshot)


class VehicleProcessor:
    name = "vehicle"

    def __init__(self, vision, settings):
        self.vision = vision
        self.settings = settings

    def handles(self, det: Detection) -> bool:
        return (det.snapshot is not None and "vehicle" in det.smart_types
                and self.settings.identify_vehicles)

    async def process(self, det: Detection) -> dict:
        return await self.vision.read_vehicle(det.snapshot)


class TranscriptionProcessor:
    """Speech-to-text on the event clip's audio (event end)."""
    name = "transcription"

    def __init__(self, whisper, settings):
        self.whisper = whisper
        self.settings = settings

    def handles(self, det: Detection) -> bool:
        return det.clip is not None and _cam_ok(det, self.settings.transcribe_cameras)

    async def process(self, det: Detection) -> dict:
        wav = await extract_wav(det.clip)
        if not wav:
            return {}
        text = await self.whisper.transcribe(wav)
        return {"text": text} if text else {}


class Pipeline:
    def __init__(self, processors, store, alerter=None, notifier=None):
        self.processors = processors
        self.store = store
        self.alerter = alerter
        self.notifier = notifier

    async def run(self, det: Detection) -> dict:
        active = [p for p in self.processors if p.handles(det)]
        results = await asyncio.gather(*(self._safe(p, det) for p in active))

        record: dict = {
            "event_id": det.event_id,
            "camera_id": det.camera_id,
            "camera": det.camera_name,
            "smart_types": det.smart_types,
        }
        for name, data in results:
            if data:
                record[name] = data

        merged = await self.store.save(det, record)

        if det.phase == "started":
            desc = merged.get("description") or {}
            plate = (merged.get("plate") or {}).get("plate")
            tail = f" [plate {plate}]" if plate else ""
            lvl = desc.get("threat_level", "none")
            if lvl != "none":
                tail += f" [{lvl}]"
            log.info("[%s] %s -> %s%s", det.camera_name, ",".join(det.smart_types),
                     desc.get("summary", "") or "(no summary)", tail)
        else:
            txt = (merged.get("transcription") or {}).get("text", "")
            if txt:
                log.info("[%s] transcript -> %s", det.camera_name, txt[:120])

        await self._maybe_alert(merged)
        return merged

    async def _maybe_alert(self, record: dict) -> None:
        if not (self.alerter and self.notifier):
            return
        alert = self.alerter.evaluate(record)
        if alert:
            log.info("ALERT (p%d) %s", alert.priority, alert.title)
            await self.notifier.send(alert, record)

    async def _safe(self, p: Processor, det: Detection):
        try:
            return p.name, await p.process(det)
        except Exception:
            log.exception("processor '%s' failed", p.name)
            return p.name, None
