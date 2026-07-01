from __future__ import annotations

import asyncio
import logging
import signal

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


async def main() -> None:
    cfg = Config()
    cfg.validate()

    embedder = Embedder(cfg.embed_model)
    store = Store(cfg, embedder)
    await store.open()

    settings = LiveSettings(cfg, store.pool)
    await settings.start()

    vision = VisionClient(cfg)
    processors = [DescriptionProcessor(vision), PlateProcessor(vision),
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
    await source.connect()
    source.start()
    log.info("gawkr running; watching for %s", ", ".join(settings.smart_types))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    log.info("shutting down")
    await source.close()
    for close in closers:
        await close()
    await settings.stop()
    await store.close()


if __name__ == "__main__":
    asyncio.run(main())
