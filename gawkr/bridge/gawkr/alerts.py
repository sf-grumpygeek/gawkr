from __future__ import annotations

import logging
import time
from dataclasses import dataclass

log = logging.getLogger("gawkr.alerts")

_LEVELS = {"none": 0, "low": 1, "medium": 2, "high": 3}


@dataclass
class Alert:
    title: str
    message: str
    priority: int


class AlertEngine:
    """Turns event signals into (optional) Gotify alerts.

    Rules read the safety fields the vision model already produced, so there's no
    extra inference per rule. Order = weapon > threat level > keyword.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self._last: dict[str, float] = {}

    def evaluate(self, record: dict) -> Alert | None:
        camera = record.get("camera", "camera")
        if self.cfg.alert_cameras and camera not in self.cfg.alert_cameras:
            return None

        desc = record.get("description") or {}

        if self.cfg.alert_on_weapon and desc.get("weapon"):
            return self._fire(camera, "weapon",
                              f"\u26a0 Weapon seen \u2014 {camera}",
                              desc.get("weapon_detail") or "possible weapon", 9)

        level = desc.get("threat_level", "none")
        if _LEVELS.get(level, 0) >= _LEVELS.get(self.cfg.alert_threat_level, 99):
            pri = 8 if level == "high" else 6
            detail = desc.get("concerning_detail") or desc.get("summary", "")
            return self._fire(camera, f"threat:{level}",
                              f"{camera}: {level} concern", detail, pri)

        if self.cfg.alert_keywords:
            veh = record.get("vehicle") or {}
            vtext = " ".join(str(veh.get(k)) for k in ("make", "model", "color", "body_type")
                             if veh.get(k))
            hay = (desc.get("summary", "") + " "
                   + " ".join(desc.get("attributes", []) or []) + " " + vtext).lower()
            for kw in self.cfg.alert_keywords:
                if kw.lower() in hay:
                    return self._fire(camera, f"kw:{kw}", f"{camera}: {kw}",
                                      desc.get("summary", ""), self.cfg.gotify_priority)
        return None

    def _fire(self, camera: str, rule: str, title: str, message: str, pri: int) -> Alert | None:
        key = f"{camera}:{rule}"
        now = time.time()
        if now - self._last.get(key, 0) < self.cfg.alert_cooldown:
            return None
        self._last[key] = now
        return Alert(title=title, message=message, priority=pri)
