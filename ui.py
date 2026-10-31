from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton as B, InlineKeyboardMarkup as K

from settings import cfg

DIGITS = ["1", "2", "3", "4", "5", "6", "7", "8", "9"]

SELLER_TEXT = (
    "%D0%9F%D1%80%D0%B8%D0%B2%D0%B5%D1%82%2C%20%D1%85%D0%BE%D1%87%D1%83%20"
    "%D0%BA%D1%83%D0%BF%D0%B8%D1%82%D1%8C%20%D0%BF%D0%BE%D0%B4%D0%BF%D0%B8%D1%81%D0%BA%D1%83%20"
    "%D0%BD%D0%B0%20%D1%81%D0%BF%D0%B0%D0%BC%20%D0%B1%D0%BE%D1%82%D0%B0"
)


class Auth(StatesGroup):
    phone = State()
    code = State()
    password = State()


class Camp(StatesGroup):
    title = State()
    pick_account = State()
    message = State()
    preview = State()
    buttons = State()
    topics = State()


class Admin(StatesGroup):
    add_admin = State()
    grant = State()
    revoke = State()


def code_pad() -> K:
    rows = [[B(text=d, callback_data=f"code:{d}") for d in DIGITS[i:i + 3]] for i in (0, 3, 6)]
    rows.append([
        B(text="0", callback_data="code:0"),
        B(text="⌫ Стереть", callback_data="code:del"),
        B(text="Далее ➡", callback_data="code:ok"),
    ])
    return K(inline_keyboard=rows)


def paywall() -> K:
    return K(inline_keyboard=[
        [B(text=f"💬 Написать @{cfg.seller}", url=f"https://t.me/{cfg.seller}?text={SELLER_TEXT}")],
        [B(text="✅ Проверить доступ", callback_data="pay:check")],
    ])


def menu() -> K:
    return K(inline_keyboard=[
        [B(text="📨 Рассылки", callback_data="c:list")],
        [B(text="👤 Аккаунты", callback_data="acc:list")],
        [B(text="💳 Подписка", callback_data="pay:info")],
    ])


def campaign_card(cid: int, running: bool, inline: bool, preview: bool) -> K:
    return K(inline_keyboard=[
        [B(text="⏹ Остановить" if running else "▶️ Запустить",
           callback_data=f"c:{'stop' if running else 'start'}:{cid}")],
        [B(text="✏️ Сообщения", callback_data=f"m:list:{cid}")],
        [B(text="👥 Группы", callback_data=f"g:list:{cid}:0")],
        [B(text="⏱ Интервалы", callback_data=f"t:menu:{cid}")],
        [B(text=f"{'🟢' if inline else '⚪️'} Инлайн-подпись", callback_data=f"c:inline:{cid}"),
         B(text="👁 Предпросмотр" if preview else "🙈 Без предпросмотра",
           callback_data=f"c:prev:{cid}")],
        [B(text="🗑 Удалить", callback_data=f"c:del:{cid}")],
        [B(text="⬅️ Назад", callback_data="c:list")],
    ])


def messages_kb(cid: int, items) -> K:
    rows = [[B(text=f"#{r['position']} · {r['mode']}", callback_data=f"m:open:{r['id']}")] for r in items]
    rows.append([B(text="➕ Добавить сообщение", callback_data=f"m:add:{cid}")])
    rows.append([B(text="⬅️ Назад", callback_data=f"c:open:{cid}")])
    return K(inline_keyboard=rows)


def message_card(mid: int, cid: int, mode: str) -> K:
    return K(inline_keyboard=[
        [B(text=f"Режим: {'копия' if mode == 'copy' else 'пересылка'}", callback_data=f"m:mode:{mid}")],
        [B(text="🗑 Удалить", callback_data=f"m:del:{mid}")],
        [B(text="⬅️ Назад", callback_data=f"m:list:{cid}")],
    ])


def preview_kb(mid: int) -> K:
    return K(inline_keyboard=[
        [B(text="👁 Показывать", callback_data=f"m:prev:1:{mid}"),
         B(text="🙈 Скрыть", callback_data=f"m:prev:0:{mid}")],
    ])


def groups_page(cid: int, rows, page: int, pages: int) -> K:
    kb = [[B(text=f"➕ {r['title'][:40]}", callback_data=f"g:add:{cid}:{r['id']}")] for r in rows]
    nav = []
    if page > 0:
        nav.append(B(text="⬅️", callback_data=f"g:list:{cid}:{page - 1}"))
    nav.append(B(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(B(text="➡️", callback_data=f"g:list:{cid}:{page + 1}"))
    kb.append(nav)
    kb.append([B(text="📋 Выбранные", callback_data=f"g:chosen:{cid}")])
    kb.append([B(text="⬅️ Назад", callback_data=f"c:open:{cid}")])
    return K(inline_keyboard=kb)


TOPIC_PAGE = 10


def topics_kb(cid: int, chat_id: int, topics, picked, page: int, pages: int) -> K:
    kb = []
    for t in topics:
        mark = "✅" if t["id"] in picked else "▫️"
        kb.append([B(text=f"{mark} {t['title'][:35]}",
                     callback_data=f"g:topic:{cid}:{chat_id}:{t['id']}")])

    nav = []
    if page > 0:
        nav.append(B(text="⬅️", callback_data=f"g:tpage:{cid}:{chat_id}:{page - 1}"))
    nav.append(B(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(B(text="➡️", callback_data=f"g:tpage:{cid}:{chat_id}:{page + 1}"))
    kb.append(nav)

    kb.append([B(text=f"💾 Сохранить ({len(picked)})", callback_data=f"g:save:{cid}:{chat_id}")])
    return K(inline_keyboard=kb))


def timing_kb(cid: int) -> K:
    return K(inline_keyboard=[
        [B(text="Между сообщениями", callback_data="noop")],
        [B(text="10с", callback_data=f"t:set:{cid}:dmin:10"),
         B(text="30с", callback_data=f"t:set:{cid}:dmin:30"),
         B(text="60с", callback_data=f"t:set:{cid}:dmin:60")],
        [B(text="Диапазон", callback_data="noop")],
        [B(text="10-25с", callback_data=f"t:rng:{cid}:10:25"),
         B(text="30-90с", callback_data=f"t:rng:{cid}:30:90")],
        [B(text="Между циклами", callback_data="noop")],
        [B(text="2м", callback_data=f"t:set:{cid}:cmin:120"),
         B(text="10м", callback_data=f"t:set:{cid}:cmin:600"),
         B(text="1ч", callback_data=f"t:set:{cid}:cmin:3600")],
        [B(text="⬅️ Назад", callback_data=f"c:open:{cid}")],
    ])


def accounts_kb(rows) -> K:
    kb = [[B(text=f"🗑 {r['name'] or r['phone']}", callback_data=f"acc:del:{r['id']}")] for r in rows]
    kb.append([B(text="➕ Добавить", callback_data="acc:add")])
    kb.append([B(text="⬅️ Назад", callback_data="menu")])
    return K(inline_keyboard=kb)
