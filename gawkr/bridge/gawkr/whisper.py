from __future__ import annotations

import logging

import httpx

log = logging.getLogger("gawkr.whisper")


class WhisperClient:
    """Talks to a whisper.cpp `whisper-server` /inference endpoint."""

    def __init__(self, cfg):
        self.url = cfg.whisper_url.rstrip("/") + "/inference"
        self._client = httpx.AsyncClient(timeout=cfg.whisper_timeout)

    async def transcribe(self, wav: bytes) -> str:
        files = {"file": ("audio.wav", wav, "audio/wav")}
        data = {"response_format": "json", "temperature": "0.0"}
        r = await self._client.post(self.url, files=files, data=data)
        r.raise_for_status()
        return _extract_text(r)

    async def close(self) -> None:
        await self._client.aclose()


def _extract_text(r: httpx.Response) -> str:
    try:
        j = r.json()
        if isinstance(j, dict):
            return (j.get("text") or "").strip()
    except Exception:
        pass
    return r.text.strip()
