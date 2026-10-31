import asyncio
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web

import db
from core import shutdown_clients
from handlers import router
from settings import cfg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")


async def health(_: web.Request) -> web.Response:
    return web.Response(text="ok")


async def serve() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/healthz", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, cfg.host, cfg.port).start()
    log.info("health server on %s:%s", cfg.host, cfg.port)
    return runner


async def main() -> None:
    if not cfg.bot_token or not cfg.api_id or not cfg.api_hash:
        raise SystemExit("Заполните BOT_TOKEN, TG_API_ID, TG_API_HASH")

    Path(cfg.media_dir).mkdir(parents=True, exist_ok=True)
    await db.init()

    runner = await serve()

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await shutdown_clients()
        await runner.cleanup()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())