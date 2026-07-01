from __future__ import annotations

import asyncio
import logging
import signal

from .config import Config
from .embeddings import Embedder
from .pipeline import (DescriptionProcessor, Pipeline, PlateProcessor,
                       TranscriptionProcessor, VehicleProcessor)
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

    vision = VisionClient(cfg)
    processors = [DescriptionProcessor(vision), PlateProcessor(vision)]
    closers = [vision.close]

    if cfg.identify_vehicles:
        processors.append(VehicleProcessor(vision))

    whisper = None
    if cfg.whisper_url:
        from .whisper import WhisperClient
        whisper = WhisperClient(cfg)
        processors.append(TranscriptionProcessor(whisper, cfg.transcribe_cameras))
        closers.append(whisper.close)
        scope = ", ".join(cfg.transcribe_cameras) if cfg.transcribe_cameras else "all cameras"
        log.info("transcription enabled via %s (%s)", cfg.whisper_url, scope)

    alerter = notifier = None
    if cfg.gotify_url and cfg.gotify_token:
        from .alerts import AlertEngine
        from .gotify import GotifyNotifier
        alerter = AlertEngine(cfg)
        notifier = GotifyNotifier(cfg)
        closers.append(notifier.close)
        log.info("gotify alerts enabled (weapon=%s, threat>=%s)",
                 cfg.alert_on_weapon, cfg.alert_threat_level)

    pipeline = Pipeline(processors, store, alerter, notifier)
    source = ProtectSource(cfg, pipeline.run)
    await source.connect()
    source.start()
    log.info("gawkr running; watching for %s", ", ".join(cfg.smart_types))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    log.info("shutting down")
    await source.close()
    for close in closers:
        await close()
    await store.close()


if __name__ == "__main__":
    asyncio.run(main())
