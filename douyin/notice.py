"""提醒：读「通知 → 互动消息」里的「在评论中提到了你」。

界面上的形态（2026-11 实测）：

    yezheyue | 朋友 | @大肥鱼Yume | 在评论中提到了你05:31
    yezheyue | 朋友 | 模型里面有敏感限制吗…@大肥鱼Yume | 在评论中提到了你05:29

面板是 `[data-e2e="listDlgTest-container"]`（约 328×696，贴在右上角），点顶栏「通知」打开。
里面每一条不带 aweme_id——和私信分享卡片一样，**点进去**地址栏才出现 `modal_id`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from astrbot.api import logger

PANEL_HINTS = ('[data-e2e="listDlgTest-container"]', '[class*="listDlgTest"]')
MENTION_HINTS = ("提到了你", "@你", "提到了我")
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
    if (pt === t) continue;
    rows.push({text: t.slice(0, 240), y: Math.round(b.y + b.height / 2), x: Math.round(b.x + b.width / 2),
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

    def describe(self) -> str:
        bit = f"{self.who} 在评论里 @ 了你"
        if self.text:
            bit += f"：「{self.text[:40]}」"
        if self.when:
            bit += f"（{self.when}）"
        return bit


def parse_row(raw: str) -> Mention | None:
    """把一行通知文本拆成 who / text / when。不是 @ 提醒返回 None。"""
    text = (raw or "").replace("\u200b", "").strip()
    if not text or not any(h in text for h in MENTION_HINTS):
        return None
    when = ""
    m = TIME_RE.search(text)
    if m:
        when = m.group(1)
        text = (text[: m.start()] + text[m.end():]).strip()
    # 去掉类型尾巴
    for tail in ("在评论中提到了你", "在评论中提到了我", "提到了你", "@你"):
        text = text.replace(tail, " ").strip()
    parts = [p.strip() for p in re.split(r"\s{1,}", text) if p.strip()]
    parts = [p for p in parts if p not in {"朋友", "互相关注", "关注了你", "·", "|"}]
    who = parts[0] if parts else ""
    body = " ".join(parts[1:]) if len(parts) > 1 else ""
    return Mention(who=who, text=body, when=when, raw=raw)


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
        """读所有 @ 我的通知（不点开，先分类）。"""
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
                out.append(m)
        return out

    async def open_mention(self, page, mention: Mention) -> str:
        """点开一条 @ 提醒，拿它指向的视频 id，然后关掉。"""
        if not mention.x or not mention.y:
            return ""
        from .im import AWEME_RE

        try:
            await page.mouse.click(mention.x, mention.y)
            for _ in range(12):
                await page.wait_for_timeout(400)
                m = AWEME_RE.search(page.url or "")
                if m:
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