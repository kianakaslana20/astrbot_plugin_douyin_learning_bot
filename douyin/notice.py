"""提醒：读「通知 → 互动消息」里的「在评论中提到了你」。

界面上的形态（2026-11 实测）：

    yezheyue | 朋友 | @大肥鱼Yume | 在评论中提到了你05:31
    yezheyue | 朋友 | 模型里面有敏感限制吗…@大肥鱼Yume | 在评论中提到了你05:29

面板是 `[data-e2e="listDlgTest-container"]`（约 328×696，贴在右上角），点顶栏「通知」打开。
历史 DOM 仅记录了文本与坐标，没有保存 data 属性/链接的实证。
新增属性/链接采集基于 synthetic schema；只用明确证据，缺失 id 保持为空。
只有点击后出现不同于旧页面的视频 id，才将其归给当前通知。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from astrbot.api import logger

PANEL_HINTS = ('[data-e2e="listDlgTest-container"]', '[class*="listDlgTest"]')
MENTION_HINTS = ("提到了你", "@你", "提到了我")
COMMENT_HINTS = ("评论了你的作品", "评论了你的视频", "评论了你的评论", "评论了你的", "评论了你",
                 "回复了你的评论", "回复了你的作品", "回复了你的视频", "回复了你的", "回复了你")
NOTICE_HINTS = MENTION_HINTS + COMMENT_HINTS
TIME_RE = re.compile(r"(\d{1,2}:\d{2}|\d{2}-\d{2}|\d+分钟前|\d+小时前|昨天|前天|刚刚)")

ROW_JS = """() => {
  const roots = [...document.querySelectorAll('[data-e2e="listDlgTest-container"], [class*="listDlgTest"]')];
  if (!roots.length) return {ok: false, why: '面板没开'};
  const root = roots[0];
  const r0 = root.getBoundingClientRect();
  const rows = [];
  // 每条通知：高度 40~200，宽度接近面板宽
  for (const d of root.querySelectorAll('div')) {
    const b = d.getBoundingClientRect();
    if (b.height < 40 || b.height > 200 || b.width < r0.width * 0.6) continue;
    const t = (d.innerText || '').replace(/\\s+/g, ' ').trim();
    if (!t || t.length < 4) continue;
    // 只留最内层那种：父节点里文本不一样
    const pt = d.parentElement ? (d.parentElement.innerText || '').replace(/\\s+/g, ' ').trim() : '';
    if (d.parentElement !== root && pt === t) continue;
    // 仅采集这条通知自身的证据；不向父面板查找 uid 或视频链接。
    const markers = t.match(/在评论中提到了[你我]|提到了[你我]|@你|评论了你|回复了你/g) || [];
    if (markers.length !== 1) continue; // 合并了多条通知的容器不是一条消息
    const attrs = el => Object.fromEntries([...el.attributes].filter(a =>
      /^data-(?:from-uid|uid|user-id|comment-id|aweme-id|item-id|event-id)$/.test(a.name)
    ).map(a => [a.name, a.value]));
    const nodes = [...d.querySelectorAll('[data-uid],[data-from-uid],[data-user-id],[data-comment-id],[data-aweme-id],[data-item-id],[data-event-id]')]
      .map(el => ({attrs: attrs(el), text: (el.innerText || '').trim()}));
    const links = [...d.querySelectorAll('a[href]')].map(a => ({href: a.href,
      text: (a.innerText || '').trim(), attrs: attrs(a)}));
    rows.push({text: t, attrs: attrs(d), nodes, links,
               y: Math.round(b.y + b.height / 2), x: Math.round(b.x + b.width / 2),
               h: Math.round(b.height), cls: (d.className || '').toString().slice(0, 40)});
  }
  // 去重（同一行会被多层命中）
  const seen = new Set();
  const out = [];
  for (const r of rows.sort((a, b) => a.y - b.y)) {
    const key = r.y + '|' + r.text.slice(0, 30);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(r);
  }
  return {ok: true, box: {x: Math.round(r0.x), y: Math.round(r0.y), w: Math.round(r0.width), h: Math.round(r0.height)}, rows: out.slice(0, 30)};
}"""


@dataclass
class Mention:
    who: str = ""
    text: str = ""
    when: str = ""
    raw: str = ""
    y: int = 0
    x: int = 0
    aweme_id: str = ""
    kind: str = "mention"
    uid: str = ""
    comment_id: str = ""
    event_id: str = ""

    def describe(self) -> str:
        bit = f"{self.who} 在评论里 @ 了你" if self.kind == "mention" else f"{self.who} 评论/回复了你"
        if self.text:
            bit += f"：「{self.text[:40]}」"
        if self.when:
            bit += f"（{self.when}）"
        return bit


def parse_row(raw: str) -> Mention | None:
    """兼容旧文本接口；普通评论/回复的 kind 为 comment，@ 为 mention。"""
    text = (raw or "").replace("\u200b", "").strip()
    if not text or not any(h in text for h in NOTICE_HINTS):
        return None
    kind = "comment" if any(h in text for h in COMMENT_HINTS) else "mention"
    when = ""
    # 只剥离末尾时间，不把正文里的 12:30 当作通知时间。
    m = re.search(TIME_RE.pattern + r"\s*$", text)
    if m:
        when = m.group(1)
        text = text[:m.start()].strip()
    tails = ("在评论中提到了你", "在评论中提到了我", *COMMENT_HINTS, *MENTION_HINTS)
    missing_sender = any(text.startswith(tail) for tail in tails)
    for tail in sorted(set(tails), key=len, reverse=True):
        text = text.replace(tail, " ").strip()
    parts = [p.strip() for p in re.split(r"\s+|[|]", text) if p.strip()]
    parts = [p for p in parts if p not in {"朋友", "互相关注", "关注了你", "·", "|"}]
    who = parts[0] if parts and not missing_sender else ""
    body = " ".join(parts if missing_sender else parts[1:]).lstrip("：: ")
    return Mention(who=who, text=body, when=when, raw=raw, kind=kind)


def _row_ids(row: dict, who: str) -> dict[str, str]:
    """只接受行级属性或同条通知的明确链接；冲突值留空，不猜数字/昵称。"""
    found = {key: set() for key in ("uid", "comment_id", "aweme_id", "event_id")}

    def add(key, value):
        value = str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
        if value and (key == "event_id" or value.isascii() and value.isdigit()):
            found[key].add(value)

    def attrs(data, sender=False):
        if not isinstance(data, dict):
            return
        for key, names in {
            "uid": ("data-from-uid", "data-uid", "data-user-id"),
            "comment_id": ("data-comment-id",),
            "aweme_id": ("data-aweme-id", "data-item-id"),
            "event_id": ("data-event-id",),
        }.items():
            if key == "uid" and not sender:
                continue
            for name in names:
                add(key, data.get(name))

    attrs(row.get("attrs"), sender=True)
    for node in row.get("nodes", []):
        if isinstance(node, dict):
            attrs(node.get("attrs"), sender=bool(who and node.get("text", "").strip() == who))
    for link in row.get("links", []):
        if not isinstance(link, dict):
            continue
        sender = bool(who and link.get("text", "").strip() == who)
        attrs(link.get("attrs"), sender=sender)
        try:
            url = urlparse(link.get("href") or "")
        except (TypeError, ValueError):
            continue
        if url.scheme and url.scheme not in {"http", "https"}:
            continue
        if url.netloc and not (url.hostname == "douyin.com" or (url.hostname or "").endswith(".douyin.com")):
            continue
        query = parse_qs(url.query)
        for key, names in {"comment_id": ("comment_id",), "aweme_id": ("aweme_id", "modal_id", "item_id")}.items():
            for name in names:
                for value in query.get(name, []):
                    add(key, value)
        video = re.fullmatch(r"/(?:video|note)/(\d+)/?", url.path)
        if video:
            add("aweme_id", video.group(1))
        # /user/MS4w... 是 sec_uid，不冒充 uid；正文 @ 的用户链接也不当发送者。
        if sender:
            user = re.fullmatch(r"/user/(\d+)/?", url.path)
            if user:
                add("uid", user.group(1))
            for value in query.get("uid", []):
                add("uid", value)
    return {key: next(iter(values)) if len(values) == 1 else "" for key, values in found.items()}



class Notices:
    """通知面板的读取。和私信一样：点开、读、必要时点进去拿 id。"""

    def __init__(self, browser):
        self.browser = browser

    async def open_panel(self, page) -> bool:
        for hint in PANEL_HINTS:
            if await page.query_selector(hint):
                return True
        box = None
        for _ in range(12):
            box = await page.evaluate(
                """() => {
                  // 不要求 children=0：图标字体没加载完时，可点的那个是个 ul/div 包一层。
                  // 所以把所有文字恰好等于「通知」的元素都收进来，取面积最小的那个点。
                  const hits = [];
                  for (const el of document.querySelectorAll('ul,li,span,div,a,button')) {
                    const t = (el.textContent || '').replace(/\\s+/g, '').trim();
                    if (t !== '通知') continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 4 || r.height < 4) continue;
                    hits.push({x: r.x + r.width / 2, y: r.y + r.height / 2, area: r.width * r.height});
                  }
                  if (!hits.length) return null;
                  hits.sort((a, b) => a.area - b.area);
                  return hits[0];
                }"""
            )
            if box:
                break
            await page.wait_for_timeout(800)
        if not box:
            logger.warning("通知：顶栏找不到「通知」（等了 10 秒）")
            return False
        await page.mouse.move(box["x"] - 15, box["y"])
        await page.wait_for_timeout(350)
        await page.mouse.click(box["x"], box["y"])
        for _ in range(10):
            await page.wait_for_timeout(400)
            for hint in PANEL_HINTS:
                if await page.query_selector(hint):
                    return True
        return False

    async def close_panel(self, page) -> None:
        try:
            await page.mouse.click(40, 520)
            await page.wait_for_timeout(400)
        except Exception:  # noqa: BLE001
            pass

    async def mentions(self, page) -> list[Mention]:
        """兼容旧方法名，读取 @、普通评论及回复（不点开，先分类）。"""
        if not await self.open_panel(page):
            return []
        data = await page.evaluate(ROW_JS) or {}
        if not data.get("ok"):
            logger.warning("通知面板读不到：%s", data.get("why"))
            return []
        out = []
        for row in data.get("rows", []):
            m = parse_row(row.get("text", ""))
            if m:
                m.x, m.y = int(row.get("x") or 0), int(row.get("y") or 0)
                for key, value in _row_ids(row, m.who).items():
                    setattr(m, key, value)
                out.append(m)
        return out

    async def open_mention(self, page, mention: Mention) -> str:
        """点开通知只认新出现的 id；旧 modal_id 未变化时保留缺失。"""
        if mention.aweme_id:
            return mention.aweme_id
        if not mention.x or not mention.y:
            return ""
        from .im import AWEME_RE

        before = AWEME_RE.search(page.url or "")
        before_id = before.group(1) if before else ""
        try:
            await page.mouse.click(mention.x, mention.y)
            for _ in range(12):
                await page.wait_for_timeout(400)
                m = AWEME_RE.search(page.url or "")
                if m and m.group(1) != before_id:
                    aid = m.group(1)
                    await self._close(page)
                    return aid
        except Exception as exc:  # noqa: BLE001
            logger.warning("点开 @ 提醒没拿到视频 id：%s", exc)
        await self._close(page)
        return ""

    async def _close(self, page) -> None:
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(400)
            if "modal_id=" in (page.url or ""):
                await page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
                await page.wait_for_timeout(1200)
        except Exception:  # noqa: BLE001
            pass


__all__ = ["Mention", "Notices", "parse_row"]