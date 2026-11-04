"""私信：读会话、读消息、找出别人分享过来的视频、回一句话。

为什么走界面而不是接口：私信的消息接口在 `imapi.douyin.com` 的 imsdk 里，要 SDK token 和
wss 长连接，纯 HTTP 拼不出来。而界面上这些东西都摆着：会话列表的标题/预览、消息区的
`[data-e2e="msg-item-content"]`、分享卡片的封面。代价是要点开面板、要按坐标点。

分享卡片里没有 aweme_id（我们翻过 DOM，只有封面图），**点开它**之后地址栏会出现
`?modal_id=<aweme_id>`——所以取 id 的办法就是点一下、读 URL、关掉。
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
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


_WHEN_RE = re.compile(r"(?:\d{1,2}:\d{2}|周[一二三四五六日]|昨天|前天|\d{1,2}/\d{1,2})$")


def _strip_when(text: str) -> str:
    """把会话标题尾部的时间削掉：`大肥鱼Yume10:37` → `大肥鱼Yume`。"""
    name = (text or "").strip().split("\n")[0].strip()
    for _ in range(2):
        cut = _WHEN_RE.sub("", name).strip()
        if cut == name:
            break
        name = cut
    return name

class ImWatcher:
    """Push receiver. Known schemas yield events; unknown business frames request DOM enrichment.

    Attach before navigation. CDP also observes sockets already open when attaching.
    No frame-length heuristic: only explicit heartbeat values are ignored.
    """

    HEARTBEATS = (b"hi", b"", "hi", "", b"ping", b"pong", "ping", "pong")

    def __init__(self, page):
        import asyncio
        from collections import deque
        self.page = page
        self.frames = self.signals = 0
        self.last_signal = self.last_heartbeat = 0.0
        self.created_at = time.monotonic()
        self.last_rx = 0.0
        self.disconnected_at = self.created_at
        self.dirty = False
        self.urls = []
        self.last_frame = ""
        self.events = deque(maxlen=256)
        self.diagnostics = deque(maxlen=12)
        self.wake = asyncio.Event()
        self.routes = set()
        self._seen = {}
        self._frame_seen = {}
        self._comment_users = set()
        self._sockets = set()
        self._cdp = None
        self._cdp_urls = {}
        self._closed = False
        self.dropped = 0
        self.dump_dir = None
        page.on("websocket", self._on_ws)

    async def start(self):
        if self._cdp is not None:
            return
        try:
            self._cdp = await self.page.context.new_cdp_session(self.page)
            self._cdp.on("Network.webSocketCreated", self._cdp_created)
            self._cdp.on("Network.webSocketFrameReceived", self._cdp_frame)
            self._cdp.on("Network.webSocketFrameSent", self._cdp_sent)
            self._cdp.on("Network.webSocketClosed", self._cdp_closed)
            await self._cdp.send("Network.enable")
        except Exception as exc:
            self.diagnostics.append("cdp:" + type(exc).__name__)

    @staticmethod
    def _route(url):
        from urllib.parse import urlsplit
        host = urlsplit(url).hostname or ""
        if host == "frontier-im.douyin.com":
            return "im"
        if host == "frontier-pc.douyin.com":
            return "notice"
        return ""

    def _cdp_created(self, event):
        key = event.get("requestId", "")
        route = self._route(event.get("url", ""))
        self._cdp_urls[key] = route
        if route:
            self._sockets.add(key)
            self._log_once(route)

    def _cdp_frame(self, event):
        import base64
        key = event.get("requestId", "")
        # A pre-existing socket has no created event after Network.enable.
        route = self._cdp_urls.get(key, "unknown")
        if not route:
            return
        response = event.get("response") or {}
        payload = response.get("payloadData", "")
        if response.get("opcode") == 2:
            try:
                payload = base64.b64decode(payload, validate=True)
            except Exception:
                return
        self._sockets.add(key)
        self._on_frame(payload, route)

    def _cdp_sent(self, event):
        key = event.get("requestId", "")
        route = self._cdp_urls.get(key, "unknown")
        if not route:
            return
        response = event.get("response") or {}
        payload = response.get("payloadData", "")
        if response.get("opcode") == 2:
            try:
                payload = base64.b64decode(payload, validate=True)
            except Exception:
                return
        self._on_sent_frame(payload, route)

    def _cdp_closed(self, event):
        self._closed_socket(event.get("requestId", ""))

    def _closed_socket(self, key):
        self._sockets.discard(key)
        if not self._sockets:
            self.disconnected_at = time.monotonic()

    def _log_once(self, route: str) -> None:
        """第一次接上某类连接时打一行 —— 日志里要能看出「WS 到底有没有连上」。"""
        seen = getattr(self, "_logged_routes", None)
        if seen is None:
            seen = self._logged_routes = set()
        if route in seen:
            return
        seen.add(route)
        name = {"im": "私信通道(frontier-im)", "notice": "平台互动(frontier-pc)"}.get(route, route)
        logger.info("事件守护：接上 %s 的 WebSocket，后面按帧解码", name)

    def _dump(self, payload, route: str, direction: str) -> None:
        """把原始帧落盘（`action_dump` 打开时才有目录）。

        不写日志、不解释内容 —— 只留字节，事后离线跑 decode_frame 就能知道
        schema 对没对，不用再猜。
        """
        root = getattr(self, "dump_dir", None)
        if not root:
            return
        try:
            if isinstance(payload, str):
                raw = payload.encode("utf-8", "replace")
            else:
                raw = bytes(payload)
            if len(raw) < 40:
                return
            path = os.path.join(root, "frames.jsonl")
            if os.path.exists(path) and os.path.getsize(path) > 4 * 1024 * 1024:
                return
            os.makedirs(root, exist_ok=True)
            rec = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "route": route,
                "dir": direction,
                "n": len(raw),
                "b64": base64.b64encode(raw).decode(),
            }
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _on_ws(self, ws):
        route = self._route(getattr(ws, "url", "") or "")
        if not route:
            return
        # Store only host/route, never query-string tokens.
        self.urls.append(route)
        self.urls[:] = self.urls[-20:]
        key = id(ws)
        self._sockets.add(key)
        self._log_once(route)
        ws.on("framereceived", lambda payload: self._on_frame(payload, route))
        ws.on("framesent", lambda payload: self._on_sent_frame(payload, route))
        ws.on("close", lambda *args: self._closed_socket(key))
        ws.on("socketerror", lambda *args: self._closed_socket(key))

    def _on_sent_frame(self, payload, route="unknown"):
        """我们发出去的帧：只落盘做证据，不当通知信号。

        自己发一条私信时，客户端会把 MessageBody 按同样的 protobuf 结构推上去 ——
        这正好是「用真实字节验 schema」最省事的来源。
        """
        if self._closed:
            return
        self._dump(payload, route, "out")

    def _on_frame(self, payload, route="unknown"):
        if self._closed:
            return
        self.frames += 1
        self._dump(payload, route, "in")
        if route != "unknown":
            self.last_rx = time.monotonic()
            self.disconnected_at = 0.0
        if payload in self.HEARTBEATS:
            self.last_heartbeat = time.time()
            return
        from .wsproto import decode_frame
        result = decode_frame(payload)
        if not result.dirty and not result.events:
            return
        if route == "unknown" and not result.events:
            return  # existing unrelated telemetry sockets are not notification signals
        now = time.monotonic()
        fingerprint = result.raw_frame_id
        if fingerprint and now - self._frame_seen.get(fingerprint, -100.0) < 2.0:
            self.routes.add(route)
            return  # Playwright and CDP observed the same frame
        if fingerprint:
            self._frame_seen[fingerprint] = now
            if len(self._frame_seen) > 1024:
                self._frame_seen.pop(next(iter(self._frame_seen)))
        self.signals += 1
        self.last_signal = time.time()
        self.last_frame = result.raw_frame_id  # no message text in diagnostics
        self.diagnostics.extend(result.diagnostics[-3:])
        self.dirty = True
        self.routes.add(route)
        for evt in result.events:
            extra = evt.setdefault("extra", {})
            extra["transport_route"] = route
            # IM comment cards and IM mentions are NOT public comment events.
            if extra.get("channel") == "im" or route == "im":
                extra["channel"] = "im"
            self.enqueue(evt)
        self.wake.set()

    def enqueue(self, evt):
        key = str(evt.get("event_id") or "")
        if key and key in self._seen:
            return False
        extra = evt.get("extra") or {}
        is_comment = evt.get("type") in {"comment", "mention"} and extra.get("channel") != "im"
        uid = str(evt.get("from_uid") or "")
        if is_comment and uid and uid in self._comment_users:
            self.dropped += 1
            return False
        if len(self.events) >= self.events.maxlen:
            self.dropped += 1
            self.diagnostics.append("queue_full:DOM_resync")
            self.dirty = True
            return False
        if key:
            self._seen[key] = time.monotonic()
            if len(self._seen) > 2048:
                self._seen.pop(next(iter(self._seen)))
        if is_comment and uid:
            self._comment_users.add(uid)
        self.events.append(evt)
        self.wake.set()
        return True

    def release(self, evt):
        # Only the public-comment events actually took the per-user slot in enqueue().
        extra = evt.get("extra") or {}
        if evt.get("type") in {"comment", "mention"} and extra.get("channel") != "im":
            self._comment_users.discard(str(evt.get("from_uid") or ""))

    def drain(self):
        events = list(self.events)
        self.events.clear()
        routes, self.routes = self.routes, set()
        self.dirty = False
        self.wake.clear()
        return events, routes

    def peek(self):
        """看队列里有什么但不清空 —— 首轮快照要用它避开「刚推过来的新消息」。"""
        return list(self.events)

    def restore(self, events):
        """Put drained events back after the UI read failed, so no push is lost."""
        back = [evt for evt in (events or []) if evt]
        if not back:
            return 0
        for evt in reversed(back):
            extra = evt.get("extra") or {}
            if evt.get("type") in {"comment", "mention"} and extra.get("channel") != "im":
                continue  # public comment lock was released; DOM will re-read it
            if len(self.events) >= self.events.maxlen:
                self.dirty = True
                break
            self.events.appendleft(evt)
        self.wake.set()
        return len(back)

    def take(self):
        got, self.dirty = self.dirty, False
        return got

    def alive(self, silent_s=300.0):
        return bool(self.last_rx and time.monotonic() - self.last_rx < silent_s and self._sockets)

    def fallback_due(self, last_poll, interval=600):
        now = time.monotonic()
        lost = self.disconnected_at or (self.last_rx + 300 if self.last_rx else self.created_at)
        return not self.alive() and now - lost >= 300 and now - last_poll >= interval

    async def close(self):
        self._closed = True
        try:
            self.page.remove_listener("websocket", self._on_ws)
        except Exception:
            pass
        if self._cdp is not None:
            try:
                await self._cdp.detach()
            except Exception:
                pass
        self.wake.set()

    def stats(self):
        return {"frames": self.frames, "signals": self.signals,
                "alive": self.alive(), "conns": len(self._sockets),
                "queued": len(self.events), "dropped": self.dropped,
                "last_frame": self.last_frame, "diagnostics": list(self.diagnostics)}


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


BOX_JS = """() => {
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
        # 顶栏是 React 后渲染的：goto 回来立刻找入口会落空，所以先等它出现
        # im 的 SDK 要 10~15 秒才把入口挂上（im-entry 出现之前点它没用）
        for _ in range(25):
            if box:
                break
            await page.wait_for_timeout(1000)
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
            logger.warning("私信：找不到「消息」入口（顶栏没渲染出来）")
            return False

        for attempt in (1, 2, 3, 4):
            if attempt >= 2:
                # 位置可能变了（顶栏会挤动），也可能有弹层挡着
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(200)
                fresh = await page.evaluate(BOX_JS)
                if fresh:
                    box = fresh
            await page.mouse.move(box["x"] - 30, box["y"])
            await page.wait_for_timeout(400)
            await page.mouse.move(box["x"], box["y"])
            await page.wait_for_timeout(500)
            await page.mouse.click(box["x"], box["y"])
            for _ in range(8):
                await page.wait_for_timeout(500)
                if await self._panel_visible(page):
                    return True
            if attempt >= 2:
                # 真点不管用就交给页面自己派发一次 click（React 的 onClick 同样会收到）
                try:
                    await page.evaluate(
                        """() => {
                          const el = document.querySelector('[data-e2e="im-entry"]');
                          if (el) { el.click(); return true; }
                          for (const n of document.querySelectorAll('ul,li,span,div,a')) {
                            if ((n.textContent || '').replace(/\\s+/g, '').trim() !== '消息') continue;
                            const r = n.getBoundingClientRect();
                            if (r.width > 4 && r.height > 4) { n.click(); return true; }
                          }
                          return false;
                        }"""
                    )
                    for _ in range(6):
                        await page.wait_for_timeout(500)
                        if await self._panel_visible(page):
                            return True
                except Exception as exc:  # noqa: BLE001
                    logger.info("私信：页面内点「消息」失败（%s）", type(exc).__name__)
            logger.info("私信：第 %s 次点「消息」没开出来，再试", attempt)
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

    # ------------------------------------------------------------------ 私信发送

    async def last_mine_text(self, page) -> str:
        """我自己发的最后一条（含分享卡标题）；只作诊断用。"""
        try:
            items = await page.evaluate(
                """() => [...document.querySelectorAll('[data-e2e="msg-item-content"]')]
                  .filter(el => el.closest('[class*="isFromMe"]'))
                  .map(el => (el.innerText || '').trim())
                  .filter(t => t)"""
            )
        except Exception:  # noqa: BLE001
            return ""
        return items[-1] if items else ""


    async def resolve_target(self, page, target: str) -> dict | None:
        """在会话列表里按昵称找人，返回 {uid, name}。

        两个实测坑：会话行的 innerText **没有换行**（`昵称10:37预览…`），名字只能
        从 title 元素取再把尾部时间削掉；会话行上也没有 `data-userid`（那只在分享
        面板里），所以纯数字 uid 由调用方先用缓存换成昵称。面板刚开时列表为空，
        所以这里轮询等它渲染。
        """
        target = (target or "").strip()
        if not target:
            return None
        rows = []
        for _ in range(12):
            try:
                rows = await page.evaluate(
                    """() => [...document.querySelectorAll('[data-e2e="conversation-item"]')]
                      .map(r => {
                        const t = r.querySelector('[class*="conversationConversationItemtitle"]');
                        const holder = r.hasAttribute('data-userid') ? r : r.querySelector('[data-userid]');
                        return {name: t ? (t.innerText || '') : '',
                                uid: holder ? (holder.getAttribute('data-userid') || '') : ''};
                      })"""
                )
            except Exception:  # noqa: BLE001
                rows = []
            if rows:
                break
            await page.wait_for_timeout(800)
        cleaned = []
        for row in rows or []:
            name = _strip_when(row.get("name") or "")
            if name:
                cleaned.append({"uid": row.get("uid") or "", "name": name})
        if not cleaned:
            logger.warning("私信：会话列表还是空的（面板开没？）")
            return None
        is_num = bool(re.fullmatch(r"[0-9]{6,20}", target))
        for row in cleaned:
            if row["uid"] and row["uid"] == target:
                return row
        for row in cleaned:
            if row["name"] == target:
                return row
        if not is_num:
            for row in cleaned:
                if target in row["name"] or row["name"] in target:
                    return row
        logger.warning("私信：列表里有 %s 个会话，没有「%s」（%s）",
                       len(cleaned), target, "/".join(x["name"] for x in cleaned[:6]))
        return None

    async def send_dm(self, page, target: str, text: str, allow=None,
                      readback: bool = True) -> tuple:
        """给某个人发一条私信：走界面，发完回读校验，失败重试一次。

        返回 (成功?, 说明)；说明里带昵称/uid，方便写回执与排查。
        """
        text = (text or "").strip()
        if not text:
            return False, "内容是空的"
        # 私信抽屉只挂在首页顶栏：页面停在视频详情/空白页时顶栏不完整，先回首页
        try:
            if "douyin.com" not in (page.url or ""):
                await page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(1500)
        except Exception:  # noqa: BLE001
            pass
        if not await self.open_panel(page):
            return False, "没打开私信面板（顶栏的「消息」没点到）"

        who = await self.resolve_target(page, target)
        if not who or not (who.get("name") or who.get("uid")):
            return False, f"会话列表里找不到「{target}」"
        name, uid = who.get("name") or "", who.get("uid") or ""

        if allow is not None:
            hit = any(
                a and (a == name or a in name or name in a or (uid and a == uid))
                for a in allow
            )
            if not hit:
                return False, f"「{name or target}」不在私信白名单里（私信 → dm_targets）"

        if not await self.open_conversation(page, name or target):
            return False, f"点不进「{name or target}」的会话"
        head = await self.chat_name(page)
        head0 = (head or "").split("\n")[0].strip()
        if head0 and name and head0 != name and name not in head0:
            return False, f"进去的会话不对（头部是「{head0}」，要的是「{name}」）"

        ok = await self.send_text(page, text)
        if not ok:
            return False, "没找到输入框（msg-input），这条没发出去"
        if not readback:
            return True, f"已发给「{name}」（未回读）"

        # 回读：先等它渲染（最多 6 秒），别急着补发 —— 补发会变成两条一样的
        for _ in range(6):
            await page.wait_for_timeout(1000)
            if await self.sent_ok(page, text):
                return True, f"已发给「{name}」并回读确认"

        # 确认没发出去：先看字是不是还留在输入框，留着就再按一次回车
        left = await self.editor_text(page)
        if left and text[:8] in left:
            await page.keyboard.press("Enter")
            for _ in range(4):
                await page.wait_for_timeout(1000)
                if await self.sent_ok(page, text):
                    return True, f"已发给「{name}」并回读确认（补按了一次回车）"
        # 还不行就重打一遍再发
        ok = await self.send_text(page, text)
        if ok:
            for _ in range(4):
                await page.wait_for_timeout(1000)
                if await self.sent_ok(page, text):
                    return True, f"已发给「{name}」并回读确认（重发一次后成功）"
        tail = (await self.last_mine_text(page))[:24]
        return False, f"回读没看到这条（最后一条是「{tail}」），可能被风控吞了"

    # ------------------------------------------------------------------ 发消息

    async def send_text(self, page, text: str) -> bool:
        """在私信输入区打字并发出。

        `[data-e2e="msg-input"]` 只是外壳，真正的可编辑区在里面
        （`div.editor-kit-container`）；Enter 只对新起一行为空时才发，所以打完
        直接按回车即可。
        """
        text = (text or "").strip()
        if not text:
            return False
        try:
            editor = page.locator("div.editor-kit-container").last
            await editor.click(timeout=6000)
        except Exception:  # noqa: BLE001
            try:
                await page.click('[data-e2e="msg-input"]', timeout=4000)
            except Exception:  # noqa: BLE001
                logger.warning("私信：点不到输入框")
                return False
        await page.wait_for_timeout(250)
        try:
            await page.keyboard.insert_text(text)
        except Exception:  # noqa: BLE001
            await page.keyboard.type(text)
        await page.wait_for_timeout(400)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(900)
        return True

    async def editor_text(self, page) -> str:
        """输入区里还没发出去的内容。"""
        try:
            return await page.evaluate(
                """() => { const e = document.querySelector('div.editor-kit-container');
                  return e ? (e.innerText || '').trim() : ''; }"""
            )
        except Exception:  # noqa: BLE001
            return ""

    async def panel_text(self, page) -> str:
        """整个私信抽屉的可见文本。"""
        try:
            return await page.evaluate(
                """() => { const d = document.querySelector('[data-e2e="im-dialog"]');
                  return d ? (d.innerText || '') : ''; }"""
            )
        except Exception:  # noqa: BLE001
            return ""

    async def sent_ok(self, page, text: str) -> bool:
        """发出去了吗：输入区已清空 **且** 抽屉里能看到这句话。"""
        text = (text or "").strip()
        if not text:
            return False
        left = await self.editor_text(page)
        if left and text[:8] in left:
            logger.info("私信：字还留在输入框里（%s 字），没发出去", len(left))
            return False
        body = await self.panel_text(page)
        return text[:8] in body



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
