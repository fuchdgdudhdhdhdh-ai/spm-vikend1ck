import os
from dataclasses import dataclass, field


def _ids(raw: str) -> set[int]:
    return {int(x) for x in raw.replace(" ", "").split(",") if x.strip().lstrip("-").isdigit()}


@dataclass
class Settings:
    bot_token: str = os.getenv("BOT_TOKEN", "")
    api_id: int = int(os.getenv("TG_API_ID", "0"))
    api_hash: str = os.getenv("TG_API_HASH", "")
    db_path: str = os.getenv("DB_PATH", "bot.db")
    media_dir: str = os.getenv("MEDIA_DIR", "media")
    owners: set[int] = field(default_factory=lambda: _ids(os.getenv("OWNER_IDS", "")))
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8080"))
    seller: str = os.getenv("SELLER_USERNAME", "vikend1ck")


cfg = Settings()