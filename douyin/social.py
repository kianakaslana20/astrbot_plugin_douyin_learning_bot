"""抖音网页版的「分享 / 评论 @ 人」动作层。

所有选择器都是真机校准过的（2026-10-31，1440×960、未改 UA 的 headful Chromium）。
踩过的坑写在对应位置，改代码前先看注释，别按文案猜。

三条通道：
- 分享面板：hover 视频右下角分享图标 → 面板列出「最近私信过的人」→ 点该行右侧「分享」。
  成功信号是那一行的文案从「分享」变成「捎句话」（实测要 2~3 秒才更新）。
  注意：面板里的搜索框在本版不生效，只能命中最近会话里的人。
- 私信：点右上角「消息」→ 搜索会话 → 进会话发文字。搜索同样不过滤，只能命中最近会话。
- 评论：点底部输入条 → Draft.js 编辑器 → 打字 → `@` 昵称 → 点候选 → 点右侧发送图标。
  发出去可能弹人机验证（白框），这时只能人工过一次。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .browser import HumanVerifyNeeded

VIDEO_URL = "https://www.douyin.com/video/{aweme_id}"
HOME_URL = "https://www.douyin.com/"

SHARE_ICON = '[data-e2e="video-share-icon-container"]'
SHARE_PANEL = '[data-e2e="video-share-container"]'
SHARE_DONE_WORDS = ("捎句话", "已分享", "已发送")

IM_ENTRY = '[data-e2e="im-entry"]'
IM_DIALOG = '[data-e2e="im-dialog"]'
IM_SEARCH = '[data-e2e="im-dialog"] input[placeholder="搜索"]'
IM_CONV = '[data-e2e="conversation-item"]'
IM_EDITOR = (
    '[data-e2e="im-dialog"] div[contenteditable="true"][data-placeholder="发送消息"]',
    '[data-e2e="im-dialog"] .messageEditorinputArea',
    '[data-e2e="im-dialog"] div[contenteditable="true"]',
)

COMMENT_BOX = '[class*="comment-input-inner-container"]'
COMMENT_PLACEHOLDER = "留下你的精彩评论吧"
DRAFT_ANY = 'div[contenteditable="true"]'
AT_BOX = ".atBox-inner-container"

CLICK_JS = """(sel) => {
    const e = document.querySelector(sel);
    if (!e) return false;
    try { e.click(); } catch (_) {}
    e.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
    return true;
}"""


@dataclass
class ActionResult:
    ok: bool
    kind: str
    target: str
    detail: str

    def __str__(self) -> str:
        mark = "✅" if self.ok else "❌"
        return f"{mark} {self.kind} → {self.target}：{self.detail}"


class Social:
    def __init__(self, browser, settings, dump_dir: str | Path):
        self._browser = browser
        self.settings = settings
        self.dump_dir = Path(dump_dir)
        # 昵称 → uid 的缓存：@ 人只需要 uid，而 uid 只能从分享面板里刮。
        # 面板只在视频详情页有，且要 hover 才展开 —— 每发一条评论去开一次视频页
        # 又慢又容易被「打不开的视频」卡住。刮到一次就存下来。
        self._uid_file = self.dump_dir / "uid_cache.json"
        self._uid_cache: dict[str, str] = self._load_uid_cache()
        # 界面发表时顺带把 publish 的回包接住 —— UI 拿不到 cid，但浏览器自己的
        # XHR 里带着 cid。接住它，事后才能删（自测、回滚都要）。
        self._cid_hook: list[str] = []
        self._hook_page = None

    # ---------- 昵称 → uid 缓存 ----------

    def _load_uid_cache(self) -> dict[str, str]:
        try:
            if self._uid_file.exists():
                data = json.loads(self._uid_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return {str(k): str(v) for k, v in data.items() if k and v}
        except Exception as exc:  # noqa: BLE001
            logger.info(f"[social] uid 缓存读不出来（{exc}），当空的用")
        return {}

    def _remember_uids(self, rows: list[dict]) -> None:
        added = 0
        for row in rows or []:
            name = (row.get("name") or row.get("label") or "").strip()
            uid = str(row.get("uid") or "").strip()
            if name and uid and self._uid_cache.get(name) != uid:
                self._uid_cache[name] = uid
                added += 1
        if not added:
            return
        try:
            self.dump_dir.mkdir(parents=True, exist_ok=True)
            self._uid_file.write_text(
                json.dumps(self._uid_cache, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            logger.info(f"[social] 记住 {added} 个 uid（共 {len(self._uid_cache)} 个）")
        except Exception as exc:  # noqa: BLE001
            logger.info(f"[social] uid 缓存写不进去：{exc}")

    def _uid_of(self, name: str) -> str:
        """精确 → 包含 → 被包含，跟面板按片段匹配昵称的思路一致。"""
        name = (name or "").strip()
        if not name:
            return ""
        if name in self._uid_cache:
            return self._uid_cache[name]
        for key, uid in self._uid_cache.items():
            if name in key or key in name:
                return uid
        return ""

    async def resolve_uid(self, page, name: str) -> str:
        """先查缓存；没有就去视频页 hover 分享面板刮一次。"""
        uid = self._uid_of(name)
        if uid:
            return uid
        rows = await self._share_rows(page)
        self._remember_uids(rows)
        return self._uid_of(name)

    # ---------- 基础工具 ----------

    async def _page(self):
        return await self._browser.page()

    def _timeout_s(self, default: float = 30.0) -> float:
        try:
            return float(getattr(self.settings, "page_timeout_s", default) or default)
        except Exception:
            return default

    async def logged_in(self) -> bool:
        try:
            page = await self._page()
            cookies = await page.context.cookies("https://www.douyin.com")
        except Exception:
            return False
        return any(c.get("name") == "sessionid" and c.get("value") for c in cookies)

    async def _captcha(self, page) -> bool:
        """页面上是不是挂着人机验证（滑块/点选）。"""
        checker = getattr(self._browser, "captcha_visible", None)
        if checker is None:
            return False
        try:
            return bool(await checker(page))
        except Exception:  # noqa: BLE001
            return False

    async def _wait(self, page, selector: str, timeout_s: float = 8.0):
        try:
            return await page.wait_for_selector(selector, timeout=int(timeout_s * 1000), state="attached")
        except Exception:
            return None

    @staticmethod
    def _is_displayed(rect: dict | None) -> bool:
        return bool(rect) and rect.get("w", 0) > 1 and rect.get("h", 0) > 1

    async def _rect(self, page, selector: str, index: int = 0) -> dict | None:
        try:
            return await page.evaluate(
                """(args) => {
                       const [sel, idx] = args;
                       const e = document.querySelectorAll(sel)[idx];
                       if (!e) return null;
                       const r = e.getBoundingClientRect();
                       return {x: r.x, y: r.y, w: r.width, h: r.height,
                               text: (e.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 40)};
                   }""",
                [selector, index],
            )
        except Exception:
            return None

    async def _click(self, page, selector: str, index: int = 0, *, js_fallback: bool = True) -> bool:
        rect = await self._rect(page, selector, index)
        if self._is_displayed(rect):
            if await self._click_rect(page, rect["x"] + rect["w"] / 2, rect["y"] + rect["h"] / 2):
                return True
        if js_fallback:
            try:
                return bool(await page.evaluate(CLICK_JS, selector))
            except Exception:
                return False
        return False

    async def _click_rect(self, page, x: float, y: float) -> bool:
        """真实鼠标点击：React 组件只认这个（JS click 有时不触发 onClick）。"""
        try:
            await page.mouse.move(x, y, steps=6)
            await page.wait_for_timeout(80)
            await page.mouse.click(x, y)
            return True
        except Exception:
            return False

    async def _click_text(self, page, needle: str, root: str = "") -> bool:
        sel = f"{root} *" if root else "*"
        try:
            return bool(
                await page.evaluate(
                    """(args) => {
                           const [sel, needle] = args;
                           const hit = Array.from(document.querySelectorAll(sel))
                               .find((e) => (e.innerText || '').trim() === needle);
                           if (!hit) return false;
                           hit.click();
                           return true;
                       }""",
                    [sel, needle],
                )
            )
        except Exception:
            return False

    async def _click_placeholder(self, page, text: str) -> bool:
        """按占位文字定位元素，取真实坐标再用鼠标点 —— 评论区这块必须真人点击。"""
        try:
            rect = await page.evaluate(
                """(needle) => {
                       const hits = Array.from(document.querySelectorAll('div,span,p'))
                           .filter((e) => (e.innerText || '').trim() === needle);
                       if (!hits.length) return null;
                       hits.sort((a, b) => b.getBoundingClientRect().width - a.getBoundingClientRect().width);
                       const r = hits[0].getBoundingClientRect();
                       return {x: r.x, y: r.y, w: r.width, h: r.height};
                   }""",
                text,
            )
        except Exception:
            return False
        if not rect or rect["w"] < 5 or rect["h"] < 5:
            return False
        return await self._click_rect(page, rect["x"] + rect["w"] / 2, rect["y"] + rect["h"] / 2)

    async def _dump(self, page, tag: str, regions: tuple[str, ...] = ()) -> dict:
        self.dump_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        png = self.dump_dir / f"{tag}-{stamp}.png"
        try:
            await page.screenshot(path=str(png), full_page=False)
        except Exception:
            png = Path("")
        html = self.dump_dir / f"{tag}-{stamp}.html"
        try:
            html.write_text(await page.content(), encoding="utf-8")
        except Exception:
            pass
        for i, sel in enumerate(regions):
            try:
                frag = await page.evaluate(
                    """(sel) => { const e = document.querySelector(sel); return e ? e.outerHTML : ''; }""",
                    sel,
                )
                if frag:
                    (self.dump_dir / f"{tag}-{stamp}.region{i}.html").write_text(frag, encoding="utf-8")
            except Exception:
                continue
        logger.info(f"[social] dump → {png}")
        return {"png": str(png), "html": str(html)}

    async def _open_video(self, page, aweme_id: str) -> bool:
        """打开详情页并确认「真的打开了」。

        feed 里挑出来的卡片不都能开：直播卡、已下架、带货页打开后是
        `data-e2e="error-page"`，播放器控件和分享面板全都没有，后面所有
        DOM 操作都会以「找不到元素」告终。所以这里先验页面，再试备用入口。
        """
        urls = (
            VIDEO_URL.format(aweme_id=aweme_id),
            f"https://www.douyin.com/discover?modal_id={aweme_id}",
            VIDEO_URL.format(aweme_id=aweme_id),   # 详情页偶发白屏，同一个 URL 再来一次
        )
        opened_js = """() => {
          const err = document.querySelector('[data-e2e="error-page"]');
          const v = document.querySelector('video');
          if (!v) return { ok: false, why: err ? 'error-page' : '没有播放器' };
          if (err && !(v.videoWidth > 0)) return { ok: false, why: 'error-page' };
          return { ok: true, why: '' };
        }"""
        for url in urls:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_s() * 1000)
                # 详情页会自动连播下一条（实测从「第2集」跳到「第3集」），
                # 播放一换 aweme_id 就变了，后面所有接口调用全会打到别的视频上。
                await page.wait_for_timeout(2400)
                got = await page.evaluate(opened_js) or {}
                if not got.get("ok"):
                    logger.info(
                        "[social] %s 打不开（%s），换下一个入口", aweme_id, got.get("why") or "?"
                    )
                    continue
                await page.evaluate(
                    """() => {
                      const v = document.querySelector('video');
                      if (v) { try { v.pause(); v.autoplay = false; } catch (e) {} }
                      return true;
                    }"""
                )
                return True
            except Exception as exc:  # noqa: BLE001
                logger.info(f"[social] 打开视频页失败：{exc}")
        return False

    # ---------- 评论（接口） ----------

    # 评论发布走接口：UI 那条路「编辑器清空」≠ 真发出去了，而且会被自动连播带到
    # 别的视频上。接口版回包直接给 cid，发没发、发在哪条视频上一目了然。
    PUBLISH_FN = """
    async (args) => {
      const [awemeId, text, extra] = args;
      const q = new URLSearchParams({
        app_name: 'aweme', enter_from: 'video_detail', previous_page: 'video_detail',
        device_platform: 'webapp', aid: '6383', channel: 'channel_pc_web',
        pc_client_type: '1', version_code: '190500', version_name: '19.5.0',
        cookie_enabled: 'true', screen_width: '1600', screen_height: '900',
        browser_language: 'zh-CN', browser_platform: 'Win32',
        browser_name: 'Chrome', browser_version: '124.0.0.0',
        platform: 'PC', downlink: '10',
      });
      const body = new URLSearchParams({
        reply_id: '0', reply_to_reply_id: '0', aweme_id: awemeId, text,
        text_extra: extra || '[]',
        comment_send_celltime: '0', comment_video_celltime: '0',
      });
      const r = await fetch('/aweme/v1/web/comment/publish/?' + q.toString(), {
        method: 'POST', credentials: 'include',
        headers: {'x-secsdk-csrf-token': 'DOWNGRADE',
                  'content-type': 'application/x-www-form-urlencoded'},
        body: body.toString(),
      });
      const t = await r.text();
      let j = null; try { j = JSON.parse(t); } catch (e) {}
      const c = j && j.comment;
      return {code: j ? j.status_code : -1, cid: c ? String(c.cid) : '',
              status: r.status, raw: t.slice(0, 120)};
    }
    """

    DELETE_FN = """
    async (args) => {
      const [awemeId, cid] = args;
      const q = new URLSearchParams({
        aweme_id: awemeId, cid,
        device_platform: 'webapp', aid: '6383', channel: 'channel_pc_web',
        pc_client_type: '1', version_code: '190500', version_name: '19.5.0',
        cookie_enabled: 'true', screen_width: '1600', screen_height: '900',
        browser_language: 'zh-CN', browser_platform: 'Win32',
        browser_name: 'Chrome', browser_version: '124.0.0.0',
        platform: 'PC', downlink: '10',
      });
      const r = await fetch('/aweme/v1/web/comment/delete/?' + q.toString(), {
        method: 'POST', credentials: 'include',
        headers: {'x-secsdk-csrf-token': 'DOWNGRADE'}, body: '',
      });
      const t = await r.text();
      let j = null; try { j = JSON.parse(t); } catch (e) {}
      return {code: j ? j.status_code : -1, raw: t.slice(0, 80)};
    }
    """

    async def _comment_api(self, page, aweme_id: str, text: str, extra: str = "[]"):
        """返回 (ok, cid, fail_reason)。

        抖音的 publish 有个坑：**短时间连发会被限流，回 HTTP 200 + 空 body**
        （实测同一个号 35 秒后再发就成功了）。所以空回包不直接判失败，退避重试。
        """
        tries = 1 + max(0, int(getattr(self.settings, "comment_retry", 2) or 0))
        gap = max(5, int(getattr(self.settings, "comment_retry_s", 20) or 20))
        last = "未知错误"
        for i in range(tries):
            res = await page.evaluate(self.PUBLISH_FN, [aweme_id, text, extra])
            code = res.get("code")
            if code == 0 and res.get("cid"):
                return True, res["cid"], ""
            if code == 8:
                return False, "", "未登录，先 /dy login"
            if code == 4 or code == 5:
                return False, "", "被拦了（内容可能不合适）"
            empty = res.get("status") == 200 and not str(res.get("raw") or "").strip()
            last = "被限流了（空回包）" if empty else f"接口返回 code={code}"
            if empty and i + 1 < tries:
                logger.info("[social] 评论被限流，%ss 后重试（%s/%s）", gap, i + 1, tries)
                await page.wait_for_timeout(gap * 1000)
                gap = min(90, gap * 2)
                continue
            break
        return False, "", last

    LIST_FN = """
    async (awemeId) => {
      const q = new URLSearchParams({
        aweme_id: awemeId, cursor: '0', count: '20', item_type: '0',
        device_platform: 'webapp', aid: '6383', channel: 'channel_pc_web',
        pc_client_type: '1', version_code: '190500', version_name: '19.5.0',
        cookie_enabled: 'true', screen_width: '1600', screen_height: '900',
        browser_language: 'zh-CN', browser_platform: 'Win32',
        browser_name: 'Chrome', browser_version: '124.0.0.0',
        platform: 'PC', downlink: '10',
      });
      const r = await fetch('/aweme/v1/web/comment/list/?' + q.toString(),
                            {credentials: 'include'});
      const t = await r.text();
      let j = null; try { j = JSON.parse(t); } catch (e) {}
      const list = (j && j.comments) || [];
      return {code: j ? j.status_code : -1, status: r.status,
              items: list.map((c) => ({
                cid: String(c.cid || ''),
                text: c.text || '',
                nick: (c.user && c.user.nickname) || '',
                uid: String((c.user && c.user.uid) || ''),
              }))};
    }
    """

    async def _find_my_comment(self, page, aweme_id: str, text: str) -> str:
        """刚发的那条评论的 cid。UI 发表拿不到 cid，用列表接口按正文认回来，才删得掉。"""
        try:
            res = await page.evaluate(self.LIST_FN, aweme_id)
        except Exception as exc:  # noqa: BLE001
            logger.info("[social] 查评论列表失败：%s", exc)
            return ""
        head = (text or "").strip()[:12]
        if not head:
            return ""
        for it in res.get("items") or []:
            if head in (it.get("text") or ""):
                return str(it.get("cid") or "")
        return ""

    async def delete_comment(self, aweme_id: str, cid: str) -> ActionResult:
        """删掉自己刚发的评论（自测/回滚用）。"""
        page = await self._page()
        try:
            if not str(page.url).startswith("https://www.douyin.com/video/"):
                await self._open_video(page, aweme_id)
            res = await page.evaluate(self.DELETE_FN, [aweme_id, str(cid)])
        except Exception as exc:  # noqa: BLE001
            return ActionResult(False, "delete", cid, f"删不掉：{exc}")
        ok = res.get("code") == 0
        return ActionResult(ok, "delete", cid, "已删除" if ok else f"删除失败 code={res.get('code')}")

    async def comment_debug(self, aweme_id: str, text: str) -> ActionResult:
        """只发不删：接口直发，回 cid。"""
        if not await self.logged_in():
            return ActionResult(False, "comment", "评论", "未登录：先 /dy login 扫码")
        page = await self._page()
        if not await self._open_video(page, aweme_id):
            return ActionResult(False, "comment", "评论", "打开视频页失败")
        ok, cid, why = await self._comment_api(page, aweme_id, text)
        return ActionResult(ok, "comment", "评论", f"cid={cid}" if ok else why)

    # ---------- 分享 ----------

    # 点赞走接口，不点像素：精选详情页里 [data-e2e="video-player-digg"] 是 0x0 的，
    # 按坐标点必失败（实测点了两次都没反应）。页面内 fetch 带 cookie 就够了，
    # 回包 status_code=0 表示成功，is_digg 是**操作前**的状态。
    DIGG_FN = """
    async (args) => {
      const [awemeId, type] = args;
      const q = new URLSearchParams({
        aweme_id: awemeId, type: String(type),
        device_platform: 'webapp', aid: '6383', channel: 'channel_pc_web',
        pc_client_type: '1', version_code: '190500', version_name: '19.5.0',
        cookie_enabled: 'true', screen_width: '1600', screen_height: '900',
        browser_language: 'zh-CN', browser_platform: 'Win32',
        browser_name: 'Chrome', browser_version: '124.0.0.0',
        platform: 'PC', downlink: '10',
      });
      const r = await fetch('/aweme/v1/web/commit/item/digg/?' + q.toString(), {
        method: 'POST', credentials: 'include',
        headers: {'x-secsdk-csrf-token': 'DOWNGRADE'},
      });
      const t = await r.text();
      let j = null;
      try { j = JSON.parse(t); } catch (e) {}
      return {status: r.status, code: j ? j.status_code : -1,
              was: j && j.is_digg !== undefined ? j.is_digg : null,
              raw: t.slice(0, 160)};
    }
    """

    async def like(self, aweme_id: str, want: bool = True) -> ActionResult:
        """点赞 / 取消赞。走 /aweme/v1/web/commit/item/digg/，幂等。"""
        page = await self._page()
        try:
            if "douyin.com" not in (page.url or ""):
                await page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(1500)
            res = await page.evaluate(self.DIGG_FN, [aweme_id, 1 if want else 0])
            code = res.get("code")
            was = res.get("was")
            if code == 0:
                before = {1: "已赞", 0: "未赞"}.get(was, "未知")
                now = "已赞" if want else "未赞"
                return ActionResult(True, "like", aweme_id, f"{before} → {now}")
            if code == 8:
                return ActionResult(False, "like", aweme_id, "接口说没登录（sessionid 失效）")
            return ActionResult(False, "like", aweme_id, f"接口回 {code}：{res.get('raw', '')}")
        except Exception as exc:  # noqa: BLE001
            return ActionResult(False, "like", aweme_id, f"出错：{exc}")

    async def share(self, aweme_id: str, targets: list[str], text: str = "") -> list[ActionResult]:
        names = [t.strip() for t in targets if t and t.strip()]
        if not names:
            return []
        if not await self.logged_in():
            return [ActionResult(False, "share", n, "未登录：先 /dy login 扫码") for n in names]

        page = await self._page()
        if not await self._open_video(page, aweme_id):
            return [ActionResult(False, "share", n, "打开视频页失败") for n in names]

        mode = (getattr(self.settings, "share_mode", "panel") or "panel").lower()
        done: list[ActionResult] = []
        pending: list[str] = list(names)
        if mode == "im":
            done, pending = await self._share_by_im(page, aweme_id, pending, text)
            if pending:
                more, pending = await self._share_by_panel(page, aweme_id, pending)
                done.extend(more)
        else:
            done, pending = await self._share_by_panel(page, aweme_id, pending)
            if pending:
                more, pending = await self._share_by_im(page, aweme_id, pending, text)
                done.extend(more)
        for name in pending:
            done.append(ActionResult(False, "share", name, "两种通道都没找到这个好友/群"))
        return done

    async def _share_rows(self, page) -> list[dict]:
        """hover 分享图标 → 面板里每个好友一行的 {uid, name}。

        评论 @ 也靠它拿 uid：text_extra 里的 user_id 就是这里的 `data-userid`，
        服务端收到后会自己把 sec_uid 补全（实测回读里 sec_uid 被填上了）。
        """
        rect = None
        for _ in range(16):
            rect = await self._rect(page, SHARE_ICON)
            if self._is_displayed(rect):
                break
            await page.wait_for_timeout(500)
        if not self._is_displayed(rect):
            logger.info("[social] 没找到分享图标（播放器控件没加载出来）")
            return []
        cx, cy = rect["x"] + rect["w"] / 2, rect["y"] + rect["h"] / 2
        rows: list[dict] = []
        # 面板靠 hover 挂载，一次不成功很常见（播放器还在起、控件位置在跳），多试几轮
        for attempt in range(4):
            try:
                await page.mouse.move(cx - 40, cy - 40, steps=4)
                await page.mouse.move(cx, cy, steps=10)
                await page.wait_for_timeout(700)
                await page.mouse.move(cx + 2, cy - 6, steps=4)   # 轻微抖动，触发 hover 重算
            except Exception:  # noqa: BLE001
                pass
            for _ in range(14):
                opened = await page.evaluate(
                    """(sel) => { const c = document.querySelector(sel);
                           return c ? (c.innerText || '').includes('分享给朋友') : false; }""",
                    SHARE_PANEL,
                )
                if opened:
                    break
                await page.wait_for_timeout(300)
            rows = await page.evaluate(
                """(panel) => {
                       const c = document.querySelector(panel);
                       if (!c) return [];
                       return Array.from(c.querySelectorAll('[data-userid]')).map((a) => {
                           const raw = (a.parentElement ? a.parentElement.innerText : '').replace(/\\s+/g, ' ').trim();
                           const label = (a.innerText || '').trim();
                           let name = raw;
                           if (label && label !== raw && raw.includes(label)) {
                               name = raw.split(label).join(' ').replace(/\\s+/g, ' ').trim();
                           }
                           return {uid: a.getAttribute('data-userid'), name: name, label: label};
                       });
                   }""",
                SHARE_PANEL,
            ) or []
            if rows:
                break
            logger.info("[social] 分享面板第 %s 轮没出行，重试", attempt + 1)
        try:  # 收面板，别挡住后面的操作
            await page.mouse.move(30, 500, steps=4)
        except Exception:  # noqa: BLE001
            pass
        if rows:                      # 刮到就存，下次 @ 不用再开面板
            self._remember_uids(rows)
        return rows

    async def _api_comment_with_at(
        self, page, aweme_id: str, text: str, mentions: list[str]
    ) -> tuple[bool, str, str]:
        """接口发评论 + text_extra 带上 @。回 cid，可回读、可删除。"""
        pairs: list[tuple[str, str]] = []
        missing: list[str] = []
        for name in mentions:
            uid = self._uid_of(name)
            if uid:
                pairs.append((name, uid))
            else:
                missing.append(name)
        if missing:  # 缓存里没有才去开面板刮，且刮到就存起来
            rows = await self._share_rows(page)
            self._remember_uids(rows)
            for name in list(missing):
                uid = self._uid_of(name)
                if uid:
                    pairs.append((name, uid))
                    missing.remove(name)
        if not pairs:
            return False, "", f"拿不到这些人的 uid（{', '.join(missing) or '菜单空'}）"
        body = text.rstrip()
        extra: list[dict] = []
        used: list[str] = []
        for name, uid in pairs:
            seg = f"@{name}"
            if seg in body:
                # 文案里自己写了 @昵称：用它的位置，别再叠一个
                start = body.index(seg)
            else:
                body += " " + seg
                start = len(body) - len(seg)
            extra.append(
                {
                    "start": start,
                    "end": start + len(name) + 1,
                    "user_id": uid,
                    "type": 0,
                    "hashtag_name": "",
                    "hashtag_id": "",
                    "sec_uid": "",
                }
            )
            used.append(name)
        if not used:
            return False, "", "这些昵称没解析到 uid"
        ok, cid, why = await self._comment_api(
            page, aweme_id, body, json.dumps(extra, ensure_ascii=False)
        )
        return ok, cid, (why if not ok else "")

    async def _share_by_panel(self, page, aweme_id: str, targets: list[str]) -> tuple[list[ActionResult], list[str]]:
        """hover 分享图标 → 面板里点该好友行的「分享」。面板只列最近私信过的人。"""
        done: list[ActionResult] = []
        pending: list[str] = []

        # 播放器控件是懒加载的，刚进来时图标还没挂上
        rect = None
        for _ in range(16):
            rect = await self._rect(page, SHARE_ICON)
            if self._is_displayed(rect):
                break
            await page.wait_for_timeout(500)
        if not self._is_displayed(rect):
            logger.info("[social] 没找到分享图标（播放器控件没加载出来）")
            await self._dump(page, "share-icon-missing")
            return done, list(targets)

        cx, cy = rect["x"] + rect["w"] / 2, rect["y"] + rect["h"] / 2
        # 面板是 hover 出来的：点一下反而会把它关掉。一次不展开很常见，多试几轮。
        opened = False
        for _attempt in range(4):
            try:
                await page.mouse.move(cx - 40, cy - 40, steps=4)
                await page.mouse.move(cx, cy, steps=10)
                await page.wait_for_timeout(700)
                await page.mouse.move(cx + 2, cy - 6, steps=4)
            except Exception:  # noqa: BLE001
                pass
            for _ in range(14):
                try:
                    opened = bool(
                        await page.evaluate(
                            """(sel) => { const c = document.querySelector(sel);
                                   return c ? (c.innerText || '').includes('分享给朋友') : false; }""",
                            SHARE_PANEL,
                        )
                    )
                except Exception:  # noqa: BLE001
                    opened = False
                if opened:
                    break
                await page.wait_for_timeout(300)
            if opened:
                break
            logger.info("[social] 分享面板没展开，第 %s 轮重试", _attempt + 1)
        if not opened:
            logger.info("[social] 分享面板没展开，交给私信通道")
            return done, list(targets)

        rows = await page.evaluate(
            """(panel) => {
                   const c = document.querySelector(panel);
                   if (!c) return [];
                   return Array.from(c.querySelectorAll('[data-userid]')).map((a) => ({
                       uid: a.getAttribute('data-userid'),
                       name: (a.parentElement ? a.parentElement.innerText : '').replace(/\\s+/g, ' ').trim(),
                       label: (a.innerText || '').trim()}));
               }""",
            SHARE_PANEL,
        )
        if not rows:
            return done, list(targets)

        for name in targets:
            hit = next((r for r in rows if name and name in (r.get("name") or "")), None)
            if not hit:
                pending.append(name)
                continue
            if any(w in (hit.get("label") or "") for w in SHARE_DONE_WORDS):
                done.append(ActionResult(True, "share", name, "之前已经分享过（按钮显示「捎句话」）"))
                continue

            # 只能 JS 点：真实鼠标一移出图标区域，hover 面板就关了，点会落到视频上
            uid = hit["uid"]
            clicked = await page.evaluate(
                """(uid) => {
                       const a = document.querySelector(`[data-userid="${uid}"]`);
                       if (!a) return false;
                       a.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
                       a.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                       a.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                       a.click();
                       return true;
                   }""",
                uid,
            )

            label = ""
            for _ in range(20):  # 实测 2~3 秒才把「分享」改成「捎句话」
                await page.wait_for_timeout(400)
                label = await page.evaluate(
                    """(uid) => { const a = document.querySelector(`[data-userid="${uid}"]`);
                           return a ? (a.innerText || '').trim() : ''; }""",
                    uid,
                )
                if any(w in (label or "") for w in SHARE_DONE_WORDS):
                    break
            ok = bool(clicked) and any(w in (label or "") for w in SHARE_DONE_WORDS)
            done.append(ActionResult(ok, "share", name, f"面板行按钮现在显示「{label or '（没了）'}」"))
            if not ok:
                await self._dump(page, f"share-{_safe(name)}-fail", (SHARE_PANEL,))

        try:  # 收面板，别挡住后面的操作
            await page.mouse.move(30, 500, steps=4)
        except Exception:
            pass
        return done, pending

    async def _im_open(self, page) -> bool:
        """抽屉真的展开了吗——用尺寸判，不能只看 DOM 里有没有内容（视频页上它是 0×0）。"""
        rect = await self._rect(page, IM_DIALOG)
        if not self._is_displayed(rect) or rect["w"] < 100:
            return False
        return self._is_displayed(await self._rect(page, IM_SEARCH))

    async def _open_im(self, page) -> bool:
        """打开私信抽屉。视频页没有这个入口，要先回首页。"""
        if await self._im_open(page):
            return True
        try:
            await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=self._timeout_s() * 1000)
        except Exception as exc:
            # 视频页还在加载时 goto 会被打断，页面通常已经切过去了，不算致命
            logger.debug(f"[social] 切首页被中断：{exc}")
        await page.wait_for_timeout(2500)

        for attempt in range(5):
            if not await self._click(page, IM_ENTRY):
                await self._click_text(page, "消息")
            for _ in range(5):
                await page.wait_for_timeout(400)
                if await self._im_open(page):
                    return True
            logger.debug(f"[social] 第 {attempt + 1} 次点「消息」没打开，重试")
            await page.wait_for_timeout(300)
        await self._dump(page, "im-open-fail", (IM_DIALOG, IM_ENTRY))
        return False

    async def _share_by_im(
        self, page, aweme_id: str, targets: list[str], text: str
    ) -> tuple[list[ActionResult], list[str]]:
        """私信通道：搜昵称 → 进会话 → 发「文案 + 链接」。本版搜索不过滤，只能命中最近会话。"""
        done: list[ActionResult] = []
        if not await self._open_im(page):
            logger.info("[social] 私信抽屉没打开")
            return done, list(targets)

        body = (text or "").strip() or VIDEO_URL.format(aweme_id=aweme_id)
        pending: list[str] = []
        for name in targets:
            search = await self._rect(page, IM_SEARCH)
            if not self._is_displayed(search):
                pending.append(name)
                continue
            await self._click_rect(page, search["x"] + search["w"] / 2, search["y"] + search["h"] / 2)
            await page.wait_for_timeout(200)
            try:
                await page.keyboard.press("Control+A")
                await page.keyboard.press("Backspace")
                await page.keyboard.insert_text(name)
            except Exception:
                pass
            await page.wait_for_timeout(1500)

            conv = await page.evaluate(
                """(args) => {
                       const [dialog, name] = args;
                       const root = document.querySelector(dialog) || document;
                       const norm = (t) => (t || '').replace(/\\s+/g, '');
                       const hit = Array.from(root.querySelectorAll('[data-e2e="conversation-item"]'))
                           .find((e) => norm(e.innerText).includes(norm(name)));
                       if (!hit) return null;
                       const r = hit.getBoundingClientRect();
                       return {x: r.x, y: r.y, w: r.width, h: r.height};
                   }""",
                [IM_DIALOG, name],
            )
            if not conv or not self._is_displayed(conv):
                pending.append(name)
                continue

            await self._click_rect(page, conv["x"] + min(conv["w"] / 2, 120), conv["y"] + conv["h"] / 2)
            await page.wait_for_timeout(1400)

            editor = None
            for sel in IM_EDITOR:
                editor = await self._wait(page, sel, 4)
                if editor:
                    break
            if not editor:
                done.append(ActionResult(False, "share", name, "进了会话但没找到输入框"))
                await self._dump(page, f"im-{_safe(name)}", (IM_DIALOG,))
                continue
            try:
                await editor.click()
                await page.keyboard.insert_text(body)
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(1500)
                left = (await editor.inner_text() or "").strip()
                ok = body[:6] not in left
                done.append(ActionResult(ok, "share", name, "已发送" if ok else "输入框没清空，可能没发出去"))
            except Exception as exc:
                done.append(ActionResult(False, "share", name, f"发送出错：{exc}"))
        return done, pending

    # ---------- 评论 ----------

    async def comment(self, aweme_id: str, text: str, mentions: list[str] | None = None) -> ActionResult:
        mentions = [m.strip() for m in (mentions or []) if m and m.strip()]
        if not text.strip():
            return ActionResult(False, "comment", "评论", "评论内容为空")
        if not await self.logged_in():
            return ActionResult(False, "comment", "评论", "未登录：先 /dy login 扫码")

        page = await self._page()

        # 不带 @ 的时候直接走接口：确定性最好（回 cid），也不会被自动连播带走。
        if not mentions:
            ok, cid, why = await self._comment_api(page, aweme_id, text)
            if ok:
                return ActionResult(True, "comment", "评论", f"已发布（cid={cid}），确实在评论区里")
            await self._open_video(page, aweme_id)      # 失败也把现场留一份
            await self._dump(page, "comment-api-fail")
            return ActionResult(False, "comment", "评论", f"接口发布失败：{why}")

        # 带 @ 的也先走接口：能拿 cid（可回读、可删），@ 由 text_extra 表达，
        # 实测服务端会把 sec_uid 补全。界面那条路留着兜底。
        # uid 已经在缓存里就不用开视频页了 —— 少一次导航，也绕开「打不开的视频」。
        if all(self._uid_of(m) for m in mentions):
            ok_api, cid_api, why_api = await self._api_comment_with_at(
                page, aweme_id, text, mentions
            )
            if ok_api:
                return ActionResult(
                    True,
                    "comment",
                    "评论",
                    f"已发布（cid={cid_api}）；@ 已带上（{', '.join(mentions)}）",
                )
        else:
            why_api = "uid 还没缓存，需要开视频页刮分享面板"
        logger.info("[social] 接口带@失败（%s），退回界面路径", why_api)
        if not await self._open_video(page, aweme_id):
            return ActionResult(
                False,
                "comment",
                "评论",
                f"这条视频打不开（可能下架／直播卡／带货页），换下一条；接口带@也没成：{why_api}",
            )
        await page.evaluate(
            "() => { const v = document.querySelector('video'); if (v) { v.pause(); v.autoplay = false; } }"
        )

        if await self._captcha(page):
            raise HumanVerifyNeeded("发评论前页面上就挂着人机验证")

        # 1) 点输入条（「留下你的精彩评论吧」）—— 播放器/评论区挂载完成前点会被水合吃掉
        await page.wait_for_timeout(2200)
        box = None
        for _ in range(16):
            box = await self._rect(page, COMMENT_BOX)
            if self._is_displayed(box):
                break
            await page.wait_for_timeout(500)

        opened = False
        for _attempt in range(3):
            if self._is_displayed(box):
                await self._click_rect(page, box["x"] + box["w"] / 2, box["y"] + box["h"] / 2)
            elif not await self._click_placeholder(page, COMMENT_PLACEHOLDER):
                await self._click_text(page, COMMENT_PLACEHOLDER)
            if await self._wait(page, DRAFT_ANY, 3):
                opened = True
                break
            box = await self._rect(page, COMMENT_BOX)
            await page.wait_for_timeout(700)
        if not opened:
            await self._dump(page, "comment-open-fail", (COMMENT_BOX,))
            return ActionResult(False, "comment", "评论", "评论编辑器没出来（视频可能关闭了评论）")

        editor = await page.query_selector(DRAFT_ANY)
        try:
            await editor.click()
        except Exception:
            pass
        await page.wait_for_timeout(200)

        # 2) 正文
        try:
            await page.keyboard.insert_text(text)
        except Exception:
            await page.keyboard.type(text, delay=30)
        await page.wait_for_timeout(300)

        # 3) @ 好友
        at_ok: list[str] = []
        at_fail: list[str] = []
        for name in mentions:
            try:
                await page.keyboard.type(" @", delay=40)
                await page.keyboard.insert_text(name)
                await page.wait_for_timeout(1500)
                if await self._pick_mention(page, name):
                    at_ok.append(name)
                else:
                    at_fail.append(name)
            except Exception:
                at_fail.append(name)
        if at_fail:
            await self._dump(page, "comment-at-fail", (AT_BOX, DRAFT_ANY))

        # 4) 发送
        self._arm_publish_hook(page)
        self._cid_hook.clear()
        sent = await self._click_send(page)
        await page.wait_for_timeout(1800)
        try:
            left = (await editor.inner_text() or "").strip()
        except Exception:
            left = ""
        ok = sent and not left
        detail = "已发送" if ok else ("点不到发送按钮" if not sent else "编辑器没清空，可能被拦了")
        why = await self._blocked_reason(page)
        if why and not ok:
            # 二次验证（second-verify-mask）跟滑块一样要人来点一下，
            # 抛给上层走「搬窗口 + 叫人」那条路，别只回一句失败。
            raise HumanVerifyNeeded(f"发评论被要求二次验证（{why}）")
        if why:
            if ok:
                detail = f"已发送；但抖音弹了二次验证（{why}），建议在浏览器窗口里过一次，否则后面可能被拦"
            else:
                detail = f"触发了人机验证（{why}），请在浏览器窗口里手动过一次验证再重试"
        if at_ok:
            detail += f"；@ 成功 {len(at_ok)} 个（{', '.join(at_ok)}）"
        if at_fail:
            detail += f"；@ 没找到：{', '.join(at_fail)}"
        if not ok:
            await self._dump(page, "comment-send-fail", (DRAFT_ANY, AT_BOX))
        else:  # 界面发表本身拿不到 cid：先看回包钩子，再回读评论区
            cid = self._cid_hook.pop() if self._cid_hook else ""
            if not cid:
                cid = await self._find_my_comment(page, aweme_id, text)
            if cid:
                detail += f"（cid={cid}）"
        return ActionResult(ok, "comment", "评论", detail)

    def _arm_publish_hook(self, page) -> None:
        """给页面挂一个 publish 回包监听，把 cid 捡起来。"""
        if getattr(self, "_hook_page", None) is page:
            return
        self._hook_page = page

        async def _on_response(resp):
            try:
                if "/comment/publish" not in resp.url:
                    return
                data = await resp.json()
                cid = str(((data or {}).get("comment") or {}).get("cid") or "")
                if cid:
                    self._cid_hook.append(cid)
                    logger.info(f"[social] 抓到评论回包（cid={cid}）")
                else:
                    logger.info(f"[social] 评论回包没 cid：{str(data)[:150]}")
            except Exception:  # noqa: BLE001
                pass

        try:
            page.on("response", _on_response)
        except Exception as exc:  # noqa: BLE001
            logger.info(f"[social] 挂不上评论回包监听：{exc}")

    async def _pick_mention(self, page, name: str) -> bool:
        """在 `.atBox-inner-container` 候选里点中昵称。候选是横向卡片，靠后的会滚出可视区。"""
        if not await self._wait(page, AT_BOX, 3):
            return False

        rect = await page.evaluate(
            """(args) => {
                   const [boxSel, name] = args;
                   const root = document.querySelector(boxSel);
                   if (!root) return null;
                   const hits = Array.from(root.querySelectorAll('div,span'))
                       .filter((e) => (e.innerText || '').trim() === name);
                   if (!hits.length) return null;
                   let card = hits[0];
                   for (let i = 0; i < 4 && card.parentElement; i += 1) {
                       if (card.querySelector('img, [role="listitem"], .semi-avatar')) break;
                       card = card.parentElement;
                   }
                   card.scrollIntoView({block: 'center', inline: 'center'});
                   const r = card.getBoundingClientRect();
                   return {x: r.x, y: r.y, w: r.width, h: r.height};
               }""",
            [AT_BOX, name],
        )
        if self._is_displayed(rect):
            await page.wait_for_timeout(250)
            await self._click_rect(page, rect["x"] + rect["w"] / 2, rect["y"] + rect["h"] / 2)
            await page.wait_for_timeout(600)
            if await self._editor_has(page, f"@{name}"):
                return True

        # 兜底：前缀匹配 + JS 点击
        try:
            await page.evaluate(
                """(args) => {
                       const [boxSel, name] = args;
                       const root = document.querySelector(boxSel);
                       if (!root) return false;
                       const hit = Array.from(root.querySelectorAll('div,span'))
                           .find((e) => { const t = (e.innerText || '').trim();
                                          return t && t.length <= 16 && t.startsWith(name.slice(0, 1)); });
                       if (!hit) return false;
                       let card = hit;
                       for (let i = 0; i < 4 && card.parentElement; i += 1) {
                           if (card.querySelector('img, [role="listitem"], .semi-avatar')) break;
                           card = card.parentElement;
                       }
                       card.click();
                       return true;
                   }""",
                [AT_BOX, name],
            )
        except Exception:
            return False
        await page.wait_for_timeout(600)
        return await self._editor_has(page, "@")

    async def _editor_has(self, page, needle: str) -> bool:
        try:
            content = await page.evaluate(
                """(sel) => { const e = document.querySelector(sel); return e ? (e.innerText || '') : ''; }""",
                DRAFT_ANY,
            )
        except Exception:
            return False
        return needle in (content or "")

    async def _click_send(self, page) -> bool:
        """发送按钮是编辑器右侧最靠右那个 36×36 图标（没有文字，class 是哈希）。"""
        rect = await page.evaluate(
            """(sel) => {
                   const ed = document.querySelector(sel);
                   if (!ed) return null;
                   const r = ed.getBoundingClientRect();
                   const cands = [];
                   for (const e of document.querySelectorAll('div,span,button')) {
                       const b = e.getBoundingClientRect();
                       if (b.width < 20 || b.width > 60 || b.height < 20 || b.height > 60) continue;
                       if (Math.abs(b.y + b.height / 2 - (r.y + r.height / 2)) > 34) continue;
                       if (b.x < r.right - 4 || b.x > r.right + 170) continue;
                       cands.push({x: b.x, y: b.y, w: b.width, h: b.height});
                   }
                   cands.sort((a, b) => b.x - a.x);
                   return cands[0] || null;
               }""",
            DRAFT_ANY,
        )
        if rect and self._is_displayed(rect):
            if await self._click_rect(page, rect["x"] + rect["w"] / 2, rect["y"] + rect["h"] / 2):
                return True
        try:
            await page.keyboard.press("Control+Enter")
            return True
        except Exception:
            return False

    async def _blocked_reason(self, page) -> str:
        """发完看有没有弹人机验证（实测发评论会弹一个白色 iframe 弹层）。"""
        try:
            info = await page.evaluate(
                """() => {
                       const vis = (e) => { const r = e.getBoundingClientRect();
                                            return r.width > 120 && r.height > 80; };
                       const hits = [];
                       for (const f of document.querySelectorAll('iframe')) {
                           const src = (f.getAttribute('src') || '') + (f.id || '');
                           if (/captcha|verify|secsdk|uc_fe_/i.test(src) && vis(f)) {
                               hits.push('iframe:' + src.slice(0, 60));
                           }
                       }
                       for (const e of document.querySelectorAll(
                               '[class*="captcha"],[id*="captcha"],[class*="verify"],' +
                               '[class*="second-verify"],[class*="uc-ui-verify"]')) {
                           if (vis(e)) hits.push('el:' + (e.className || '').toString().slice(0, 44));
                       }
                       for (const d of document.querySelectorAll('div[role="dialog"]')) {
                           const r = d.getBoundingClientRect();
                           if (r.width > 200 && r.height > 120 && (d.innerText || '').trim().length < 3) {
                               hits.push('blank-dialog');
                           }
                       }
                       return hits.slice(0, 4);
                   }"""
            )
        except Exception:
            return ""
        return "、".join(info or [])

    # ---------- 探针 ----------

    async def probe(self, aweme_id: str, kind: str = "both") -> dict:
        """只打开面板、不发送，把结构 dump 下来。返回可 JSON 化的摘要。"""
        kind = (kind or "both").lower()
        want_share = kind in ("share", "panel", "both", "all")
        want_im = kind in ("im", "message", "both", "all")
        want_comment = kind in ("comment", "both", "all")

        report: dict[str, Any] = {"aweme_id": aweme_id, "kind": kind, "logged_in": None, "steps": []}
        page = await self._page()
        report["logged_in"] = await self.logged_in()
        if not await self._open_video(page, aweme_id):
            report["error"] = "打开视频页失败"
            return report
        await page.wait_for_timeout(2200)

        if want_share:
            rect = None
            for _ in range(16):
                rect = await self._rect(page, SHARE_ICON)
                if self._is_displayed(rect):
                    break
                await page.wait_for_timeout(500)
            step: dict[str, Any] = {"share_icon": rect}
            if self._is_displayed(rect):
                cx, cy = rect["x"] + rect["w"] / 2, rect["y"] + rect["h"] / 2
                await page.mouse.move(cx, cy, steps=8)
                await page.wait_for_timeout(300)
                await page.mouse.move(cx, cy - 20, steps=6)
                text = ""
                for _ in range(20):
                    text = await page.evaluate(
                        """(sel) => { const c = document.querySelector(sel);
                               return c ? (c.innerText || '').replace(/\\s+/g, ' ') : ''; }""",
                        SHARE_PANEL,
                    ) or ""
                    if "分享给朋友" in text:
                        break
                    await page.wait_for_timeout(300)
                rows = await page.evaluate(
                    """(sel) => { const c = document.querySelector(sel);
                           if (!c) return [];
                           return Array.from(c.querySelectorAll('[data-userid]')).map((a) => ({
                               uid: a.getAttribute('data-userid'),
                               name: (a.parentElement ? a.parentElement.innerText : '').replace(/\\s+/g, ' ').trim(),
                               label: (a.innerText || '').trim()})); }""",
                    SHARE_PANEL,
                )
                step.update({"panel_opened": "分享给朋友" in text, "panel_text": text[:300], "rows": rows})
                report["share_dump"] = await self._dump(page, "probe-share", (SHARE_PANEL,))
                await page.mouse.move(30, 500, steps=4)
            report["steps"].append(step)

        if want_im:
            opened = await self._open_im(page)
            info = await page.evaluate(
                """(sel) => { const d = document.querySelector(sel);
                       if (!d) return null;
                       return {text: (d.innerText || '').replace(/\\s+/g, ' ').slice(0, 400),
                               convs: Array.from(d.querySelectorAll('[data-e2e="conversation-item"]'))
                                   .map((e) => (e.innerText || '').replace(/\\s+/g, ' ').slice(0, 30)).slice(0, 10),
                               inputs: Array.from(d.querySelectorAll('input'))
                                   .map((e) => e.getAttribute('placeholder') || '').slice(0, 6)}; }""",
                IM_DIALOG,
            )
            report["steps"].append({"im_opened": opened, "im": info})
            report["im_dump"] = await self._dump(page, "probe-im", (IM_DIALOG,))

        if want_comment:
            await self._open_video(page, aweme_id)
            await page.wait_for_timeout(2200)
            box = await self._rect(page, COMMENT_BOX)
            if self._is_displayed(box):
                await self._click_rect(page, box["x"] + box["w"] / 2, box["y"] + box["h"] / 2)
            elif not await self._click_placeholder(page, COMMENT_PLACEHOLDER):
                await self._click_text(page, COMMENT_PLACEHOLDER)
            editor = await self._wait(page, DRAFT_ANY, 4)
            state = None
            if editor:
                state = await page.evaluate(
                    """(sel) => { const e = document.querySelector(sel);
                           if (!e) return null;
                           const r = e.getBoundingClientRect();
                           return {cls: (e.className || '').toString().slice(0, 60),
                                   rect: {x: Math.round(r.x), y: Math.round(r.y),
                                          w: Math.round(r.width), h: Math.round(r.height)}}; }""",
                    DRAFT_ANY,
                )
            at_state = None
            if editor:
                try:
                    await page.keyboard.type(" @测", delay=40)
                    await page.wait_for_timeout(1500)
                    at_state = await page.evaluate(
                        """(sel) => { const b = document.querySelector(sel);
                               if (!b) return null;
                               return {text: (b.innerText || '').replace(/\\s+/g, ' ').slice(0, 200)}; }""",
                        AT_BOX,
                    )
                finally:
                    try:
                        await page.keyboard.press("Escape")
                        await page.evaluate(
                            """(sel) => { const e = document.querySelector(sel); if (e) e.innerHTML = ''; }""",
                            DRAFT_ANY,
                        )
                    except Exception:
                        pass
            report["steps"].append({"comment_editor": state, "at_box": at_state})
            report["comment_dump"] = await self._dump(page, "probe-comment", (COMMENT_BOX, AT_BOX))

        return report


def _safe(name: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fa5-]+", "_", name or "x")[:24]
