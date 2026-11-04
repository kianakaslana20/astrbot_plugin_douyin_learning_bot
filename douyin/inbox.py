"""收件箱：把「私信里别人发来的视频」和「评论里 @ 我的提醒」捞成条目。

两条来源都只能走界面（私信消息在 imsdk 长连接里，纯 HTTP 拼不出来；提醒同理），
所以这里干的事就是：点开面板 → 逐条读 → 该点开的点开拿 aweme_id → 记进 inbox 表。

去重靠 `Store.inbox_add()` 的 `UNIQUE(kind, key)`，所以重复扫不会重复处理、也不会重复回人。
"""

from __future__ import annotations

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
    kind: str                    # dm_share / mention
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
        else:
            head = f"评论提醒 · {self.who or '有人'} 在评论里 @ 了你"
        if self.text:
            head += f"：「{self.text[:36]}」"
        if self.aweme_id:
            head += f"\n　　https://www.douyin.com/video/{self.aweme_id}"
        return head


class Inbox:
    """扫两条来源。一个实例跟一个 browser 走。"""

    def __init__(self, browser, settings=None):
        self.browser = browser
        self.settings = settings
        self.dm = DirectMessages(browser)
        self.notices = Notices(browser)

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

    async def collect_dm(self, page, max_conversations: int = 8, max_cards: int = 3) -> list[Entry]:
        convs = await self.dm.conversations(page)
        if not convs:
            return []
        # 只看「预览里写着分享」的会话，别的会话没必要点进去
        targets = [c for c in convs if c.looks_share][:max_conversations]
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
        return out

    async def collect_mentions(self, page) -> list[Entry]:
        mentions: list[Mention] = await self.notices.mentions(page)
        out: list[Entry] = []
        for mt in mentions:
            aid = mt.aweme_id or await self.notices.open_mention(page, mt)
            if not aid:
                # 同一个人的同一句话只记一次，拿不到视频 id 也留着，方便主人自己点
                aid = ""
            out.append(
                Entry(
                    kind="mention",
                    key=f"mt:{aid or mt.who}:{mt.text[:16]}",
                    aweme_id=aid,
                    who=mt.who,
                    text=mt.text,
                    when=mt.when,
                    src=mt.raw,
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