"""跨平台记忆：让抖音私信里的"她"记得你在 QQ 上说过什么。

两条来源：
1. 抖音这边的私聊记录 —— 自己记（`plugin_data/.../dy.db` 的 `chat_log` 表）；
2. QQ 那边的聊天 —— 读 AstrBot 自己的 `data_v4.db`（`platform_message_history`），
   按 QQ 号过滤，只取纯文本，图片/语音/表情包一律跳过。

为什么要读宿主库而不是抄一份：AstrBot 本来就把每条消息落库了，抄一份只会两边不一致。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from astrbot.api import logger


def _texts_of(content: str) -> list[str]:
    """从 AstrBot 的消息 JSON 里抠出纯文本，别的成分跳过。"""
    out: list[str] = []
    try:
        data = json.loads(content)
    except Exception:
        return out
    if isinstance(data, list):  # 有些记录直接存成分段数组
        parts = data
    elif isinstance(data, dict):
        parts = data.get("message") or []
    else:
        return out
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "plain" and part.get("text"):
            txt = str(part["text"]).strip()
            # 图片/表情/语音在纯文本段里就是 [Image] 这种占位符，没用，别塞给模型
            if txt and not (txt.startswith("[") and txt.endswith("]")):
                out.append(txt)
    return [t for t in out if t]


class PersonMemory:
    """一个"人"的记忆。同一个人在不同平台共用一个 key。"""

    def __init__(self, db_path: Path, *, astrbot_db: Path | None = None) -> None:
        self.db_path = Path(db_path)
        self.astrbot_db = Path(astrbot_db) if astrbot_db else None
        self._init()

    # ---------------------------------------------------------------- 自己的表
    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS chat_log ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " person TEXT NOT NULL, role TEXT NOT NULL,"
                " text TEXT NOT NULL, platform TEXT NOT NULL DEFAULT '',"
                " ts REAL NOT NULL)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_person ON chat_log(person, id)")

    def remember(self, person: str, role: str, text: str, platform: str = "douyin") -> None:
        import time

        text = (text or "").strip()
        if not person or not text:
            return
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO chat_log(person, role, text, platform, ts) VALUES (?,?,?,?,?)",
                (person, role, text[:500], platform, time.time()),
            )

    def recent(self, person: str, limit: int = 6) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT role, text FROM chat_log WHERE person=? ORDER BY id DESC LIMIT ?",
                (person, max(1, limit)),
            ).fetchall()
        lines = []
        for r in reversed(rows):
            who = "我" if r["role"] == "me" else "对方"
            lines.append(f"{who}：{r['text']}")
        return lines

    def recent_me(self, person: str, limit: int = 30) -> set[str]:
        """我自己（含插件代发）最近说过的话，用来把自聊会话里的"回声"排掉。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT text FROM chat_log WHERE person=? AND role='me' ORDER BY id DESC LIMIT ?",
                (person, max(1, limit)),
            ).fetchall()
        return {str(r["text"]).strip() for r in rows if str(r["text"]).strip()}

    # ------------------------------------------------------------ 宿主的历史
    def _host_conn(self) -> sqlite3.Connection | None:
        path = self.astrbot_db
        if not path or not path.exists():
            return None
        conn = sqlite3.connect("file:%s?mode=ro" % path.as_posix(), uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def from_host(self, qq_id: str, limit: int = 10) -> list[str]:
        """从 AstrBot 的库里捞这个 QQ 号最近的对话（私聊 + 群里他说的）。"""
        if not qq_id:
            return []
        conn = self._host_conn()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT sender_id, content FROM platform_message_history"
                " WHERE user_id=? OR sender_id=? ORDER BY id DESC LIMIT ?",
                (str(qq_id), str(qq_id), max(40, limit * 8)),
            ).fetchall()
        except Exception as exc:  # 表结构变了也别把私聊搞挂
            logger.warning("读 QQ 历史失败：%s", exc)
            return []
        finally:
            conn.close()
        lines: list[str] = []
        for r in reversed(rows):
            texts = _texts_of(r["content"] or "")
            if not texts:
                continue
            who = "我" if str(r["sender_id"]) == "bot" else "对方"
            lines.append(f"{who}：{' '.join(texts)[:200]}")
        return lines[-max(1, limit):]

    # ---------------------------------------------------------------- 合成
    def background(
        self,
        *,
        person: str,
        qq_id: str = "",
        limit: int = 6,
        host_limit: int = 8,
    ) -> str:
        """给模型的一段"背景"，没有就返回空串。"""
        blocks: list[str] = []
        host = self.from_host(qq_id, host_limit) if qq_id else []
        if host:
            blocks.append("这个人你之前在 QQ 上聊过（同一个人，别装作不认识）：\n" + "\n".join(host))
        here = self.recent(person, limit)
        if here:
            blocks.append("你们在抖音私聊里最近的几句：\n" + "\n".join(here))
        return "\n\n".join(blocks)