import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger("settings")


def _ids(raw: str) -> set[int]:
    return {int(x) for x in raw.replace(" ", "").split(",") if x.strip().lstrip("-").isdigit()}


def _dsn() -> str:
    raw = os.getenv("DATABASE_URL", "").strip()
    if raw.startswith("postgres://"):
        raw = "postgresql://" + raw[len("postgres://"):]
    return raw.split("?")[0]


@dataclass
class Settings:
    bot_token: str = os.getenv("BOT_TOKEN", "")
    api_id: int = int(os.getenv("TG_API_ID", "0"))
    api_hash: str = os.getenv("TG_API_HASH", "")
    database_url: str = field(default_factory=_dsn)
    owners: set[int] = field(default_factory=lambda: _ids(os.getenv("OWNER_IDS", "")))
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8080"))
    seller: str = os.getenv("SELLER_USERNAME", "vikend1ck")


cfg = Settings()
