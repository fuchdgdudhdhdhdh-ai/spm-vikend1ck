import json
import time

import aiosqlite

from settings import cfg

_conn: aiosqlite.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    tg_id INTEGER PRIMARY KEY,
    username TEXT,
    access_until INTEGER DEFAULT 0,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS accounts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    phone TEXT, session TEXT, tg_id INTEGER,
    name TEXT, username TEXT, status TEXT DEFAULT 'ok',
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS campaigns(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    account_id INTEGER,
    inline_mode INTEGER DEFAULT 0,
    via_bot TEXT DEFAULT '',
    link_preview INTEGER DEFAULT 1,
    delay_min INTEGER DEFAULT 10,
    delay_max INTEGER DEFAULT 10,
    cycle_min INTEGER DEFAULT 120,
    cycle_max INTEGER DEFAULT 120,
    status TEXT DEFAULT 'stopped',
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    position INTEGER DEFAULT 0,
    mode TEXT DEFAULT 'copy',
    text TEXT DEFAULT '',
    entities TEXT DEFAULT '[]',
    media TEXT,
    preview INTEGER DEFAULT NULL,
    buttons TEXT DEFAULT '[]',
    file_path TEXT,
    is_document INTEGER DEFAULT 0,
    src_chat INTEGER,
    src_msg INTEGER
);
CREATE TABLE IF NOT EXISTS targets(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    access_hash INTEGER,
    title TEXT,
    topic_ids TEXT DEFAULT '[]',
    UNIQUE(campaign_id, chat_id)
);
CREATE TABLE IF NOT EXISTS admins(
    tg_id INTEGER PRIMARY KEY,
    added_by INTEGER,
    created_at INTEGER
);
"""


async def init() -> None:
    global _conn
    _conn = await aiosqlite.connect(cfg.db_path)
    _conn.row_factory = aiosqlite.Row
    await _conn.executescript(SCHEMA)
    await _conn.commit()


async def close() -> None:
    if _conn:
        await _conn.close()
        _conn = None


def q() -> aiosqlite.Connection:
    assert _conn is not None, "db not initialised"
    return _conn


async def fetchone(sql: str, args=()):
    cur = await q().execute(sql, args)
    row = await cur.fetchone()
    await cur.close()
    return row


async def fetchall(sql: str, args=()):
    cur = await q().execute(sql, args)
    rows = await cur.fetchall()
    await cur.close()
    return rows


async def execute(sql: str, args=()):
    cur = await q().execute(sql, args)
    await q().commit()
    last = cur.lastrowid
    await cur.close()
    return last


# ---------- users ----------

async def touch_user(tg_id: int, username: str | None) -> None:
    if await fetchone("SELECT tg_id FROM users WHERE tg_id=?", (tg_id,)):
        await execute("UPDATE users SET username=? WHERE tg_id=?", (username, tg_id))
    else:
        await execute(
            "INSERT INTO users(tg_id, username, access_until, created_at) VALUES(?,?,0,?)",
            (tg_id, username, int(time.time())),
        )


async def get_user(tg_id: int):
    return await fetchone("SELECT * FROM users WHERE tg_id=?", (tg_id,))


async def user_by_account(tg_id: int):
    row = await fetchone("SELECT user_id FROM accounts WHERE tg_id=?", (tg_id,))
    return await get_user(row["user_id"]) if row else None


async def has_access(tg_id: int) -> bool:
    if tg_id in cfg.owners:
        return True
    row = await fetchone("SELECT access_until FROM users WHERE tg_id=?", (tg_id,))
    return bool(row and row["access_until"] > time.time())


async def grant_days(tg_id: int, days: int) -> int:
    row = await fetchone("SELECT access_until FROM users WHERE tg_id=?", (tg_id,))
    base = max(row["access_until"] if row else 0, int(time.time()))
    until = base + days * 86400
    await execute("UPDATE users SET access_until=? WHERE tg_id=?", (until, tg_id))
    return until


async def revoke(tg_id: int) -> None:
    await execute("UPDATE users SET access_until=0 WHERE tg_id=?", (tg_id,))


# ---------- admins ----------

async def is_admin(tg_id: int) -> bool:
    if tg_id in cfg.owners:
        return True
    return bool(await fetchone("SELECT 1 FROM admins WHERE tg_id=?", (tg_id,)))


async def add_admin(tg_id: int, by: int) -> None:
    await execute(
        "INSERT OR IGNORE INTO admins(tg_id, added_by, created_at) VALUES(?,?,?)",
        (tg_id, by, int(time.time())),
    )


async def del_admin(tg_id: int) -> None:
    await execute("DELETE FROM admins WHERE tg_id=?", (tg_id,))


async def list_admins():
    return await fetchall("SELECT tg_id FROM admins ORDER BY created_at")


# ---------- accounts ----------

async def add_account(user_id: int, phone: str, session: str, tg_id: int,
                      name: str, username: str | None) -> int:
    return await execute(
        "INSERT INTO accounts(user_id, phone, session, tg_id, name, username, created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (user_id, phone, session, tg_id, name, username, int(time.time())),
    )


async def get_account(acc_id: int):
    return await fetchone("SELECT * FROM accounts WHERE id=?", (acc_id,))


async def user_accounts(user_id: int):
    return await fetchall("SELECT * FROM accounts WHERE user_id=? ORDER BY id DESC", (user_id,))


async def set_account_status(acc_id: int, status: str) -> None:
    await execute("UPDATE accounts SET status=? WHERE id=?", (status, acc_id))


async def del_account(acc_id: int) -> None:
    await execute("DELETE FROM accounts WHERE id=?", (acc_id,))


# ---------- campaigns ----------

async def create_campaign(user_id: int, title: str, account_id: int) -> int:
    return await execute(
        "INSERT INTO campaigns(user_id, title, account_id, created_at) VALUES(?,?,?,?)",
        (user_id, title, account_id, int(time.time())),
    )


async def get_campaign(cid: int):
    return await fetchone("SELECT * FROM campaigns WHERE id=?", (cid,))


async def user_campaigns(user_id: int):
    return await fetchall("SELECT * FROM campaigns WHERE user_id=? ORDER BY id DESC", (user_id,))


async def by_title(user_id: int, title: str):
    return await fetchone(
        "SELECT * FROM campaigns WHERE user_id=? AND lower(title)=lower(?)", (user_id, title)
    )


async def set_campaign(cid: int, **fields) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    await execute(f"UPDATE campaigns SET {sets} WHERE id=?", (*fields.values(), cid))


async def del_campaign(cid: int) -> None:
    for row in await fetchall("SELECT file_path FROM messages WHERE campaign_id=?", (cid,)):
        from pathlib import Path
        if row["file_path"]:
            Path(row["file_path"]).unlink(missing_ok=True)
    await execute("DELETE FROM messages WHERE campaign_id=?", (cid,))
    await execute("DELETE FROM targets WHERE campaign_id=?", (cid,))
    await execute("DELETE FROM campaigns WHERE id=?", (cid,))


# ---------- messages ----------

async def add_message(cid: int, **data) -> int:
    pos = await fetchone(
        "SELECT COALESCE(MAX(position),0)+1 AS p FROM messages WHERE campaign_id=?", (cid,)
    )
    return await execute(
        "INSERT INTO messages(campaign_id, position, mode, text, entities, media, preview,"
        " buttons, file_path, is_document, src_chat, src_msg) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            cid, pos["p"], data.get("mode", "copy"), data.get("text", ""),
            data.get("entities", "[]"), data.get("media"), data.get("preview"),
            data.get("buttons", "[]"), data.get("file_path"),
            int(bool(data.get("is_document"))), data.get("src_chat"), data.get("src_msg"),
        ),
    )


async def campaign_messages(cid: int):
    return await fetchall("SELECT * FROM messages WHERE campaign_id=? ORDER BY position", (cid,))


async def get_message(mid: int):
    return await fetchone("SELECT * FROM messages WHERE id=?", (mid,))


async def set_message(mid: int, **fields) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    await execute(f"UPDATE messages SET {sets} WHERE id=?", (*fields.values(), mid))


async def del_message(mid: int) -> None:
    await execute("DELETE FROM messages WHERE id=?", (mid,))


# ---------- targets ----------

async def add_target(cid: int, chat_id: int, access_hash: int | None, title: str) -> int:
    row = await fetchone("SELECT id FROM targets WHERE campaign_id=? AND chat_id=?", (cid, chat_id))
    if row:
        return row["id"]
    return await execute(
        "INSERT INTO targets(campaign_id, chat_id, access_hash, title) VALUES(?,?,?,?)",
        (cid, chat_id, access_hash, title),
    )


async def campaign_targets(cid: int):
    return await fetchall("SELECT * FROM targets WHERE campaign_id=? ORDER BY id", (cid,))


async def set_topics(tid: int, topic_ids: list[int]) -> None:
    await execute("UPDATE targets SET topic_ids=? WHERE id=?", (json.dumps(topic_ids), tid))


async def del_target(tid: int) -> None:
    await execute("DELETE FROM targets WHERE id=?", (tid,))