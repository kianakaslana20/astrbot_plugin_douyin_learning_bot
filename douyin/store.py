"""SQLite 存储：去重、状态机、知识库检索。

放在插件数据目录（StarTools.get_data_dir）下的 dy.db，WAL 模式。
全部同步 sqlite3 调用，异步侧用 asyncio.to_thread 包一层。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path

from .feed import AwemeItem

SCHEMA = """
CREATE TABLE IF NOT EXISTS aweme (
    aweme_id      TEXT PRIMARY KEY,
    desc          TEXT DEFAULT '',
    author        TEXT DEFAULT '',
    duration_ms   INTEGER DEFAULT 0,
    width         INTEGER DEFAULT 0,
    height        INTEGER DEFAULT 0,
    digg          INTEGER DEFAULT 0,
    comment_count INTEGER DEFAULT 0,
    create_time   INTEGER DEFAULT 0,
    play_url      TEXT DEFAULT '',
    tags          TEXT DEFAULT '[]',
    status        TEXT DEFAULT 'new',
    score         REAL DEFAULT 0,
    transcript    TEXT DEFAULT '',
    visual        TEXT DEFAULT '',
    summary       TEXT DEFAULT '',
    keywords      TEXT DEFAULT '',
    verdict       TEXT DEFAULT '',
    first_seen    INTEGER DEFAULT 0,
    updated_at    INTEGER DEFAULT 0,
    error         TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_aweme_status ON aweme(status);
CREATE INDEX IF NOT EXISTS idx_aweme_seen ON aweme(first_seen DESC);

CREATE TABLE IF NOT EXISTS event (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     INTEGER,
    kind   TEXT,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS social_action (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER,
    aweme_id  TEXT,
    kind      TEXT,
    target    TEXT,
    ok        INTEGER DEFAULT 0,
    detail    TEXT DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_social_once
    ON social_action(aweme_id, kind, target);

-- 收件箱：私信里的视频链接、评论区 @ 我的提醒、私信消息，都从这里记账。
-- key 是幂等键（aweme_id / cid / msg_id），同一条只处理一次。
CREATE TABLE IF NOT EXISTS inbox (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,          -- dm_video / mention / dm_msg
    key        TEXT NOT NULL,
    src        TEXT DEFAULT '',        -- 谁发来的（昵称/会话名）
    payload    TEXT DEFAULT '',        -- JSON：链接、正文、时间等
    seen_at    INTEGER,
    handled_at INTEGER DEFAULT 0,      -- 0 = 还没处理
    result     TEXT DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_inbox_key ON inbox(kind, key);
CREATE INDEX IF NOT EXISTS idx_inbox_pending ON inbox(handled_at, seen_at DESC);

-- 回过谁：非自己人只回一次，靠这张表数次数（外人每 uid 一条，自己人也会记但不受限）
CREATE TABLE IF NOT EXISTS comment_reply_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    uid       TEXT NOT NULL,
    who       TEXT DEFAULT '',
    cid       TEXT DEFAULT '',
    aweme_id  TEXT DEFAULT '',
    at        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_crlog_uid ON comment_reply_log(uid);
CREATE INDEX IF NOT EXISTS idx_crlog_at ON comment_reply_log(at DESC);

-- 知识点卡片：从「看过」变成「记住」。summary 是流水，卡片才是能拿去说话的实体。
CREATE TABLE IF NOT EXISTS knowledge_card (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    aweme_id TEXT DEFAULT '',
    topic TEXT DEFAULT '',
    gist TEXT DEFAULT '',
    take TEXT DEFAULT '',
    keywords TEXT DEFAULT '',
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_card_time ON knowledge_card(created_at);

CREATE TABLE IF NOT EXISTS dm_judge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    type TEXT NOT NULL,
    from_nick TEXT NOT NULL,
    content TEXT DEFAULT '',
    decision TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_judge_time ON dm_judge(created_at DESC);

CREATE TABLE IF NOT EXISTS reply_cool (
    kind TEXT NOT NULL,
    who TEXT NOT NULL,
    ts REAL NOT NULL,
    count INTEGER DEFAULT 1,
    PRIMARY KEY (kind, who)
);

-- 私信发件箱：主动发出去的每一条都要留档。
-- 为什么单独一张表而不是塞进 event：这条记录要能被 /dy confirm 捞出来、被重试、
-- 被事后追查「到底发没发出去」，event 是流水，不适合当状态机。
CREATE TABLE IF NOT EXISTS dm_outbox (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    target_uid TEXT NOT NULL,
    text       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',  -- pending / sent / failed / skipped
    created_at REAL NOT NULL,
    sent_at    REAL,
    error      TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_dm_out_status ON dm_outbox(status, created_at);
"""

# status: new → picked → downloaded → analyzed / failed / skipped


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # ---------------------------------------------------------------- 收件箱

    def _inbox_seen_sync(self, kind: str, key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM inbox WHERE kind=? AND key=? LIMIT 1", (kind, key)
            ).fetchone()
        return row is not None

    async def inbox_seen(self, kind: str, key: str) -> bool:
        """这条是不是已经见过（含已处理）。"""
        return await asyncio.to_thread(self._inbox_seen_sync, kind, key)

    def _inbox_add_sync(self, kind: str, key: str, src: str, payload: str) -> bool:
        now = int(time.time())
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO inbox(kind, key, src, payload, seen_at, handled_at)
                   VALUES(?,?,?,?,?,0)""",
                (kind, key, src, payload, now),
            )
            return cur.rowcount > 0

    async def inbox_add(self, kind: str, key: str, src: str = "", payload: str = "") -> bool:
        """落一条待办；已经存在返回 False（幂等）。"""
        return await asyncio.to_thread(self._inbox_add_sync, kind, key, src, payload)

    def _inbox_pending_sync(self, kind: str, limit: int) -> list[dict]:
        sql = "SELECT * FROM inbox WHERE handled_at=0"
        args: list = []
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        sql += " ORDER BY seen_at ASC LIMIT ?"
        args.append(limit)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    async def inbox_pending(self, kind: str = "", limit: int = 20) -> list[dict]:
        return await asyncio.to_thread(self._inbox_pending_sync, kind, limit)

    # ---------- 评论回复计数（非自己人只回一次）----------

    def _reply_count_sync(self, uid: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM comment_reply_log WHERE uid=?", (str(uid),)
            ).fetchone()
            return int(row[0] if row else 0)

    async def reply_count(self, uid: str) -> int:
        """这个人我们回过几次了。"""
        if not uid:
            return 0
        return await asyncio.to_thread(self._reply_count_sync, uid)

    def _reply_hit_sync(self, uid: str, who: str, cid: str, aweme_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO comment_reply_log (uid, who, cid, aweme_id, at) VALUES (?,?,?,?,?)",
                (str(uid), who or "", cid or "", aweme_id or "", int(time.time())),
            )

    async def reply_hit(self, uid: str, who: str = "", cid: str = "", aweme_id: str = "") -> None:
        """记下「回过这个人一次」，用于非自己人的一次上限。"""
        if not uid:
            return
        await asyncio.to_thread(self._reply_hit_sync, uid, who, cid, aweme_id)

    def _inbox_done_sync(self, kind: str, key: str, result: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE inbox SET handled_at=?, result=? WHERE kind=? AND key=?",
                (int(time.time()), result[:400], kind, key),
            )

    async def inbox_done(self, kind: str, key: str, result: str = "") -> None:
        await asyncio.to_thread(self._inbox_done_sync, kind, key, result)

    def _inbox_stats_sync(self) -> dict:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT kind, COUNT(*) c, SUM(handled_at=0) p FROM inbox GROUP BY kind"
            ).fetchall()
        return {r["kind"]: {"total": r["c"], "pending": r["p"] or 0} for r in rows}

    async def inbox_stats(self) -> dict:
        return await asyncio.to_thread(self._inbox_stats_sync)

    # ------------------------------------------------------------ 判断记录
    def _judge_add_sync(self, event_id: str, type_: str, from_nick: str,
                        content: str, decision: str, reason: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO dm_judge(event_id, type, from_nick, content, decision, reason, created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (event_id, type_, from_nick, content[:600], decision, reason[:300], time.time()),
            )
            return int(cur.lastrowid or 0)

    async def judge_add(self, event_id: str, type_: str, from_nick: str,
                        content: str = "", decision: str = "", reason: str = "") -> int:
        return await asyncio.to_thread(
            self._judge_add_sync, event_id, type_, from_nick, content, decision, reason
        )

    def _judge_recent_sync(self, limit: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM dm_judge ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    async def judge_recent(self, limit: int = 10) -> list[dict]:
        return await asyncio.to_thread(self._judge_recent_sync, limit)

    def _judge_stats_sync(self) -> dict:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT decision, COUNT(*) c FROM dm_judge GROUP BY decision"
            ).fetchall()
        return {r["decision"] or "?": int(r["c"]) for r in rows}

    async def judge_stats(self) -> dict:
        return await asyncio.to_thread(self._judge_stats_sync)

    # ------------------------------------------------------------ 回话冷却
    def _cool_left_sync(self, kind: str, who: str, hours: float) -> float:
        if hours <= 0:
            return 0.0
        with self._connect() as conn:
            row = conn.execute(
                "SELECT ts FROM reply_cool WHERE kind=? AND who=?", (kind, who or "")
            ).fetchone()
        if not row:
            return 0.0
        left = float(row["ts"]) + hours * 3600 - time.time()
        return left if left > 0 else 0.0

    async def cool_left(self, kind: str, who: str, hours: float) -> float:
        """这个人还要等多少秒才能再回（0 = 现在就能回）。"""
        return await asyncio.to_thread(self._cool_left_sync, kind, who, hours)

    def _cool_hit_sync(self, kind: str, who: str) -> int:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO reply_cool(kind, who, ts, count) VALUES(?,?,?,1)
                   ON CONFLICT(kind, who) DO UPDATE SET ts=excluded.ts, count=reply_cool.count+1""",
                (kind, who or "", now),
            )
            row = conn.execute(
                "SELECT count FROM reply_cool WHERE kind=? AND who=?", (kind, who or "")
            ).fetchone()
        return int(row["count"]) if row else 1

    async def cool_hit(self, kind: str, who: str) -> int:
        """记一次「刚回过他」，返回累计回过几次。"""
        return await asyncio.to_thread(self._cool_hit_sync, kind, who)

    def _cool_reset_sync(self, kind: str, who: str) -> int:
        with self._connect() as conn:
            if who:
                cur = conn.execute("DELETE FROM reply_cool WHERE kind=? AND who=?", (kind, who))
            else:
                cur = conn.execute("DELETE FROM reply_cool WHERE kind=?", (kind,))
            return int(cur.rowcount or 0)

    async def cool_reset(self, kind: str, who: str = "") -> int:
        return await asyncio.to_thread(self._cool_reset_sync, kind, who)

    def _cool_list_sync(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT kind, who, ts, count FROM reply_cool ORDER BY ts DESC LIMIT 30"
            ).fetchall()
        return [dict(r) for r in rows]

    async def cool_list(self) -> list[dict]:
        return await asyncio.to_thread(self._cool_list_sync)

    # ------------------------------------------------------------ 私信发件箱

    def _dm_queue_sync(self, target_uid: str, text: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO dm_outbox(target_uid, text, status, created_at) VALUES(?,?,?,?)",
                (target_uid, text, "pending", time.time()),
            )
            return int(cur.lastrowid or 0)

    async def dm_queue(self, target_uid: str, text: str) -> int:
        """进待发队列，返回这条的 id。"""
        return await asyncio.to_thread(self._dm_queue_sync, target_uid, text)

    def _dm_pending_sync(self, limit: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM dm_outbox WHERE status='pending' ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    async def dm_pending(self, limit: int = 20) -> list[dict]:
        return await asyncio.to_thread(self._dm_pending_sync, limit)

    def _dm_mark_sync(self, row_id: int, status: str, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE dm_outbox SET status=?, error=?, sent_at=? WHERE id=?",
                (status, error[:300], time.time() if status == "sent" else None, row_id),
            )

    async def dm_mark(self, row_id: int, status: str, error: str = "") -> None:
        await asyncio.to_thread(self._dm_mark_sync, row_id, status, error)

    def _dm_stats_sync(self) -> dict:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) c FROM dm_outbox GROUP BY status"
            ).fetchall()
        return {r["status"]: int(r["c"]) for r in rows}

    # ---------- 知识点卡片 ----------

    def _card_add_sync(self, aweme_id: str, topic: str, gist: str, take: str, keywords: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO knowledge_card(aweme_id, topic, gist, take, keywords, created_at) "
                "VALUES(?,?,?,?,?,?)",
                (aweme_id, topic, gist, take, keywords, time.time()),
            )
            return int(cur.lastrowid or 0)

    async def card_add(self, aweme_id: str, topic: str, gist: str, take: str,
                       keywords: str = "") -> int:
        return await asyncio.to_thread(
            self._card_add_sync, aweme_id, topic, gist, take, keywords
        )

    def _card_search_sync(self, words: list[str], limit: int) -> list[dict]:
        words = [w.strip() for w in words if w and w.strip()][:6]
        if not words:
            return []
        where = " OR ".join(["(keywords LIKE ? OR gist LIKE ? OR topic LIKE ?)"] * len(words))
        args: list = []
        for w in words:
            like = f"%{w}%"
            args.extend([like, like, like])
        sql = (
            "SELECT aweme_id, topic, gist, take, keywords FROM knowledge_card "
            f"WHERE {where} ORDER BY created_at DESC LIMIT ?"
        )
        args.append(max(1, int(limit)))
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(sql, args).fetchall()]

    async def card_search(self, words: list[str], limit: int = 3) -> list[dict]:
        return await asyncio.to_thread(self._card_search_sync, words, limit)

    def _card_recent_sync(self, limit: int, since: float = 0.0) -> list[dict]:
        sql = ("SELECT aweme_id, topic, gist, take, keywords, created_at "
               "FROM knowledge_card WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?")
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(sql, (since, max(1, int(limit)))).fetchall()]

    async def card_recent(self, limit: int = 10, since: float = 0.0) -> list[dict]:
        return await asyncio.to_thread(self._card_recent_sync, limit, since)

    def _card_count_sync(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM knowledge_card").fetchone()[0])

    async def card_count(self) -> int:
        return await asyncio.to_thread(self._card_count_sync)

    def _card_delete_sync(self, card_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM knowledge_card WHERE id=?", (int(card_id),))
            return cur.rowcount > 0

    async def card_delete(self, card_id: int) -> bool:
        return await asyncio.to_thread(self._card_delete_sync, card_id)

    def _actions_recent_sync(self, limit: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT id, ts, aweme_id, kind, target, ok, detail
                   FROM social_action ORDER BY id DESC LIMIT ?""",
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    async def actions_recent(self, limit: int = 20) -> list[dict]:
        return await asyncio.to_thread(self._actions_recent_sync, limit)

    async def dm_stats(self) -> dict:
        return await asyncio.to_thread(self._dm_stats_sync)

    # -------------------------------------------------------------- 保留期

    def _prune_sync(self, keep_days: int) -> dict:
        """删掉超过保留期的记录，返回删了多少。media 文件由调用方顺手清。"""
        if keep_days <= 0:
            return {"aweme": 0, "event": 0, "inbox": 0, "social_action": 0}
        cutoff = int(time.time()) - keep_days * 86400
        out = {}
        with self._connect() as conn:
            for table, col in (
                ("aweme", "first_seen"),
                ("event", "ts"),
                ("social_action", "ts"),
            ):
                cur = conn.execute(f"DELETE FROM {table} WHERE {col} < ?", (cutoff,))
                out[table] = cur.rowcount
            cur = conn.execute(
                "DELETE FROM inbox WHERE handled_at>0 AND handled_at < ?", (cutoff,)
            )
            out["inbox"] = cur.rowcount
        return out

    async def prune(self, keep_days: int) -> dict:
        return await asyncio.to_thread(self._prune_sync, keep_days)

    def _init_sync(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    async def init(self) -> None:
        await asyncio.to_thread(self._init_sync)

    # ------------------------------------------------------------------ 写入

    def _upsert_sync(self, item: AwemeItem, status: str, score: float) -> None:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO aweme (aweme_id, desc, author, duration_ms, width, height,
                                   digg, comment_count, create_time, play_url, tags,
                                   status, score, first_seen, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(aweme_id) DO UPDATE SET
                    desc=excluded.desc, author=excluded.author,
                    duration_ms=excluded.duration_ms, digg=excluded.digg,
                    comment_count=excluded.comment_count, play_url=excluded.play_url,
                    tags=excluded.tags, updated_at=excluded.updated_at
                """,
                (
                    item.aweme_id, item.desc, item.author, item.duration_ms,
                    item.width, item.height, item.digg, item.comment_count,
                    item.create_time, item.play_url,
                    json.dumps(item.tags, ensure_ascii=False),
                    status, score, now, now,
                ),
            )

    async def upsert(self, item: AwemeItem, status: str = "new", score: float = 0.0) -> None:
        async with self._lock:
            await asyncio.to_thread(self._upsert_sync, item, status, score)

    def _mark_sync(self, aweme_id: str, status: str, fields: dict) -> None:
        allowed = {"transcript", "visual", "summary", "keywords", "verdict", "error", "score", "play_url"}
        sets = ["status=?", "updated_at=?"]
        params: list = [status, int(time.time())]
        for key, value in fields.items():
            if key in allowed:
                sets.append(f"{key}=?")
                params.append(value)
        params.append(aweme_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE aweme SET {', '.join(sets)} WHERE aweme_id=?", params)

    async def mark(self, aweme_id: str, status: str, **fields) -> None:
        async with self._lock:
            await asyncio.to_thread(self._mark_sync, aweme_id, status, fields)

    def _log_sync(self, kind: str, detail: str) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO event (ts, kind, detail) VALUES (?,?,?)",
                         (int(time.time()), kind, detail[:2000]))

    async def log(self, kind: str, detail: str = "") -> None:
        try:
            await asyncio.to_thread(self._log_sync, kind, detail)
        except Exception:
            pass

    # ------------------------------------------------------------------ 读取

    def _seen_sync(self, aweme_ids: list[str]) -> set[str]:
        if not aweme_ids:
            return set()
        marks = ",".join("?" for _ in aweme_ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT aweme_id FROM aweme WHERE aweme_id IN ({marks})", aweme_ids
            ).fetchall()
        return {row["aweme_id"] for row in rows}

    async def seen(self, aweme_ids: list[str]) -> set[str]:
        return await asyncio.to_thread(self._seen_sync, aweme_ids)

    def _recent_sync(self, limit: int, only_done: bool) -> list[dict]:
        sql = "SELECT * FROM aweme"
        if only_done:
            sql += " WHERE status='analyzed'"
        sql += " ORDER BY first_seen DESC LIMIT ?"
        with self._connect() as conn:
            rows = conn.execute(sql, (limit,)).fetchall()
        return [dict(row) for row in rows]

    async def recent(self, limit: int = 5, only_done: bool = True) -> list[dict]:
        return await asyncio.to_thread(self._recent_sync, limit, only_done)

    def _stats_sync(self) -> dict:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) c FROM aweme").fetchone()["c"]
            done = conn.execute("SELECT COUNT(*) c FROM aweme WHERE status='analyzed'").fetchone()["c"]
        return {"total": total, "analyzed": done}

    async def stats(self) -> dict:
        return await asyncio.to_thread(self._stats_sync)

    def _search_sync(self, keyword: str, limit: int) -> list[dict]:
        like = f"%{keyword}%"
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT aweme_id, desc, author, summary, keywords, transcript
                   FROM aweme
                   WHERE status='analyzed' AND (desc LIKE ? OR summary LIKE ?
                         OR keywords LIKE ? OR transcript LIKE ?)
                   ORDER BY first_seen DESC LIMIT ?""",
                (like, like, like, like, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    async def search(self, keyword: str, limit: int = 5) -> list[dict]:
        return await asyncio.to_thread(self._search_sync, keyword, limit)

    # ------------------------------------------------------------- 写操作留痕

    def _acted_sync(self, aweme_id: str, kind: str, target: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT ok FROM social_action WHERE aweme_id=? AND kind=? AND target=?",
                (aweme_id, kind, target),
            ).fetchone()
        return bool(row and row["ok"])

    async def acted(self, aweme_id: str, kind: str, target: str) -> bool:
        """这条视频对这个目标是不是已经成功操作过（防重复发送）。"""
        try:
            return await asyncio.to_thread(self._acted_sync, aweme_id, kind, target)
        except Exception:
            return False

    def _record_action_sync(self, aweme_id: str, kind: str, target: str, ok: bool, detail: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO social_action (ts, aweme_id, kind, target, ok, detail)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(aweme_id, kind, target) DO UPDATE SET
                       ts=excluded.ts, ok=excluded.ok, detail=excluded.detail""",
                (int(time.time()), aweme_id, kind, target, 1 if ok else 0, detail[:500]),
            )

    async def record_action(self, aweme_id: str, kind: str, target: str, ok: bool, detail: str = "") -> None:
        try:
            await asyncio.to_thread(self._record_action_sync, aweme_id, kind, target, ok, detail)
        except Exception:
            pass
