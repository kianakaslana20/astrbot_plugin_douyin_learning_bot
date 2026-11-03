"""抖音取流：headful Playwright 拦截页面自身的 tab/feed 响应，并下载无水印源。

为什么不用 HTTP 直打接口：
- `POST /aweme/v1/web/module/feed`（网上流传的"免签"接口）实测返回 HTTP 200 但 body 0 字节；
- `GET /aweme/v1/web/general/search/single` 直接 status_code=2483 风控；
- `GET /aweme/v1/web/tab/feed` 需要 a_bogus 签名（页面自己的 JS 会算）。
所以本模块让页面自己发请求，我们只做「旁观者」把响应收下来。
- headless=True 会被识别成「验证码中间页」，必须 headful。
"""

from __future__ import annotations

import io

import asyncio
import logging
import time
from pathlib import Path

import httpx

from .config import Settings

logger = logging.getLogger("astrbot")

FEED_URL = "https://www.douyin.com/?recommend=1"
HOME_URL = "https://www.douyin.com/"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 推荐流接口：既有 /aweme/v1/web/tab/feed，也有 /aweme/v2/web/module/feed
# （2026-10 实测：同一台机器上前者是「抖音-记录美好生活」，后者是「抖音精选电脑版」，
#  抖音在做 A/B，所以这里不锁版本，按路径特征匹配）
FEED_HINTS = ("/tab/feed", "/module/feed")
FEED_HOST_HINT = "/aweme/"

HOME_URL = "https://www.douyin.com/"

_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
"""


class DouyinBlocked(RuntimeError):
    """被抖音风控拦下（验证码页 / 空响应）。"""


class HumanVerifyNeeded(RuntimeError):
    """抖音弹了人机验证（滑块 / 点选），需要真人操作才能继续。

    为什么单独一个异常：这不是「失败」，是「等你」。上层要能把窗口挪到屏幕中央、
    发消息叫人、然后原地等滑块拖完再接着跑，而不是把这一轮当错误扔掉。
    """

    def __init__(self, reason: str = "抖音弹出了人机验证"):
        super().__init__(reason)
        self.reason = reason


class DouyinBrowser:
    """单例式浏览器：整个插件生命周期只开一份，collect 之间复用登录态。"""

    def __init__(self, settings: Settings, profile_dir: Path):
        self.settings = settings
        self.profile_dir = profile_dir
        self._pw = None
        self._ctx = None
        self._lock = asyncio.Lock()
        self._closed = False
        self.channel_used = ""            # 实际用上的内核：msedge / chromium / chromium(fallback)
        self._last_keepalive = 0.0        # 上次保活的时间戳（time.time()）
        self._session_checked_at = 0.0    # 上次用 /user/profile/self/ 复核登录态的时间
        self._session_expired = False     # sid_guard 说会话已过期（cookie 还在，写操作会回 8）

    # ------------------------------------------------------------------ 生命周期

    async def _ensure(self):
        if self._ctx is not None:
            return self._ctx
        from playwright.async_api import async_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        launch_kwargs: dict = {
            "headless": bool(self.settings.headless),
            "viewport": {"width": 1600, "height": 1000},
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            # 不覆盖 UA：自带的 Client Hints 与真实 UA 一致，改了反而更容易被判定为机器人
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--mute-audio",
                "--disable-notifications",
                "--no-first-run",
                "--lang=zh-CN",
            ],
        }
        # 静默后台：把窗口挪到屏幕外 + 关掉后台节流，跑起来不抢焦点、不打断别人干活。
        # 注意：headless 会被抖音识别成验证码中间页，所以只能「headful 但看不见」。
        if getattr(self.settings, "quiet_window", True):
            launch_kwargs["args"].extend(
                [
                    "--window-position=-32000,-32000",
                    "--window-size=1280,900",
                    "--disable-background-timer-throttling",
                    "--disable-renderer-backgrounding",
                    "--disable-backgrounding-occluded-windows",
                ]
            )
        if self.settings.channel:
            launch_kwargs["channel"] = self.settings.channel
        try:
            self._ctx = await self._pw.chromium.launch_persistent_context(
                str(self.profile_dir), **launch_kwargs
            )
            self.channel_used = self.settings.channel or "chromium"
        except Exception as exc:  # 目录被上一个进程占用等
            # 通道不可用（比如这台机器没装 Edge）就退回自带 chromium，别把整轮搞死
            if launch_kwargs.pop("channel", None):
                logger.warning(
                    "用 %s 内核启动失败（%s），退回 Playwright 自带 chromium",
                    self.settings.channel, type(exc).__name__,
                )
                try:
                    self._ctx = await self._pw.chromium.launch_persistent_context(
                        str(self.profile_dir), **launch_kwargs
                    )
                    self.channel_used = "chromium(fallback)"
                except Exception as exc2:  # noqa: BLE001
                    await self._shutdown_playwright()
                    raise DouyinBlocked(
                        f"浏览器启动失败：{type(exc2).__name__}: {exc2}"
                    ) from exc2
            else:
                await self._shutdown_playwright()
                raise DouyinBlocked(f"浏览器启动失败：{type(exc).__name__}: {exc}") from exc
        self._ctx.set_default_timeout(45_000)
        try:
            await self._ctx.add_init_script(_STEALTH_JS)
        except Exception:
            pass
        return self._ctx

    async def _shutdown_playwright(self) -> None:
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
        self._pw = None

    async def close(self) -> None:
        self._closed = True
        if self._ctx is not None:
            try:
                await self._ctx.close()
            except Exception:
                pass
            self._ctx = None
        await self._shutdown_playwright()

    async def _page(self):
        ctx = await self._ensure()
        pages = [p for p in ctx.pages if not p.is_closed()]
        return pages[0] if pages else await ctx.new_page()

    async def page(self):
        """对外暴露的页面句柄：写操作（分享/评论）复用同一个登录态。"""
        return await self._page()

    # ------------------------------------------------------------------ 取流

    @staticmethod
    def _is_feed(url: str) -> bool:
        if FEED_HOST_HINT not in url:
            return False
        return any(hint in url for hint in FEED_HINTS)

    async def _ingest(self, response, sink: dict[str, dict]) -> None:
        try:
            body = await response.json()
        except Exception:
            return
        if not isinstance(body, dict):
            return
        for key in ("aweme_list", "chime_video_list"):
            for aweme in body.get(key) or []:
                if not isinstance(aweme, dict):
                    continue
                aid = str(aweme.get("aweme_id") or "")
                if aid and aid not in sink:
                    sink[aid] = aweme

    @staticmethod
    def _has_playable(aweme: dict) -> bool:
        video = aweme.get("video") or {}
        for key in ("play_addr", "play_addr_h264", "play_addr_265",
                    "new_download_addr", "download_addr"):
            addr = video.get(key)
            if isinstance(addr, dict) and addr.get("url_list"):
                return True
        for rate in video.get("bit_rate") or []:
            if isinstance(rate, dict) and (rate.get("play_addr") or {}).get("url_list"):
                return True
        return False

    # -------------------------------------------------------------- 人机验证

    CAPTCHA_JS = """
    () => {
      const sels = ['#captcha_container', '.captcha_verify_container',
                    '[class*="captcha_verify"]', '[id*="captcha-verify"]',
                    '[class*="captcha_modal"]', '[class*="verify-wrap"]'];
      for (const s of sels) {
        const e = document.querySelector(s);
        if (!e) continue;
        const r = e.getBoundingClientRect();
        if (r.width > 60 && r.height > 40) return true;
      }
      const hints = ['请完成下列验证', '拖动滑块', '请拖动滑块', '安全验证',
                     '点击完成验证', '请完成安全验证', '滑动验证'];
      return [...document.querySelectorAll('div,span,p,h1,h2')].some(e => {
        const t = (e.innerText || '').trim();
        if (!t || t.length > 30) return false;
        if (!hints.some(h => t.includes(h))) return false;
        const r = e.getBoundingClientRect();
        return r.width > 40 && r.height > 10 && r.top < window.innerHeight;
      });
    }
    """

    async def captcha_visible(self, page=None) -> bool:
        """现在屏幕上是不是挂着人机验证。"""
        try:
            page = page if page is not None else await self._page()
            return bool(await page.evaluate(self.CAPTCHA_JS))
        except Exception:  # noqa: BLE001
            return False

    async def show_window(self, page=None) -> bool:
        """把窗口从屏幕外挪回屏幕中央。

        quiet_window 把窗口放在 (-32000,-32000)，人既看不到也拖不到滑块，
        所以一旦要人工介入就得先搬回来（CDP Browser.setWindowBounds）。
        """
        try:
            page = page if page is not None else await self._page()
            cdp = await page.context.new_cdp_session(page)
            info = await cdp.send("Browser.getWindowForTarget")
            await cdp.send(
                "Browser.setWindowBounds",
                {
                    "windowId": info.get("windowId"),
                    "bounds": {
                        "left": 80, "top": 60, "width": 1280, "height": 900,
                        "windowState": "normal",
                    },
                },
            )
            await page.bring_to_front()
            logger.info("浏览器窗口已挪到屏幕中央")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("挪窗口失败：%s", exc)
            return False

    async def wait_captcha_clear(
        self, timeout_s: int = 300, poll_s: int = 2, page=None
    ) -> bool:
        """等真人把滑块拖完。"""
        page = page if page is not None else await self._page()
        deadline = time.time() + max(30, timeout_s)
        while time.time() < deadline:
            if not await self.captcha_visible(page):
                return True
            await asyncio.sleep(max(1, poll_s))
        return False

    async def collect(self, want: int | None = None) -> list[dict]:
        """打开推荐流，滚动若干屏，返回去重后的 aweme 原始 dict 列表。"""
        want = want or self.settings.want
        async with self._lock:
            page = await self._page()
            sink: dict[str, dict] = {}
            tasks: list[asyncio.Task] = []

            def hook(response) -> None:
                if self._is_feed(response.url):
                    tasks.append(asyncio.create_task(self._ingest(response, sink)))

            page.on("response", hook)
            try:
                await page.goto(FEED_URL, wait_until="domcontentloaded", timeout=60_000)
                await page.wait_for_timeout(2500)

                title = await page.title()
                if "验证" in title or "captcha" in title.lower():
                    raise HumanVerifyNeeded(f"页面标题={title!r}")
                if await self.captcha_visible(page):
                    raise HumanVerifyNeeded("推荐流上挂出了滑块验证")

                scrolls = 0
                while scrolls < self.settings.max_scroll:
                    playable = sum(1 for a in sink.values() if self._has_playable(a))
                    if playable >= want:
                        break
                    try:
                        await page.mouse.wheel(0, 2600)
                    except Exception:
                        break
                    await page.wait_for_timeout(self.settings.scroll_wait_ms)
                    scrolls += 1
                    if scrolls % 2 == 0 and await self.captcha_visible(page):
                        raise HumanVerifyNeeded(f"滚到第 {scrolls} 屏时弹出了人机验证")

                await page.wait_for_timeout(1200)  # 等最后一批响应体落地
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                try:
                    page.remove_listener("response", hook)
                except Exception:
                    pass

            if not sink:
                raise DouyinBlocked("没拦到任何 aweme_list：可能被风控、或页面结构变了")
            return list(sink.values())

    # ------------------------------------------------------------------ 下载

    async def download(self, url: str, dest: Path, max_bytes: int) -> int:
        """流式下载，超过 max_bytes 立即中止。返回写入字节数。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        headers = {"User-Agent": UA, "Referer": "https://www.douyin.com/"}
        written = 0
        timeout = httpx.Timeout(30.0, read=120.0, write=60.0, pool=30.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream("GET", url, headers=headers) as resp:
                resp.raise_for_status()
                if "video" not in (resp.headers.get("content-type") or ""):
                    raise DouyinBlocked(f"直链不是视频：{resp.headers.get('content-type')}")
                with dest.open("wb") as fh:
                    async for chunk in resp.aiter_bytes(256 * 1024):
                        written += len(chunk)
                        if written > max_bytes:
                            fh.close()
                            try:
                                dest.unlink(missing_ok=True)
                            except Exception:
                                pass
                            raise DouyinBlocked(
                                f"视频超过上限 {max_bytes // (1024 * 1024)}MB，已放弃"
                            )
                        fh.write(chunk)
        if written < 10_000:
            try:
                dest.unlink(missing_ok=True)
            except Exception:
                pass
            raise DouyinBlocked(f"下载内容过小（{written} 字节），直链可能已过期")
        return written

    # ------------------------------------------------------------------ 登录

    async def open_login(self, timeout_s: int = 180) -> tuple[str, bool]:
        """打开抖音首页让人扫码登录；返回 (标题, 是否已登录)。"""
        async with self._lock:
            page = await self._page()
            await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                names = {c.get("name") for c in await self._ctx.cookies() if isinstance(c, dict)}
                if "sessionid" in names or "sessionid_ss" in names:
                    return await page.title(), True
                await page.wait_for_timeout(3000)
            return await page.title(), False

    async def cookies(self) -> set[str]:
        if self._ctx is None:
            return set()
        try:
            return {c.get("name") for c in await self._ctx.cookies() if isinstance(c, dict)}
        except Exception:
            return set()

    SELF_JS = """async () => {
    try {
        const r = await fetch('/aweme/v1/web/user/profile/self/?aid=6383',
                              {credentials: 'include'});
        const j = await r.json();
        return {code: j.status_code, nick: (j.user && j.user.nickname) || '',
                uid: (j.user && j.user.uid) || ''};
    } catch (e) { return {code: -999, nick: '', uid: '', err: String(e)}; }
}"""

    # ------------------------------------------------------------ cookie 维护

    async def raw_cookies(self) -> list[dict]:
        """完整 cookie 对象（跨内核搬登录态用，别只拿名字）。"""
        if self._ctx is None:
            return []
        try:
            return [c for c in await self._ctx.cookies() if isinstance(c, dict)]
        except Exception:  # noqa: BLE001
            return []

    async def cookie_map(self) -> dict[str, str]:
        """当前上下文里所有 cookie 的 name→value（写操作排查用）。"""
        if self._ctx is None:
            return {}
        try:
            return {
                c.get("name"): c.get("value")
                for c in await self._ctx.cookies()
                if isinstance(c, dict) and c.get("name")
            }
        except Exception:  # noqa: BLE001
            return {}

    @staticmethod
    def _sid_expiry(value: str) -> float:
        """从 sid_guard 里解过期时间。

        真实格式（实测）：
        `0170c856...|1793743872|5184000|Sat, 02-Jan-2027 22:11:12 GMT`
        第 2 段是**签发时间**不是过期时间，第 3 段是有效期秒数，
        第 4 段才是过期时刻。早先按第 2 段算，会把刚登录的会话判成已过期。
        拿不到第 4 段就退回「签发时间 + 有效期」。
        """
        if not value:
            return 0.0
        parts = [x.strip() for x in str(value).split("|")]
        if len(parts) >= 4 and parts[3]:
            raw = parts[3].replace("+", " ")
            for fmt in ("%a, %d-%b-%Y %H:%M:%S GMT", "%a, %d %b %Y %H:%M:%S GMT"):
                try:
                    import calendar

                    return float(calendar.timegm(__import__("time").strptime(raw, fmt)))
                except Exception:  # noqa: BLE001
                    continue
        issued = next((float(x) for x in parts[1:3] if x.isdigit() and len(x) >= 10), 0.0)
        ttl = next((float(x) for x in parts[2:4] if x.isdigit() and int(x) > 0), 0.0)
        return issued + ttl if issued and ttl else issued

    async def session_info(self) -> dict:
        """当前会话的样子：有没有 sessionid、哪天到期、还剩多久、是不是已复核过。"""
        m = await self.cookie_map()
        sid = m.get("sessionid") or m.get("sessionid_ss") or ""
        exp = self._sid_expiry(m.get("sid_guard", ""))
        now = time.time()
        return {
            "has_session": bool(sid),
            "sessionid_len": len(sid),
            "expires_at": exp,
            "expires_in_h": round((exp - now) / 3600, 1) if exp else 0.0,
            "expired": bool(exp and exp <= now),
            "ttwid": bool(m.get("ttwid")),
            "csrf": bool(m.get("passport_csrf_token") or m.get("passport_csrf_token_default")),
            "checked_at": self._session_checked_at,
            "keepalive_at": self._last_keepalive,
            "channel": self.channel_used,
        }

    async def import_cookies(self, browser: str = "edge", profile: str = "Default") -> int:
        """把本机 Edge/Chrome 里已登录的抖音 cookie 搬进当前上下文，返回搬了几个。

        这是「不用再扫一次码」的捷径；搬完还要用 logged_in() 复核，
        因为浏览器里的 session 本身也可能已经过期。
        """
        from . import cookies as ck

        items = ck.read_douyin_session(browser, profile=profile)
        await self._ensure()
        # 域名统一成 .douyin.com，避免 add_cookies 校验失败
        fixed = []
        for c in items:
            d = c.get("domain") or ".douyin.com"
            fixed.append({**c, "domain": ".douyin.com" if "douyin" in d else d})
        await self._ctx.add_cookies(fixed)
        self._last_keepalive = 0.0
        logger.info("从 %s 搬进 %d 个抖音 cookie", browser, len(fixed))
        return len(fixed)

    async def keepalive(self, *, force: bool = False) -> bool:
        """摸一下抖音，让站点自己续 ttwid / msToken / bd-ticket-guard 这类短寿 cookie。

        网页端的 cookie 不是我们发的，是页面每访问一次就刷新一次。所以「实时拿到
        cookie」这件事的正解不是去偷浏览器的库，而是**让这个浏览器一直像人在用**：
        定时打开一次首页并调一次 self 接口，cookie 由站点自己下发、由 Playwright
        自动落盘到 profile。间隔由 `cookie_keepalive_min` 控制，0 = 关掉。
        """
        gap_min = int(getattr(self.settings, "cookie_keepalive_min", 20) or 0)
        if gap_min <= 0:
            return False
        now = time.time()
        if not force and now - self._last_keepalive < gap_min * 60:
            return False
        if self._ctx is None:
            return False
        try:
            page = await self._page()
            if "douyin.com" not in (page.url or ""):
                await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
            await page.evaluate(self.SELF_JS)
            self._last_keepalive = now
            logger.info("cookie 保活：已摸过一次抖音（间隔 %s 分钟）", gap_min)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("cookie 保活失败：%s", exc)
            return False

    async def logged_in(self, *, launch: bool = True) -> bool:
        """有没有登录态。

        注意：**不能只看 cookie**。session 过期后 cookie 里的 sessionid 还在，
        只看 cookie 会一直报"已登录"，直到点赞/评论回 code 8 才暴露。
        这里用 /user/profile/self/ 回包判定：status_code 0 且带昵称
        （实测游客态是 code 8 且没有 sessionid，过期态是"有 sessionid 但 code 8"，
        两种都能拦住）。
        """
        if self._ctx is None:
            if not launch:
                return False
            try:
                await self._ensure()
            except DouyinBlocked:
                return False
        jar = await self.cookies()
        if not ({"sessionid", "sessionid_ss"} & jar):
            return False
        # sid_guard 的第二段是服务端给的过期时间戳。过期之后 sessionid 还在，
        # /user/profile/self/ 甚至还能回昵称，但**写操作一律回 status_code 8**。
        # 所以要单独看这个时间：快到点了就按「需要重新登录」处理，让上层去叫人。
        exp = self._sid_expiry((await self.cookie_map()).get("sid_guard", ""))
        if exp and exp - time.time() < 300:
            logger.warning(
                "抖音会话%s（sid_guard 过期时间 %s），需要重新扫码",
                "已过期" if exp <= time.time() else "5 分钟内到期",
                time.strftime("%m-%d %H:%M", time.localtime(exp)),
            )
            self._session_expired = True
            return False
        self._session_expired = False
        self._who: dict = {}
        try:
            page = await self._page()
            if "douyin.com" not in (page.url or ""):
                await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
            who = await page.evaluate(self.SELF_JS)
            self._session_checked_at = time.time()
            self._last_keepalive = self._session_checked_at   # 复核也算一次保活
        except Exception as exc:  # noqa: BLE001
            logger.warning("登录态复核失败（按未登录处理）：%s", exc)
            return False
        if isinstance(who, dict) and who.get("code") == 0 and who.get("nick"):
            self._who = who
            return True
        return False

    async def whoami(self) -> dict:
        """返回 {'nick','uid'}（没登录就是空 dict）。"""
        if await self.logged_in():
            return dict(getattr(self, "_who", {}) or {})
        return {}

    @staticmethod
    def _qr_dark_ratio(img) -> float:
        """二维码区域里暗像素占比。二维码通常是 30~50%，刚打开时只有白底+logo（<5%）。"""
        try:
            gray = img.convert("L")
            hist = gray.histogram()
            total = sum(hist)
            dark = sum(hist[:110])
            return (dark / total) if total else 0.0
        except Exception:
            return 0.0

    @staticmethod
    def _qr_score(img) -> tuple[float, float]:
        """(黑占比, 黑白切换密度)。

        只看"暗像素多"会挑错：抖音首页右下那张「抖音精选 / 下载 APP」是整块深色卡片，
        暗像素 100%，但二维码的特征是**黑白高频交替**。所以再加一个切换密度：
        相邻像素不同色的比例——实测掉登录面板里的二维码是 0.048（黑占比 0.22），
        深色卡片是 0.000（黑占比 1.0），一格一格的文字块约 0.03~0.05。
        因此阈值定成「黑占比 0.12~0.6 且切换密度 ≥0.02」，IMG/CANVAS 再给一点加权。
        """
        try:
            from PIL import Image as _Image

            g = img.convert("L")
            if g.width > 200:
                g = g.resize((200, 200))
            px = list(g.getdata())
            w, h = g.size
            black = sum(1 for v in px if v < 128) / float(len(px))
            flips = 0
            total = 0
            for y in range(h):
                row = px[y * w : (y + 1) * w]
                for x in range(w - 1):
                    total += 1
                    if (row[x] < 128) != (row[x + 1] < 128):
                        flips += 1
            for x in range(w):
                for y in range(h - 1):
                    total += 1
                    if (px[y * w + x] < 128) != (px[(y + 1) * w + x] < 128):
                        flips += 1
            return black, (flips / total if total else 0.0)
        except Exception:  # noqa: BLE001
            return 0.0, 0.0

    # 抖音登录弹层有几种变体（A/B + 半登录），这些文字用来判断现在在哪一步
    LOGIN_TABS = ("扫码登录", "验证码登录", "密码登录")
    ONE_CLICK_HINTS = ("一键登录", "登录其他账号")

    # 二维码在弹层里是一个 data:image/png;base64 的 <img>，直接读 src 比截图稳
    QR_DATA_JS = """
    () => {
      const vw = innerWidth, vh = innerHeight;
      const cand = [];
      document.querySelectorAll('img').forEach(e => {
        const src = e.currentSrc || e.src || '';
        if (!src.startsWith('data:image')) return;
        const r = e.getBoundingClientRect();
        const w = Math.round(r.width), h = Math.round(r.height);
        if (w < 110 || w > 360 || Math.abs(w - h) > 10) return;
        if (r.x < 0 || r.y < 0 || r.x + w > vw || r.y + h > vh) return;
        cand.push({w, h, x: Math.round(r.x), y: Math.round(r.y), src});
      });
      const best = cand.sort((a, b) => b.w - a.w)[0];
      return best ? best : null;
    }
    """

    # 兜底用：枚举视口内的方形候选（老版本的弹层把码画在 canvas/div 上，没有 data:image）
    QR_CANDIDATE_JS = """
    () => {
      const vw = innerWidth, vh = innerHeight;
      const seen = new Set(), out = [];
      document.querySelectorAll('canvas,img,div,span').forEach(e => {
        const r = e.getBoundingClientRect();
        const w = Math.round(r.width), h = Math.round(r.height);
        if (w < 90 || w > 330 || Math.abs(w - h) > 8) return;
        if (r.x < 0 || r.y < 0 || r.x + w > vw || r.y + h > vh) return;
        const key = `${Math.round(r.x)},${Math.round(r.y)},${w}`;
        if (seen.has(key)) return;
        seen.add(key);
        out.push({w, h, x: Math.round(r.x), y: Math.round(r.y)});
      });
      return out.slice(0, 40);
    }
    """

    async def _open_login_panel(self, page) -> None:
        """把弹层开到「扫码登录」这一屏。

        掉登录但 cookie 还在时，抖音给的是**一键登录**半登录面板（头像 + 一键登录按钮），
        里面根本没有二维码——必须先点「登录其他账号」才会切到扫码登录。
        """
        need = await page.evaluate(
            "(tabs) => !tabs.some(t => [...document.querySelectorAll('div,span,p')]"
            ".some(e => (e.innerText||'').trim() === t))",
            list(self.LOGIN_TABS),
        )
        if need:
            try:
                await page.evaluate(
                    """() => {
                      const hits = [...document.querySelectorAll('div,span,a,button')].filter(e => {
                        if ((e.innerText || '').trim() !== '登录') return false;
                        const r = e.getBoundingClientRect();
                        return r.width > 10 && r.width < 220 && r.height > 10 && r.height < 80;
                      });
                      if (!hits.length) return false;
                      const el = hits[hits.length - 1];
                      el.click();
                      el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                      return true;
                    }"""
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("点登录入口失败：%s", exc)
            await page.wait_for_timeout(1800)

        # 一键登录面板没有二维码，得先切「登录其他账号」
        one_click = await page.evaluate(
            "(hints) => hints.some(h => [...document.querySelectorAll('div,span,p')]"
            ".some(e => (e.innerText||'').trim() === h))",
            list(self.ONE_CLICK_HINTS),
        )
        if one_click:
            try:
                await page.evaluate(
                    """() => {
                      const hit = [...document.querySelectorAll('div,span,a,button,p')].find(e =>
                        (e.innerText || '').trim() === '登录其他账号' && e.children.length <= 2);
                      if (!hit) return false;
                      hit.click();
                      hit.dispatchEvent(new MouseEvent('click', {bubbles: true}));
                      return true;
                    }"""
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("点「登录其他账号」失败：%s", exc)
            await page.wait_for_timeout(1500)

    async def login_qr(self, dest: Path, timeout_s: int = 30) -> Path | None:
        """让登录弹层停到扫码那一屏，把二维码存成图片，返回路径。

        真机踩出来的六条（前四条是 v1~v4，第五条 v0.5.1，第六条 v0.5.2）：
        1. goto 完立刻截图只有骨架屏，必须等真内容；
        2. 二维码不是 canvas，也不是 img[src*=qrcode]，DOM 里按 class 抓不到；
        3. 不能把 clip 交给 CDP：那层动效没合成，截出来只有中间 logo；
        4. 刚点开时那块还是白底，要按暗像素轮询（画好约 30%，白底 <5%）；
        5. 只认「扫码登录」锚点会漏：半登录态给的是**一键登录面板**，压根没有二维码；
        6. 真正的二维码是 `<img src="data:image/png;base64,...">`，**直接读 src 解 base64** 最稳，
           既不用算坐标，也不会截到导航头像那种 160x160 的方图（踩过：截到了用户头像）。
        """
        try:
            async with self._lock:
                page = await self._page()
                await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
                for _ in range(30):
                    ready = await page.evaluate(
                        "() => !!document.querySelector('img[src*=\"douyinpic\"], img[src*=\"aweme\"]')"
                    )
                    if ready:
                        break
                    await page.wait_for_timeout(500)
                await page.wait_for_timeout(1200)

                await self._open_login_panel(page)

                dest.parent.mkdir(parents=True, exist_ok=True)
                import base64

                from PIL import Image as _Image

                deadline = time.time() + max(12, timeout_s)
                last: dict | None = None
                while time.time() < deadline:
                    try:
                        last = await page.evaluate(self.QR_DATA_JS)
                    except Exception:  # noqa: BLE001
                        last = None
                    if last:
                        try:
                            raw = base64.b64decode(last["src"].split(",", 1)[1])
                        except Exception:  # noqa: BLE001
                            raw = b""
                        if raw:
                            try:
                                img = _Image.open(io.BytesIO(raw))
                                img.load()
                            except Exception:  # noqa: BLE001
                                img = None
                            if img is not None:
                                black, flips = self._qr_score(img)
                                if 0.1 <= black <= 0.7 and flips >= 0.02:
                                    dest.write_bytes(raw)
                                    logger.info(
                                        "二维码已取得（%sx%s 位置 %s，黑占比 %.2f，切换密度 %.3f）",
                                        last["w"], last["h"], (last["x"], last["y"]), black, flips,
                                    )
                                    return dest
                    await page.wait_for_timeout(600)

                # 兜底：没有 data:image 时，用方框候选 + 像素特征再试一次
                best = None
                best_score = 0.0
                for c in (await page.evaluate(self.QR_CANDIDATE_JS)) or []:
                    shot = await page.screenshot(full_page=False)
                    img = _Image.open(io.BytesIO(shot))
                    vw = (page.viewport_size or {}).get("width", img.width)
                    f = img.width / float(vw or img.width)
                    cropped = img.crop(
                        (
                            int(c["x"] * f), int(c["y"] * f),
                            int((c["x"] + c["w"]) * f), int((c["y"] + c["h"]) * f),
                        )
                    )
                    black, flips = self._qr_score(cropped)
                    if 0.12 <= black <= 0.6 and flips >= 0.02 and flips > best_score:
                        best, best_score = cropped, flips
                if best is not None:
                    best.save(dest)
                    logger.warning("二维码走的是兜底截图（切换密度 %.3f）", best_score)
                    return dest
                logger.warning("没拿到二维码（读到 %s），不发图", "data 图" if last else "空")
                return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("截登录二维码失败：%s", exc)
            return None

    async def wait_login(self, timeout_s: int = 360, poll_s: int = 3) -> bool:
        """等人扫码：轮询 cookie，登录上就返回 True。"""
        if self._ctx is None:
            return False
        deadline = time.time() + max(30, timeout_s)
        while time.time() < deadline:
            if bool({"sessionid", "sessionid_ss"} & await self.cookies()):
                return True
            await asyncio.sleep(max(1, poll_s))
        return False
