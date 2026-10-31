import asyncio
import json
import logging
import random
from pathlib import Path

from telethon import Button, TelegramClient, events, types
from telethon.errors import (
    AuthKeyDuplicatedError,
    AuthKeyError,
    AuthKeyUnregisteredError,
    FloodWaitError,
    SessionRevokedError,
    SlowModeWaitError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)
from telethon.sessions import StringSession

import db
from settings import cfg

log = logging.getLogger(__name__)

# ================= сессии =================

_clients: dict[int, TelegramClient] = {}
_locks: dict[int, asyncio.Lock] = {}


def _lock(account_id: int) -> asyncio.Lock:
    if account_id not in _locks:
        _locks[account_id] = asyncio.Lock()
    return _locks[account_id]


async def get_client(account) -> TelegramClient:
    async with _lock(account["id"]):
        client = _clients.get(account["id"])
        if client is not None:
            if client.is_connected():
                return client
            try:
                await client.disconnect()
            except Exception:
                pass
            _clients.pop(account["id"], None)

        if not account["session"]:
            raise RuntimeError("нет сессии")

        client = TelegramClient(StringSession(account["session"]), cfg.api_id, cfg.api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise RuntimeError("сессия отозвана")
        client.add_event_handler(secretary_handler, events.NewMessage(outgoing=True))
        _clients[account["id"]] = client
        log.info("account %s connected", account["id"])
        return client


async def drop_client(account_id: int) -> None:
    async with _lock(account_id):
        client = _clients.pop(account_id, None)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass


async def shutdown_clients() -> None:
    for account_id in list(_clients):
        await drop_client(account_id)


# ================= форматирование =================

_ENTITIES = {n: getattr(types, n) for n in dir(types) if n.startswith("MessageEntity")}


def dump_entities(entities) -> str:
    out = []
    for e in entities or []:
        d = {"_": type(e).__name__, "offset": e.offset, "length": e.length}
        for f in ("url", "user_id", "document_id", "language"):
            v = getattr(e, f, None)
            if v is not None:
                d[f] = v
        out.append(d)
    return json.dumps(out)


def load_entities(raw: str):
    res = []
    for d in json.loads(raw or "[]"):
        cls = _ENTITIES.get(d.get("_"))
        if not cls:
            continue
        try:
            res.append(cls(**{k: v for k, v in d.items() if k != "_"}))
        except TypeError:
            pass
    return res


def load_buttons(raw: str):
    out = []
    for row in json.loads(raw or "[]"):
        line = [Button.url(b["text"], b["url"]) for b in row if b.get("url")]
        if line:
            out.append(line)
    return out or None


def parse_buttons(text: str) -> str:
    rows = []
    for chunk in text.split(";"):
        if "|" not in chunk:
            continue
        label, url = chunk.split("|", 1)
        label, url = label.strip(), url.strip()
        if url.startswith("http"):
            rows.append([{"text": label[:64], "url": url}])
    return json.dumps(rows)


def has_url(text: str | None) -> bool:
    return bool(text) and ("http://" in text or "https://" in text or "t.me/" in text)


def is_document(mime: str | None) -> bool:
    return not (mime or "").startswith(("image/", "video/"))


# ================= отправка =================

_tasks: dict[int, asyncio.Task] = {}
_session_fails: dict[int, int] = {}

SESSION_ERRORS = (
    AuthKeyUnregisteredError, AuthKeyDuplicatedError, SessionRevokedError,
    UserDeactivatedError, UserDeactivatedBanError, AuthKeyError,
)
MAX_FAILS = 3


def is_running(cid: int) -> bool:
    task = _tasks.get(cid)
    return bool(task and not task.done())


def stop(cid: int) -> None:
    task = _tasks.pop(cid, None)
    if task:
        task.cancel()


async def start(cid: int) -> None:
    if is_running(cid):
        return
    await db.set_campaign(cid, status="running")
    _tasks[cid] = asyncio.create_task(_loop(cid))


async def _pause(lo: int, hi: int) -> None:
    await asyncio.sleep(random.randint(min(lo, hi), max(lo, hi)))


def _preview(msg, campaign) -> bool:
    return bool(msg["preview"] if msg["preview"] is not None else campaign["link_preview"])


async def _local_file(client, msg) -> str | None:
    path = msg["file_path"]
    if path and Path(path).exists():
        return path
    if not msg["src_msg"]:
        return None
    try:
        source = await client.get_messages(msg["src_chat"], ids=msg["src_msg"])
    except Exception:
        return None
    if not source or not source.media:
        return None
    dest = Path(cfg.media_dir) / "recover"
    dest.mkdir(parents=True, exist_ok=True)
    got = await client.download_media(source, file=str(dest / str(msg["id"])))
    if got:
        await db.set_message(msg["id"], file_path=got)
    return got


async def _send(client, target, msg, campaign) -> None:
    entities = load_entities(msg["entities"])
    buttons = load_buttons(msg["buttons"])
    topics = json.loads(target["topic_ids"] or "[]") or [None]

    via = None
    if campaign["via_bot"] and (msg["file_path"] or msg["media"]):
        try:
            via = await client.get_input_entity(campaign["via_bot"])
        except Exception:
            via = None

    for topic in topics:
        for attempt in (1, 2):
            try:
                if msg["mode"] == "forward" and msg["src_msg"]:
                    await client.forward_messages(
                        target["chat_id"], msg["src_msg"], msg["src_chat"], reply_to=topic,
                    )
                elif msg["file_path"] or msg["media"]:
                    path = await _local_file(client, msg)
                    if path is None:
                        raise RuntimeError("файл недоступен")
                    kwargs = {
                        "caption": msg["text"] or None,
                        "formatting_entities": entities or None,
                        "reply_to": topic,
                    }
                    if buttons:
                        kwargs["buttons"] = buttons
                    if msg["is_document"]:
                        kwargs["force_document"] = True
                    if via is not None:
                        kwargs["via_bot_id"] = via
                    try:
                        await client.send_file(target["chat_id"], path, **kwargs)
                    except TypeError:
                        kwargs.pop("via_bot_id", None)
                        await client.send_file(target["chat_id"], path, **kwargs)
                else:
                    kwargs = {
                        "formatting_entities": entities or None,
                        "reply_to": topic,
                        "link_preview": _preview(msg, campaign),
                    }
                    if buttons:
                        kwargs["buttons"] = buttons
                    if campaign["via_bot"]:
                        kwargs["via_bot"] = campaign["via_bot"]
                    await client.send_message(target["chat_id"], msg["text"], **kwargs)
                break
            except (FloodWaitError, SlowModeWaitError) as e:
                await asyncio.sleep(e.seconds + 2)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if attempt == 2:
                    log.warning("send %s: %s", target["chat_id"], e)
                await asyncio.sleep(2)


async def _guarded(account, target, msg, campaign) -> None:
    for attempt in (1, 2):
        try:
            client = await get_client(account)
            return await _send(client, target, msg, campaign)
        except SESSION_ERRORS as e:
            await drop_client(account["id"])
            if attempt == 2:
                raise
            await asyncio.sleep(3)


async def _loop(cid: int) -> None:
    try:
        while True:
            campaign = await db.get_campaign(cid)
            if not campaign or campaign["status"] != "running":
                return
            account = await db.get_account(campaign["account_id"])
            if not account:
                await db.set_campaign(cid, status="stopped")
                return

            targets = await db.campaign_targets(cid)
            messages = await db.campaign_messages(cid)
            if not targets or not messages:
                await asyncio.sleep(30)
                continue

            for target in targets:
                for msg in messages:
                    try:
                        await _guarded(account, target, msg, campaign)
                        _session_fails[account["id"]] = 0
                        await db.set_account_status(account["id"], "ok")
                    except SESSION_ERRORS as e:
                        fails = _session_fails.get(account["id"], 0) + 1
                        _session_fails[account["id"]] = fails
                        log.error("session dead %s: %s", account["id"], e)
                        if fails >= MAX_FAILS:
                            await db.set_account_status(account["id"], "dead")
                            await db.set_campaign(cid, status="stopped")
                            return
                        await asyncio.sleep(min(60, 5 * fails))
                    except asyncio.CancelledError:
                        raise
                    await _pause(campaign["delay_min"], campaign["delay_max"])

            await _pause(campaign["cycle_min"], campaign["cycle_max"])
    except asyncio.CancelledError:
        pass
    finally:
        _tasks.pop(cid, None)
        await db.set_campaign(cid, status="stopped")


# ================= секретарь =================

import re

SECRETARY = re.compile(r"^\.старт\s+(.+)$", re.I)


async def secretary_handler(event) -> None:
    match = SECRETARY.match((event.raw_text or "").strip())
    if not match:
        return
    title = match.group(1).strip()
    try:
        me = await event.client.get_me()
        user = await db.user_by_account(me.id)
        if not user:
            return
        campaign = await db.by_title(user["tg_id"], title)
        if not campaign:
            await event.reply(f"⚠️ Шаблон «{title}» не найден.")
            return

        reply = event.message.reply_to
        topic = None
        if reply is not None:
            topic = getattr(reply, "reply_to_top_id", None) or getattr(reply, "reply_to_msg_id", None)

        chat = await event.get_chat()
        chat_id = event.chat_id
        tid = await db.add_target(
            campaign["id"], chat_id, getattr(chat, "access_hash", None),
            getattr(chat, "title", None) or str(chat_id),
        )
        if topic:
            row = await db.fetchone("SELECT topic_ids FROM targets WHERE id=?", (tid,))
            current = json.loads(row["topic_ids"] or "[]")
            if topic not in current:
                await db.set_topics(tid, (current + [topic])[:2])

        suffix = f" (тема {topic})" if topic else ""
        await event.reply(f"✅ Чат добавлен в «{title}»{suffix}")
    except Exception as e:
        log.warning("secretary: %s", e)