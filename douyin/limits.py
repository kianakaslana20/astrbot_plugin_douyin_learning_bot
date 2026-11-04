"""写操作的闸门：频率预算 + 静默时段。

为什么要落 sqlite 而不是记在内存里：

- 重载插件、重启 AstrBot 之后计数必须还在，否则「今天已经发了 12 条评论」这件事
  一重载就忘了，等于没有限制。
- 计数按「动作 × 时间窗」分桶：评论/分享/点赞按天，私信按小时。不同动作的风险不同：
  点赞便宜、分享中等、评论最贵（要过审），所以不能共用一个月配额。

静默时段的判断用本地时间，支持跨午夜（"23:00-07:00"）。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import datetime
from pathlib import Path


class Limits:
    """所有 reject 都返回"为什么"的中文原因，调用方直接拼进回执。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    # ------------------------------------------------------------------ 存储

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS rate_hit (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       kind TEXT NOT NULL,
                       ts REAL NOT NULL
                   )"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rate_kind ON rate_hit(kind, ts)")

    # -------------------------------------------------------------- 时间窗口

    @staticmethod
    def _since(seconds: float) -> float:
        return time.time() - seconds

    def count(self, kind: str, window_s: float) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) c FROM rate_hit WHERE kind=? AND ts>=?",
                (kind, self._since(window_s)),
            ).fetchone()
        return int(row["c"] if row else 0)

    async def count_async(self, kind: str, window_s: float) -> int:
        return await asyncio.to_thread(self.count, kind, window_s)

    def hit(self, kind: str) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO rate_hit(kind, ts) VALUES(?,?)", (kind, time.time()))

    async def hit_async(self, kind: str) -> None:
        await asyncio.to_thread(self.hit, kind)

    def prune(self, keep_s: float = 30 * 86400) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM rate_hit WHERE ts < ?", (self._since(keep_s),))

    # -------------------------------------------------------------- 静默时段

    @staticmethod
    def parse_quiet(spec: str) -> tuple[int, int] | None:
        """'01:00-08:00' -> (60, 480)；格式不对返回 None。"""
        raw = (spec or "").strip()
        if not raw or "-" not in raw:
            return None
        left, right = raw.split("-", 1)
        try:
            h1, m1 = [int(x) for x in left.strip().split(":")]
            h2, m2 = [int(x) for x in right.strip().split(":")]
        except (TypeError, ValueError):
            return None
        if not (0 <= h1 <= 24 and 0 <= h2 <= 24 and 0 <= m1 < 60 and 0 <= m2 < 60):
            return None
        return h1 * 60 + m1, h2 * 60 + m2

    def in_quiet_hours(self, spec: str, now: datetime | None = None) -> bool:
        window = self.parse_quiet(spec)
        if window is None:
            return False
        start, end = window
        now = now or datetime.now()
        cur = now.hour * 60 + now.minute
        if start == end:
            return False
        if start < end:
            return start <= cur < end
        return cur >= start or cur < end       # 跨午夜

    # ------------------------------------------------------------------ 判定

    def check(self, kind: str, settings) -> str:
        """返回空串 = 可以动手；否则是不动手的中文原因。"""
        if not getattr(settings, "rate_enabled", True):
            return ""
        quiet = getattr(settings, "quiet_hours", "") or ""
        if self.in_quiet_hours(quiet):
            return f"静默时段（{quiet}），不发任何东西"

        if kind == "dm_out":
            # 私信发送单独算配额：小时 + 天双窗口（能和点赞评论分开，也能共用同一个池）
            hour_cap = getattr(settings, "rate_dm_out_per_hour", 3)
            day_cap = getattr(settings, "rate_dm_out_per_day", 20)
            if not hour_cap or not day_cap:
                return "私信发送的上限设成 0（等于把主动发私信关掉了）"
            used_h = self.count("dm_out", 3600)
            used_d = self.count("dm_out", 86400)
            if used_h >= hour_cap:
                return f"这个小时已经主动发了 {used_h} 条私信，到上限 {hour_cap} 了"
            if used_d >= day_cap:
                return f"今天已经主动发了 {used_d} 条私信，到上限 {day_cap} 了"
            return ""

        caps = {
            "comment": (getattr(settings, "rate_comment_per_day", 8), 86400, "每天"),
            "share": (getattr(settings, "rate_share_per_day", 10), 86400, "每天"),
            "like": (getattr(settings, "rate_like_per_day", 30), 86400, "每天"),
            "dm": (getattr(settings, "rate_dm_per_hour", 6), 3600, "每小时"),
            "summary": (getattr(settings, "rate_summary_per_day", 20), 86400, "每天"),
        }
        cap, window, unit = caps.get(kind, (0, 86400, "每天"))
        if not cap:
            return f"{kind} 的频率上限设成 0（关掉了这个动作）"
        used = self.count(kind, window)
        if used >= cap:
            return f"{unit}已经做了 {used} 次{self._label(kind)}，到上限 {cap} 了"
        return ""

    async def check_async(self, kind: str, settings) -> str:
        return await asyncio.to_thread(self.check, kind, settings)

    @staticmethod
    def _label(kind: str) -> str:
        return {"comment": "评论", "share": "分享", "like": "点赞", "dm": "私信",
                "dm_out": "主动私信", "summary": "总结"}.get(kind, kind)

    def summary(self, settings) -> str:
        parts = []
        for kind, unit, win in (
            ("comment", "每天", 86400),
            ("share", "每天", 86400),
            ("like", "每天", 86400),
            ("dm", "每小时", 3600),
            ("summary", "每天", 86400),
        ):
            cap = {
                "comment": getattr(settings, "rate_comment_per_day", 8),
                "share": getattr(settings, "rate_share_per_day", 10),
                "like": getattr(settings, "rate_like_per_day", 30),
                "dm": getattr(settings, "rate_dm_per_hour", 6),
                "summary": getattr(settings, "rate_summary_per_day", 20),
            }[kind]
            parts.append(f"{self._label(kind)} {self.count(kind, win)}/{cap}{unit}")
        return "｜".join(parts)