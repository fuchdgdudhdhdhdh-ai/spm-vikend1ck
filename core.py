import asyncio
import json
import logging
import random
import re

from telethon import Button, TelegramClient, events, types
from telethon.errors import (
    AuthKeyDuplicatedError,
    AuthKeyError,
    AuthKeyUnregisteredError,
    FileReferenceExpiredError,
    FloodWaitError,
    RPCError,
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


# ================= форматирование и медиа =================

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


def media_input(message):
    """InputMedia со свежим file_reference из сохранённого сообщения."""
    media = getattr(message, "media", None)
    if isinstance(media, types.MessageMediaPhoto) and isinstance(media.photo, types.Photo):
        photo = media.photo
        return types.InputMediaPhoto(
            types.InputPhoto(photo.id, photo.access_hash, photo.file_reference)
        )
    if isinstance(media, types.MessageMediaDocument) and isinstance(media.document, types.Document):
        doc = media.document
        return types.InputMediaDocument(
            types.InputDocument(doc.id, doc.access_hash, doc.file_reference)
        )
    return None


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


async def store_media(client, source_chat: int, source_msg: int) -> tuple[int, int] | None:
    """Пересылает сообщение в Избранное и возвращает (chat_id, msg_id)."""
    try:
        me = await client.get_me()
        forwarded = await client.forward_messages(me.id, source_msg, source_chat)
        return forwarded.chat_id or me.id, forwarded.id
    except Exception as e:
        log.warning("store_media: %s", e)
        return None


async def drop_media(client, chat_id: int | None, msg_id: int | None) -> None:
    if not chat_id or not msg_id:
        return
    try:
        await client.delete_messages(chat_id, [msg_id])
    except Exception:
        pass


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


async def _saved(client, msg):
    if not msg["saved_msg"]:
        return None
    try:
        return await client.get_messages(msg["saved_chat"], ids=msg["saved_msg"])
    except Exception:
        return None


async def _send(client, target, msg, campaign) -> None:
    entities = load_entities(msg["entities"])
    buttons = load_buttons(msg["buttons"])
    topics = json.loads(target["topic_ids"] or "[]") or [None]

    via = None
    if campaign["via_bot"] and msg["saved_msg"]:
        try:
            via = await client.get_input_entity(campaign["via_bot"])
        except Exception:
            via = None

    for topic in topics:
        for attempt in (1, 2):
            try:
                if msg["mode"] == "forward" and msg["saved_msg"]:
                    await client.forward_messages(
                        target["chat_id"], msg["saved_msg"], msg["saved_chat"], reply_to=topic,
                    )
                    break

                source = await _saved(client, msg)
                media = media_input(source) if source else None

                if media is not None:
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
                        await client.send_file(target["chat_id"], media, **kwargs)
                    except TypeError:
                        kwargs.pop("via_bot_id", None)
                        await client.send_file(target["chat_id"], media, **kwargs)
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
                if isinstance(e, (FileReferenceExpiredError,)) or (
                    isinstance(e, RPCError) and "FILE_REFERENCE" in str(e).upper()
                ) and attempt == 1:
                    await asyncio.sleep(1)
                    continue
                if attempt == 2:
                    log.warning("send %s: %s", target["chat_id"], e)
                await asyncio.sleep(2)


async def _guarded(account, target, msg, campaign) -> None:
    for attempt in (1, 2):
        try:
            client = await get_client(account)
            return await _send(client, target, msg, campaign)
        except SESSION_ERRORS:
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
            row = await db.fetchone("SELECT topic_ids FROM targets WHERE id=$1", tid)
            current = json.loads(row["topic_ids"] or "[]")
            if topic not in current:
                await db.set_topics(tid, (current + [topic])[:2])

        suffix = f" (тема {topic})" if topic else ""
        await event.reply(f"✅ Чат добавлен в «{title}»{suffix}")
    except Exception as e:
        log.warning("secretary: %s", e)
