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
