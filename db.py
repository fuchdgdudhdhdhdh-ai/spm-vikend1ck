import json
import time

import asyncpg

from settings import cfg

_pool: asyncpg.Pool | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    tg_id BIGINT PRIMARY KEY,
    username TEXT,
    access_until BIGINT DEFAULT 0,
    created_at BIGINT
);
CREATE TABLE IF NOT EXISTS accounts(
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    phone TEXT, session TEXT, tg_id BIGINT,
    name TEXT, username TEXT, status TEXT DEFAULT 'ok',
    created_at BIGINT
);
CREATE TABLE IF NOT EXISTS campaigns(
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    title TEXT NOT NULL,
    account_id BIGINT,
    inline_mode SMALLINT DEFAULT 0,
    via_bot TEXT DEFAULT '',
    link_preview SMALLINT DEFAULT 1,
    delay_min INT DEFAULT 10,
    delay_max INT DEFAULT 10,
    cycle_min INT DEFAULT 120,
    cycle_max INT DEFAULT 120,
    status TEXT DEFAULT 'stopped',
    created_at BIGINT
);
CREATE TABLE IF NOT EXISTS messages(
    id BIGSERIAL PRIMARY KEY,
    campaign_id BIGINT NOT NULL,
    position INT DEFAULT 0,
    mode TEXT DEFAULT 'copy',
    text TEXT DEFAULT '',
    entities TEXT DEFAULT '[]',
    preview SMALLINT DEFAULT NULL,
    buttons TEXT DEFAULT '[]',
    saved_chat BIGINT,
    saved_msg BIGINT,
    is_document SMALLINT DEFAULT 0
);
CREATE TABLE IF NOT EXISTS targets(
    id BIGSERIAL PRIMARY KEY,
    campaign_id BIGINT NOT NULL,
    chat_id BIGINT NOT NULL,
    access_hash BIGINT,
    title TEXT,
    topic_ids TEXT DEFAULT '[]',
    UNIQUE(campaign_id, chat_id)
);
CREATE TABLE IF NOT EXISTS admins(
    tg_id BIGINT PRIMARY KEY,
    added_by BIGINT,
    created_at BIGINT
);
CREATE INDEX IF NOT EXISTS idx_messages_campaign ON messages(campaign_id);
CREATE INDEX IF NOT EXISTS idx_targets_campaign ON targets(campaign_id);
"""


async def init() -> None:
    global _pool
    if not cfg.database_url:
        raise SystemExit("DATABASE_URL не задан")
    _pool = await asyncpg.create_pool(cfg.database_url, min_size=1, max_size=5, command_timeout=30)
    async with _pool.acquire() as conn:
        await conn.execute(SCHEMA)


async def close() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    assert _pool is not None, "db not initialised"
    return _pool


async def fetchone(sql: str, *args):
    return await pool().fetchrow(sql, *args)


async def fetchall(sql: str, *args):
    return await pool().fetch(sql, *args)


async def execute(sql: str, *args):
    return await pool().execute(sql, *args)


async def fetchval(sql: str, *args):
    return await pool().fetchval(sql, *args)


# ---------- users ----------

async def touch_user(tg_id: int, username: str | None) -> None:
    await execute(
        "INSERT INTO users(tg_id, username, access_until, created_at) VALUES($1,$2,0,$3)"
        " ON CONFLICT (tg_id) DO UPDATE SET username = EXCLUDED.username",
        tg_id, username, int(time.time()),
    )


async def get_user(tg_id: int):
    return await fetchone("SELECT * FROM users WHERE tg_id=$1", tg_id)


async def user_by_account(tg_id: int):
    row = await fetchone("SELECT user_id FROM accounts WHERE tg_id=$1", tg_id)
    return await get_user(row["user_id"]) if row else None


async def has_access(tg_id: int) -> bool:
    if tg_id in cfg.owners:
        return True
    until = await fetchval("SELECT access_until FROM users WHERE tg_id=$1", tg_id)
    return bool(until and until > time.time())


async def grant_days(tg_id: int, days: int) -> int:
    until = await fetchval("SELECT access_until FROM users WHERE tg_id=$1", tg_id) or 0
    new = max(until, int(time.time())) + days * 86400
    await execute("UPDATE users SET access_until=$1 WHERE tg_id=$2", new, tg_id)
    return new


async def revoke(tg_id: int) -> None:
    await execute("UPDATE users SET access_until=0 WHERE tg_id=$1", tg_id)


# ---------- admins ----------

async def is_admin(tg_id: int) -> bool:
    if tg_id in cfg.owners:
        return True
    return bool(await fetchval("SELECT 1 FROM admins WHERE tg_id=$1", tg_id))


async def add_admin(tg_id: int, by: int) -> None:
    await execute(
        "INSERT INTO admins(tg_id, added_by, created_at) VALUES($1,$2,$3) ON CONFLICT DO NOTHING",
        tg_id, by, int(time.time()),
    )


async def del_admin(tg_id: int) -> None:
    await execute("DELETE FROM admins WHERE tg_id=$1", tg_id)


async def list_admins():
    return await fetchall("SELECT tg_id FROM admins ORDER BY created_at")


# ---------- accounts ----------

async def add_account(user_id: int, phone: str, session: str, tg_id: int,
                      name: str, username: str | None) -> int:
    return await fetchval(
        "INSERT INTO accounts(user_id, phone, session, tg_id, name, username, created_at)"
        " VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING id",
        user_id, phone, session, tg_id, name, username, int(time.time()),
    )


async def get_account(acc_id: int):
    return await fetchone("SELECT * FROM accounts WHERE id=$1", acc_id)


async def user_accounts(user_id: int):
    return await fetchall("SELECT * FROM accounts WHERE user_id=$1 ORDER BY id DESC", user_id)


async def set_account_status(acc_id: int, status: str) -> None:
    await execute("UPDATE accounts SET status=$1 WHERE id=$2", status, acc_id)


async def del_account(acc_id: int) -> None:
    await execute("DELETE FROM accounts WHERE id=$1", acc_id)


# ---------- campaigns ----------

async def create_campaign(user_id: int, title: str, account_id: int) -> int:
    return await fetchval(
        "INSERT INTO campaigns(user_id, title, account_id, created_at) VALUES($1,$2,$3,$4) RETURNING id",
        user_id, title, account_id, int(time.time()),
    )


async def get_campaign(cid: int):
    return await fetchone("SELECT * FROM campaigns WHERE id=$1", cid)


async def user_campaigns(user_id: int):
    return await fetchall("SELECT * FROM campaigns WHERE user_id=$1 ORDER BY id DESC", user_id)


async def by_title(user_id: int, title: str):
    return await fetchone(
        "SELECT * FROM campaigns WHERE user_id=$1 AND lower(title)=lower($2)", user_id, title
    )


async def set_campaign(cid: int, **fields) -> None:
    if not fields:
        return
    parts = [f"{k}=${i + 2}" for i, k in enumerate(fields)]
    await execute(f"UPDATE campaigns SET {', '.join(parts)} WHERE id=$1", cid, *fields.values())


async def del_campaign(cid: int) -> None:
    await execute("DELETE FROM messages WHERE campaign_id=$1", cid)
    await execute("DELETE FROM targets WHERE campaign_id=$1", cid)
    await execute("DELETE FROM campaigns WHERE id=$1", cid)


# ---------- messages ----------

async def add_message(cid: int, **data) -> int:
    pos = await fetchval(
        "SELECT COALESCE(MAX(position),0)+1 FROM messages WHERE campaign_id=$1", cid
    )
    return await fetchval(
        "INSERT INTO messages(campaign_id, position, mode, text, entities, preview, buttons,"
        " saved_chat, saved_msg, is_document)"
        " VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING id",
        cid, pos, data.get("mode", "copy"), data.get("text", ""), data.get("entities", "[]"),
        data.get("preview"), data.get("buttons", "[]"), data.get("saved_chat"),
        data.get("saved_msg"), int(bool(data.get("is_document"))),
    )


async def campaign_messages(cid: int):
    return await fetchall("SELECT * FROM messages WHERE campaign_id=$1 ORDER BY position", cid)


async def get_message(mid: int):
    return await fetchone("SELECT * FROM messages WHERE id=$1", mid)


async def set_message(mid: int, **fields) -> None:
    if not fields:
        return
    parts = [f"{k}=${i + 2}" for i, k in enumerate(fields)]
    await execute(f"UPDATE messages SET {', '.join(parts)} WHERE id=$1", mid, *fields.values())


async def del_message(mid: int) -> None:
    await execute("DELETE FROM messages WHERE id=$1", mid)


# ---------- targets ----------

async def add_target(cid: int, chat_id: int, access_hash: int | None, title: str) -> int:
    existing = await fetchval("SELECT id FROM targets WHERE campaign_id=$1 AND chat_id=$2", cid, chat_id)
    if existing:
        return existing
    return await fetchval(
        "INSERT INTO targets(campaign_id, chat_id, access_hash, title) VALUES($1,$2,$3,$4) RETURNING id",
        cid, chat_id, access_hash, title,
    )


async def campaign_targets(cid: int):
    return await fetchall("SELECT * FROM targets WHERE campaign_id=$1 ORDER BY id", cid)


async def set_topics(tid: int, topic_ids: list[int]) -> None:
    await execute("UPDATE targets SET topic_ids=$1 WHERE id=$2", json.dumps(topic_ids), tid)


async def del_target(tid: int) -> None:
    await execute("DELETE FROM targets WHERE id=$1", tid)
