import asyncio
import io
import json
import logging
import random
import re

from telethon import Button, TelegramClient, events, types, utils
from telethon.errors import (
    AuthKeyDuplicatedError,
    AuthKeyError,
    AuthKeyUnregisteredError,
    BotInlineDisabledError,
    ChannelPrivateError,
    ChatAdminRequiredError,
    ChatSendMediaForbiddenError,
    ChatWriteForbiddenError,
    FileReferenceExpiredError,
    FloodWaitError,
    RPCError,
    SessionRevokedError,
    SlowModeWaitError,
    UserBannedInChannelError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetForumTopicsRequest

import db
from settings import cfg

log = logging.getLogger(__name__)


class SessionDead(Exception):
    """Сессия аккаунта недействительна или отсутствует."""


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
            raise SessionDead("нет сессии")

        client = TelegramClient(StringSession(account["session"]), cfg.api_id, cfg.api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise SessionDead("сессия отозвана")
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

_FROM_TG = {
    "bold": types.MessageEntityBold,
    "italic": types.MessageEntityItalic,
    "underline": types.MessageEntityUnderline,
    "strikethrough": types.MessageEntityStrike,
    "spoiler": types.MessageEntitySpoiler,
    "code": types.MessageEntityCode,
    "pre": types.MessageEntityPre,
    "blockquote": types.MessageEntityBlockquote,
    "text_link": types.MessageEntityTextUrl,
    "url": types.MessageEntityUrl,
    "email": types.MessageEntityEmail,
    "phone_number": types.MessageEntityPhone,
    "mention": types.MessageEntityMention,
    "hashtag": types.MessageEntityHashtag,
    "cashtag": types.MessageEntityCashtag,
    "bot_command": types.MessageEntityBotCommand,
    "text_mention": types.MessageEntityMentionName,
    "custom_emoji": types.MessageEntityCustomEmoji,
}


def entities_from_tg(entities) -> str:
    """aiogram MessageEntity -> json с классами Telethon."""
    out = []
    for e in entities or []:
        kind = getattr(e, "type", None)
        cls = _FROM_TG.get(kind)
        if not cls:
            continue
        d = {"_": cls.__name__, "offset": e.offset, "length": e.length}
        if kind == "text_link" and getattr(e, "url", None):
            d["url"] = e.url
        elif kind == "pre" and getattr(e, "language", None):
            d["language"] = e.language
        elif kind == "text_mention" and getattr(e, "user", None):
            d["user_id"] = e.user.id
        elif kind == "custom_emoji" and getattr(e, "custom_emoji_id", None):
            d["document_id"] = int(e.custom_emoji_id)
        out.append(d)
    return json.dumps(out)

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


def can_write_chat(entity) -> tuple[bool, str]:
    """Можно ли вообще писать в чат этим аккаунтом."""
    if getattr(entity, "deactivated", False):
        return False, "группа деактивирована"
    if getattr(entity, "left", False):
        return False, "вы не состоите в чате"
    if getattr(entity, "broadcast", False) and not getattr(entity, "megagroup", False):
        return False, "это канал, а не группа"
    if getattr(entity, "gigagroup", False):
        return False, "в гигагруппе пишут только админы"
    rights = getattr(entity, "banned_rights", None)
    if rights is not None and getattr(rights, "send_messages", False):
        return False, "нет прав на отправку"
    return True, ""


def _is_topic_closed(e) -> bool:
    return isinstance(e, RPCError) and "TOPIC_CLOSED" in str(e).upper()


async def topic_is_closed(client, chat, topic_id: int) -> bool:
    try:
        res = await client(GetForumTopicsRequest(
            channel=chat, offset_date=None, offset_id=0, offset_topic=0, limit=100,
        ))
    except Exception:
        return False
    return any(
        getattr(t, "id", None) == topic_id and getattr(t, "closed", False)
        for t in res.topics
    )


async def store_media(client, data: bytes, name: str, as_document: bool) -> tuple[int, int] | None:
    """Загружает файл в Избранное аккаунта и возвращает (chat_id, msg_id)."""
    try:
        buf = io.BytesIO(data)
        buf.name = name
        sent = await client.send_file("me", buf, force_document=as_document)
        return sent.chat_id, sent.id
    except Exception as e:
        log.warning("store_media: %s", e)
        return None


async def drop_media(client, chat_id: int | None, msg_id: int | None) -> None:
    if not chat_id or not msg_id:
        return
    try:
        await client.delete_messages("me", [msg_id])
    except Exception:
        pass


# ================= отправка =================

_tasks: dict[int, asyncio.Task] = {}
_session_fails: dict[int, int] = {}

SESSION_ERRORS = (
    AuthKeyUnregisteredError, AuthKeyDuplicatedError, SessionRevokedError,
    UserDeactivatedError, UserDeactivatedBanError, AuthKeyError, SessionDead,
)
FATAL_TARGET = (
    ChatWriteForbiddenError, ChatAdminRequiredError, ChatSendMediaForbiddenError,
    UserBannedInChannelError, ChannelPrivateError,
)

_FATAL_TEXT = (
    "CHAT_WRITE_FORBIDDEN", "CHAT_SEND_PLAIN_FORBIDDEN", "CHAT_SEND_MEDIA_FORBIDDEN",
    "CHAT_SEND_DOCS_FORBIDDEN", "CHAT_ADMIN_REQUIRED", "USER_BANNED_IN_CHANNEL",
    "CHANNEL_PRIVATE", "CHAT_RESTRICTED",
)


def _is_fatal(e) -> bool:
    if isinstance(e, FATAL_TARGET):
        return True
    text = str(e).upper()
    return any(x in text for x in _FATAL_TEXT)
MAX_FAILS = 3


def is_running(cid: int) -> bool:
    task = _tasks.get(cid)
    return bool(task and not task.done())


def stop(cid: int) -> None:
    task = _tasks.pop(cid, None)
    if task:
        task.cancel()


def _on_done(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        log.error("campaign task crashed: %r", exc, exc_info=exc)


async def start(cid: int) -> None:
    if is_running(cid):
        return
    await db.set_campaign(cid, status="running")
    task = asyncio.create_task(_loop(cid))
    task.add_done_callback(_on_done)
    _tasks[cid] = task


async def _pause(lo: int, hi: int) -> None:
    await asyncio.sleep(random.randint(min(lo, hi), max(lo, hi)))


def _preview(msg, campaign) -> bool:
    return bool(msg["preview"] if msg["preview"] is not None else campaign["link_preview"])


async def _saved(client, msg):
    if not msg["saved_msg"]:
        return None
    try:
        return await client.get_messages("me", ids=msg["saved_msg"])
    except Exception:
        return None


async def _peer(client, target):
    """Получатель. У новой сессии кэш сущностей пуст, поэтому голый chat_id не работает."""
    chat_id = target["chat_id"]
    try:
        return await client.get_input_entity(chat_id)
    except ValueError:
        pass

    real_id, kind = utils.resolve_id(chat_id)
    access_hash = target["access_hash"]
    if kind is types.PeerChannel and access_hash:
        return types.InputPeerChannel(real_id, access_hash)
    if kind is types.PeerUser and access_hash:
        return types.InputPeerUser(real_id, access_hash)
    if kind is types.PeerChat:
        return types.InputPeerChat(real_id)

    await client.get_dialogs()
    return await client.get_input_entity(chat_id)


def _reply(topic):
    if not topic:
        return None
    return types.InputReplyToMessage(reply_to_msg_id=topic, top_msg_id=topic)


async def _send_inline(client, peer, msg, campaign, topic) -> bool:
    bot = (campaign["via_bot"] or "").lstrip("@")
    if not bot:
        return False
    try:
        results = await client.inline_query(bot, f"{campaign['id']}_{msg['id']}")
        if not results:
            return False
        await results[0].click(peer, reply_to=_reply(topic))
        return True
    except (FloodWaitError, SlowModeWaitError):
        raise
    except Exception as e:
        log.warning("inline %s: %r", bot, e)
        return False

async def _call(make, topic):
    """Пробует InputReplyTo, при TypeError откатывается на голый int."""
    try:
        return await make(_reply(topic))
    except TypeError:
        if not topic:
            raise
        return await make(topic)

async def _deliver(client, peer, target, msg, campaign, topic, entities, buttons) -> None:
    if msg["mode"] == "forward" and msg["saved_msg"]:
        await _call(lambda rt: client.forward_messages(peer, msg["saved_msg"], "me", reply_to=rt), topic)
        return

    text = msg["text"] or ""

    # инлайн-подпись: только текст без медиа и кнопок
    if campaign["inline_mode"] and text and not msg["saved_msg"] and not buttons:
        if await _send_inline(client, peer, msg, campaign, topic):
            return

    source = await _saved(client, msg) if msg["saved_msg"] else None
    media = media_input(source) if source else None

    if media is not None:
        kwargs = {"caption": text or None, "formatting_entities": entities or None}
        if buttons:
            kwargs["buttons"] = buttons
        if msg["is_document"]:
            kwargs["force_document"] = True
        await _call(lambda rt: client.send_file(peer, media, reply_to=rt, **kwargs), topic)
    else:
        kwargs = {"formatting_entities": entities or None, "link_preview": _preview(msg, campaign)}
        if buttons:
            kwargs["buttons"] = buttons
        await _call(lambda rt: client.send_message(peer, text, reply_to=rt, **kwargs), topic)

async def _send(client, target, msg, campaign) -> None:
    entities = load_entities(msg["entities"])
    buttons = load_buttons(msg["buttons"])
    topics = json.loads(target["topic_ids"] or "[]") or [None]

    if not msg["text"] and not msg["saved_msg"]:
        log.info("message %s пустое, пропуск", msg["id"])
        return

    peer = await _peer(client, target)

    for topic in topics:
        for attempt in (1, 2):
            try:
                await _deliver(client, peer, target, msg, campaign, topic, entities, buttons)
                break
            except (FloodWaitError, SlowModeWaitError) as e:
                await asyncio.sleep(e.seconds + 2)
            except asyncio.CancelledError:
                raise
            except SESSION_ERRORS:
                raise
            except FATAL_TARGET as e:
                log.warning("чат %s закрыт для записи (%s), пропуск", target["chat_id"], type(e).__name__)
                return
            except Exception as e:
                if _is_topic_closed(e):
                    if topic is not None:
                        log.warning("тема %s закрыта в %s → пишу в общий чат", topic, target["chat_id"])
                        topic = None
                        continue
                    log.warning("общий чат %s тоже закрыт, пропуск", target["chat_id"])
                    return
                refexp = isinstance(e, FileReferenceExpiredError) or (
                    isinstance(e, RPCError) and "FILE_REFERENCE" in str(e).upper()
                )
                log.warning("send to %s (attempt %s): %r", target["chat_id"], attempt, e)
                await asyncio.sleep(1 if refexp else 2)


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
    log.info("campaign %s started", cid)
    try:
        while True:
            campaign = await db.get_campaign(cid)
            if not campaign or campaign["status"] != "running":
                return
            account = await db.get_account(campaign["account_id"])
            if not account:
                log.error("campaign %s: аккаунт не найден", cid)
                return

            targets = await db.campaign_targets(cid)
            messages = await db.campaign_messages(cid)
            if not targets or not messages:
                log.warning("campaign %s: нет групп или сообщений, остановка", cid)
                return

            for target in targets:
                for msg in messages:
                    try:
                        await _guarded(account, target, msg, campaign)
                        _session_fails[account["id"]] = 0
                        await db.set_account_status(account["id"], "ok")
                    except asyncio.CancelledError:
                        raise
                    except SESSION_ERRORS as e:
                        fails = _session_fails.get(account["id"], 0) + 1
                        _session_fails[account["id"]] = fails
                        log.error("session dead %s: %r", account["id"], e)
                        if fails >= MAX_FAILS:
                            await db.set_account_status(account["id"], "dead")
                            return
                        await asyncio.sleep(min(60, 5 * fails))
                    except Exception as e:
                        log.exception("campaign %s: сбой отправки в %s: %r", cid, target["chat_id"], e)
                    await _pause(campaign["delay_min"], campaign["delay_max"])

            await _pause(campaign["cycle_min"], campaign["cycle_max"])
    except asyncio.CancelledError:
        pass
    finally:
        _tasks.pop(cid, None)
        try:
            await db.set_campaign(cid, status="stopped")
        except Exception:
            pass
        log.info("campaign %s stopped", cid)


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

        ok, reason = can_write_chat(chat)
        if not ok:
            await event.reply(f"⚠️ Писать сюда нельзя: {reason}.")
            return

        if topic and await topic_is_closed(event.client, chat, topic):
            topic = None
            note = " (тема закрыта — в общий чат)"
        else:
            note = f" (тема {topic})" if topic else ""

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

        await event.reply(f"✅ Чат добавлен в «{title}»{note}")
    except Exception as e:
        log.warning("secretary: %s", e)
