import json
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton as B, InlineKeyboardMarkup as K, Message,
    InlineQuery, InlineQueryResultArticle, InputTextMessageContent, LinkPreviewOptions,
    MessageEntity,
)
from telethon import TelegramClient
from telethon.errors import BotInlineDisabledError, SessionPasswordNeededError
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetForumTopicsRequest

import core
import db
from settings import cfg
from ui import (
    TOPIC_PAGE, Admin, Auth, Camp, accounts_kb, campaign_card, code_pad, groups_page,
    menu, message_card, messages_kb, paywall, preview_kb, timing_kb, topics_kb,
)

log = logging.getLogger(__name__)
router = Router()

_pending: dict[int, dict] = {}
PAGE = 8
LIST_CAP = 40
MEDIA_FIELDS = ("photo", "video", "document", "audio", "voice", "video_note", "animation", "sticker")

PLANS = (
    "💳 <b>Подписка</b>\n\n• 0.5 TON / неделя\n• 50 ⭐ / неделя\n• 50 ₽ / неделя\n\n"
    f"Для получения доступа напишите @{cfg.seller} — выдача вручную."
)


DEFAULT_NAMES = {
    "photo": "photo.jpg", "video": "video.mp4", "voice": "voice.ogg", "audio": "audio.mp3",
    "video_note": "video_note.mp4", "animation": "animation.mp4", "sticker": "sticker.webp",
}


def media_info(message: Message):
    """(объект для скачивания, имя файла, mime) или None."""
    for field in MEDIA_FIELDS:
        obj = getattr(message, field, None)
        if not obj:
            continue
        if field == "photo":
            return obj[-1], "photo.jpg", "image/jpeg"
        mime = getattr(obj, "mime_type", None)
        if field in ("video_note", "animation") and not mime:
            mime = "video/mp4"
        name = getattr(obj, "file_name", None) or DEFAULT_NAMES.get(field, "file.bin")
        return obj, name, mime
    return None


def chunk_text(text: str, size: int = 3500) -> list[str]:
    out, buf, cur = [], [], 0
    for ch in text:
        w = 2 if ord(ch) > 0xFFFF else 1
        if cur + w > size:
            out.append("".join(buf))
            buf, cur = [], 0
        buf.append(ch)
        cur += w
    if buf:
        out.append("".join(buf))
    return out or [""]


async def gate(event) -> bool:
    user = event.from_user
    if await db.has_access(user.id):
        return True
    if isinstance(event, CallbackQuery):
        await event.answer("Нужна подписка", show_alert=True)
        await db.touch_user(user.id, user.username)
        await event.message.answer(PLANS, reply_markup=paywall())
    else:
        await db.touch_user(user.id, user.username)
        await event.answer(PLANS, reply_markup=paywall())
    return False


# ================= старт и оплата =================

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await db.touch_user(message.from_user.id, message.from_user.username)
    if not await db.has_access(message.from_user.id):
        await message.answer(PLANS, reply_markup=paywall())
        return
    await begin_login(message, message.from_user.id, state)


async def begin_login(message: Message, tg_id: int, state: FSMContext, edit: bool = False) -> None:
    if await db.user_accounts(tg_id):
        if edit:
            await message.edit_text("Главное меню", reply_markup=menu())
        else:
            await message.answer("Главное меню", reply_markup=menu())
        return
    await state.set_state(Auth.phone)
    text = "Отправьте номер телефона в формате <code>+79991234567</code>."
    if edit:
        await message.edit_text(text)
    else:
        await message.answer(text)


@router.callback_query(F.data == "pay:info")
async def pay_info(cb: CallbackQuery) -> None:
    await cb.message.edit_text(PLANS, reply_markup=paywall())
    await cb.answer()


@router.callback_query(F.data == "pay:check")
async def pay_check(cb: CallbackQuery, state: FSMContext) -> None:
    if not await db.has_access(cb.from_user.id):
        await cb.answer("Доступ пока не выдан", show_alert=True)
        return
    await cb.answer("Доступ активен")
    await begin_login(cb.message, cb.from_user.id, state, edit=True)


@router.callback_query(F.data == "menu")
async def to_menu(cb: CallbackQuery) -> None:
    await cb.message.edit_text("Главное меню", reply_markup=menu())
    await cb.answer()


@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery) -> None:
    await cb.answer()


# ================= аккаунты и вход =================

@router.callback_query(F.data == "acc:add")
async def acc_add(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Auth.phone)
    await cb.message.edit_text("Отправьте номер телефона в формате <code>+79991234567</code>.")
    await cb.answer()


@router.message(Command("phone"))
async def cmd_phone(message: Message, state: FSMContext) -> None:
    await state.set_state(Auth.phone)
    await message.answer("Отправьте номер телефона в формате <code>+79991234567</code>.")


@router.message(Auth.phone)
async def on_phone(message: Message, state: FSMContext) -> None:
    phone = (message.text or "").replace(" ", "").replace("-", "")
    if not phone.startswith("+") or not phone[1:].isdigit():
        await message.answer("Не похоже на номер. Пример: <code>+79991234567</code>")
        return

    client = TelegramClient(StringSession(), cfg.api_id, cfg.api_hash)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
    except Exception as e:
        await client.disconnect()
        await message.answer(f"Ошибка запроса кода: <code>{e}</code>")
        return

    _pending[message.from_user.id] = {"client": client, "phone": phone, "hash": sent.phone_code_hash}
    await state.set_state(Auth.code)
    await state.update_data(buf="")
    await message.answer("Введите код из Telegram:", reply_markup=code_pad())


@router.callback_query(Auth.code, F.data.startswith("code:"))
async def on_digit(cb: CallbackQuery, state: FSMContext) -> None:
    action = cb.data.split(":", 1)[1]
    buf = (await state.get_data()).get("buf", "")

    if action == "del":
        buf = buf[:-1]
    elif action == "ok":
        if not buf:
            await cb.answer("Код пуст")
            return
        await cb.answer("Проверяю…")
        await _submit_code(cb, state, buf)
        return
    else:
        if len(buf) >= 6:
            await cb.answer("Максимум 6 цифр")
            return
        buf += action

    await state.update_data(buf=buf)
    try:
        await cb.message.edit_text(f"Код: {'•' * len(buf) or '—'}", reply_markup=code_pad())
    except Exception:
        pass
    await cb.answer()


async def _submit_code(cb: CallbackQuery, state: FSMContext, code: str) -> None:
    ctx = _pending.get(cb.from_user.id)
    if not ctx:
        await cb.message.answer("Сессия входа потеряна, начните заново /start")
        return
    try:
        await ctx["client"].sign_in(ctx["phone"], code, phone_code_hash=ctx["hash"])
    except SessionPasswordNeededError:
        await state.set_state(Auth.password)
        await cb.message.edit_text("🔐 Введите пароль двухфакторной аутентификации:")
        return
    except Exception as e:
        await state.update_data(buf="")
        await cb.message.edit_text(f"Неверный код: <code>{e}</code>", reply_markup=code_pad())
        return
    await _finish_login(cb.message, cb.from_user.id, state)


@router.message(Auth.password)
async def on_password(message: Message, state: FSMContext) -> None:
    ctx = _pending.get(message.from_user.id)
    if not ctx:
        await message.answer("Сессия входа потеряна, начните заново /start")
        return
    try:
        await ctx["client"].sign_in(password=message.text)
    except Exception as e:
        await message.answer(f"Ошибка пароля: <code>{e}</code>")
        return
    await _finish_login(message, message.from_user.id, state)


async def _finish_login(message: Message, tg_id: int, state: FSMContext) -> None:
    ctx = _pending.pop(tg_id, None)
    client = ctx["client"]
    try:
        me = await client.get_me()
        session = client.session.save()
    finally:
        await client.disconnect()
    await db.add_account(tg_id, ctx["phone"], session, me.id, me.first_name or "", me.username)
    await state.clear()
    await message.answer(f"✅ Аккаунт <b>{me.first_name}</b> подключён.", reply_markup=menu())


@router.callback_query(F.data == "acc:list")
async def acc_list(cb: CallbackQuery) -> None:
    rows = await db.user_accounts(cb.from_user.id)
    if not rows:
        await cb.message.edit_text("Аккаунтов нет.\n\n/start — подключить", reply_markup=menu())
        await cb.answer()
        return
    lines = "\n".join(
        f"• {r['name']} (@{r['username'] or '—'}) — <code>{r['phone']}</code> [{r['status']}]"
        for r in rows
    )
    await cb.message.edit_text(f"👤 <b>Аккаунты</b>\n\n{lines}", reply_markup=accounts_kb(rows))
    await cb.answer()


@router.callback_query(F.data.startswith("acc:del:"))
async def acc_del(cb: CallbackQuery) -> None:
    acc_id = int(cb.data.split(":")[2])
    await core.drop_client(acc_id)
    await db.del_account(acc_id)
    await cb.answer("Удалён")
    await acc_list(cb)


# ================= рассылки =================

@router.callback_query(F.data == "c:list")
async def c_list(cb: CallbackQuery) -> None:
    if not await gate(cb):
        return
    rows = await db.user_campaigns(cb.from_user.id)
    if not rows:
        await cb.message.edit_text("Рассылок нет.\n\n/new — создать", reply_markup=menu())
        await cb.answer()
        return
    lines = "\n".join(f"• #{r['id']} {r['title']} — {r['status']}" for r in rows)
    kb = K(inline_keyboard=[
        *[[B(text=f"#{r['id']} {r['title'][:30]}", callback_data=f"c:open:{r['id']}")] for r in rows[:LIST_CAP]],
        [B(text="⬅️ Назад", callback_data="menu")],
    ])
    await cb.message.edit_text(f"📨 <b>Рассылки</b>\n\n{lines}\n\n/new — создать", reply_markup=kb)
    await cb.answer()


@router.message(Command("new"))
async def c_new(message: Message, state: FSMContext) -> None:
    if not await gate(message):
        return
    if not await db.user_accounts(message.from_user.id):
        await message.answer("Сначала подключите аккаунт: /start")
        return
    await state.set_state(Camp.title)
    await message.answer("Название рассылки (по нему работает <code>.старт название</code>):")


@router.message(Camp.title)
async def c_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not title:
        return
    if len(title) > 60:
        await message.answer("Название длиннее 60 символов.")
        return
    if await db.by_title(message.from_user.id, title):
        await message.answer("Такое название уже есть.")
        return
    accounts = await db.user_accounts(message.from_user.id)
    if len(accounts) == 1:
        cid = await db.create_campaign(message.from_user.id, title, accounts[0]["id"])
        await state.clear()
        await message.answer(f"Создано #{cid}.", reply_markup=campaign_card(cid, False, False, True))
        return
    await state.update_data(title=title)
    await state.set_state(Camp.pick_account)
    kb = K(inline_keyboard=[
        [B(text=f"{a['name']} ({a['phone']})", callback_data=f"c:acc:{a['id']}")] for a in accounts
    ])
    await message.answer("С какого аккаунта отправлять?", reply_markup=kb)


@router.callback_query(Camp.pick_account, F.data.startswith("c:acc:"))
async def c_pick(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    cid = await db.create_campaign(cb.from_user.id, data["title"], int(cb.data.split(":")[2]))
    await state.clear()
    await cb.message.edit_text(f"Создано #{cid}.", reply_markup=campaign_card(cid, False, False, True))
    await cb.answer()


@router.callback_query(F.data.startswith("c:open:"))
async def c_open(cb: CallbackQuery) -> None:
    cid = int(cb.data.split(":")[2])
    camp = await db.get_campaign(cid)
    if not camp or camp["user_id"] != cb.from_user.id:
        await cb.answer("Нет доступа")
        return
    msgs = await db.campaign_messages(cid)
    targets = await db.campaign_targets(cid)
    text = (
        f"<b>{camp['title']}</b>\n\nСообщений: {len(msgs)}\nГрупп: {len(targets)}\n"
        f"Интервал: {camp['delay_min']}-{camp['delay_max']}с · цикл {camp['cycle_min']}-{camp['cycle_max']}с\n"
        f"Статус: {camp['status']}"
    )
    if camp["inline_mode"]:
        text += "\n🟢 Инлайн-подпись: только текст без кнопок, медиа уходит обычной отправкой"
    await cb.message.edit_text(text, reply_markup=campaign_card(
        cid, camp["status"] == "running", bool(camp["inline_mode"]), bool(camp["link_preview"])
    ))
    await cb.answer()


@router.callback_query(F.data.startswith("c:start:"))
async def c_start(cb: CallbackQuery) -> None:
    cid = int(cb.data.split(":")[2])
    camp = await db.get_campaign(cid)
    if not camp or camp["user_id"] != cb.from_user.id:
        await cb.answer("Нет доступа", show_alert=True)
        return
    if not await gate(cb):
        return
    if not await db.campaign_messages(cid):
        await cb.answer("Сначала добавьте хотя бы одно сообщение", show_alert=True)
        return
    if not await db.campaign_targets(cid):
        await cb.answer("Сначала добавьте хотя бы одну группу", show_alert=True)
        return
    await core.start(cid)
    await cb.answer("Запущено")
    await c_open(cb)


@router.callback_query(F.data.startswith("c:stop:"))
async def c_stop(cb: CallbackQuery) -> None:
    cid = int(cb.data.split(":")[2])
    core.stop(cid)
    await db.set_campaign(cid, status="stopped")
    await cb.answer("Остановлено")
    await c_open(cb)


@router.callback_query(F.data.startswith("c:inline:"))
async def c_inline(cb: CallbackQuery) -> None:
    cid = int(cb.data.split(":")[2])
    camp = await db.get_campaign(cid)
    if not camp or camp["user_id"] != cb.from_user.id:
        await cb.answer("Нет доступа", show_alert=True)
        return

    new = 0 if camp["inline_mode"] else 1
    username = ""
    if new:
        me = await cb.bot.get_me()
        username = (me.username or "").lstrip("@")
        if not username:
            await cb.answer("У бота нет username", show_alert=True)
            return
        try:
            await (await _client_for(cid)).inline_query(username, f"{cid}_0")
        except BotInlineDisabledError:
            await cb.answer("Сначала включите инлайн у бота: @BotFather → /setinline", show_alert=True)
            return
        except Exception as e:
            log.warning("inline probe: %s", e)

    await db.set_campaign(cid, inline_mode=new, via_bot=username)
    await cb.answer("Инлайн-подпись включена" if new else "Выключена")
    await c_open(cb)


@router.callback_query(F.data.startswith("c:prev:"))
async def c_prev(cb: CallbackQuery) -> None:
    cid = int(cb.data.split(":")[2])
    camp = await db.get_campaign(cid)
    await db.set_campaign(cid, link_preview=0 if camp["link_preview"] else 1)
    await cb.answer()
    await c_open(cb)


@router.callback_query(F.data.startswith("c:del:"))
async def c_del(cb: CallbackQuery) -> None:
    cid = int(cb.data.split(":")[2])
    core.stop(cid)
    camp = await db.get_campaign(cid)
    try:
        client = await core.get_client(await db.get_account(camp["account_id"]))
        for msg in await db.campaign_messages(cid):
            await core.drop_media(client, msg["saved_chat"], msg["saved_msg"])
    except Exception as e:
        log.warning("cleanup media: %s", e)
    await db.del_campaign(cid)
    await cb.message.edit_text("Удалено.", reply_markup=menu())
    await cb.answer()


# ================= сообщения =================

@router.callback_query(F.data.startswith("m:list:"))
async def m_list(cb: CallbackQuery) -> None:
    cid = int(cb.data.split(":")[2])
    items = await db.campaign_messages(cid)
    text = "✏️ <b>Сообщения</b>\n\n" + (
        "\n".join(f"#{r['position']} [{r['mode']}]" for r in items) or "Пока пусто."
    )
    await cb.message.edit_text(text, reply_markup=messages_kb(cid, items))
    await cb.answer()


@router.callback_query(F.data.startswith("m:add:"))
async def m_add(cb: CallbackQuery, state: FSMContext) -> None:
    cid = int(cb.data.split(":")[2])
    await state.set_state(Camp.message)
    await state.update_data(cid=cid)
    await cb.message.answer(
        "Отправьте сообщение как есть — текст, форматирование, премиум-эмодзи, фото/видео/файл сохранятся.\n"
        "Кнопки-ссылки: <code>/btn</code> и затем строки <code>Текст | https://url</code>."
    )
    await cb.answer()


@router.message(Camp.message)
async def m_receive(message: Message, bot: Bot, state: FSMContext) -> None:
    data = await state.get_data()
    cid = data.get("cid")
    if not cid:
        await state.clear()
        await message.answer("Контекст рассылки потерян, откройте её заново.")
        return

    text = message.text or message.caption or ""
    info = media_info(message)
    has_media = info is not None
    if not text and not has_media:
        return

    saved_chat = saved_msg = None
    doc = False
    if has_media:
        camp = await db.get_campaign(cid)
        obj, name, mime = info
        doc = core.is_document(mime)
        try:
            client = await core.get_client(await db.get_account(camp["account_id"]))
            buf = await bot.download(obj)
            stored = await core.store_media(client, buf.read(), name, doc)
            if stored:
                saved_chat, saved_msg = stored
            else:
                await message.answer("⚠️ Файл не сохранился в Избранное, сообщение добавлено без медиа.")
        except Exception as e:
            log.warning("store media failed: %s", e)
            await message.answer(f"⚠️ Не удалось сохранить файл: <code>{e}</code>")

    mid = await db.add_message(
        cid,
        mode="copy",
        text=text,
        entities=core.dump_entities(message.entities or message.caption_entities),
        saved_chat=saved_chat,
        saved_msg=saved_msg,
        is_document=doc,
    )

    if core.has_url(text):
        await state.set_state(Camp.preview)
        await state.update_data(mid=mid)
        await message.answer("В сообщении есть ссылка. Показывать предпросмотр?", reply_markup=preview_kb(mid))
        return

    await state.clear()
    await message.answer(f"Сообщение #{mid} добавлено.", reply_markup=message_card(mid, cid, "copy"))


@router.callback_query(Camp.preview, F.data.startswith("m:prev:"))
async def m_preview(cb: CallbackQuery, state: FSMContext) -> None:
    _, _, value, mid = cb.data.split(":")
    mid = int(mid)
    data = await state.get_data()
    await db.set_message(mid, preview=int(value))
    await state.clear()
    await cb.message.edit_text(
        f"Сообщение #{mid} добавлено (предпросмотр: {'вкл' if value == '1' else 'выкл'}).",
        reply_markup=message_card(mid, data["cid"], "copy"),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("m:open:"))
async def m_open(cb: CallbackQuery) -> None:
    mid = int(cb.data.split(":")[2])
    msg = await db.get_message(mid)
    if not msg:
        await cb.answer("Не найдено")
        return

    meta = []
    if msg["saved_msg"]:
        meta.append("медиа")
    if msg["mode"] == "forward":
        meta.append("пересылка")
    if msg["preview"] is not None:
        meta.append("предпросмотр: " + ("вкл" if msg["preview"] else "выкл"))
    buttons = json.loads(msg["buttons"] or "[]")
    if buttons:
        meta.append(f"кнопок: {sum(len(r) for r in buttons)}")

    head = f"Сообщение #{mid}"
    if meta:
        head += "\n" + " · ".join(meta)

    parts = chunk_text(head + "\n\n" + (msg["text"] or "[без текста]"))
    await cb.message.edit_text(parts[0], parse_mode=None)

    last = cb.message
    for part in parts[1:]:
        last = await last.answer(part, parse_mode=None)

    await last.edit_reply_markup(reply_markup=message_card(mid, msg["campaign_id"], msg["mode"]))
    await cb.answer()


@router.callback_query(F.data.startswith("m:mode:"))
async def m_mode(cb: CallbackQuery) -> None:
    mid = int(cb.data.split(":")[2])
    msg = await db.get_message(mid)
    new = "forward" if msg["mode"] == "copy" else "copy"
    await db.set_message(mid, mode=new)
    await cb.answer("Режим: " + ("пересылка" if new == "forward" else "копия"))
    try:
        await cb.message.edit_reply_markup(
            reply_markup=message_card(mid, msg["campaign_id"], new)
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("m:del:"))
async def m_del(cb: CallbackQuery) -> None:
    mid = int(cb.data.split(":")[2])
    msg = await db.get_message(mid)
    camp = await db.get_campaign(msg["campaign_id"])
    if msg["saved_msg"] and camp:
        try:
            client = await core.get_client(await db.get_account(camp["account_id"]))
            await core.drop_media(client, msg["saved_chat"], msg["saved_msg"])
        except Exception as e:
            log.warning("drop media: %s", e)
    await db.del_message(mid)
    items = await db.campaign_messages(msg["campaign_id"])
    await cb.message.edit_text("Удалено.", reply_markup=messages_kb(msg["campaign_id"], items))
    await cb.answer()


@router.message(Command("btn"))
async def m_btn(message: Message, state: FSMContext) -> None:
    if not (await state.get_data()).get("cid"):
        await message.answer("Сначала откройте рассылку → «Сообщения».")
        return
    await state.set_state(Camp.buttons)
    await message.answer("Пришлите кнопки: <code>Текст | https://url</code>, строки через <code>;</code>")


@router.message(Camp.buttons)
async def m_btn_receive(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    spec = core.parse_buttons(message.text or "")
    if spec == "[]":
        await message.answer("Не распознал. Пример: <code>Купить | https://t.me/x</code>")
        return
    mid = await db.add_message(data["cid"], mode="copy", buttons=spec)
    await state.clear()
    await message.answer(f"Кнопки в сообщении #{mid}.", reply_markup=message_card(mid, data["cid"], "copy"))


# ================= инлайн-подпись =================

_ENT = {
    "MessageEntityBold": "bold", "MessageEntityItalic": "italic",
    "MessageEntityUnderline": "underline", "MessageEntityStrike": "strikethrough",
    "MessageEntitySpoiler": "spoiler", "MessageEntityCode": "code",
    "MessageEntityPre": "pre", "MessageEntityBlockquote": "blockquote",
    "MessageEntityTextUrl": "text_link", "MessageEntityUrl": "url",
    "MessageEntityEmail": "email", "MessageEntityPhone": "phone_number",
    "MessageEntityMention": "mention", "MessageEntityHashtag": "hashtag",
    "MessageEntityCashtag": "cashtag", "MessageEntityBotCommand": "bot_command",
}


def bot_entities(raw: str):
    out = []
    for e in core.load_entities(raw):
        kind = _ENT.get(type(e).__name__)
        if not kind:
            continue
        kwargs = {"type": kind, "offset": e.offset, "length": e.length}
        if kind == "text_link" and getattr(e, "url", None):
            kwargs["url"] = e.url
        elif kind == "pre" and getattr(e, "language", None):
            kwargs["language"] = e.language
        out.append(MessageEntity(**kwargs))
    return out


@router.inline_query()
async def inline_msg(query: InlineQuery) -> None:
    async def empty() -> None:
        await query.answer([], cache_time=1, is_personal=True)

    try:
        cid, mid = (int(x) for x in (query.query or "").strip().split("_", 1))
    except ValueError:
        await empty()
        return

    camp = await db.get_campaign(cid)
    msg = await db.get_message(mid)
    text = (msg["text"] if msg else "") or ""
    if not camp or not msg or not text:
        await empty()
        return

    owner = await db.user_by_account(query.from_user.id)
    if owner and camp["user_id"] != owner["tg_id"]:
        await empty()
        return

    preview = bool(msg["preview"] if msg["preview"] is not None else camp["link_preview"])
    await query.answer(
        [InlineQueryResultArticle(
            id=str(mid),
            title=(text.splitlines()[0][:60] or "Сообщение"),
            description=text[:120],
            input_message_content=InputTextMessageContent(
                message_text=text,
                entities=bot_entities(msg["entities"]) or None,
                link_preview_options=LinkPreviewOptions(is_disabled=not preview),
            ),
        )],
        cache_time=1,
        is_personal=True,
    )


# ================= группы =================

async def _client_for(cid: int):
    camp = await db.get_campaign(cid)
    return await core.get_client(await db.get_account(camp["account_id"]))


async def _topics(client, chat_id: int):
    entity = await client.get_entity(chat_id)
    if not getattr(entity, "forum", False):
        return []
    try:
        res = await client(GetForumTopicsRequest(
            channel=entity, offset_date=None, offset_id=0, offset_topic=0, limit=100,
        ))
    except Exception:
        return []
    return [
        {"id": t.id, "title": t.title}
        for t in res.topics
        if getattr(t, "title", None) and not getattr(t, "closed", False)
    ]


async def _show_topics(cb: CallbackQuery, state: FSMContext, cid: int, chat_id: int, page: int = 0) -> None:
    topics = await _topics(await _client_for(cid), chat_id)
    if not topics:
        await cb.message.edit_text(
            "В группе нет открытых тем — сообщения уйдут в общий чат.",
            reply_markup=K(inline_keyboard=[[B(text="⬅️ К списку", callback_data=f"g:chosen:{cid}")]]),
        )
        return

    pages = max(1, (len(topics) + TOPIC_PAGE - 1) // TOPIC_PAGE)
    page = max(0, min(page, pages - 1))
    await state.update_data(page=page)

    picked = (await state.get_data()).get("picked", [])
    await cb.message.edit_text(
        f"Выберите до двух тем (всего {len(topics)}):",
        reply_markup=topics_kb(
            cid, chat_id, topics[page * TOPIC_PAGE:(page + 1) * TOPIC_PAGE], picked, page, pages
        ),
    )


@router.callback_query(F.data.startswith("g:list:"))
async def g_list(cb: CallbackQuery) -> None:
    _, _, cid, page = cb.data.split(":")
    cid, page = int(cid), int(page)
    client = await _client_for(cid)
    await cb.answer("Сканирую…")
    found, skipped = [], 0
    async for dialog in client.iter_dialogs():
        if not dialog.is_group:
            continue
        ok, _reason = core.can_write_chat(dialog.entity)
        if not ok:
            skipped += 1
            continue
        found.append({"id": dialog.id, "title": dialog.name or str(dialog.id)})
    pages = max(1, (len(found) + PAGE - 1) // PAGE)
    page = min(page, pages - 1)
    head = f"👥 Групп с доступом на запись: {len(found)}"
    if skipped:
        head += f"\n🚫 Скрыто закрытых и каналов: {skipped}"
    await cb.message.edit_text(
        head,
        reply_markup=groups_page(cid, found[page * PAGE:(page + 1) * PAGE], page, pages),
    )


@router.callback_query(F.data.startswith("g:add:"))
async def g_add(cb: CallbackQuery, state: FSMContext) -> None:
    _, _, cid, chat_id = cb.data.split(":")
    cid, chat_id = int(cid), int(chat_id)

    client = await _client_for(cid)
    entity = await client.get_entity(chat_id)
    ok, reason = core.can_write_chat(entity)
    if not ok:
        await cb.answer(f"Писать нельзя: {reason}", show_alert=True)
        return

    tid = await db.add_target(cid, chat_id, getattr(entity, "access_hash", None), entity.title or str(chat_id))

    topics = await _topics(client, chat_id)
    if not topics:
        await cb.answer("Добавлено")
        await cb.message.edit_text(
            f"✅ {entity.title} добавлена (в общий чат).",
            reply_markup=K(inline_keyboard=[[B(text="⬅️ К списку", callback_data=f"g:chosen:{cid}")]]),
        )
        return

    await state.set_state(Camp.topics)
    await state.update_data(cid=cid, chat_id=chat_id, tid=tid, picked=[], page=0)
    await _show_topics(cb, state, cid, chat_id, 0)
    await cb.answer()


@router.callback_query(Camp.topics, F.data.startswith("g:tpage:"))
async def g_tpage(cb: CallbackQuery, state: FSMContext) -> None:
    _, _, cid, chat_id, page = cb.data.split(":")
    await _show_topics(cb, state, int(cid), int(chat_id), int(page))
    await cb.answer()


@router.callback_query(Camp.topics, F.data.startswith("g:topic:"))
async def g_topic(cb: CallbackQuery, state: FSMContext) -> None:
    _, _, cid, chat_id, topic_id = cb.data.split(":")
    cid, chat_id, topic_id = int(cid), int(chat_id), int(topic_id)

    data = await state.get_data()
    picked = data.get("picked", [])
    if topic_id in picked:
        picked.remove(topic_id)
    else:
        if len(picked) >= 2:
            await cb.answer("Максимум две темы")
            return
        picked.append(topic_id)
    await state.update_data(picked=picked)

    await _show_topics(cb, state, cid, chat_id, data.get("page", 0))
    await cb.answer()


@router.callback_query(F.data.startswith("g:save:"))
async def g_save(cb: CallbackQuery, state: FSMContext) -> None:
    _, _, cid, _chat_id = cb.data.split(":")
    data = await state.get_data()
    picked = data.get("picked", [])
    await db.set_topics(data["tid"], picked)
    await state.clear()
    await cb.message.edit_text(
        "✅ Группа добавлена." if picked else "✅ Группа добавлена (без тем — в общий чат).",
        reply_markup=K(inline_keyboard=[[B(text="⬅️ К списку", callback_data=f"g:chosen:{cid}")]]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("g:chosen:"))
async def g_chosen(cb: CallbackQuery) -> None:
    cid = int(cb.data.split(":")[2])
    rows = await db.campaign_targets(cid)
    if not rows:
        await cb.message.edit_text(
            "Список пуст.",
            reply_markup=K(inline_keyboard=[[B(text="⬅️ Назад", callback_data=f"c:open:{cid}")]]),
        )
        await cb.answer()
        return
    kb = []
    for row in rows[:LIST_CAP]:
        count = len(json.loads(row["topic_ids"] or "[]"))
        label = row["title"] + (f" [{count} тем]" if count else "")
        kb.append([B(text=f"🗑 {label[:40]}", callback_data=f"g:rm:{cid}:{row['id']}")])
    kb.append([B(text="⬅️ Назад", callback_data=f"c:open:{cid}")])
    await cb.message.edit_text("Выбранные (нажмите, чтобы убрать):", reply_markup=K(inline_keyboard=kb))
    await cb.answer()


@router.callback_query(F.data.startswith("g:rm:"))
async def g_rm(cb: CallbackQuery) -> None:
    _, _, cid, tid = cb.data.split(":")
    await db.del_target(int(tid))
    await g_chosen(cb.model_copy(update={"data": f"g:chosen:{cid}"}))


# ================= интервалы =================

async def _timing(cb: CallbackQuery, cid: int) -> None:
    camp = await db.get_campaign(cid)
    await cb.message.edit_text(
        f"⏱ Интервалы\n\nСообщения: {camp['delay_min']}-{camp['delay_max']}с\n"
        f"Цикл: {camp['cycle_min']}-{camp['cycle_max']}с\n\nМинимум: 10с / 120с.",
        reply_markup=timing_kb(cid),
    )


@router.callback_query(F.data.startswith("t:menu:"))
async def t_menu(cb: CallbackQuery) -> None:
    await _timing(cb, int(cb.data.split(":")[2]))
    await cb.answer()


@router.callback_query(F.data.startswith("t:set:"))
async def t_set(cb: CallbackQuery) -> None:
    _, _, cid, key, value = cb.data.split(":")
    cid, value = int(cid), int(value)
    camp = await db.get_campaign(cid)
    if key == "dmin":
        await db.set_campaign(cid, delay_min=value, delay_max=max(value, camp["delay_max"]))
    else:
        await db.set_campaign(cid, cycle_min=value, cycle_max=max(value, camp["cycle_max"]))
    await _timing(cb, cid)
    await cb.answer()


@router.callback_query(F.data.startswith("t:rng:"))
async def t_rng(cb: CallbackQuery) -> None:
    _, _, cid, lo, hi = cb.data.split(":")
    await db.set_campaign(int(cid), delay_min=int(lo), delay_max=int(hi))
    await _timing(cb, int(cid))
    await cb.answer()


# ================= админка =================

async def admin_only(event) -> bool:
    if await db.is_admin(event.from_user.id):
        return True
    if isinstance(event, CallbackQuery):
        await event.answer("Только для админов", show_alert=True)
    else:
        await event.answer("Только для админов")
    return False


@router.message(Command("admin"))
async def a_panel(message: Message) -> None:
    if not await admin_only(message):
        return
    rows = await db.list_admins()
    kb = K(inline_keyboard=[
        [B(text="➕ Добавить админа", callback_data="a:add")],
        [B(text="🎁 Выдать доступ", callback_data="a:grant")],
        [B(text="🚫 Снять доступ", callback_data="a:revoke")],
    ])
    await message.answer(
        "🛠 <b>Админ-панель</b>\n\n" + ("\n".join(f"• <code>{r['tg_id']}</code>" for r in rows) or "—"),
        reply_markup=kb,
    )


@router.callback_query(F.data == "a:add")
async def a_add(cb: CallbackQuery, state: FSMContext) -> None:
    if not await admin_only(cb):
        return
    await state.set_state(Admin.add_admin)
    await cb.message.answer("ID нового админа:")


@router.message(Admin.add_admin)
async def a_add_done(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().lstrip("@")
    if not raw.isdigit():
        await message.answer("Нужен числовой ID.")
        return
    await db.add_admin(int(raw), message.from_user.id)
    await state.clear()
    await message.answer(f"✅ {raw} теперь админ.")


@router.callback_query(F.data == "a:grant")
async def a_grant(cb: CallbackQuery, state: FSMContext) -> None:
    if not await admin_only(cb):
        return
    await state.set_state(Admin.grant)
    await cb.message.answer("ID и дни: <code>123456789 7</code>")


@router.message(Admin.grant)
async def a_grant_done(message: Message, state: FSMContext) -> None:
    parts = (message.text or "").split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        await message.answer("Формат: <code>ID дни</code>")
        return
    target, days = int(parts[0]), int(parts[1])
    await db.touch_user(target, None)
    await db.grant_days(target, days)
    await state.clear()
    await message.answer(f"✅ Доступ выдан на {days} дн.")


@router.callback_query(F.data == "a:revoke")
async def a_revoke(cb: CallbackQuery, state: FSMContext) -> None:
    if not await admin_only(cb):
        return
    await state.set_state(Admin.revoke)
    await cb.message.answer("ID для снятия доступа:")


@router.message(Admin.revoke)
async def a_revoke_done(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Нужен ID.")
        return
    await db.revoke(int(raw))
    await state.clear()
    await message.answer("🚫 Доступ снят.")