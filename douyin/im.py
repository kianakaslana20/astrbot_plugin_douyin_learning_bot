"""私信：读会话、读消息、找出别人分享过来的视频、回一句话。

为什么走界面而不是接口：私信的消息接口在 `imapi.douyin.com` 的 imsdk 里，要 SDK token 和
wss 长连接，纯 HTTP 拼不出来。而界面上这些东西都摆着：会话列表的标题/预览、消息区的
`[data-e2e="msg-item-content"]`、分享卡片的封面。代价是要点开面板、要按坐标点。

分享卡片里没有 aweme_id（我们翻过 DOM，只有封面图），**点开它**之后地址栏会出现
`?modal_id=<aweme_id>`——所以取 id 的办法就是点一下、读 URL、关掉。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger

from .browser import DouyinBrowser, HumanVerifyNeeded

PANEL = '[data-e2e="im-dialog"]'
ENTRY = '[data-e2e="im-entry"]'
ROW = '[data-e2e="conversation-item"]'
MSG = '[data-e2e="msg-item-content"]'
SHARE_CARD = '[class*="MessageItemShareAwemecontainer"]'
SHARE_AUTHOR = '[class*="MessageItemShareAwemeauthorName"]'
INPUT = '[data-e2e="msg-input"]'

AWEME_RE = re.compile(r"(?:modal_id=|/video/|/note/|aweme_id=)(\d{12,25})")
URL_RE = re.compile(r"https?://v\.douyin\.com/[A-Za-z0-9]+|https?://www\.douyin\.com/video/\d+")


@dataclass
class Conversation:
    name: str
    desc: str = ""
    when: str = ""
    x: int = 0
    y: int = 0

    @property
    def looks_share(self) -> bool:
        return "分享" in self.desc or "[视频]" in self.desc


@dataclass
class Message:
    kind: str = "text"            # text / share / system
    text: str = ""
    aweme_id: str = ""
    author: str = ""
    mine: bool = False
    extra: dict = field(default_factory=dict)


CONV_JS = """() => {
  const rows = [...document.querySelectorAll('[data-e2e="conversation-item"]')];
  const out = [];
  for (const el of rows) {
    const r = el.getBoundingClientRect();
    if (r.width < 40 || r.height < 20) continue;
    const q = (sel) => { const n = el.querySelector(sel); return n ? (n.innerText || '').trim() : ''; };
    const nameEl = el.querySelector('[class*="conversationConversationItemtitle"]')
                 || el.querySelector('[class*="ConversationItemtitle"]');
    out.push({
      name: nameEl ? (nameEl.innerText || '').trim().split('\\n')[0].trim()
                   : (el.innerText || '').trim().split('\\n')[0].trim(),
      desc: q('[class*="ConversationItemDescleft"]') || q('[class*="ConversationItemDesc"]'),
      when: q('[class*="ConversationItemTagNextToTitle"]'),
      x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2),
    });
  }
  return out;
}"""

PLACEHOLDERS = {"打个招呼吧", "发一条消息", "发送消息", "说点什么…", "说点什么..."}

ROW_CLICK_JS = """
(name) => {
  const rows = [...document.querySelectorAll('[data-e2e="conversation-item"]')];
  const hit = rows.find(r => (r.innerText || '').split('\\n')[0].trim() === name)
           || rows.find(r => (r.innerText || '').includes(name));
  if (!hit) return 'no-row:' + rows.length;
  const fire = (t) => hit.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window}));
  fire('pointerover'); fire('mouseover'); fire('mousedown'); fire('mouseup'); fire('click');
  return 'ok';
}
"""

CHAT_NAME_JS = """
() => {
  const p = document.querySelector('[data-e2e="im-dialog"]');
  if (!p) return '';
  for (const sel of ['[class*="ChatHeadertitleContainer"]',
                     '[class*="ChatHeaderinfoContainer"]']) {
    const e = p.querySelector(sel);
    const t = e && (e.innerText || '').trim();
    if (t) return t.split('\\n')[0].trim();
  }
  return '';
}
"""

MSG_JS = """() => {
  const rows = [...document.querySelectorAll('[data-e2e="msg-item-content"]')];
  const dlg = document.querySelector('[data-e2e="im-dialog"]');
  const dr = dlg ? dlg.getBoundingClientRect() : {x: 0, width: 336};
  // 气泡靠右 = 自己发的。按中心点跟面板中线比，比猜 class 名靠谱。
  const mid = dr.x + dr.width * 0.62;
  const out = [];
  for (let i = 0; i < rows.length; i++) {
    const el = rows[i];
    const r = el.getBoundingClientRect();
    if (r.width < 10 || r.height < 8) continue;
    const card = el.querySelector('[class*="MessageItemShareAwemecontainer"]');
    const author = el.querySelector('[class*="MessageItemShareAwemeauthorName"]');
    out.push({
      i,
      share: !!card,
      text: (el.innerText || '').trim().slice(0, 400),
      author: author ? (author.innerText || '').trim() : '',
      // 卡片中心点给外面点开用
      x: card ? Math.round((card.getBoundingClientRect().x + card.getBoundingClientRect().width / 2)) : 0,
      y: card ? Math.round((card.getBoundingClientRect().y + card.getBoundingClientRect().height / 2)) : 0,
      cx: Math.round(r.x + r.width / 2),
      // 气泡根节点上有 isFromMe 标记（MessageBoxContentisFromMe），比位置可靠
      mine: !!(el.closest('[class*="isFromMe"]') || (r.x + r.width / 2) > mid),
    });
  }
  return out;
}"""


class DirectMessages:
    """私信面板的读写。一个实例跟一个 page 走，别跨浏览器复用。"""

    def __init__(self, browser: DouyinBrowser):
        self.browser = browser

    # ------------------------------------------------------------------ 面板

    async def open_panel(self, page) -> bool:
        """点开私信抽屉。必须先 hover 再点：直接 JS click 出来的面板是 0×0 的。"""
        if await self._panel_visible(page):
            return True
        box = await page.evaluate(
            """() => {
              const direct = document.querySelector('[data-e2e="im-entry"]');
              const pick = (el) => { const r = el.getBoundingClientRect();
                return r.width < 4 ? null : {x: r.x + r.width / 2, y: r.y + r.height / 2}; };
              if (direct) { const b = pick(direct); if (b) return b; }
              const cands = [];
              for (const el of document.querySelectorAll('ul,li,span,div,a')) {
                if ((el.textContent || '').replace(/\\s+/g, '').trim() !== '消息') continue;
                const r = el.getBoundingClientRect();
                if (r.width < 4 || r.height < 4) continue;
                cands.push({x: r.x + r.width / 2, y: r.y + r.height / 2, area: r.width * r.height});
              }
              if (!cands.length) return null;
              cands.sort((a, b) => a.area - b.area);
              return cands[0];
            }"""
        )
        if not box:
            logger.warning("私信：找不到「消息」入口")
            return False
        await page.mouse.move(box["x"] - 30, box["y"])
        await page.wait_for_timeout(400)
        await page.mouse.move(box["x"], box["y"])
        await page.wait_for_timeout(500)
        await page.mouse.click(box["x"], box["y"])
        for _ in range(10):
            await page.wait_for_timeout(500)
            if await self._panel_visible(page):
                return True
        return False

    async def close_panel(self, page) -> None:
        try:
            await page.mouse.click(30, 500)
            await page.wait_for_timeout(300)
        except Exception:  # noqa: BLE001
            pass

    async def _panel_visible(self, page) -> bool:
        return bool(
            await page.evaluate(
                """() => {
                  const el = document.querySelector('[data-e2e="im-dialog"]');
                  if (!el) return false;
                  const r = el.getBoundingClientRect();
                  return r.width > 100 && r.height > 100;
                }"""
            )
        )

    # ---------------------------------------------------------------- 会话列表

    async def conversations(self, page) -> list[Conversation]:
        if not await self.open_panel(page):
            return []
        rows = await page.evaluate(CONV_JS) or []
        # 昵称有时比列表晚一步渲染，抓到的会是纯数字 uid。等它长出来再读。
        for _ in range(4):
            if rows and not any(_looks_uid(r.get("name", "")) for r in rows):
                break
            await page.wait_for_timeout(600)
            rows = await page.evaluate(CONV_JS) or rows
        if not rows:
            # 面板刚打开时列表是异步挂载的，等两轮；还是空就重开一次面板
            for _ in range(2):
                await page.wait_for_timeout(900)
                rows = await page.evaluate(CONV_JS) or []
                if rows:
                    break
            if not rows:
                await self.open_panel(page)
                await page.wait_for_timeout(1200)
                rows = await page.evaluate(CONV_JS) or []
        return [Conversation(**{k: r.get(k, "") for k in ("name", "desc", "when", "x", "y")}) for r in rows]

    async def open_conversation(self, page, name: str) -> bool:
        """点进某个会话。

        不用坐标点：聊天视图会盖住列表，坐标点经常点空或点到别的会话。
        改成在 DOM 里按名字找到列表行、直接派发鼠标事件，再用顶部标题确认进对了人。
        """
        for attempt in range(2):
            hit = await page.evaluate(ROW_CLICK_JS, name)
            for _ in range(8):
                await page.wait_for_timeout(400)
                who = await self.chat_name(page)
                if who and (who == name or name in who or who in name):
                    return True
            logger.info("私信：点「%s」没进对会话（第 %d 次，返回 %s，头部「%s」）",
                        name, attempt + 1, hit, await self.chat_name(page))
            # 列表可能还没渲染，等一会儿再试
            await self.conversations(page)
        return False

    async def chat_name(self, page) -> str:
        """当前聊天视图顶部显示的名字（空串=没在聊天里）。"""
        try:
            return (await page.evaluate(CHAT_NAME_JS)) or ""
        except Exception:  # noqa: BLE001
            return ""

    # ------------------------------------------------------------------ 消息

    async def messages(self, page, *, resolve_share: bool = True, limit: int = 6) -> list[Message]:
        """读当前会话的消息。

        `resolve_share=True` 时会点开分享卡片拿 aweme_id —— 每张卡点一次，点完按 Esc 收回，
        所以只处理最后 `limit` 张，免得一屏几十张卡把浏览器拖死。
        """
        raw = await page.evaluate(MSG_JS) or []
        out: list[Message] = []
        shares = [r for r in raw if r["share"]]
        todo = {r["i"] for r in shares[-limit:]} if resolve_share else set()
        for r in raw:
            text = (r.get("text") or "").strip()
            if r["share"]:
                msg = Message(kind="share", text=text, author=r.get("author", ""), mine=r.get("mine", False))
                if r["i"] in todo:
                    msg.aweme_id = await self._share_aweme_id(page, r["x"], r["y"]) or ""
                # 卡片底下常带一句「捎句话」之类，纯文本里也能捡到 v.douyin.com 短链
                found = AWEME_RE.search(text)
                if not msg.aweme_id and found:
                    msg.aweme_id = found.group(1)
                out.append(msg)
            elif text and text not in PLACEHOLDERS:
                out.append(Message(kind="text", text=text, mine=r.get("mine", False)))
        return out

    async def _share_aweme_id(self, page, x: int, y: int) -> str:
        """点开分享卡片，从地址栏摸 aweme_id，再关上。"""
        if not x or not y:
            return ""
        try:
            await page.mouse.click(x, y)
            for _ in range(10):
                await page.wait_for_timeout(400)
                m = AWEME_RE.search(page.url or "")
                if m:
                    aid = m.group(1)
                    await self._close_modal(page)
                    return aid
        except HumanVerifyNeeded:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("点开分享卡片没拿到 aweme_id：%s", exc)
        await self._close_modal(page)
        return ""

    async def _close_modal(self, page) -> None:
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(500)
            still = await page.evaluate("() => !!document.querySelector('[data-e2e=\"modal-video-container\"]')")
            if still:
                await page.mouse.click(20, 500)
                await page.wait_for_timeout(300)
            # 地址栏里的 modal_id 也要擦掉，否则下一条视频会被误认成同一条
            url = page.url or ""
            if "modal_id=" in url:
                await page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
                await page.wait_for_timeout(1500)
        except Exception:  # noqa: BLE001
            pass

    def pick_shares(self, messages: list[Message]) -> list[Message]:
        """哪些是「别人发过来的视频」：带 aweme_id 的分享卡，去掉自己发的。"""
        return [m for m in messages if m.kind == "share" and m.aweme_id and not m.mine]

    # ------------------------------------------------------------------ 发消息

    async def send_text(self, page, text: str) -> bool:
        """在当前会话里发一句话（走界面，Enter 发送）。"""
        text = (text or "").strip()
        if not text:
            return False
        box = await page.evaluate(
            """() => {
              const el = document.querySelector('[data-e2e="msg-input"]');
              if (!el) return null;
              const r = el.getBoundingClientRect();
              return {x: r.x + Math.min(60, r.width / 2), y: r.y + r.height / 2};
            }"""
        )
        if not box:
            return False
        await page.mouse.click(box["x"], box["y"])
        await page.wait_for_timeout(400)
        await page.keyboard.insert_text(text)
        await page.wait_for_timeout(500)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(900)
        return True


def extract_urls(text: str) -> list[str]:
    """从一段私信文本里捡出抖音链接（短链或整链）。"""
    return URL_RE.findall(text or "")


def aweme_id_of(text_or_url: str) -> str:
    m = AWEME_RE.search(text_or_url or "")
    return m.group(1) if m else ""


__all__ = [
    "Conversation",
    "DirectMessages",
    "Message",
    "aweme_id_of",
    "extract_urls",
]


def _looks_uid(name: str) -> bool:
    n = (name or "").strip()
    return n.isdigit() and len(n) >= 6
