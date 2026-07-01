from __future__ import annotations

import asyncio
import logging
import signal

from . import doctor
from .config import Config
from .embeddings import Embedder
from .pipeline import (DescriptionProcessor, Pipeline, PlateProcessor,
                       TranscriptionProcessor, VehicleProcessor)
from .settings import LiveSettings
from .source import ProtectSource
from .store import Store
from .vision import VisionClient

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("gawkr")

CONNECT_BACKOFF_CAP = 60  # seconds


async def main() -> None:
    cfg = Config()
    cfg.validate()

    embedder = Embedder(cfg.embed_model)
    store = Store(cfg, embedder)
    await store.open()

    settings = LiveSettings(cfg, store.pool)
    await settings.start()

    vision = VisionClient(cfg)
    processors = [DescriptionProcessor(vision, settings), PlateProcessor(vision),
                  VehicleProcessor(vision, settings)]
    closers = [vision.close]

    whisper = None
    if cfg.whisper_url:
        from .whisper import WhisperClient
        whisper = WhisperClient(cfg)
        processors.append(TranscriptionProcessor(whisper, settings))
        closers.append(whisper.close)
        log.info("transcription enabled via %s", cfg.whisper_url)

    alerter = notifier = None
    if cfg.gotify_url and cfg.gotify_token:
        from .alerts import AlertEngine
        from .gotify import GotifyNotifier
        alerter = AlertEngine(settings)
        notifier = GotifyNotifier(cfg)
        closers.append(notifier.close)
        log.info("gotify alerts enabled")

    pipeline = Pipeline(processors, store, alerter, notifier)
    source = ProtectSource(settings, pipeline.run)

    # Install signal handling before the connect-retry loop below so a
    # SIGTERM during a slow/failing Protect connect still triggers a clean
    # shutdown (via stop) instead of relying on the container being killed.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    await doctor.write_status(store.pool, doctor.check_configured(cfg))
    doctor_task = asyncio.create_task(doctor.recheck_loop(store.pool, cfg, vision))

    # Bad Protect creds/network must not crash-loop the container -- that
    # hides the failure from the doctor page. Retry with capped exponential
    # backoff instead, writing status each attempt, until either it connects
    # or shutdown is requested.
    backoff = 1
    while not stop.is_set():
        try:
            await source.connect()
            await doctor.write_status(store.pool, {"protect": doctor.CheckResult(True)})
            break
        except Exception as e:
            log.warning("Protect connect failed, retrying in %ss: %s", backoff, e)
            await doctor.write_status(store.pool, {"protect": doctor.CheckResult(False, str(e))})
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, CONNECT_BACKOFF_CAP)

    if not stop.is_set():
        source.start()
        log.info("gawkr running; watching for %s", ", ".join(settings.smart_types))
        await stop.wait()

    log.info("shutting down")
    await source.close()
    for close in closers:
        await close()
    doctor_task.cancel()
    await asyncio.gather(doctor_task, return_exceptions=True)
    await settings.stop()
    await store.close()


if __name__ == "__main__":
    asyncio.run(main())
