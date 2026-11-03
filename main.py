"""astrbot_plugin_douyin_learning_bot —— 抖音刷视频学习助手

让 AstrBot 自己刷抖音推荐流：按兴趣与热度挑片 → 下载无水印源 → 抽 9 帧 + 语音转写
→ 模型产出简报 → 推送并沉淀知识库。

作者：YG
AstrBot：>=4.29.1,<5（本机 4.29.3 实测）
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

try:  # v4 标准导出
    from astrbot.api.message_components import Image, Plain
except Exception:  # pragma: no cover - 兼容旧路径
    from astrbot.core.message.components import Image, Plain  # type: ignore

try:  # 正常情况：被 AstrBot 当作 data.plugins.<name>.main 加载
    from .douyin import scoring
    from .douyin.browser import (
        DouyinBlocked,
        DouyinBrowser,
        HumanVerifyNeeded,
        ProfileKernelMismatch,
    )
    from .douyin.config import Settings, resolve_umo
    from .douyin.feed import parse_aweme
    from .douyin.jobs import Job, JobQueue
    from .douyin.pipeline import Pipeline, RoundResult
    from .douyin.social import ActionResult, Social
    from .douyin.store import Store
    from .douyin.understanding import Understander
except ImportError:  # 兜底：被当成顶层模块加载时，把插件目录塞进 sys.path
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from douyin import scoring  # type: ignore
    from douyin.browser import (  # type: ignore
        DouyinBlocked,
        DouyinBrowser,
        HumanVerifyNeeded,
        ProfileKernelMismatch,
    )
    from douyin.config import Settings, resolve_umo  # type: ignore
    from douyin.feed import parse_aweme  # type: ignore
    from douyin.jobs import Job, JobQueue  # type: ignore
    from douyin.pipeline import Pipeline, RoundResult  # type: ignore
    from douyin.social import ActionResult, Social  # type: ignore
    from douyin.store import Store  # type: ignore
    from douyin.understanding import Understander  # type: ignore

PLUGIN_NAME = "astrbot_plugin_douyin_learning_bot"

_SPLIT_NAMES = re.compile(r"[,，、;；/\s]+")


@dataclass
class _ItemRef:
    """从知识库里取出来的一条视频（只带写操作需要的字段）。"""

    aweme_id: str
    title: str = ""
    share_url: str = ""

HELP_TEXT = """/dy            状态与用法
/dy start      排一轮到后台（立刻返回，跑完推给你）
/dy start 2    挑 2 条
/dy loop on|off  后台自动刷
/dy login      把窗口摆到桌面上 + 发一张二维码，扫完自己继续
/dy show       把抖音浏览器窗口调到桌面并置顶（随时可用）
/dy push       把当前会话设为简报推送目标
/dy push 群482755644   固定推到某个群
/dy push 1931855425    固定推到某个 QQ（私聊）
/dy like       给最近这条点赞（/dy like off 取消赞）
/dy share      按配置里的白名单分享最近这条
/dy share 老张,兄弟群   临时指定对象（受白名单限制）
/dy comment    按配置模板评论并 @ 白名单里的人
/dy comment 这条讲得清楚 @小王   临时指定文案与 @ 对象
/dy account ｜ 多开：看 profile / 切号 / 新建（一个号一份，扫码一次就一直在）
/dy verify     抖音弹人机验证时：把窗口搬到桌面并等它过去
/dy jobs       看后台队列：现在在跑什么、进度、排了几个
/dy do         执行待确认的分享/评论
/dy act off|ask|auto   写操作模式
/dy cfg        看当前生效的配置（白名单、模式、模板）
/dy calib      抓 40 条样本，看当前阈值下会学哪些、过筛率多少
/dy dom im|share|comment|both  抓对应面板的真实结构（登录后校准用）
/dy kb [n]     看最近 n 条知识库
/dy cookie     看抖音 cookie 状态（到期时间、保活）；/dy cookie import edge 从本机浏览器借登录态
/dy update     检查 GitHub 上有没有新版本（apply 直接拉取）
/dy stop       关掉浏览器，释放内存"""

SUB_ALIASES = {
    "状态": "status",
    "校准": "calib",
    "分数": "calib",
    "账号": "account",
    "号": "account",
    "多开": "account",
    "profile": "account",
    "帮助": "help",
    "用法": "help",
    "开始": "start",
    "下一轮": "start",
    "登录": "login",
    "窗口": "show",
    "打开": "show",
    "前台": "show",
    "界面": "show",
    "window": "show",
    "show": "show",
    "推送": "push",
    "分享": "share",
    "转发": "share",
    "评论": "comment",
    "点赞": "like",
    "赞": "like",
    "取消赞": "like",
    "执行": "do",
    "确认": "do",
    "模式": "act",
    "验证": "verify",
    "人机验证": "verify",
    "配置": "cfg",
    "参数": "cfg",
    "白名单": "cfg",
    "知识库": "kb",
    "停止": "stop",
    "关闭": "stop",
    "重载": "reload",
    "更新": "update",
    "检查更新": "update",
    "升级": "update",
    "版本": "update",
    "cookie": "cookie",
    "cookies": "cookie",
    "登录态": "cookie",
    "ck": "cookie",
}


class DouyinLearningBot(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.config: object = config if config is not None else {}
        self.settings = Settings.load(self.config)

        self.data_dir: Path = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.media_dir = self.data_dir / "media"
        self.work_dir = self.data_dir / "work"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)

        profile = Path(self.settings.profile_dir) if self.settings.profile_dir else self._profile_path()
        self.store = Store(self.data_dir / "dy.db")
        self.browser = DouyinBrowser(self.settings, Path(profile))
        self.understander = Understander(self.context, self.settings, self.work_dir)
        self.pipeline = Pipeline(
            self.settings, self.store, self.browser, self.understander, self.media_dir
        )
        dump_dir = (self.data_dir / "social_dumps") if self.settings.action_dump else None
        self.social = Social(self.browser, self.settings, dump_dir)
        self._pending: list[tuple] = []

        self._ready = False
        self._busy = asyncio.Lock()
        # 后台队列：所有「刷一轮」都排进这里串行执行，命令不再原地等
        self.jobs = JobQueue(self._run_job, self._notify_job)
        self._login_notice_at = 0.0
        self._loop_task: asyncio.Task | None = None
        self._default_platform = self._detect_platform()
        self._push_target = self._normalize_target(self.settings.push_target)
        self._last_umo = ""
        self._no_target_noticed = False
        self._want_loop = bool(self.settings.enabled and self.settings.loop_enabled)

    def _detect_platform(self) -> str:
        """猜一个平台 id（就是 umo 的第一段，比如「大肥鱼」）。"""
        try:
            insts = list(self.context.platform_manager.platform_insts)
        except Exception:
            return ""
        names = []
        for inst in insts:
            try:
                names.append(inst.meta().name)
            except Exception:
                continue
        return names[0] if names else ""

    def _start_loop_if_needed(self) -> None:
        """配置里开了自动刷就起任务；不在事件循环里时留到第一次命令再起。"""
        if not self._want_loop:
            return
        if self._loop_task and not self._loop_task.done():
            return
        try:
            self._loop_task = asyncio.create_task(self._auto_loop())
        except RuntimeError:  # 当前没有运行中的事件循环
            pass

    # ------------------------------------------------------------------ 工具

    async def _rebind_profile(self) -> None:
        """换 profile 之后把所有依赖浏览器的组件重新指一遍（旧浏览器先关掉）。"""
        try:
            await self.browser.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("关旧浏览器失败：%s", exc)
        profile = self._profile_path()
        profile.mkdir(parents=True, exist_ok=True)
        self.browser = DouyinBrowser(self.settings, Path(profile))
        self.pipeline = Pipeline(
            self.settings, self.store, self.browser, self.understander, self.media_dir
        )
        dump_dir = (self.data_dir / "social_dumps") if self.settings.action_dump else None
        self.social = Social(self.browser, self.settings, dump_dir)
        self._login_notice_at = 0.0

    async def _prompt_login_window(self, umo: str, name: str, seconds: int = 90) -> bool:
        """切到空 profile 后把窗口摆到桌面，让人直接在里面扫码。"""
        old = self.settings.quiet_window
        self.settings.quiet_window = False
        page = None
        try:
            page = await self.browser._page()
            await page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=60_000)
            await asyncio.sleep(1.5)
            shown = await self.browser.show_window(page)
            if not shown:
                await self._ask_help(
                    "浏览器已经起来，但没能把窗口摆到桌面上。你可以在任务栏里找"
                    "「Google Chrome for Testing」，或把面板 browser.quiet_window 关掉后重试。",
                    umo=umo,
                )
            await self._ask_help(
                f"profile「{name}」还没登录，窗口已经开到你桌面上了。\n"
                "在里面点右上角「登录」→ 扫码登录 → 手机点确认。\n"
                f"我盯 {seconds} 秒，登录上就自动记下来、把窗口收回，不用回话。",
                umo=umo,
            )
            deadline = asyncio.get_running_loop().time() + max(30, seconds)
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(5)
                if await self.browser.logged_in(launch=False):
                    await self._ask_help(
                        f"登录成功，cookie 已存进「{name}」，窗口收回了。", umo=umo
                    )
                    return True
            await self._ask_help(
                f"{seconds} 秒内没检测到登录，窗口收回了。\n"
                f"想再试：/dy account {name}（或 /dy login 发一张二维码图）。",
                umo=umo,
            )
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("开窗登录失败：%s", exc)
            return False
        finally:
            self.settings.quiet_window = old
            try:
                await self.browser.close()
            except Exception:  # noqa: BLE001
                pass

    async def _ensure_ready(self) -> None:
        if self._ready:
            return
        await self.store.init()
        self._ready = True
        self._start_loop_if_needed()

    def _reload(self) -> None:
        """配置热更新后重新读一遍（命令里显式调用）。"""
        self.settings = Settings.load(self.config)
        self.browser.settings = self.settings
        self.understander.settings = self.settings
        self.pipeline.settings = self.settings
        self.social.settings = self.settings
        self.social.dump_dir = (
            (self.data_dir / "social_dumps") if self.settings.action_dump else None
        )
        self._push_target = self._normalize_target(self.settings.push_target)

    def _normalize_target(self, spec: str, current_umo: str = "") -> str:
        """把配置里或命令里写的推送目标补全成 umo；补不出来就原样留着。"""
        if not spec:
            return ""
        umo, _ = resolve_umo(
            spec,
            default_platform=self._default_platform,
            current_umo=current_umo,
        )
        return umo or spec

    async def _run_round(self, umo: str, want: int | None = None, take: int = 1) -> RoundResult:
        await self._ensure_ready()
        if not self.settings.enabled:
            return RoundResult(error="插件总开关是关的（配置里 enabled=false）")
        async with self._busy:
            started = time.time()
            result = await self.pipeline.run_once(umo, want=want, take=take)
            await self.store.log(
                "round_done",
                f"{time.time() - started:.1f}s｜candidates={result.candidates}｜"
                f"analyzed={len(result.analyzed)}｜error={result.error}",
            )
            return result

    # ---------------------------------------------------- 多开 profile（一个号一份）

    def _state_path(self) -> Path:
        return self.data_dir / "state.json"

    def _read_state(self) -> dict:
        try:
            return json.loads(self._state_path().read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _write_state(self, **kw) -> None:
        data = self._read_state()
        data.update(kw)
        try:
            self._state_path().write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("写 state.json 失败：%s", exc)

    def _profile_root(self) -> Path:
        root = self.data_dir / "profiles"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _active_profile(self) -> str:
        return str(self._read_state().get("active_profile") or "default")

    def _chan_tag(self) -> str:
        """内核标签：不同内核的 profile 不能混用（实测 Edge 读 chromium 写的库会直接崩）。"""
        chan = (getattr(self.settings, "channel", "") or "").strip().lower()
        return {"": "chromium", "msedge": "msedge", "chrome": "chrome"}.get(chan, chan or "chromium")

    def _profile_base(self, name: str | None = None) -> Path:
        """一个号一份基目录（不带内核标签）。"""
        name = name or self._active_profile()
        if name == "default":
            legacy = self.data_dir / "browser_profile"
            if legacy.exists() and not (self._profile_root() / "default").exists():
                return legacy
        return self._profile_root() / name

    def _profile_path(self, name: str | None = None) -> Path:
        """真正给 Playwright 用的目录 = 基目录 / 内核标签。

        为什么要按内核分：Chromium 和 Edge 写的 profile 数据结构不完全兼容，
        实测拿 chromium 写出来的目录去启 Edge 会以 0xC0000409/STATUS_BREAKPOINT
        直接崩（TargetClosedError）。分开存，再在切换时把 cookie 搬过去。
        """
        return self._profile_base(name) / self._chan_tag()

    def _migration_plan(self) -> tuple[Path, Path] | None:
        """新内核目录里还没有登录态、别处有 → 需要把 cookie 搬一次。

        注意判定用「有没有 sessionid」，不是「目录空不空」：Edge 起过一次就已经
        写了一堆文件，但里面没有登录态，那种情况也要搬。
        """
        base = self._profile_base()
        target = base / self._chan_tag()
        if self._has_session(target):
            return None
        for sub in ("chromium", "msedge", "chrome"):
            cand = base / sub
            if cand != target and self._has_session(cand):
                return cand, target
        if self._has_session(base):
            return base, target          # 升级前的老布局
        return None

    def _list_profiles(self) -> list[tuple[str, Path, bool, str]]:
        """列出所有 profile：(名字, 路径, 有没有登录态, 最后修改时间)。"""
        out: list[tuple[str, Path, bool, str]] = []
        seen: set[str] = set()
        legacy = self.data_dir / "browser_profile"
        if legacy.exists():
            seen.add("default")
            out.append(("default", legacy, self._session_in(legacy), self._mtime(legacy)))
        root = self._profile_root()
        for d in sorted(root.iterdir()) if root.exists() else []:
            if not d.is_dir() or d.name in seen or d.name in {"chromium", "msedge", "chrome"}:
                continue
            out.append((d.name, d, self._session_in(d), self._mtime(d)))
        return out

    def _session_in(self, base: Path) -> bool:
        """基目录（或它下面任一内核子目录）里有没有登录态。"""
        if self._has_session(base):
            return True
        for sub in ("chromium", "msedge", "chrome"):
            if self._has_session(base / sub):
                return True
        return False

    @staticmethod
    def _mtime(p: Path) -> str:
        try:
            return time.strftime("%m-%d %H:%M", time.localtime(p.stat().st_mtime))
        except Exception:  # noqa: BLE001
            return "?"

    @staticmethod
    def _has_session(profile: Path) -> bool:
        """只读地看一眼这份 profile 的 Cookies 里有没有 sessionid（不用起浏览器）。"""
        import shutil
        import sqlite3
        import tempfile

        for rel in ("Default/Network/Cookies", "Network/Cookies", "Cookies"):
            db = profile / rel
            if not db.exists():
                continue
            tmp = Path(tempfile.mkdtemp(prefix="dyck")) / "c.db"
            try:
                shutil.copy2(db, tmp)          # 复制一份查，避免和运行中的浏览器抢锁
                con = sqlite3.connect(str(tmp))
                row = con.execute(
                    "select count(*) from cookies where host_key like '%douyin%' "
                    "and name in ('sessionid','sessionid_ss')"
                ).fetchone()
                con.close()
                if row and row[0]:
                    return True
            except Exception:  # noqa: BLE001
                continue
            finally:
                shutil.rmtree(tmp.parent, ignore_errors=True)
        return False

# ------------------------------------------------------------- 后台任务

    # ---------------------------------------------------------- 找人帮忙

    def _admin_ids(self) -> list[str]:
        """从 AstrBot 主配置里取管理员 id（admins_id）。"""
        out: list[str] = []
        try:
            cfg = self.context.get_config()
            ids = cfg.get("admins_id") if hasattr(cfg, "get") else None
            for x in ids or []:
                x = str(x).strip()
                if x and x not in out:
                    out.append(x)
        except Exception as exc:  # noqa: BLE001
            logger.debug("读 admins_id 失败：%s", exc)
        return out

    def _platform_ids(self) -> list[str]:
        """拿平台 id（拼 unified_msg_origin 用），读不到就退回 default。"""
        out: list[str] = []
        try:
            cfg = self.context.get_config()
            for p in (cfg.get("platform") or []):
                pid = str(p.get("id") or "").strip()
                if pid and pid not in out:
                    out.append(pid)
        except Exception as exc:  # noqa: BLE001
            logger.debug("读平台 id 失败：%s", exc)
        return out or ["default"]

    def _help_targets(self, umo: str = "") -> list[str]:
        """出事时该敲谁的门：面板填的 → 起这一轮的会话 → 推送目标 → 管理员 → 最近说过话的会话。"""
        out: list[str] = []

        def add(x: str | None):
            x = (x or "").strip()
            if x and x not in out:
                out.append(x)

        raw = getattr(self.settings, "help_target", "") or ""
        add(self._normalize_target(raw) if raw else "")
        add(umo)
        add(self.settings.push_target)
        for qq in self._admin_ids():
            for pid in self._platform_ids():
                add(f"{pid}:FriendMessage:{qq}")
        add(self._last_umo)
        return out

    async def _ask_help(self, text: str, image: Path | None = None, umo: str = "") -> bool:
        """把求助信息送到能送到的地方，至少成功一个算成功。"""
        targets = self._help_targets(umo)
        if not targets:
            logger.warning("需要人帮忙，但一个可用的求助目标都没有（面板 notify.help_target 空、也没记到会话）")
            return False
        for target in targets:
            try:
                chain = MessageChain([Plain(text)])
                if image is not None and Path(image).exists():
                    chain.chain.append(Image.fromFileSystem(str(image)))
                await self.context.send_message(target, chain)
                logger.info("求助消息已发到 %s", target)
                return True
            except Exception as exc:  # noqa: BLE001
                logger.warning("求助消息发不到 %s：%s", target, exc)
        return False

    async def _notify_text(self, umo: str, text: str, image: Path | None = None) -> None:
        """往会话里发一条文本（可选带一张图）。失败只记日志，不影响主流程。"""
        if not umo:
            return
        try:
            chain = MessageChain([Plain(text)])
            if image is not None and Path(image).exists():
                chain.chain.append(Image.fromFileSystem(str(image)))
            await self.context.send_message(umo, chain)
        except Exception as exc:  # noqa: BLE001
            logger.warning("推送消息失败（%s）：%s", umo, exc)

    async def _migrate_kernel_profile(self) -> None:
        """换内核（比如 chromium → Edge）后，把老目录里的 cookie 搬到新目录。

        两个内核的 profile 结构不兼容（用 chromium 的目录启 Edge 会 STATUS_BREAKPOINT 崩溃），
        所以只能新建目录 + 把登录态搬过去。搬不成就当没登录，走扫码——不会让整轮失败。
        """
        if getattr(self, "_migrate_tried", False):
            return
        plan = self._migration_plan()
        if not plan:
            return
        self._migrate_tried = True
        old, new = plan
        tag = old.name.lower()
        src_chan = tag if tag in {"msedge", "chrome"} else ""
        logger.info("换内核：把 %s（%s）里的登录态搬到 %s", old, src_chan or "chromium", new)
        snap = None
        try:
            snap = dataclasses.replace(
                self.settings, channel=src_chan, quiet_window=True, headless=False
            )
            src = DouyinBrowser(snap, old)
            await src._ensure()          # 不起上下文就没有 cookie 可读（raw_cookies 会回空）
            all_cookies = await src.raw_cookies()
            cookies = [c for c in all_cookies if "douyin" in (c.get("domain") or "")] or all_cookies
            await src.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("读老 profile 的 cookie 失败（跳过迁移）：%s", exc)
            new.mkdir(parents=True, exist_ok=True)
            return
        if not cookies:
            logger.info("老 profile 里没有 cookie，迁移跳过")
            new.mkdir(parents=True, exist_ok=True)
            return
        try:
            new.mkdir(parents=True, exist_ok=True)
            dst = DouyinBrowser(self.settings, new)
            await dst._ensure()          # 用新内核把目录建起来
            await dst._ctx.add_cookies(cookies)
            await dst.close()
            logger.info("已搬 %d 个 cookie 到 %s（下次直接用，不用扫码）", len(cookies), new)
        except Exception as exc:  # noqa: BLE001
            logger.warning("把 cookie 写进新内核目录失败：%s", exc)
            new.mkdir(parents=True, exist_ok=True)

    async def _try_borrow_cookies(self, job: Job | None = None) -> bool:
        """profile 没登录时，试着从本机 Edge/Chrome 借一次抖音 cookie。

        只在 cookie_source 允许时做（auto/edge/chrome）。借不到就静默返回 False，
        交给扫码流程——绝不让"借不到"变成一个新错误。
        """
        source = (getattr(self.settings, "cookie_source", "auto") or "auto").lower()
        if source == "profile":
            return False
        order = ["edge"] if source == "edge" else (["chrome"] if source == "chrome" else ["edge", "chrome"])
        for name in order:
            try:
                n = await self.browser.import_cookies(name)
                if await self.browser.logged_in():
                    logger.info("已从 %s 借到登录态（%d 个 cookie），不用扫码", name, n)
                    return True
                logger.warning("从 %s 搬了 %d 个 cookie，但 session 已失效", name, n)
            except Exception as exc:  # noqa: BLE001
                logger.info("从 %s 借 cookie 不成功：%s", name, exc)
        return False

    async def _ensure_login(self, job: Job) -> bool:
        """后台跑之前确认登录态；没登录就主动叫用户扫码（发二维码截图 + 等待）。"""
        try:
            if await self.browser.logged_in(launch=False):
                return True
            if await self.browser.logged_in():          # 还没开浏览器，顺手开一个再看
                return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("检查登录态失败：%s", exc)

        # profile 里没有登录态：先看能不能从本机浏览器直接借一次（用户不用扫码）
        if await self._try_borrow_cookies(job):
            return True

        if not self.settings.notify_login:
            job.error = "抖音没登录（notify_login 关着，所以没叫你）"
            return False
        # 同一轮别反复轰炸：10 分钟内只提示一次
        now = time.time()
        if now - self._login_notice_at < 600:
            job.error = "抖音没登录（刚刚提示过，等你扫码）"
            return False
        self._login_notice_at = now

        # 先把窗口摆到桌面上：截图里的码 1 分钟就过期，窗口里的码会自己刷新，
        # 而且人还能在窗口里干别的（选账号、过验证、看报错）。
        shown = False
        if getattr(self.settings, "window_on_login", True):
            shown = await self.browser.show_window()
            if not shown:
                logger.warning("登录时没能把窗口摆到桌面")

        qr: Path | None = None
        if self.settings.login_qr:
            qr = await self.browser.login_qr(self.work_dir / "login_qr.png")
        wait_min = max(1, self.settings.login_wait_min)
        if qr and shown:
            tip = (
                "需要登录抖音才能继续刷。\n"
                "浏览器窗口我已经摆到你桌面上了（置顶）——在里面点右上角「登录」扫码最稳，"
                "窗口里的码会自己刷新。\n"
                f"下面这张图也能扫，但大约 1 分钟就过期（{wait_min} 分钟内我都在等）。\n"
                "扫完不用回话，我会自己发现并继续；超时这轮先跳过，下轮再叫你。"
            )
        elif qr:
            tip = (
                "需要登录抖音才能继续刷。\n"
                f"二维码在下面这张图里，用抖音 App 扫它（{wait_min} 分钟内有效）。\n"
                "扫完不用回话，我会自己发现并继续；超时这轮就先跳过，下轮再叫你。"
            )
        else:
            tip = (
                "需要登录抖音才能继续刷，但这轮没截到二维码图。\n"
                "发一句 /dy login 再试一次（它会把窗口摆到桌面上）；也可以直接发 /dy show 调窗口。"
                "窗口会出现在桌面上，直接在窗口里扫码。\n"
                f"我在这儿等 {wait_min} 分钟。"
            )
        job.stage = "等扫码登录"
        sent = await self._ask_help(tip, qr, job.umo)
        if not sent:
            job.error = "抖音没登录，而且这条求助没送到任何人（面板 notify.help_target 填个会话/QQ）"
            return False

        ok = await self.browser.wait_login(timeout_s=wait_min * 60)
        if ok:
            self._login_notice_at = 0.0
            await self._notify_text(job.umo, "登录成功，继续刷这一轮。")
            job.stage = "已登录，继续"
            return True
        job.error = f"等扫码超时（{wait_min} 分钟），这轮跳过"
        return False

    async def _handle_verify(self, job: Job, exc) -> bool:
        """撞上人机验证：把窗口搬回屏幕、叫人、等滑块拖完。返回是否已恢复。"""
        wait_min = max(1, self.settings.verify_wait_min)
        job.stage = "等人机验证"
        shown = False
        try:
            shown = await self.browser.show_window()
        except Exception as err:  # noqa: BLE001
            logger.warning("搬窗口失败：%s", err)

        if self.settings.verify_notify:
            reason = getattr(exc, "reason", str(exc))
            if shown:
                tip = (
                    f"抖音弹人机验证了（{reason}）。\n"
                    "浏览器窗口我已经搬到屏幕上了，去把它拖一下/点完（窗口别关）。\n"
                    f"我在这儿等 {wait_min} 分钟，过了就接着刷，不用回话。"
                )
            else:
                tip = (
                    f"抖音弹人机验证了（{reason}），但窗口没能搬出来。\n"
                    "把面板里 browser.quiet_window 关掉，再发 /dy verify。"
                )
            await self._ask_help(tip, None, job.umo)

        try:
            ok = await self.browser.wait_captcha_clear(timeout_s=wait_min * 60)
        except Exception as err:  # noqa: BLE001
            logger.warning("等验证失败：%s", err)
            ok = False
        if ok:
            job.stage = "验证通过，继续"
            if self.settings.verify_notify:
                await self._notify_text(job.umo, "验证过了，继续。")
            return True
        job.error = f"人机验证等了 {wait_min} 分钟没人处理，这轮先跳过"
        return False

    async def _run_job(self, job: Job):
        """队列里的实际执行体：登录检查 → 刷一轮 → 写操作。"""
        await self._ensure_ready()
        if not self.settings.enabled:
            job.error = "插件总开关是关的（配置里 enabled=false）"
            return None
        try:
            await self._migrate_kernel_profile()
            if not await self._ensure_login(job):
                return None
        except ProfileKernelMismatch as exc:
            # profile 被要求换内核 = 会写坏它，直接停手，把原因原样告诉他
            job.error = str(exc)
            logger.error("profile 内核不一致，拒绝启动：%s", exc)
            return None
        # 顺手保活：让站点自己续 ttwid / msToken / bd-ticket-guard，别等它悄悄过期
        try:
            await self.browser.keepalive()
        except Exception as exc:  # noqa: BLE001
            logger.warning("保活失败（不影响这一轮）：%s", exc)
        if self.settings.notify_start:
            await self._notify_text(job.umo, f"开始在后台刷这一轮（任务 #{job.id}）…")
        self.pipeline.on_stage = lambda text: self.jobs.set_stage(job, text)
        attempts = 2 if self.settings.verify_retry else 1
        result = None
        try:
            for i in range(attempts):
                try:
                    result = await self._run_round(job.umo, take=job.take)
                    break
                except HumanVerifyNeeded as exc:
                    if i + 1 >= attempts:
                        job.error = f"又弹人机验证（{exc.reason}），这轮放弃"
                        return None
                    if not await self._handle_verify(job, exc):
                        return None
        finally:
            self.pipeline.on_stage = None
        job.result = result
        if result.error and not result.text:
            job.error = result.error
            return result
        try:
            extra = await self._auto_actions(result)
            if extra:
                result.text = (result.text or "") + extra
        except HumanVerifyNeeded as exc:
            if await self._handle_verify(job, exc):
                try:
                    extra = await self._auto_actions(result)
                    if extra:
                        result.text = (result.text or "") + extra
                except Exception as err:  # noqa: BLE001
                    logger.warning("验证后重试写操作失败：%s", err)
        except Exception as exc:  # noqa: BLE001
            logger.warning("写操作阶段失败：%s", exc)
        return result

    async def _notify_job(self, job: Job) -> None:
        """任务结束时通知用户：成功推简报，失败推原因。"""
        result = job.result
        if job.ok and result is not None and getattr(result, "text", ""):
            if not self.settings.notify_done:
                return
            try:
                await self.context.send_message(job.umo, self._chain(result))
            except Exception as exc:  # noqa: BLE001
                logger.warning("推送简报失败：%s", exc)
            return
        if job.error and self.settings.notify_error:
            await self._notify_text(job.umo, f"这轮没成：{job.error}")

    # ------------------------------------------------------------- 写操作（分享/评论）

    async def _pick_item(self, aweme_id: str = "") -> _ItemRef | None:
        """优先用指定 aweme_id，否则取知识库里最近一条已分析的视频。"""
        try:
            rows = await self.store.recent(20)
        except Exception:
            rows = []
        if not rows:
            return None
        row = None
        if aweme_id:
            row = next((r for r in rows if r.get("aweme_id") == aweme_id), None)
        row = row or rows[0]
        return _ItemRef(
            row.get("aweme_id", ""),
            row.get("desc") or "",
            f"https://www.douyin.com/video/{row.get('aweme_id', '')}",
        )

    def _act_text(self) -> str:
        mode = self.settings.action_mode
        label = {"off": "关（只播报）", "ask": "问一句再执行", "auto": "自动执行"}.get(mode, mode)
        guard = "开（只认白名单）" if self.settings.enforce_allowlist else "关（谁都能发）"
        shares = "、".join(self.settings.share_allowlist) or "（空 = 谁都不发）"
        mentions = "、".join(self.settings.mention_allowlist) or "（空 = 不 @ 人）"
        return (
            f"写操作模式：{label}\n"
            f"白名单：{guard}\n"
            f"分享对象：{shares}\n"
            f"评论 @：{mentions}\n"
            f"每轮最多 {self.settings.max_actions_per_round} 条｜动作间隔 {self.settings.action_delay_s}s\n"
            "切换：/dy act off|ask|auto；立即执行：/dy do；手动动：/dy like、/dy share、/dy comment\n"
            "名单和文案都在插件配置面板里改（配置 → 抖音刷视频学习助手 → 写操作），改完 /dy reload\n"
            "分享/评论都要求抖音已登录（/dy login 扫码），没登录会直接告诉你"
        )

    def _comment_text(self, item, analysis) -> str:
        tpl = self.settings.comment_template or "看完记一笔：{summary}"
        values = {
            "title": item.title or "",
            "summary": (analysis.summary or "")[:60],
            "keywords": "、".join(analysis.keywords[:4]),
            "author": item.author or "",
            "url": item.share_url,
        }
        try:
            text = tpl.format(**values)
        except Exception:
            text = "看完记一笔：" + values["summary"]
        return text.strip()[:120]

    def _share_text(self, item) -> str:
        """分享时一起发出去的文字（默认只发链接）。可用 {title} {author} {url}。"""
        tpl = self.settings.share_text or "{url}"
        try:
            text = tpl.format(title=item.title or "", author=item.author or "", url=item.share_url)
        except Exception:
            text = item.share_url
        return text.strip()[:300]

    async def _do_actions(self, pairs: list[tuple]) -> list[str]:
        """对已分析的视频执行分享 + 评论，返回给人看的回执行。"""
        lines: list[str] = []
        budget = max(0, self.settings.max_actions_per_round)
        for item, analysis in pairs[:budget]:
            # 点赞（最低风险的那一档，先做）
            if self.settings.auto_like and not await self.store.acted(
                item.aweme_id, "like", "digg"
            ):
                res = await self.social.like(item.aweme_id, True)
                await self.store.record_action(item.aweme_id, "like", "digg", res.ok, res.detail)
                lines.append(f"　{res}")
                await asyncio.sleep(self.settings.action_delay_s)
            # 分享
            wanted, _rejected = self.settings.check_targets(
                list(self.settings.share_allowlist), kind="share"
            )
            todo = [t for t in wanted if not await self.store.acted(item.aweme_id, "share", t)]
            if todo:
                results = await self.social.share(item.aweme_id, todo, self._share_text(item))
                for res in results:
                    await self.store.record_action(
                        item.aweme_id, "share", res.target, res.ok, res.detail
                    )
                    lines.append(f"　{res}")
                await asyncio.sleep(self.settings.action_delay_s)
            # 评论
            text = self._comment_text(item, analysis)
            mentions, _blocked = self.settings.check_targets(
                list(self.settings.mention_allowlist), kind="mention"
            )
            key = "@".join(mentions) or "评论"
            if text and not await self.store.acted(item.aweme_id, "comment", key):
                res = await self.social.comment(item.aweme_id, text, mentions)
                await self.store.record_action(item.aweme_id, "comment", key, res.ok, res.detail)
                lines.append(f"　{res}")
                await asyncio.sleep(self.settings.action_delay_s)
        return lines

    async def _auto_actions(self, result: RoundResult) -> str:
        """按 action_mode 决定要不要动真格；返回要追加到简报后面的文字。"""
        mode = self.settings.action_mode
        if mode == "off" or not result.analyzed:
            return ""
        if mode == "ask":
            self._pending = result.analyzed[: max(1, self.settings.max_actions_per_round)]
            return "\n\n这条要分享/评论吗？回 /dy do 执行，或 /dy do 跳过"
        try:
            lines = await self._do_actions(result.analyzed)
        except HumanVerifyNeeded:
            raise                      # 交给 _run_job：搬窗口、叫人、验过再重试
        except Exception as exc:  # noqa: BLE001
            logger.warning("写操作失败：%s", exc)
            return f"\n\n写操作失败：{exc}"
        if not lines:
            return ""
        return "\n\n写操作回执：\n" + "\n".join(lines)

    # ------------------------------------------------------------- 版本与更新

    @property
    def _plugin_dir(self) -> Path:
        return Path(__file__).resolve().parent

    @staticmethod
    def _read_version(text: str) -> str:
        m = re.search(r"^version:\s*['\"]?([0-9][0-9A-Za-z.\-]*)", text, re.M)
        return m.group(1) if m else ""

    @staticmethod
    def _ver_key(v: str) -> tuple:
        parts = re.split(r"[.\-+]", v or "")
        out = []
        for p in parts:
            out.append(int(p) if p.isdigit() else 0)
        return tuple(out) or (0,)

    def _git(self, *args: str, timeout: int = 30) -> tuple[int, str]:
        """在插件目录跑一条 git 命令，返回 (returncode, 输出)。不抛异常。"""
        try:
            p = subprocess.run(
                ["git", "-C", str(self._plugin_dir), *args],
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
            return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
        except FileNotFoundError:
            return -2, "这台机器上没有 git"
        except subprocess.TimeoutExpired:
            return -3, f"git {' '.join(args)} 超过 {timeout}s 没回来"
        except Exception as exc:  # noqa: BLE001
            return -1, str(exc)

    async def _cookie_text(self, args: list[str]) -> str:
        """`/dy cookie`：现在这套 cookie 什么状态；`/dy cookie import edge` 真去借一次。"""
        act = (args[0].lower() if args else "").strip()
        if act in {"import", "借", "导入", "edge", "chrome"}:
            name = act if act in {"edge", "chrome"} else (args[1].lower() if len(args) > 1 else "edge")
            try:
                n = await self.browser.import_cookies(name)
            except Exception as exc:  # noqa: BLE001
                return (
                    f"从 {name} 借 cookie 没成：{exc}\n"
                    "（Edge/Chrome 开着的时候库被独占，先把浏览器完全退出再试；"
                    "新版本浏览器的 cookie 用了 App-Bound 加密也解不开——那就扫码）"
                )
            ok = await self.browser.logged_in()
            who = getattr(self.browser, "_who", {}) or {}
            if ok:
                return (
                    f"从 {name} 搬了 {n} 个 cookie，复核通过：{who.get('nick') or '（没拿到昵称）'}\n"
                    "以后不用再扫码了，这些 cookie 已经落在这个 profile 里。"
                )
            return (
                f"从 {name} 搬了 {n} 个 cookie，但抖音说这个 session 已经失效。\n"
                f"发 /dy login 重新扫一次码。"
            )

        info = await self.browser.session_info()
        ident = await self.browser.whoami()
        lines = ["抖音 cookie 状态："]
        lines.append(
            "  登录态：" + ("有效" if ident else ("有 sessionid 但已被拒" if info["has_session"] else "没有 sessionid"))
        )
        if ident:
            lines.append(f"  身份：{ident.get('nick')}（uid {ident.get('uid')}）")
        if info["has_session"]:
            lines.append(f"  sessionid：{info['sessionid_len']} 字符")
        if info["expires_at"]:
            left = info["expires_in_h"]
            lines.append(f"  sid_guard 到期：还剩 {left} 小时" if left > 0 else f"  sid_guard 已到期（{-left} 小时前）")
        lines.append(
            f"  ttwid：{'有' if info['ttwid'] else '没有'}"
            f"｜csrf：{'有' if info['csrf'] else '没有'}"
        )
        lines.append(f"  内核：{info['channel'] or '还没起浏览器'}")
        if info["checked_at"]:
            lines.append(f"  上次向抖音复核：{time.strftime('%H:%M:%S', time.localtime(info['checked_at']))}")
        if info["keepalive_at"]:
            lines.append(f"  上次保活：{time.strftime('%H:%M:%S', time.localtime(info['keepalive_at']))}")
        gap = int(getattr(self.settings, "cookie_keepalive_min", 0) or 0)
        lines.append(
            f"  保活间隔：{str(gap) + ' 分钟' if gap else '关着（cookie 只能靠自然访问续命）'}"
            f"｜借 cookie：{self.settings.cookie_source}"
        )
        paste = str(getattr(self.settings, "cookie_string", "") or "").strip()
        if paste:
            n = len([p for p in paste.replace("\n", ";").split(";") if "=" in p])
            lines.append(f"  粘贴式 cookie：填了（约 {n} 段，启动时灌进 profile）")
        else:
            lines.append("  粘贴式 cookie：没填（browser.cookie_string）")
        lines.append(
            "\n说明：评论/点赞/分享用的就是这套 cookie，它由页面自己下发、"
            "存在 profile 里，不需要你手工粘任何东西。失效时会自动叫你扫码。"
        )
        lines.append("想把本机浏览器里的登录态直接搬过来：/dy cookie import edge")
        lines.append(
            "只有 cookie 串没有浏览器：把它贴到面板 browser.cookie_string，重启一次即可。"
        )
        return "\n".join(lines)

    async def _update_text(self, apply: bool = False) -> str:
        if not (self._plugin_dir / ".git").exists():
            return (
                "这个插件目录不是 git 克隆（多半是解压 zip 装的），没法自动比对版本。\n"
                "要看更新：git clone https://github.com/kianakaslana20/"
                "astrbot_plugin_douyin_learning_bot.git 换掉这个目录，"
                "以后 /dy update 就能用了。"
            )
        local = ""
        try:
            local = self._read_version(
                (self._plugin_dir / "metadata.yaml").read_text(encoding="utf-8")
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("读本地版本失败：%s", exc)

        code, out = await asyncio.to_thread(self._git, "fetch", "--tags", "origin")
        if code != 0:
            return f"取远端失败（git fetch）：{out[:400]}\n网络问题的话稍后再试。"

        code, remote_meta = await asyncio.to_thread(
            self._git, "show", "origin/main:metadata.yaml"
        )
        remote = self._read_version(remote_meta) if code == 0 else ""
        if not remote:
            return f"取不到远端版本号。git show 的输出：{remote_meta[:300]}"

        head_n = self._ver_key(local)
        remote_n = self._ver_key(remote)
        behind_code, behind = await asyncio.to_thread(
            self._git, "log", "--oneline", "--no-decorate", "HEAD..origin/main"
        )
        commits = [ln for ln in behind.splitlines() if ln.strip()] if behind_code == 0 else []

        if remote_n <= head_n and not commits:
            return f"已经是最新的（本地 {local}，远端 {remote}）。"

        lines = [f"本地 {local} → 远端 {remote}"]
        if commits:
            lines.append(f"落后的提交 {len(commits)} 个：")
            lines += [f"　· {c}" for c in commits[:8]]
            if len(commits) > 8:
                lines.append(f"　· …还有 {len(commits) - 8} 个")

        if not apply:
            lines.append("")
            lines.append("要更新：/dy update apply（会 git pull，之后重载插件生效）")
            return "\n".join(lines)

        pull_code, pull_out = await asyncio.to_thread(
            self._git, "pull", "--ff-only", timeout=60
        )
        if pull_code != 0:
            return "\n".join(lines) + f"\n\n拉取失败：{pull_out[:400]}"
        newv = ""
        try:
            newv = self._read_version(
                (self._plugin_dir / "metadata.yaml").read_text(encoding="utf-8")
            )
        except Exception:  # noqa: BLE001
            pass
        lines.append("")
        lines.append(f"已拉到 {newv or remote}。")
        lines.append("代码已更新但还跑在旧代码上——去面板重载本插件（或发 /dy reload）。")
        return "\n".join(lines)

    # ------------------------------------------------------------------ 命令

    @filter.command("dy", alias={"抖音"})
    async def dy(self, event: AstrMessageEvent):
        self._last_umo = getattr(event, "unified_msg_origin", "") or ""
        args = self._args(event.get_message_str())
        sub = args[0].lower() if args else ""
        sub = SUB_ALIASES.get(sub, sub)

        if sub in {"", "status", "info"}:
            yield event.plain_result(await self._status_text())
            return

        if sub in {"help", "?", "用法"}:
            yield event.plain_result(self._help_text())
            return

        if sub in {"start", "next", "go"}:
            take = 1
            if len(args) > 1:
                try:
                    take = max(1, min(5, int(args[1])))
                except ValueError:
                    pass
            job = self.jobs.submit("round", event.unified_msg_origin, take=take)
            if job is None:
                yield event.plain_result(
                    f"队列满了（{len(self.jobs.pending())} 个在排），等它们跑完再提。"
                )
                return
            ahead = max(0, len(self.jobs.pending()) - 1)
            tail = f"，前面还有 {ahead} 个" if ahead else ""
            yield event.plain_result(
                f"已排进后台：任务 #{job.id}，挑 {take} 条{tail}。\n"
                "刷的过程完全在后台（浏览器窗口挪到屏幕外，不抢焦点），"
                "跑完我把简报推给你；中途可以照常用别的命令，/dy status 看进度。"
            )
            return

        if sub == "loop":
            mode = args[1].lower() if len(args) > 1 else ""
            if mode in {"on", "开", "1", "true"}:
                if self._loop_task and not self._loop_task.done():
                    yield event.plain_result("后台自动刷已经在跑了")
                    return
                self._want_loop = True
                self._push_target = self._push_target or event.unified_msg_origin
                self._loop_task = asyncio.create_task(self._auto_loop())
                yield event.plain_result(
                    f"已开启后台自动刷，每 {self.settings.loop_interval_min} 分钟一轮，"
                    f"推送到 {self._push_target or '（未设置，先 /dy push）'}"
                )
                return
            if mode in {"off", "关", "0", "false"}:
                self._want_loop = False
                await self._cancel_loop()
                yield event.plain_result("已关闭后台自动刷")
                return
            yield event.plain_result("用法：/dy loop on|off")
            return

        if sub == "login":
            if await self.browser.logged_in(launch=False):
                yield event.plain_result(
                    "已经是登录状态了，不用再扫。\n"
                    "想看浏览器长什么样：/dy show（会把窗口调到桌面并置顶）"
                )
                return
            yield event.plain_result("正在把浏览器窗口摆到桌面上，顺便要一张二维码…")
            shown = await self.browser.show_window()
            qr = await self.browser.login_qr(self.work_dir / "login_qr.png")
            if shown:
                rect = self.browser.window_rect()
                where = (
                    f"（窗口在屏幕 {rect[0]},{rect[1]}，{rect[2]}×{rect[3]}）"
                    if rect[0] is not None else ""
                )
                yield event.plain_result(
                    f"窗口已经摆到桌面上了{where}，直接在窗口里点右上角「登录」→ 扫码更稳\n"
                    "——窗口里的二维码会自己刷新，聊天里这张约 1 分钟就过期。"
                )
            if qr is None:
                yield event.plain_result(
                    "二维码没截到，但窗口已经开出来了：直接在窗口里扫码就行。\n"
                    "窗口还是找不到就发 /dy show 再叫一次。"
                )
                wait_min0 = max(1, self.settings.login_wait_min)
                ok0 = await self.browser.wait_login(timeout_s=wait_min0 * 60)
                if ok0:
                    self._login_notice_at = 0.0
                    yield event.plain_result("登录成功，登录态已存进 profile，下轮不用再扫。")
                else:
                    yield event.plain_result(f"等了 {wait_min0} 分钟没检测到登录，下轮再叫你。")
                return
            chain = MessageChain([Plain("也可以在聊天里扫这张（约 1 分钟有效）：")])
            chain.chain.append(Image.fromFileSystem(str(qr)))
            yield chain
            wait_min = max(1, self.settings.login_wait_min)
            yield event.plain_result(f"最多等你 {wait_min} 分钟，我这边会轮询登录态。")
            ok = await self.browser.wait_login(timeout_s=wait_min * 60)
            if ok:
                self._login_notice_at = 0.0
                yield event.plain_result("登录成功，登录态已存进 profile，下轮不用再扫。")
            else:
                yield event.plain_result(
                    "还没检测到登录。二维码过期的话再发 /dy login，它会重新截一张。"
                )
            return

        if sub in {"show", "window", "open"}:
            shown = await self.browser.show_window()
            if not shown:
                yield event.plain_result(
                    "没能把窗口摆出来（浏览器可能没起来）。\n"
                    "先发 /dy status 看状态；要还是不行，把面板 browser.quiet_window 关掉再试。"
                )
                return
            rect = self.browser.window_rect()
            who = await self.browser.whoami()
            line = (
                f"窗口已经摆到桌面上并置顶了：屏幕 {rect[0]},{rect[1]}，{rect[2]}×{rect[3]}"
                if rect[0] is not None
                else "窗口已经调用出来了（拿不到坐标，看任务栏）"
            )
            if who:
                line += f"\n当前登录：{who.get('nick')}"
            else:
                line += "\n当前还没登录（窗口里点右上角「登录」扫码，或用 /dy login）"
            yield event.plain_result(line)
            return

        if sub == "account":
            arg = args[1] if len(args) > 1 else ""
            name = args[2] if len(args) > 2 else ""
            if arg in {"new", "add", "新建"}:
                if not name:
                    yield event.plain_result("用法：/dy account new 小号名")
                    return
                path = self._profile_path(name)
                path.mkdir(parents=True, exist_ok=True)
                self._write_state(active_profile=name)
                await self._rebind_profile()
                yield event.plain_result(
                    f"已建好并切到 profile「{name}」：{path}\n"
                    f"这份 profile 是空的，窗口现在开到你桌面上，"
                    f"{self.settings.login_window_s} 秒内扫码就长期有效（和别的小号互不干扰）。"
                )
                asyncio.create_task(
                    self._prompt_login_window(
                        event.unified_msg_origin, name, self.settings.login_window_s
                    )
                )
                return
            if arg:
                if not self._profile_path(arg).exists():
                    yield event.plain_result(f"没有叫「{arg}」的 profile。新建：/dy account new {arg}")
                    return
                self._write_state(active_profile=arg)
                await self._rebind_profile()
                has = bool(self._has_session(self._profile_path(arg)))
                tip = "Cookie 都在这儿，已经登录着。" if has else (
                    f"这份 profile 还没登录，窗口已开到你桌面上，"
                    f"{self.settings.login_window_s} 秒内扫码就行。"
                )
                yield event.plain_result(
                    f"已切到 profile「{arg}」：{self.browser.profile_dir}\n{tip}"
                )
                if not has:
                    asyncio.create_task(
                        self._prompt_login_window(
                            event.unified_msg_origin, arg, self.settings.login_window_s
                        )
                    )
                return
            lines = ["浏览器 profile（一个 profile = 一个抖音号）：", ""]
            active = self._active_profile()
            for pname, ppath, has, mt in self._list_profiles():
                mark = "→" if pname == active else " "
                lines.append(f"{mark} {pname}｜登录态：{'有' if has else '没有'}｜改于 {mt}")
                lines.append(f"    {ppath}")
            if not lines[2:]:
                lines.append("（还没有任何 profile）")
            lines += [
                "",
                f"当前用的是：{active}",
                "切号：/dy account 名字 ｜ 新建：/dy account new 名字",
                "好处：每份 profile 单独存 cookie，扫码一次就一直在，两个号不会互相顶掉。",
            ]
            yield event.plain_result("\n".join(lines))
            return

        if sub == "verify":
            wait_min = max(1, self.settings.verify_wait_min)
            shown = await self.browser.show_window()
            if not shown:
                yield event.plain_result(
                    "窗口没能搬到桌面上（浏览器可能还没起来）。先发 /dy start 或 /dy status 把浏览器叫起来，再试。"
                )
                return
            hanging = await self.browser.captcha_visible()
            if not hanging:
                yield event.plain_result(
                    "窗口已经放到桌面上了，现在页面上没挂着验证。要是你想自己过一次，直接在窗口里操作就行。\n"
                    f"我在这儿盯 {wait_min} 分钟，验证一出来就等它过去。"
                )
            else:
                yield event.plain_result(
                    f"窗口在桌面上了，去把滑块拖完（别关窗口）。等 {wait_min} 分钟，过了我接着说一声。"
                )
            ok = await self.browser.wait_captcha_clear(timeout_s=wait_min * 60)
            yield event.plain_result(
                "验证过了，可以继续了。" if ok else f"等了 {wait_min} 分钟还没过，稍后再发一次 /dy verify。"
            )
            return

        if sub == "calib":
            want = 40
            if len(args) > 1 and args[1].strip().isdigit():
                want = max(10, min(120, int(args[1].strip())))
            yield event.plain_result(f"抓一批样本（{want} 条）看看它们的分数分布，大概 30 秒。")
            try:
                raws = await self.browser.collect(want=want)
            except HumanVerifyNeeded as exc:
                yield event.plain_result(f"取样途中撞上人机验证：{exc.reason}。发 /dy verify 把窗口叫出来。")
                return
            except Exception as exc:  # noqa: BLE001
                yield event.plain_result(f"取样失败：{exc}")
                return
            items = [i for i in (parse_aweme(r) for r in raws) if i]
            if not items:
                yield event.plain_result("没取到可用样本，稍后再试。")
                return
            kept = scoring.rank(items, self.settings)
            ratio = len(kept) / max(1, len(items))
            lines = [
                f"样本 {len(items)} 条，当前阈值下过筛 {len(kept)} 条（{ratio:.0%}）。",
                f"门槛：min_score={self.settings.min_score:g}"
                f"｜min_knowledge_hits={self.settings.min_knowledge_hits}"
                f"｜min_like={self.settings.min_like}"
                f"｜时长 {self.settings.min_duration_s}~{self.settings.max_duration_s}s",
                "",
            ]
            if kept:
                lines.append("会学这些：")
                for s, it in kept[:8]:
                    lines.append(f"  {s:>5.1f}  {it.duration_s:.0f}s  {(it.title or it.desc)[:38]}")
            else:
                lines.append("一条都没过 —— 阈值太紧了。")
                lines.append("")
                lines.append("被挡掉的里分数最高的几条：")
                raw_scores = sorted(
                    ((scoring.score_item(it, self.settings), it) for it in items),
                    key=lambda p: -p[0],
                )
                for s, it in raw_scores[:6]:
                    mark = "（未命中知识词）" if s <= scoring.BLOCKED_SCORE else ""
                    lines.append(f"  {s:>6.1f}{mark}  {it.duration_s:.0f}s  {(it.title or it.desc)[:34]}")
            if ratio < 0.08:
                lines.append("")
                lines.append("→ 过筛偏低。把 feed.min_score 降到 3，或 min_knowledge_hits 设 0 试试。")
            elif ratio > 0.75:
                lines.append("")
                lines.append("→ 过筛偏高，泛娱乐容易混进来。把 feed.min_score 提到 6~8，或 min_knowledge_hits 设 2。")
            yield event.plain_result("\n".join(lines))
            return

        if sub == "push":
            wanted = args[1].strip() if len(args) > 1 else ""
            if not wanted:
                self._push_target = event.unified_msg_origin
                yield event.plain_result(f"简报将推送到当前会话：{self._push_target}")
                return
            umo, note = resolve_umo(
                wanted,
                default_platform=self._default_platform,
                current_umo=event.unified_msg_origin,
            )
            if not umo:
                yield event.plain_result(f"没设成：{note}")
                return
            self._push_target = umo
            yield event.plain_result(f"简报将推送到 {note}：{umo}")
            return

        if sub in {"cfg", "config"}:
            st = self.settings
            mode_label = {
                "off": "关（只播报）",
                "ask": "问一句再执行",
                "auto": "自动执行",
            }.get(st.action_mode, st.action_mode)
            lines = [
                "当前生效配置：",
                f"  写操作模式：{mode_label}",
                f"  白名单：{'开（只认名单）' if st.enforce_allowlist else '关（谁都能发）'}",
                f"  分享通道：{st.share_mode}（panel = 点视频右侧分享面板；im = 私信抽屉）",
                f"  分享白名单：{'、'.join(st.share_allowlist) or '（空 = 谁都不发）'}",
                f"  评论 @ 白名单：{'、'.join(st.mention_allowlist) or '（空 = 不 @ 人）'}",
                f"  评论模板：{st.comment_template or '（默认：看完记一笔：{summary}）'}",
                f"  分享文案：{st.share_text or '（默认：只发链接）'}",
                f"  每轮上限：{st.max_actions_per_round} 条｜动作间隔：{st.action_delay_s}s",
                f"  选片：时长 {st.min_duration_s}-{st.max_duration_s}s｜最低点赞 {st.min_like}"
                f"｜每轮 {st.want} 条｜{st.frames} 帧拼图",
                f"  理解：ASR {st.asr_backend}｜单条下载上限 {st.max_download_mb}MB",
                "改法：配置面板 → 抖音刷视频学习助手 → 写操作 / 选片 / 理解，改完 /dy reload",
            ]
            yield event.plain_result("\n".join(lines))
            return

        if sub == "act":
            mode = (args[1].lower() if len(args) > 1 else "")
            if mode in {"off", "关", "0", "false"}:
                self.settings.action_mode = "off"
            elif mode in {"ask", "confirm", "问", "确认"}:
                self.settings.action_mode = "ask"
            elif mode in {"auto", "on", "开", "1", "true"}:
                self.settings.action_mode = "auto"
            yield event.plain_result(self._act_text())
            return

        if sub == "like":
            rest = list(args[1:])
            want = True
            if rest and rest[-1].lower() in ("off", "0", "取消", "撤", "撤回", "unlike"):
                want = False
                rest.pop()
            aweme_id = ""
            if rest and rest[-1].isdigit() and len(rest[-1]) >= 8:
                aweme_id = rest.pop()
            item = await self._pick_item(aweme_id)
            if item is None:
                yield event.plain_result("还没有可点赞的视频，先 /dy start 刷一条")
                return
            act = "点赞" if want else "取消赞"
            yield event.plain_result(
                f"正在给「{item.title[:30] or item.aweme_id}」{act}…（要动浏览器，约 10 秒）"
            )
            res = await self.social.like(item.aweme_id, want)
            await self.store.record_action(item.aweme_id, "like", "digg", res.ok, res.detail)
            tail = "" if self.settings.auto_like else (
                "\n（auto_like 是关的，所以只有这次手动点赞会动；要每轮自动点就在配置面板打开它）"
            )
            yield event.plain_result(f"{res}{tail}")
            return

        if sub == "share":
            rest = list(args[1:])
            aweme_id = ""
            if rest and rest[-1].isdigit() and len(rest[-1]) >= 8:
                aweme_id = rest.pop()
            names: list[str] = []
            for chunk in rest:
                names.extend([n for n in _SPLIT_NAMES.split(chunk) if n])
            names = names or list(self.settings.share_allowlist)
            if not names:
                yield event.plain_result(
                    "还没设分享对象：在插件配置面板的「写操作 → 分享白名单」里加一行（抖音昵称或群名），"
                    "或者 /dy share 老张,兄弟群 临时指定"
                )
                return
            names, rejected = self.settings.check_targets(names, kind="share")
            if rejected:
                tip = (
                    f"这些人不在分享白名单里，已跳过：{'、'.join(rejected)}\n"
                    "要放开就去配置面板加进「分享白名单」，或者把 enforce_allowlist 关掉"
                )
                if not names:
                    yield event.plain_result(tip)
                    return
                yield event.plain_result(tip)
            if not names:
                return
            item = await self._pick_item(aweme_id)
            if item is None:
                yield event.plain_result("还没有可分享的视频，先 /dy start 刷一条")
                return
            yield event.plain_result(
                f"正在把「{item.title[:30] or item.aweme_id}」分享给 {'、'.join(names)}…"
                "（要动浏览器，约 10-30 秒）"
            )
            results = await self.social.share(item.aweme_id, names, self._share_text(item))
            for res in results:
                await self.store.record_action(
                    item.aweme_id, "share", res.target, res.ok, res.detail
                )
            yield event.plain_result("\n".join(str(r) for r in results) or "没有执行")
            return

        if sub == "comment":
            rest = list(args[1:])
            aweme_id = ""
            if rest and rest[-1].isdigit() and len(rest[-1]) >= 8 and not rest[-1].startswith("@"):
                aweme_id = rest.pop()
            mentions: list[str] = []
            words: list[str] = []
            for token in rest:
                if token.startswith("@") or token.startswith("＠"):
                    name = token.lstrip("@＠").strip()
                    if name:
                        mentions.append(name)
                else:
                    words.append(token)
            mentions = mentions or list(self.settings.mention_allowlist)
            mentions, at_rejected = self.settings.check_targets(mentions, kind="mention")
            item = await self._pick_item(aweme_id)
            if item is None:
                yield event.plain_result("还没有可评论的视频，先 /dy start 刷一条")
                return
            text = " ".join(words).strip()
            if not text:
                rows = await self.store.recent(1)
                summary = (rows[0].get("summary") if rows else "") or ""
                text = (self.settings.comment_template or "看完记一笔：{summary}").replace(
                    "{summary}", summary[:60] or "这条信息量挺大"
                )
            yield event.plain_result(
                f"正在评论「{item.title[:24] or item.aweme_id}」：{text[:40]}"
                f"{'　@' + ' @'.join(mentions) if mentions else ''}…"
            )
            res = await self.social.comment(item.aweme_id, text, mentions)
            await self.store.record_action(
                item.aweme_id, "comment", "@".join(mentions) or "评论", res.ok, res.detail
            )
            out = str(res)
            if at_rejected:
                out += (
                    f"\n（{'、'.join(at_rejected)} 不在 @ 白名单里，没 @ 上；"
                    "加进配置面板的「评论 @ 白名单」即可）"
                )
            yield event.plain_result(out)
            return

        if sub == "do":
            first = args[1].lower() if len(args) > 1 else ""
            if first in {"跳过", "skip", "取消", "cancel", "no"}:
                self._pending = []
                yield event.plain_result("已取消这轮写操作")
                return
            if not self._pending:
                yield event.plain_result(
                    "没有待执行的动作。action_mode=ask 时刷完一轮会问你一次；"
                    "也可以直接用 /dy share、/dy comment"
                )
                return
            pairs = self._pending
            self._pending = []
            yield event.plain_result(f"开始执行 {len(pairs)} 条视频的分享/评论…")
            lines = await self._do_actions(pairs)
            yield event.plain_result("\n".join(lines) or "没有需要执行的（这些视频可能都做过了）")
            return

        if sub in {"dom", "probe", "校准"}:
            kind = (args[1].lower() if len(args) > 1 else "both")
            if kind in {"发消息", "私信"}:
                kind = "im"
            if kind in {"分享", "share"}:
                kind = "share"
            if kind in {"评论", "comment"}:
                kind = "comment"
            if kind in {"全部", "all"}:
                kind = "both"
            aweme_id = ""
            if len(args) > 2 and args[2].isdigit():
                aweme_id = args[2]
            item = await self._pick_item(aweme_id)
            if item is None:
                yield event.plain_result("知识库还是空的，先 /dy start 刷一条，再跑 /dy dom 校准")
                return
            yield event.plain_result(
                f"开始抓「{item.title[:24] or item.aweme_id}」的面板结构"
                f"（{kind}），大概 20 秒…"
            )
            report = await self.social.probe(item.aweme_id, kind)
            yield event.plain_result(report[-1500:] if len(report) > 1500 else report)
            return

        if sub in {"kb", "list"}:
            await self._ensure_ready()
            limit = 5
            if len(args) > 1:
                try:
                    limit = max(1, min(20, int(args[1])))
                except ValueError:
                    pass
            rows = await self.store.recent(limit)
            if not rows:
                yield event.plain_result("知识库还是空的，先来一次 /dy start")
                return
            lines = [f"最近 {len(rows)} 条："]
            for idx, row in enumerate(rows, 1):
                verdict = row.get("verdict") or "-"
                lines.append(
                    f"{idx}. [{verdict}] {(row.get('desc') or '')[:36]}"
                    f"　@{row.get('author') or '?'}\n   {row.get('summary') or '（无摘要）'}"
                )
            yield event.plain_result("\n".join(lines))
            return

        if sub in {"stop", "关闭"}:
            await self._cancel_loop()
            await self.jobs.stop()
            await self.browser.close()
            yield event.plain_result(
                "后台队列已停、浏览器已关闭（恢复：/dy loop on 或 /dy start）"
            )
            return

        if sub in {"cookie", "cookies", "登录态", "ck"}:
            yield event.plain_result(await self._cookie_text(args[1:]))
            return

        if sub in {"update", "upgrade"}:
            apply = any(a in {"apply", "yes", "拉取", "更新"} for a in args[1:])
            yield event.plain_result(await self._update_text(apply=apply))
            return

        if sub in {"jobs", "queue", "任务"}:
            yield event.plain_result(self.jobs.status_text())
            return

        if sub in {"reload", "config"}:
            self._reload()
            yield event.plain_result("配置已重新读取")
            return

        yield event.plain_result(f"没这个子命令：{sub}\n\n{HELP_TEXT}")

    # ------------------------------------------------------------------ LLM 工具

    @filter.llm_tool(name="douyin_watch")
    async def douyin_watch(self, event: AstrMessageEvent, count: int = 1) -> str:
        """刷一条抖音推荐视频，下载并做语音转写与画面理解，返回内容简报。

        当用户说「刷刷抖音」「看看有什么新视频」「帮我找点有意思的」时调用。
        该工具会真实打开浏览器并下载视频，单次约 40-90 秒，不要连续调用。

        Args:
            count(number): 这一轮要理解几条，默认 1，最多 3
        """
        try:
            take = max(1, min(3, int(count or 1)))
        except (TypeError, ValueError):
            take = 1
        result = await self._run_round(event.unified_msg_origin, take=take)
        if result.error and not result.text:
            return f"刷抖音失败：{result.error}"
        return result.text

    # ------------------------------------------------------------------ 后台循环

    async def _auto_loop(self) -> None:
        interval = max(300, self.settings.loop_interval_min * 60)
        logger.info(
            "抖音学习助手：后台循环已启用，间隔 %ss，推送目标 %s",
            interval,
            self._push_target or "（未设置）",
        )
        await asyncio.sleep(90)  # 插件刚加载时先缓一缓，别和别的初始化抢资源
        while True:
            try:
                target = self._push_target
                if not target:
                    logger.info("抖音学习助手：没有推送目标，跳过这轮（先发一次 /dy push）")
                    if not self._no_target_noticed:
                        self._no_target_noticed = True
                        await self._ask_help(
                            "后台自动刷没在跑：还没设推送目标。\n"
                            "在这个会话里发一句 /dy push，我就把简报推这儿来。"
                        )
                else:
                    job = self.jobs.submit("round", target, take=1)
                    if job is None:
                        logger.info("抖音学习助手：队列积压，这轮先跳过")
                    else:
                        # 等这轮跑完再睡，避免任务堆在一起把 profile 锁住
                        for _ in range(int(interval / 3) or 1):
                            await asyncio.sleep(3)
                            if self.jobs.current is None and not self.jobs.pending():
                                break
                        await self._idle_sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("抖音学习助手后台循环异常：%s", exc)
            await asyncio.sleep(interval)

    async def _idle_sleep(self, seconds: float) -> None:
        """两轮之间长睡；闲置够久就把浏览器关掉省内存（下轮会自动重开）。"""
        idle_min = max(0, self.settings.idle_close_min)
        if idle_min and seconds >= idle_min * 60:
            try:
                await self.browser.close()
                logger.info(
                    "抖音学习助手：闲置超过 %s 分钟，浏览器已关闭（下轮自动重开）", idle_min
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("闲置关闭浏览器失败：%s", exc)
        await asyncio.sleep(seconds)

    async def _cancel_loop(self) -> None:
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):
                pass
        self._loop_task = None

    async def _send(self, umo: str, result: RoundResult) -> None:
        try:
            await self.context.send_message(umo, self._chain(result))
        except Exception as exc:
            logger.warning("推送简报失败：%s", exc)

    # ------------------------------------------------------------------ 杂项

    @staticmethod
    def _args(message: str) -> list[str]:
        text = (message or "").strip()
        parts = text.split()
        if parts and (parts[0].startswith("/") or parts[0].lower() in {"dy", "抖音"}):
            parts = parts[1:]
        return parts

    @staticmethod
    def _chain(result: RoundResult) -> MessageChain:
        components: list = [Plain(result.text or "（没有内容）")]
        if result.montage is not None and Path(result.montage).is_file():
            try:
                components.append(Image.fromFileSystem(str(result.montage)))
            except Exception:
                pass
        return MessageChain(components)

    async def _status_text(self) -> str:
        await self._ensure_ready()
        stats = await self.store.stats()
        cookies = await self.browser.cookies()
        logged = "已登录" if ("sessionid" in cookies or "sessionid_ss" in cookies) else "未登录"
        running = "运行中" if (self._loop_task and not self._loop_task.done()) else "关"
        lines = [
            "抖音刷视频学习助手",
            f"总开关：{'开' if self.settings.enabled else '关'}｜后台循环：{running}"
            f"（每 {self.settings.loop_interval_min} 分钟）",
            f"知识库：{stats['analyzed']} 条已分析 / 共记录 {stats['total']} 条",
            f"抖音登录态：{logged}｜浏览器 profile：{self.browser.profile_dir}",
            f"筛选：{self.settings.min_duration_s}-{self.settings.max_duration_s}s"
            f"｜点赞≥{self.settings.min_like}｜每轮想拿 {self.settings.want} 条",
            f"推送目标：{self._push_target or '未设置（/dy push 设当前会话）'}",
            f"写操作：{self.settings.action_mode}"
            f"（分享 {len(self.settings.share_targets)} 个目标 / @ {len(self.settings.mention_targets)} 人）",
        ]
        lines.append(self.jobs.status_text())
        lines.append(
            f"后台方式：窗口{'挪到屏幕外（不抢焦点）' if self.settings.quiet_window else '正常显示'}"
            f"｜等扫码 {self.settings.login_wait_min} 分钟"
            f"｜闲置 {self.settings.idle_close_min} 分钟关浏览器"
        )
        if self._last_umo:
            lines.append(f"当前会话 umo：{self._last_umo}（可直接填进配置 push_target）")
        lines.append("")
        lines.append("用 /dy help 看完整指令")
        return "\n".join(lines)

    @staticmethod
    def _help_text() -> str:
        return "抖音刷视频学习助手·指令\n" + HELP_TEXT

    async def terminate(self) -> None:
        await self._cancel_loop()
        try:
            await self.jobs.stop()
        except Exception:
            pass
        try:
            await self.browser.close()
        except Exception:
            pass
        logger.info("抖音学习助手已卸载")
