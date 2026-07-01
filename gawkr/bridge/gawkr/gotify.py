from __future__ import annotations

import logging

import httpx

log = logging.getLogger("gawkr.gotify")


class GotifyNotifier:
    def __init__(self, cfg):
        self.cfg = cfg
        self.url = cfg.gotify_url.rstrip("/") + "/message"
        self._client = httpx.AsyncClient(timeout=10)

    async def send(self, alert, record: dict) -> None:
        message = alert.message or "(no detail)"
        extras: dict = {}
        # Attach the snapshot if we know where the web UI serves it.
        if self.cfg.web_base_url and record.get("event_id"):
            img = f"{self.cfg.web_base_url.rstrip('/')}/api/snapshot/{record['event_id']}"
            message = f"{message}\n\n![snapshot]({img})"
            extras = {
                "client::display": {"contentType": "text/markdown"},
                "client::notification": {"bigImageUrl": img},
            }
        payload = {"title": alert.title, "message": message, "priority": alert.priority}
        if extras:
            payload["extras"] = extras
        try:
            r = await self._client.post(self.url, params={"token": self.cfg.gotify_token},
                                        json=payload)
            r.raise_for_status()
        except Exception:
            log.exception("gotify send failed")

    async def close(self) -> None:
        await self._client.aclose()
