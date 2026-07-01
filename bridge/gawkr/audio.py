from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("gawkr.audio")


async def extract_wav(media: bytes) -> bytes | None:
    """Pipe an MP4 clip through ffmpeg -> 16 kHz mono WAV (what whisper wants).

    Returns None if the clip has no audio track or ffmpeg fails.
    """
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-loglevel", "error",
        "-i", "pipe:0",
        "-vn", "-ar", "16000", "-ac", "1", "-f", "wav", "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(input=media)
    if proc.returncode != 0:
        log.warning("ffmpeg failed (no audio track?): %s", err.decode("utf-8", "ignore")[:300])
        return None
    return out or None
