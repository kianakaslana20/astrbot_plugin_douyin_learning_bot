"""收件箱：将私信、评论/回复和 @ 通知归一为条目。

既保留界面采集，也提供规范 WS dict → Entry 的纯转换接口。
DOM 扩展与 WS 转换由 synthetic fixtures 验证，未连接线上/浏览器验证。

去重靠 `Store.inbox_add()` 的 `UNIQUE(kind, key)`，所以重复扫不会重复处理、也不会重复回人。
"""

from __future__ import annotations

import hashlib

from dataclasses import dataclass, field

from astrbot.api import logger

from .feed import AwemeItem, parse_aweme
from .im import DirectMessages, aweme_id_of
from .notice import Mention, Notices

DETAIL_FN = """
async (awemeId) => {
  const q = new URLSearchParams({
    aweme_id: awemeId,
    device_platform: 'webapp', aid: '6383', channel: 'channel_pc_web',
    pc_client_type: '1', version_code: '190500', version_name: '19.5.0',
    cookie_enabled: 'true', screen_width: '1600', screen_height: '900',
    browser_language: 'zh-CN', browser_platform: 'Win32',
    browser_name: 'Chrome', browser_version: '124.0.0.0',
    platform: 'PC', downlink: '10',
  });
  const r = await fetch('/aweme/v1/web/aweme/detail/?' + q.toString(),
                        {credentials: 'include'});
  const t = await r.text();
  let j = null; try { j = JSON.parse(t); } catch (e) {}
  return {code: j ? j.status_code : -1, status: r.status,
          aweme: (j && j.aweme_detail) || null};
}
"""


@dataclass
class Entry:
    kind: str                    # dm_text / dm_share / mention / comment
    key: str = ""                # 幂等键
    aweme_id: str = ""
    who: str = ""
    text: str = ""
    when: str = ""
    src: str = ""                # 会话名 / 提醒原文
    extra: dict = field(default_factory=dict)

    def describe(self) -> str:
        if self.kind == "dm_share":
            head = f"私信 · {self.who or '有人'} 发来一条视频"
        elif self.kind == "dm_text":
            head = f"私信 · {self.who or '有人'} 说"
        elif self.kind == "comment":
            head = f"评论提醒 · {self.who or '有人'} 评论/回复了你"
        else:
            head = f"评论提醒 · {self.who or '有人'} 在评论里 @ 了你"
        if self.text:
            head += f"：「{self.text[:36]}」"
        if self.aweme_id:
            head += f"\n　　https://www.douyin.com/video/{self.aweme_id}"
        return head


class Inbox:
    """收件箱：私信 + @ 提醒。self.sent_lookup 由 main 挂上，自聊会话靠它去回声。"""

    sent_lookup = None  # Callable[[str, int], set[str]] | None
    """扫两条来源。一个实例跟一个 browser 走。"""

    def __init__(self, browser, settings=None):
        self.browser = browser
        self.settings = settings
        self.dm = DirectMessages(browser)
        self.notices = Notices(browser)

    @staticmethod
    def event_entry(evt: dict) -> Entry | None:
        """规范 WS 事件转 Entry；缺失字段留空，extra.missing_fields 交给后续补齐。

        接受 type（或 kind）、from_nick、content、timestamp；兼容 text/time。
        event_id 优先用作幂等键；仅 comment_id 可充当评论的原始键。
        没有原始 id 时 key 留空，不把一整帧或昵称/正文冒充事件 id。
        """
        if not isinstance(evt, dict):
            return None
        kind = {"dm": "dm_text", "share": "dm_share", "dm_text": "dm_text",
                "dm_share": "dm_share", "mention": "mention", "comment": "comment"}.get(
                    evt.get("type") or evt.get("kind"))
        if not kind:
            return None

        def value(name, fallback=None):
            raw = evt.get(name)
            if raw is None and fallback:
                raw = evt.get(fallback)
            return str(raw).strip() if isinstance(raw, (str, int, float)) and not isinstance(raw, bool) else ""

        extra = dict(evt.get("extra")) if isinstance(evt.get("extra"), dict) else {}
        for name in ("event_id", "raw_frame_id", "from_uid", "comment_id", "timestamp"):
            extra[name] = evt.get(name, "")
        if "timestamp" not in evt and "time" in evt:
            extra["timestamp"] = evt["time"]
        key = value("event_id")
        if not key and kind in {"mention", "comment"}:
            key = value("comment_id")
        text = value("content", "text")
        uid = value("from_uid")
        missing = [name for name, val in (("from_uid", uid), ("text", text)) if not val]
        if not key:
            missing.append("event_id")
        if kind in {"mention", "comment"} and not value("comment_id"):
            missing.append("comment_id")
        if kind in {"dm_share", "mention", "comment"} and not value("aweme_id"):
            missing.append("aweme_id")
        extra["missing_fields"] = missing
        extra["pending_enrichment"] = bool(missing)
        return Entry(kind=kind, key=key, aweme_id=value("aweme_id"),
                     who=value("from_nick"), text=text, when=value("timestamp", "time"),
                     src=value("src"), extra=extra)

    # ------------------------------------------------------------------ 采集

    async def collect(
        self,
        page,
        *,
        include_dm: bool = True,
        include_mentions: bool = True,
        max_conversations: int = 8,
        max_cards: int = 3,
    ) -> list[Entry]:
        out: list[Entry] = []
        if include_dm:
            try:
                out.extend(await self.collect_dm(page, max_conversations, max_cards))
            except Exception as exc:  # noqa: BLE001
                logger.warning("扫私信失败：%s", exc)
        if include_mentions:
            try:
                out.extend(await self.collect_mentions(page))
            except Exception as exc:  # noqa: BLE001
                logger.warning("扫提醒失败：%s", exc)
        return out

    def _self_names(self) -> list[str]:
        raw = getattr(self.settings, "dm_self_names", "") or ""
        raw = raw.replace("，", ",").replace("、", ",")
        return [x.strip() for x in raw.split(",") if x.strip()]

    async def collect_dm(self, page, max_conversations: int = 8, max_cards: int = 3) -> list[Entry]:
        convs = await self.dm.conversations(page)
        if not convs:
            return []
        # 分享会话一定要看；开了日常聊天时，白名单里的普通会话也要看
        chat_on = bool(getattr(self.settings, "dm_chat_enabled", False))
        chat_allow = _names(getattr(self.settings, "dm_chat_allow", "") or "")
        targets = [c for c in convs if c.looks_share]
        if chat_on:
            for c in convs:
                if c not in targets and _allowed(c.name, chat_allow):
                    targets.append(c)
        targets = targets[:max_conversations]
        out: list[Entry] = []
        for conv in targets:
            if not await self.dm.open_conversation(page, conv.name):
                logger.info("私信：进不去「%s」，跳过", conv.name)
                continue
            msgs = await self.dm.messages(page, resolve_share=True, limit=max_cards)
            for m in self.dm.pick_shares(msgs):
                out.append(
                    Entry(
                        kind="dm_share",
                        key=f"dm:{m.aweme_id}",
                        aweme_id=m.aweme_id,
                        who=conv.name,
                        text=m.text,
                        when=conv.when,
                        src=conv.name,
                    )
                )
            if chat_on and _allowed(conv.name, chat_allow):
                self_chat = any(
                    a == conv.name or a in conv.name or conv.name in a for a in self._self_names()
                )
                echo = set()
                if self_chat and callable(self.sent_lookup):
                    echo = self.sent_lookup(conv.name, 30) or set()
                for m in _incoming_texts(
                    msgs, limit=max_cards, echo=echo, self_chat=self_chat
                ):
                    out.append(
                        Entry(
                            kind="dm_text",
                            key="dt:" + _digest(conv.name + "|" + m.text[:60]),
                            who=conv.name,
                            text=m.text,
                            when=conv.when,
                            src=conv.name,
                        )
                    )
        return out

    async def collect_mentions(self, page) -> list[Entry]:
        mentions: list[Mention] = await self.notices.mentions(page)
        out: list[Entry] = []
        for mt in mentions:
            aid = mt.aweme_id or await self.notices.open_mention(page, mt)
            if not aid:
                # 同一个人的同一句话只记一次，拿不到视频 id 也留着，方便主人自己点
                aid = ""
            kind = getattr(mt, "kind", "mention")
            event_id = getattr(mt, "event_id", "")
            comment_id = getattr(mt, "comment_id", "")
            uid = getattr(mt, "uid", "")
            missing = [name for name, value in (("from_uid", uid), ("text", mt.text),
                       ("comment_id", comment_id), ("aweme_id", aid)) if not value]
            out.append(
                Entry(
                    kind=kind,
                    key=event_id or comment_id or f"{'mt' if kind == 'mention' else 'cm'}:{aid or mt.who}:{mt.text[:16]}",
                    aweme_id=aid,
                    who=mt.who,
                    text=mt.text,
                    when=mt.when,
                    src=mt.raw,
                    extra={"event_id": event_id, "raw_frame_id": "", "from_uid": uid,
                           "comment_id": comment_id, "timestamp": "",
                           "missing_fields": missing, "pending_enrichment": bool(missing)},
                )
            )
        return out

    # ------------------------------------------------------------ 取视频元数据

    async def fetch_item(self, page, aweme_id: str) -> AwemeItem | None:
        """按 id 拿一条视频的元信息（标题/作者/直链/时长…），给「下载来看」用。"""
        aweme_id = (aweme_id or "").strip()
        if not aweme_id:
            return None
        try:
            res = await page.evaluate(DETAIL_FN, aweme_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("取视频详情失败（%s）：%s", aweme_id, exc)
            return None
        if not isinstance(res, dict) or not res.get("aweme"):
            logger.info("取视频详情空回包（%s）：code=%s", aweme_id, (res or {}).get("code"))
            return None
        item = parse_aweme(res["aweme"])
        if item is None:
            logger.info("视频 %s 解析不出来（可能图集或已下架）", aweme_id)
        return item


def aweme_of(text: str) -> str:
    return aweme_id_of(text)


__all__ = ["Entry", "Inbox", "aweme_of"]


def _names(spec: str) -> list[str]:
    if not spec:
        return []
    for sep in ("，", "、", ";", "；", "/", "|"):
        spec = spec.replace(sep, ",")
    return [x.strip() for x in spec.split(",") if x.strip()]


def _allowed(name: str, allow: list[str]) -> bool:
    """白名单为空 = 谁都不回（日常聊天比分享敏感，宁可不动）。"""
    if not allow:
        return False
    return any(a == name or a in name or name in a for a in allow)


def _incoming_texts(
    msgs: list, limit: int = 3, echo: set | None = None, self_chat: bool = False
) -> list:
    """只取别人发来的、有内容的纯文本，最后 limit 条。

    self_chat：自己跟自己的会话。那种会话两边都标成"我发的"，只能反过来认：
      不在 echo（我自己发过的话）里的，就是对方刚打的。
      注意 echo 可能是空集（插件还没说过话），这时**不能**当成"全是我发的"。
    """
    echo = echo or set()
    out = []
    for m in msgs:
        if m.kind != "text":
            continue
        text = (m.text or "").strip()
        if not text:
            continue
        if m.mine:
            if not self_chat or text in echo:
                continue
        out.append(m)
    return out[-limit:]


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:16]
