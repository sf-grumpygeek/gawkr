from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _list(v: str | None) -> list[str]:
    if not v:
        return []
    return [x.strip() for x in v.split(",") if x.strip()]


@dataclass
class Config:
    # --- UniFi Protect (LOCAL access user, never SSO/cloud) ---
    protect_host: str = field(default_factory=lambda: os.environ.get("UFP_ADDRESS", ""))
    protect_port: int = field(default_factory=lambda: int(os.environ.get("UFP_PORT", "443")))
    protect_username: str = field(default_factory=lambda: os.environ.get("UFP_USERNAME", ""))
    protect_password: str = field(default_factory=lambda: os.environ.get("UFP_PASSWORD", ""))
    protect_api_key: str = field(default_factory=lambda: os.environ.get("UFP_API_KEY", ""))
    protect_verify_ssl: bool = field(default_factory=lambda: _bool(os.environ.get("UFP_SSL_VERIFY"), False))

    # --- Vision model (external llama.cpp OpenAI-compatible endpoint) ---
    vision_url: str = field(default_factory=lambda: os.environ.get(
        "VISION_URL", "http://VISION_HOST:8081/v1/chat/completions"))
    vision_api_key: str = field(default_factory=lambda: os.environ.get("VISION_API_KEY", ""))
    vision_model: str = field(default_factory=lambda: os.environ.get("VISION_MODEL", "qwen2.5-vl"))
    vision_timeout: float = field(default_factory=lambda: float(os.environ.get("VISION_TIMEOUT", "60")))

    # --- Whisper transcription (optional; blank = off) ---
    whisper_url: str = field(default_factory=lambda: os.environ.get("WHISPER_URL", ""))
    whisper_timeout: float = field(default_factory=lambda: float(os.environ.get("WHISPER_TIMEOUT", "120")))
    transcribe_cameras: list[str] = field(default_factory=lambda: _list(os.environ.get("TRANSCRIBE_CAMERAS")))

    # --- Vehicle identification (make/model/color/type on vehicle events) ---
    identify_vehicles: bool = field(default_factory=lambda: _bool(os.environ.get("IDENTIFY_VEHICLES"), True))

    # --- Gotify alerts (blank url/token = alerting off) ---
    gotify_url: str = field(default_factory=lambda: os.environ.get("GOTIFY_URL", ""))
    gotify_token: str = field(default_factory=lambda: os.environ.get("GOTIFY_TOKEN", ""))
    gotify_priority: int = field(default_factory=lambda: int(os.environ.get("GOTIFY_PRIORITY", "5")))
    web_base_url: str = field(default_factory=lambda: os.environ.get("WEB_BASE_URL", ""))
    alert_on_weapon: bool = field(default_factory=lambda: _bool(os.environ.get("ALERT_ON_WEAPON"), True))
    alert_threat_level: str = field(default_factory=lambda: os.environ.get("ALERT_THREAT_LEVEL", "medium"))
    alert_keywords: list[str] = field(default_factory=lambda: _list(os.environ.get("ALERT_KEYWORDS")))
    alert_cameras: list[str] = field(default_factory=lambda: _list(os.environ.get("ALERT_CAMERAS")))
    alert_cooldown: float = field(default_factory=lambda: float(os.environ.get("ALERT_COOLDOWN", "120")))

    # --- Behaviour ---
    smart_types: list[str] = field(default_factory=lambda: _list(os.environ.get("SMART_TYPES"))
                                    or ["person", "vehicle", "animal", "licensePlate", "package", "face"])

    # --- Storage / search ---
    database_url: str = field(default_factory=lambda: os.environ.get("DATABASE_URL", ""))
    data_dir: str = field(default_factory=lambda: os.environ.get("DATA_DIR", "/data"))
    embed_model: str = field(default_factory=lambda: os.environ.get("EMBED_MODEL", "BAAI/bge-small-en-v1.5"))

    def validate(self) -> None:
        missing = [k for k in ("protect_host", "protect_username", "protect_password", "protect_api_key")
                   if not getattr(self, k)]
        if missing:
            raise SystemExit("Missing required config: " + ", ".join(missing))
        if not self.database_url:
            raise SystemExit("Missing DATABASE_URL")
        os.makedirs(os.path.join(self.data_dir, "snapshots"), exist_ok=True)
