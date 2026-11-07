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
import random
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
    from .douyin.agent import DouyinAgent
    from .douyin.im import ImWatcher
    from .douyin.imapi import ImApi, ImApiError
    from .douyin.noticeapi import NoticeApi, NoticeApiError
    from .douyin.inbox import Entry, Inbox
    from .douyin.jobs import Job, JobQueue
    from .douyin.limits import Limits
    from .douyin.memory import PersonMemory
    from .douyin.pipeline import Pipeline, RoundResult
    from .douyin.social import ActionResult, Social
    from .douyin.store import Store
    from .douyin.understanding import Understander
    from .douyin.voice import Voice
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
    from douyin.agent import DouyinAgent
    from douyin.im import ImWatcher
    from douyin.imapi import ImApi, ImApiError  # type: ignore
    from douyin.noticeapi import NoticeApi, NoticeApiError  # type: ignore
    from douyin.inbox import Entry, Inbox  # type: ignore
    from douyin.jobs import Job, JobQueue  # type: ignore
    from douyin.limits import Limits  # type: ignore
    from douyin.memory import PersonMemory  # type: ignore
    from douyin.pipeline import Pipeline, RoundResult  # type: ignore
    from douyin.social import ActionResult, Social  # type: ignore
    from douyin.store import Store  # type: ignore
    from douyin.understanding import Understander  # type: ignore
    from douyin.voice import Voice  # type: ignore

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
    "/dy inbox                翻私信和 @ 提醒，列出新到的视频（加「处理」才动手）",
    "/dy dm 昵称 内容          用登录的号给某人发一条私信（走 dm_allow 白名单）",
/dy update     检查 GitHub 上有没有新版本（apply 直接拉取）
/dy dm 昵称 话  给这个人发条抖音私信（ask 模式会先攒着）
/dy confirm     把攒着的私信真发出去
/dy imapi        试私信 HTTP 通道（只读）：会话数、消息时间戳齐不齐
/dy noticeapi    试通知 HTTP 通道（只读）：有几条、带不带 comment_id
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
    "发送": "send",
    "发出去": "confirm",
    "发出": "confirm",
    "收件箱": "inbox",
    "私信": "dm",
    "提醒": "inbox",
    "@": "inbox",
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

        profile = self._resolve_profile()
        self.store = Store(self.data_dir / "dy.db")
        self.limits = Limits(self.data_dir / "dy.db")
        # 跨平台记忆：自己的 dy.db + 宿主的 data_v4.db（读 QQ 那边的历史）
        self.memory = PersonMemory(
            self.data_dir / "dy.db", astrbot_db=self.data_dir.parent / "data_v4.db"
        )
        self._dm_task: asyncio.Task | None = None
        self._dm_err_at: float = 0.0
        self.browser = DouyinBrowser(self.settings, Path(profile))
        self.understander = Understander(self.context, self.settings, self.work_dir)
        self.pipeline = Pipeline(
            self.settings, self.store, self.browser, self.understander, self.media_dir
        )
        dump_dir = (self.data_dir / "social_dumps") if self.settings.action_dump else None
        self.social = Social(self.browser, self.settings, dump_dir)
        self.voice = Voice(self.context, self.settings)
        self.inbox = Inbox(self.browser, self.settings)
        # 两条只读 HTTP 通道（M0 起）：私信 / 通知。都不碰 DOM，也不发任何东西。
        self.imapi = ImApi(self.browser)
        self.noticeapi = NoticeApi(self.browser)
        self.agent = DouyinAgent(self.voice, self.settings, self.store, self.memory)
        self.watcher = None
        self._ws_dm_seen = []      # [(who, text, ts)] —— WS 已经认领的私信，DOM 那条要跳过
        self._watch_attach_lock = asyncio.Lock()
        self._comment_confirm_lock = asyncio.Lock()
        self.browser.on_page_ready = self._attach_event_page
        self._mention_seen: dict[str, str] = {}
        self._last_round_at = 0.0
        self._auto_rounds_today = 0
        self._auto_items_today = 0
        self._auto_day = ''
        self._think_task = None
        self._last_user_at = 0.0
        self.inbox.sent_lookup = self.memory.recent_me
        self._http_state: dict = {}
        self._pending: list[tuple] = []
        self._pending_kinds: set[str] = set()
        self._pending_at: float = 0.0

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
        self._ensure_dm_watch()
        try:
            if (self.data_dir / "probe_request.json").exists():
                self._probe_task = asyncio.create_task(self._probe_hook())
        except RuntimeError:      # 当前没有运行中的事件循环
            pass

    # ------------------------------------------------------- M0 探针（运维钩子）
    async def _probe_hook(self) -> None:
        """`data_dir/probe_request.json` 存在时跑一次只读探针，结果写 `probe_result.json`。

        为什么要这条钩子：这两条 HTTP 通道只能在**插件进程内**调 —— cookie 活在浏览器
        上下文里，而浏览器又被本进程占着 profile，外部脚本连不上它的 CDP。命令则要有人
        发消息才动。留一个本地文件开关，重载插件就把结果算出来，谁放文件谁拿结果。
        """
        req = self.data_dir / "probe_request.json"
        if not req.exists():
            return
        started = time.time()
        targets = ["im", "notice"]
        try:
            raw = json.loads(req.read_text(encoding="utf-8-sig") or "{}")
            if isinstance(raw, dict) and isinstance(raw.get("probe"), list):
                targets = [str(t) for t in raw["probe"]]
        except Exception:  # noqa: BLE001
            pass
        attempt = 0
        if isinstance(raw, dict):
            try:
                attempt = int(raw.get("attempt") or 0)
            except (TypeError, ValueError):
                attempt = 0
        out: dict = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "targets": targets,
                     "attempt": attempt}
        try:
            page = await self.browser.page()      # 确保浏览器/登录态就绪
            # 刚起的上下文里 cookie jar 还是空的，必须先把 douyin.com 打开一次，
            # profile 里的 cookie 才会被带进这个上下文（否则一律报「没 sessionid」）。
            if "douyin.com" not in (page.url or ""):
                await page.goto("https://www.douyin.com/", wait_until="domcontentloaded",
                                timeout=45000)
                await asyncio.sleep(2.0)
            out["page_url"] = page.url
        except Exception as exc:  # noqa: BLE001
            out["browser"] = f"{type(exc).__name__}: {exc}"
        if "im" in targets:
            out["im"] = await self.imapi.probe()
        if "notice" in targets:
            out["notice"] = await self.noticeapi.probe()
        out["ms"] = int((time.time() - started) * 1000)
        try:
            (self.data_dir / "probe_result.json").write_text(
                json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            req.unlink()
            logger.info("探针：结果已写入 probe_result.json（%s ms）", out["ms"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("探针：结果写不进去（%s）", type(exc).__name__)
        if not out.get("browser"):
            return
        # 浏览器还没起来（上一次内核还在退出）：留着请求，下一轮再来，最多五次。
        if attempt >= 4:
            try:
                req.unlink()
            except OSError:
                pass
            logger.warning("探针：浏览器连续 %s 次没起来，放弃", attempt + 1)
            return
        try:
            req.write_text(
                json.dumps({"probe": targets, "attempt": attempt + 1}), encoding="utf-8"
            )
            logger.info("探针：浏览器还没起来，下一轮再试（第 %s 次）", attempt + 1)
        except OSError:
            pass

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
            self._ensure_dm_watch()
        except RuntimeError:  # 当前没有运行中的事件循环
            pass

    # ------------------------------------------------------------------ 工具

    async def _rebind_profile(self) -> None:
        """换 profile 之后把所有依赖浏览器的组件重新指一遍（旧浏览器先关掉）。"""
        try:
            await self.browser.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("关旧浏览器失败：%s", exc)
        profile = self._resolve_profile()
        profile.mkdir(parents=True, exist_ok=True)
        self.browser = DouyinBrowser(self.settings, Path(profile))
        self.pipeline = Pipeline(
            self.settings, self.store, self.browser, self.understander, self.media_dir
        )
        dump_dir = (self.data_dir / "social_dumps") if self.settings.action_dump else None
        self.social = Social(self.browser, self.settings, dump_dir)
        self.voice = Voice(self.context, self.settings)
        self.inbox = Inbox(self.browser, self.settings)
        self.inbox.sent_lookup = self.memory.recent_me
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
        self.inbox.settings = self.settings
        self.agent.settings = self.settings
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

    def _resolve_profile(self) -> Path:
        """决定这次到底用哪个目录。

        settings.profile_dir 是"人填的"，很容易填到基目录（比如 profiles\小号），
        而那里可能是另一个内核写过的 —— 直接用它会被 channel.lock 拦住，
        表现为「窗口不出来、私信也没动静」。这里做一次无害的纠正：
        基目录带着别的内核锁、而 <内核> 子目录确实存在 → 用子目录。
        """
        raw = (getattr(self.settings, "profile_dir", "") or "").strip()
        if not raw:
            return self._profile_path()
        p = Path(raw).expanduser()
        tag = self._chan_tag()
        if p.name.lower() == tag:
            return p
        other = ""
        lock = p / "channel.lock"
        if lock.exists():
            try:
                other = str(json.loads(lock.read_text("utf-8")).get("channel") or "")
            except Exception:  # noqa: BLE001
                other = ""
        sub = p / tag
        if other and other != tag and sub.exists():
            logger.warning(
                "profile_dir 指到了 %s 目录（那份是 %s 写的），当前内核是 %s，"
                "自动改用 %s/%s 启动（不动原目录）",
                p.name, other, tag, p.name, tag,
            )
            return sub
        if not other and (sub / "Default").exists():
            logger.info("profile_dir 没带内核标签，按内核自动落到 %s", sub)
            return sub
        return p

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
        if getattr(self.settings, "profile_pin", False):
            # profile_pin：目录钉死，绝不动。自动迁移会换目录，
            # 换目录在抖音眼里等于换了台设备，登录态和风控画像都得重来。
            if not getattr(self, "_pin_logged", False):
                self._pin_logged = True
                logger.info(
                    "profile_pin 开着，跳过自动迁移（当前目录：%s）", self._profile_path()
                )
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
        # 顺手清一次过期的记录和临时文件（keep_days 说了算，失败不影响这一轮）
        try:
            await self._retire()
        except Exception as exc:  # noqa: BLE001
            logger.warning("保留期清理失败（不影响这一轮）：%s", exc)
        self._last_round_at = time.time()
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
            self._last_action_lines = []
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

    def _brief_mode(self) -> str:
        """简报模式：off 不推 / actions 只在真动了手时推一句 / all 推完整简报。"""
        mode = str(getattr(self.settings, "brief_mode", "actions") or "actions").lower()
        return mode if mode in {"off", "actions", "all"} else "actions"

    def _brief_of(self, result) -> str:
        """按 brief_mode 决定这轮要推什么；空串表示不推。"""
        if not getattr(self.settings, "notify_done", True):
            return ""
        mode = self._brief_mode()
        if mode == "off":
            return ""
        if mode == "all":
            return getattr(result, "text", "") or ""
        lines = list(getattr(self, "_last_action_lines", []) or [])
        if not lines:
            return ""                      # 这轮只是看看，什么都没发出去：不吵人
        head = "这轮动了手："
        return head + "\n" + "\n".join(lines)

    async def _notify_job(self, job: Job) -> None:
        """任务结束时通知用户：成功推简报，失败推原因。"""
        result = job.result
        if job.ok and result is not None and getattr(result, "text", ""):
            if not self.settings.notify_done:
                return
            payload = self._brief_of(result)
            if not payload:
                return
            if not str(job.umo or "").strip():
                logger.info("简报没发：这个任务没有会话（多半是自主刷且没配 push_target）")
                return
            try:
                await self.context.send_message(job.umo, self._chain_text(result, payload))
            except Exception as exc:  # noqa: BLE001
                logger.warning("推送简报失败：%s", exc)
            return
        if job.error and self.settings.notify_error:
            await self._notify_text(job.umo, f"这轮没成：{job.error}")

    # ------------------------------------------------------------- 写操作（分享/评论）

    # ---------------------------------------------------------- 收件箱（私信/提醒）

    async def _inbox_scan(self, umo: str = "") -> list[Entry]:
        """翻一遍私信与提醒，把没见过的条目落进 inbox 表，返回新的那些。"""
        page = await self.browser.page()
        entries = await self.inbox.collect(
            page,
            include_dm=bool(getattr(self.settings, "inbox_dm", True)),
            include_mentions=bool(getattr(self.settings, "inbox_mentions", True)),
            max_conversations=int(getattr(self.settings, "inbox_max_conversations", 8) or 8),
            max_cards=int(getattr(self.settings, "inbox_max_cards", 3) or 3),
        )
        fresh: list[Entry] = []
        for e in entries:
            is_new = await self.store.inbox_add(
                e.kind,
                e.key,
                src=e.src,
                payload=json.dumps(
                    {"aweme_id": e.aweme_id, "who": e.who, "text": e.text, "when": e.when},
                    ensure_ascii=False,
                ),
            )
            if is_new:
                fresh.append(e)
        if fresh:
            await self.store.log("inbox", f"新 {len(fresh)} 条（{umo}）")
        return fresh

    async def _pending_entries(self, limit: int = 20) -> list[Entry]:
        """库里攒着还没处理的（含以前扫描到但没动手的）。"""
        rows = await self.store.inbox_pending(limit=limit)
        out: list[Entry] = []
        for row in rows:
            payload = {}
            try:
                payload = json.loads(row.get("payload") or "{}")
            except (TypeError, ValueError):
                payload = {}
            out.append(
                Entry(
                    kind=row.get("kind", ""),
                    key=row.get("key", ""),
                    aweme_id=str(payload.get("aweme_id") or ""),
                    who=str(payload.get("who") or ""),
                    text=str(payload.get("text") or ""),
                    when=str(payload.get("when") or ""),
                    src=row.get("src", ""),
                )
            )
        return out

    async def _inbox_handle(self, entries: list[Entry], umo: str) -> list[str]:
        """一条条处理：私信视频写人味总结，@ 提醒看完了回一句。"""
        page = await self.browser.page()
        report: list[str] = []
        for e in entries:
            try:
                if e.kind == "dm_share":
                    line = await self._handle_dm_share(page, e, umo)
                elif e.kind == "dm_text":
                    line = await self._handle_dm_text(e, umo)
                else:
                    line = await self._handle_mention(page, e, umo)
            except HumanVerifyNeeded as exc:
                report.append(f"　{e.key}：撞上人机验证（{exc}），先停")
                break
            except Exception as exc:  # noqa: BLE001
                line = f"没成：{type(exc).__name__}: {exc}"
            report.append(f"· {e.who or e.kind}｜{line}")
            await self.store.inbox_done(e.kind, e.key, line[:200])
        return report

    async def _handle_dm_share(self, page, entry: Entry, umo: str) -> str:
        if not entry.aweme_id:
            return "只有分享卡片、拿不到视频 id，先跳过"
        item = await self.inbox.fetch_item(page, entry.aweme_id)
        if item is None:
            return "这条视频取不到详情（可能已下架）"
        analysis = await self.pipeline._handle_one(item, 0.0, umo)
        if analysis is None:
            return "看不懂这条（转写和画面都没读出来），没敢乱写"
        note = ""
        if getattr(self.settings, "inbox_share_summary", True):
            why = await self.limits.check_async("summary", self.settings)
            if why:
                return f"这条先不写总结：{why}"
            note = await self.voice.summary(
                title=item.desc or item.title,
                author=item.author,
                analysis_summary=analysis.summary,
                keywords=analysis.keywords,
                transcript=analysis.transcript,
                frames=[str(analysis.montage)] if analysis.montage else None,
                umo=umo,
            )
        if note:
            await self.limits.hit_async("summary")
        body = note or analysis.summary or ""
        head = f"{entry.who or '有人'} 发来的：{item.title[:30] or entry.aweme_id}"
        tail = f"\n　　https://www.douyin.com/video/{entry.aweme_id}"
        text = f"{head}\n{body}{tail}" if body else head + tail
        await self._notify_text(umo or self._last_umo, text)
        return "看完了，总结发你了" if note else "看完了（模型没给总结，附了原始摘要）"


    def _identity_map(self) -> dict[str, str]:
        """抖音昵称 -> QQ 号。配置写法：一行一条，`大肥鱼Yume=1931855425`。"""
        raw = getattr(self.settings, "dm_identity", "") or ""
        out: dict[str, str] = {}
        for line in raw.replace("，", "\n").replace(",", "\n").splitlines():
            line = line.strip()
            if not line:
                continue
            for sep in ("=", ":", "：", "\t"):
                if sep in line:
                    k, v = line.split(sep, 1)
                    k, v = k.strip(), v.strip()
                    if k and v.isdigit():
                        out[k] = v
                    break
        return out

    def _qq_of(self, who: str) -> str:
        table = self._identity_map()
        if not table:
            return ""
        if who in table:
            return table[who]
        for k, v in table.items():
            if k and (k in who or who in k):
                return v
        return ""

    def _bridge_people(self) -> list[str]:
        """哪几个抖音昵称的记忆要带到 QQ 那边去。"""
        names: list[str] = []
        for attr in ("dm_chat_allow", "dm_targets", "dm_self_names"):
            raw = (getattr(self.settings, attr, "") or "").replace("，", ",").replace("、", ",")
            for item in raw.split(","):
                item = item.strip()
                if item and item not in names:
                    names.append(item)
        return names

    async def _bridge_prompt_text(self) -> str:
        """QQ 会话要多带的那段：抖音私聊最近几句 + 最近看过的视频。"""
        lines = int(getattr(self.settings, "memory_bridge_lines", 6) or 0)
        items = int(getattr(self.settings, "memory_bridge_items", 4) or 0)
        blocks: list[str] = []
        if lines:
            for who in self._bridge_people():
                try:
                    rows = self.memory.recent(who, limit=lines)
                except Exception:  # noqa: BLE001
                    rows = []
                if rows:
                    blocks.append(
                        f"你在抖音私信里和「{who}」最近聊过（同一个人，别装作不认识）：\n"
                        + "\n".join(rows)
                    )
        if items:
            try:
                rows = await self.store.recent(limit=items)
            except Exception:  # noqa: BLE001
                rows = []
            seen: list[str] = []
            for row in rows or []:
                summary = str(row.get("summary") or "").strip()
                if not summary:
                    continue
                title = str(row.get("title") or row.get("desc") or "").strip()[:40]
                seen.append(f"- {title or '（无标题）'}：{summary[:120]}")
            if seen:
                blocks.append(
                    "你最近刷抖音看过的（你自己看的，可以主动提）：\n" + "\n".join(seen)
                )
        return "\n\n".join(blocks)

    @filter.on_llm_request()
    async def dy_memory_bridge(self, event: AstrMessageEvent, req) -> None:
        """主人在 QQ 上跟她说话时，把抖音那边的记忆一并带进 system prompt。"""
        if not getattr(self.settings, "memory_bridge", True):
            return
        ids = {v for v in self._identity_map().values() if v}
        if not ids:
            return
        try:
            sender = str(event.get_sender_id() or "")
        except Exception:  # noqa: BLE001
            sender = ""
        if sender not in ids:
            return                      # 只对主人自己的号生效，别人的会话不塞这些
        try:
            extra = await self._bridge_prompt_text()
        except Exception as exc:  # noqa: BLE001
            logger.info("记忆互通：拼背景失败（%s: %s）", type(exc).__name__, exc)
            return
        if not extra:
            return
        req.system_prompt = ((getattr(req, "system_prompt", "") or "") + "\n\n" + extra).strip()
        logger.info("记忆互通：给 QQ 会话带了 %s 字抖音记忆", len(extra))

    def _window_needs_restore(self) -> bool:
        """窗口跑到屏幕外/被最小化了吗（只在 quiet_window 关掉时有意义）。"""
        if getattr(self.settings, "quiet_window", True):
            return False
        try:
            left, top, w, h = self.browser.window_rect()
        except Exception:  # noqa: BLE001
            return False
        if left is None:
            return False
        # 最小化态的典型样子：位置 -25600、只剩标题栏 159x27
        return left < -3000 or top < -3000 or w <= 260 or h <= 120

    async def _dm_watch_loop(self) -> None:
        """私信守护入口（0.10.0 起改为「事件触发 + 兜底轮询」）。"""
        await self._event_loop()

    async def _attach_event_page(self, page) -> None:
        """浏览器交出页面的那一刻就把 WS 监听挂上 —— 等到 goto 之后再挂，
        页面加载期间就已经建好的连接就永远看不到了。"""
        if not getattr(self.settings, "event_ws", True):
            return
        if not getattr(self.settings, "dm_chat_enabled", False):
            return
        lock = getattr(self, "_watch_attach_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._watch_attach_lock = lock
        async with lock:
            try:
                if self.watcher is not None and self.watcher.page is page:
                    return
                if self.watcher is not None:
                    await self.watcher.close()
                self.watcher = await self._make_watcher(page)
                logger.info("事件守护：页面刚建就挂上 WS 监听（导航前）")
            except Exception as exc:  # noqa: BLE001
                logger.warning("事件守护：提前挂 WS 没成功（%s），等事件循环再试", exc)

    async def _make_watcher(self, page) -> "ImWatcher":
        """新建监听器：`action_dump` 打开时顺手把原始帧落盘，方便事后验 schema。"""
        watcher = ImWatcher(page)
        if getattr(self.settings, "action_dump", False):
            watcher.dump_dir = str(self.data_dir / "ws_dumps")
        await watcher.start()
        return watcher

    async def _event_loop(self) -> None:
        """Attach before navigation; business pushes wake the consumer, not a scan timer."""
        fallback_s = max(600, int(getattr(self.settings, "poll_fallback_min", 10) or 10) * 60)
        last_poll = time.monotonic()
        logger.info("事件守护：WS 解码 + 通知补读；仅断线超过 300 秒后每 %s 秒兜底", fallback_s)
        while True:
            try:
                await self._ensure_ready()
                page = await self.browser.page()
                if (self.data_dir / "probe_request.json").exists():
                    # 运维探针：谁往数据目录放了这个文件，就在这一轮跑一次只读探针。
                    await self._probe_hook()
                if self.watcher is None or self.watcher.page is not page:
                    if self.watcher is not None:
                        await self.watcher.close()
                    self.watcher = await self._make_watcher(page)
                    logger.info("事件守护：已挂 WS 监听（导航前）")
                if "douyin.com" not in (page.url or ""):
                    await page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=45000)
                if not await self._dm_logged_in():
                    # 没登录时点「消息」只会弹出「登录后即可查看私信消息」，
                    # 以前这里会默默试四轮再放弃，看着像插件坏了 —— 直接叫人扫码。
                    await self._dm_login_notice()
                    await asyncio.sleep(60)
                    continue
                if not getattr(self, "_dm_warmed", False):
                    # Snapshot old DOM only; queued pushes remain available for the next turn.
                    await self._one_event_round("baseline")
                if not getattr(self.settings, "event_ws", True):
                    await asyncio.sleep(fallback_s)
                    source = "poll"
                elif await self._wait_signal(self.watcher, 15):
                    source = "ws"
                elif self.watcher.fallback_due(last_poll, fallback_s):
                    source = "poll"
                else:
                    continue
                if source == "poll":
                    last_poll = time.monotonic()
                await self._one_event_round(source)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("事件守护异常：%s", exc)
                await asyncio.sleep(15)

    async def _dm_logged_in(self) -> bool:
        """私信/事件守护用的轻量登录检查：只看当前会话，不主动开浏览器。"""
        try:
            return bool(await self.browser.logged_in(launch=False))
        except Exception as exc:  # noqa: BLE001
            logger.info("私信守护：登录态没查出来（%s），先当已登录继续", type(exc).__name__)
            return True

    async def _dm_login_notice(self) -> None:
        """掉登录时叫一次人：把窗口搬上来 + 发二维码，10 分钟内只叫一次。"""
        if not getattr(self.settings, "notify_login", True):
            return
        now = time.time()
        if now - self._login_notice_at < 600:
            return
        self._login_notice_at = now
        logger.warning("私信守护：抖音没登录，私信读不到，正在叫人扫码")
        try:
            if getattr(self.settings, "window_on_login", True):
                await self.browser.show_window()
        except Exception:  # noqa: BLE001
            pass
        qr: Path | None = None
        try:
            if getattr(self.settings, "login_qr", True):
                qr = await self.browser.login_qr(self.work_dir / "login_qr.png")
        except Exception as exc:  # noqa: BLE001
            logger.info("私信守护：截二维码没成功（%s）", type(exc).__name__)
        tip = (
            "抖音掉登录了，私信和评论都读不到。\n"
            "浏览器窗口我摆到桌面上了，点右上角「登录」扫码（窗口里的码会自己刷新），"
            "下面这张图也能扫，大约 1 分钟过期。\n"
            "扫完不用回话，我每分钟看一眼，登录上了自己接着干。"
        )
        await self._ask_help(tip, qr, getattr(self.settings, "current_umo", "") or "")

    async def _wait_signal(self, watcher, timeout_s: int) -> bool:
        try:
            await asyncio.wait_for(watcher.wake.wait(), timeout=timeout_s)
            await asyncio.sleep(0.35)  # coalesce a burst, no polling of the UI
            return True
        except asyncio.TimeoutError:
            return False

    async def _one_event_round(self, source: str) -> None:
        page = await self.browser.page()
        watcher = getattr(self, "watcher", None)
        raw, routes = watcher.drain() if watcher and source == "ws" else ([], set())
        include_dm = source != "ws" or bool(routes & {"im", "unknown"})
        include_mentions = source != "ws" or bool(routes & {"notice", "unknown"})
        try:
            entries = await self.inbox.collect(page, include_dm=include_dm, include_mentions=include_mentions)
        except Exception:
            # 界面读失败不能把已经解析出来的推送一起丢掉：放回队列，下一轮再读。
            if watcher and raw:
                watcher.restore(raw)
                for evt in raw:
                    watcher.release(evt)
            raise
        entries = list(entries or [])
        # Enrich a decoded IM event only with an exact UID cache or matching DOM item.
        # No guessing nicknames from a protobuf number; group cards never become public replies.
        for evt in raw:
            extra = evt.get("extra") or {}
            if extra.get("channel") == "im" and extra.get("conversation_type") not in (None, 1, "1"):
                continue
            uid = str(evt.get("from_uid") or "")
            who = str(evt.get("from_nick") or "")
            if not who and uid:
                names = [name for name, slot in getattr(self.social, "_uid_cache", {}).items()
                         if isinstance(slot, dict) and str(slot.get("uid") or "") == uid]
                if len(names) == 1:
                    who = names[0]
            evt["from_nick"] = who
            # Use DOM key where possible, so a later fallback does not replay the same event.
            candidates = [e for e in entries if e.kind in {"dm_text", "dm_share"}
                          and who and e.who == who
                          and ((evt.get("aweme_id") and e.aweme_id == evt["aweme_id"])
                               or (evt.get("content") and e.text == evt["content"]))]
            if len(candidates) == 1:
                candidates[0].extra.update({"event_id": evt.get("event_id"),
                    "raw_frame_id": evt.get("raw_frame_id"), "from_uid": uid,
                    "timestamp": evt.get("timestamp"), "protocol": True})
                if evt.get("type") == "dm" and evt.get("content"):
                    self._ws_mark(who, str(evt["content"]).strip())
                continue
            # 界面没读到这条（面板没跟上、或者对方秒删）—— 协议层已经拿到了内容，
            # 就别丢了：自己造一条条目走同一条处理链路（白名单/限流/静默照旧生效）。
            # 界面里能对上（哪怕对上多条）就不另外造，避免同一条私信回两遍。
            kind_map = {"dm": "dm_text", "share": "dm_share"}
            kind = kind_map.get(str(evt.get("type")))
            if self._ws_self_echo(evt, who):
                # 自己发出去的话也会被推回来一条；当新消息处理就会自己跟自己无限对话。
                await self.store.inbox_add("ws_evidence", str(evt.get("event_id") or evt.get("raw_frame_id")),
                    src="ws", payload=json.dumps(evt, ensure_ascii=False))
                await self.store.inbox_done("ws_evidence", str(evt.get("event_id") or evt.get("raw_frame_id")), "self_echo")
                continue
            if not candidates and kind and who and str(evt.get("content") or "").strip():
                entry = Entry(kind=kind, key=str(evt.get("event_id") or ""), src=who,
                    who=who, text=str(evt["content"]).strip(), when="",
                    aweme_id=str(evt.get("aweme_id") or ""),
                    extra={"event_id": evt.get("event_id"), "raw_frame_id": evt.get("raw_frame_id"),
                           "from_uid": uid, "timestamp": evt.get("timestamp"),
                           "protocol": True, "source": "ws"})
                entries.append(entry)
                if kind == "dm_text":
                    self._ws_mark(who, entry.text)
                continue
            # Preserve structured evidence even when DOM/identity is not ready.
            await self.store.inbox_add("ws_evidence", str(evt.get("event_id") or evt.get("raw_frame_id")),
                src="ws", payload=json.dumps(evt, ensure_ascii=False))
            await self.store.inbox_done("ws_evidence", str(evt.get("event_id") or evt.get("raw_frame_id")), "decoded_wait_identity")
            # Exact nickname alone does not establish that this is an incoming message.
            # Unmatched IM events remain evidence until the UI confirms sender direction.
        # WS 只是「有动静」的信号；正文、时间戳、评论 id 从接口拿。
        # 界面那条路继续留着兜底（接口万一变了，DOM 还能读到）。
        try:
            http_entries = await self._http_scan(
                first=(source == "baseline"), dm=include_dm, notices=include_mentions
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("HTTP 增量拉取失败（走界面兜底）：%s: %s", type(exc).__name__, exc)
            try:
                import traceback
                with open(self.data_dir / "event_errors.log", "a", encoding="utf-8") as fh:
                    fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] http_scan:\n"
                             f"{traceback.format_exc()}\n")
            except Exception:  # noqa: BLE001
                pass
            http_entries = []
        if http_entries:
            entries.extend(http_entries)
        fresh = []
        queued_users = set()
        skipped = replied = judged = 0
        why = []
        peek = getattr(watcher, "peek", None) if (watcher and source == "baseline") else None
        pending_push = list(peek() or []) if callable(peek) else []
        try:
            for e in entries:
                if e.kind not in {"dm_text", "dm_share", "mention", "comment"}:
                    continue
                if e.kind == "dm_text" and not e.extra.get("protocol") and self._ws_claimed(e):
                    await self.store.inbox_done(e.kind, e.key, "ws_dup")
                    skipped += 1
                    why.append("WS 已经认领这条私信")
                    continue
                if pending_push and self._matches_push(e, pending_push):
                    # 首次快照时队列里已经有这条推送 → 留到 WS 那一轮按新消息处理，
                    # 否则「backlog」会把它一起埋掉，看着像没收到。
                    skipped += 1
                    why.append("刚推过来的，留给 WS 那轮")
                    continue
                added = await self.store.inbox_add(e.kind, e.key, src=e.src,
                    payload=json.dumps({"aweme_id": e.aweme_id, "who": e.who, "text": e.text,
                                        "when": e.when, "extra": e.extra}, ensure_ascii=False))
                if not added:
                    continue
                if source == "baseline":
                    await self.store.inbox_done(e.kind, e.key, "backlog")
                    skipped += 1
                    continue
                uid = str(e.extra.get("from_uid") or e.who or e.key)
                if e.kind in {"mention", "comment"}:
                    if uid in queued_users:
                        await self.store.inbox_done(e.kind, e.key, "same_user_pending")
                        skipped += 1
                        why.append("同用户已有待回复评论")
                        continue
                    queued_users.add(uid)
                fresh.append(e)
            self._dm_warmed = True
            self._event_judged_reply = 0
            for e in fresh:
                try:
                    self._last_user_at = time.time()
                    if e.kind in {"mention", "comment"}:
                        line = await self._handle_mention(page, e, self._last_umo or "")
                    elif e.kind == "dm_share":
                        line = await self._handle_dm_share_event(e, self._last_umo or "")
                    else:
                        line = await self._handle_dm_text(e, self._last_umo or "")
                    if line.startswith("回了："):
                        replied += 1
                    else:
                        skipped += 1
                        why.append(line[:100])
                    await self.store.inbox_done(e.kind, e.key, line[:300])
                except Exception as exc:
                    skipped += 1
                    why.append(type(exc).__name__)
                    await self.store.inbox_done(e.kind, e.key, "error:" + type(exc).__name__)
                    stack = ""
                    try:
                        import traceback
                        stack = traceback.format_exc()
                        with open(self.data_dir / "event_errors.log", "a", encoding="utf-8") as fh:
                            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {source} {e.kind}:\n{stack}\n")
                    except Exception:
                        stack = ""
                    logger.warning(f"事件处理失败({source}, {e.kind}): {type(exc).__name__}: {exc}"
                                   + ((" | " + stack.strip().splitlines()[-1][:160]) if stack else ""))
            logger.info("事件守护(%s)：解码 %s 条，读到 %s 条，新 %s 条，模型判断 reply %s 条，发出 %s 条，跳过 %s 条；%s",
                        source, len(raw), len(entries), len(fresh), getattr(self, "_event_judged_reply", 0),
                        replied, skipped, "；".join(why[:5]) or "无")
        finally:
            for evt in raw:
                watcher.release(evt)

    # ------------------------------------------------ HTTP 增量（M1 私信 / M2 通知）
    def _state_load(self, name: str):
        """读一次 HTTP 游标（进程内缓存，落盘 http_state.json）。"""
        cache = getattr(self, "_http_state", None)
        if cache is None:
            cache = self._http_state = {}
        if name in cache:
            return cache[name]
        data: dict = {}
        data_dir = getattr(self, "data_dir", None)
        if data_dir is not None:
            try:
                loaded = json.loads((data_dir / "http_state.json").read_text(encoding="utf-8") or "{}")
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, ValueError):
                data = {}
        default = {} if name == "im" else []
        value = data.get(name)
        cache[name] = value if isinstance(value, type(default)) else default
        return cache[name]

    def _state_save(self) -> None:
        data_dir = getattr(self, "data_dir", None)
        if data_dir is None:
            return
        try:
            (data_dir / "http_state.json").write_text(
                json.dumps(getattr(self, "_http_state", {}) or {}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("HTTP 游标存不下：%s", exc)

    def _name_of(self, uid: str) -> str:
        """uid 反查昵称：只认唯一命中，猜不出来就返回空（宁可不回，不回错人）。"""
        if not uid:
            return ""
        names = [name for name, slot in getattr(self.social, "_uid_cache", {}).items()
                 if isinstance(slot, dict) and str(slot.get("uid") or "") == uid]
        return names[0] if len(names) == 1 else ""

    async def _http_scan(self, *, first: bool, dm: bool, notices: bool) -> list:
        out: list = []
        if dm and getattr(self.settings, "dm_http", True):
            out.extend(await self._http_dm_scan(first=first))
        if notices and getattr(self.settings, "notice_http", True):
            out.extend(await self._http_notice_scan(first=first))
        return out

    async def _http_dm_scan(self, *, first: bool) -> list:
        """私信增量。`first` 只推进游标，不回积压。"""
        api = getattr(self, "imapi", None)
        if api is None:
            return []
        try:
            convs = await api.conversations(
                limit=int(getattr(self.settings, "dm_http_limit", 20) or 20)
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("私信 HTTP 拉取跳过：%s", exc)
            return []
        cursor = self._state_load("im")
        if not isinstance(cursor, dict):
            cursor = {}
            self._http_state["im"] = cursor
        out: list = []
        for conv in convs:
            conv_id = str(conv.get("conv_id") or "")
            if not conv_id:
                continue
            try:
                mark = int(cursor.get(conv_id) or 0)
            except (TypeError, ValueError):
                mark = 0
            newest = mark
            for message in conv.get("messages") or []:
                ts = int(message.get("create_time") or 0)
                if message.get("mine"):
                    newest = max(newest, ts)
                    continue
                if ts <= mark:
                    continue
                newest = max(newest, ts)
                if first:
                    continue            # 首轮只对游标，不把旧消息当新私信回一遍
                uid = str(message.get("from_uid") or "")
                text = str(message.get("text") or "").strip()
                who = self._name_of(uid)
                if not who or not text:
                    continue
                kind = "dm_share" if message.get("kind") == "share" else "dm_text"
                key = "im:%s:%s" % (conv_id, message.get("server_message_id") or ts)
                out.append(Entry(
                    kind=kind, key=key, src=who, who=who, text=text, when="",
                    aweme_id=str(message.get("aweme_id") or ""),
                    extra={"protocol": True, "source": "http", "from_uid": uid,
                           "timestamp": ts, "conv_id": conv_id},
                ))
                if kind == "dm_text":
                    self._ws_mark(who, text)   # 让界面那条重复的走 ws_dup，不回两遍
            if newest:
                cursor[conv_id] = newest
        self._state_save()
        return out

    async def _http_notice_scan(self, *, first: bool) -> list:
        """通知/评论/@ 增量。带真实 comment_id 的走「按 cid 回复」。"""
        api = getattr(self, "noticeapi", None)
        if api is None:
            return []
        try:
            data = await api.items()
        except Exception as exc:  # noqa: BLE001
            logger.info("通知 HTTP 拉取跳过：%s", exc)
            return []
        # items() 返回的是整包 {counts, items, warnings}，不是列表
        items = data.get("items") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return []
        seen = self._state_load("notice")
        if not isinstance(seen, list):
            seen = []
            self._http_state["notice"] = seen
        known = {str(x) for x in seen}
        out: list = []
        for item in items:
            if not item.get("needs_reply"):
                continue
            nid = str(item.get("nid") or "")
            key = "nt:" + (nid or str(item.get("comment_id") or item.get("create_time") or ""))
            if key in known:
                continue
            known.add(key)
            seen.append(key)
            if first:
                continue
            aweme_id = str(item.get("aweme_id") or "")
            if not aweme_id:
                continue
            who = str(item.get("from_nick") or "")
            kind = "comment" if item.get("type") in (31, 32, 35) else "mention"
            out.append(Entry(
                kind=kind, key=key, src=who, who=who,
                text=str(item.get("content") or "").strip(), when="", aweme_id=aweme_id,
                extra={"protocol": True, "source": "http",
                       "from_uid": str(item.get("from_uid") or ""),
                       "comment_id": str(item.get("comment_id") or ""),
                       "root_comment_id": str(item.get("root_comment_id") or ""),
                       "timestamp": int(item.get("create_time") or 0),
                       "notice_type": item.get("type")},
            ))
        if len(seen) > 300:
            del seen[:-300]
        self._state_save()
        return out

    def _ws_self_echo(self, evt: dict, who: str) -> bool:
        """这条推送是不是我自己发出去、又被服务端推回来的。

        自己的消息 sender 也是「我」，当成新私信处理就会自己回自己，回出来的又推回来。
        三条判据任一命中就算回声：会话两侧是同一个人、发送者是 dm_self_names 里的名字、
        或者 from_uid 跟自聊名字在 uid 缓存里的 uid 相同。
        """
        extra = evt.get("extra") or {}
        conv = str(extra.get("conversation_id") or "")
        parts = [p for p in conv.split(":") if p]
        if len(parts) >= 4 and parts[-1] == parts[-2]:
            return True
        raw = getattr(self.settings, "dm_self_names", "") or ""
        names = [x.strip() for x in raw.replace("，", ",").replace("、", ",").split(",") if x.strip()]
        if not names:
            return False
        if who and any(a == who or a in who or who in a for a in names):
            return True
        uid = str(evt.get("from_uid") or "")
        cache = getattr(self.social, "_uid_cache", {}) or {}
        for a in names:
            slot = cache.get(a)
            if isinstance(slot, dict) and uid and str(slot.get("uid") or "") == uid:
                return True
        return False

    def _ws_mark(self, who: str, text: str) -> None:
        seen = getattr(self, "_ws_dm_seen", None)
        if seen is None:
            seen = self._ws_dm_seen = []
        seen.append((who or "", text or "", time.time()))
        del seen[:-40]

    def _ws_claimed(self, entry: Entry, ttl: float = 180.0) -> bool:
        """这条私信是不是 WS 那一路已经拿去处理过了（同名同文，三分钟内）。"""
        now = time.time()
        text = (entry.text or "").strip()
        who = entry.who or ""
        for seen_who, seen_text, ts in reversed(getattr(self, "_ws_dm_seen", [])):
            if now - ts > ttl:
                break
            if seen_text == text and (not who or not seen_who or seen_who == who):
                return True
        return False

    def _matches_push(self, entry: Entry, events: list) -> bool:
        """DOM 读到的这一条，是不是 WS 队列里刚推过来的同一条。"""
        text = (entry.text or "").strip()
        for evt in events or []:
            content = str(evt.get("content") or "").strip()
            if content and text and content == text:
                return True
            if evt.get("aweme_id") and str(evt["aweme_id"]) == entry.aweme_id:
                return True
        return False

    async def _handle_dm_share_event(self, entry: Entry, umo: str) -> str:
        """有人分享来一条视频：值得看就学，再写一句人味总结。"""
        if not getattr(self.settings, "inbox_share_summary", True):
            return "分享总结没开，只记账"
        item = await self.inbox.fetch_item(await self.browser.page(), entry.aweme_id)
        if item is None:
            return f"这条视频读不到（{entry.aweme_id}）"
        analysis = await self.pipeline._handle_one(item, 0.0, umo or "")
        if analysis is None:
            return "这条没看懂，先不写"
        text = await self.voice.summary(
            title=item.title, author=item.author,
            analysis_summary=getattr(analysis, "summary", ""),
            keywords=", ".join(getattr(analysis, "keywords", []) or []),
            transcript=getattr(analysis, "transcript", ""),
            frames=[getattr(analysis, "montage", "")] if getattr(self.settings, "voice_frames", 1) else [],
            umo=umo,
        )
        if not text:
            return "模型没写出像人话的总结，先不发"
        await self._notify_text(umo, f"{entry.who} 分享来一条：{item.title}\n\n{text}")
        return "回了：分享总结已发"

    # ---------------------------------------------------------------- 自主刷
    async def _auto_think_loop(self) -> None:
        """自主触发器：不靠用户指令也能自己决定刷一轮。"""
        if not getattr(self.settings, "autonomous_round", True):
            return
        logger.info("自主刷：已启动（每 5 分钟看一次条件）")
        while True:
            try:
                await asyncio.sleep(300)
                await self._maybe_autonomous_round()
            except asyncio.CancelledError:
                logger.info("自主刷已停止")
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("自主刷异常：%s", exc)

    def _auto_state_path(self):
        return self.data_dir / "auto_state.json"

    def _auto_state_load(self) -> None:
        """把「今天已经自主刷了几轮 / 上次刷完是什么时候」从盘上读回来。

        这些原来只存在内存里 —— 插件一重载（/dy reload 或宿主重启）就清零，
        于是「距上次刷完是否够久」永远成立，每 5 分钟就自己刷一轮。
        """
        if getattr(self, "_auto_state_loaded", False):
            return
        self._auto_state_loaded = True
        raw = {}
        try:
            raw = json.loads(self._auto_state_path().read_text("utf-8"))
        except Exception:  # noqa: BLE001
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        day = str(raw.get("day") or "")
        self._auto_day = day
        try:
            self._auto_rounds_today = int(raw.get("rounds") or 0)
        except Exception:  # noqa: BLE001
            self._auto_rounds_today = 0
        try:
            self._auto_items_today = int(raw.get("items") or 0)
        except Exception:  # noqa: BLE001
            self._auto_items_today = 0
        try:
            self._last_round_at = float(raw.get("last_round_at") or 0.0)
        except Exception:  # noqa: BLE001
            self._last_round_at = 0.0

    def _auto_state_save(self) -> None:
        try:
            self._auto_state_path().write_text(
                json.dumps(
                    {
                        "day": getattr(self, "_auto_day", ""),
                        "rounds": getattr(self, "_auto_rounds_today", 0),
                        "items": getattr(self, "_auto_items_today", 0),
                        "last_round_at": round(float(getattr(self, "_last_round_at", 0.0) or 0.0), 3),
                    },
                    ensure_ascii=False,
                ),
                "utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("自主刷：状态没能落盘（%s）", exc)

    def _auto_budget_ok(self) -> tuple[bool, str]:
        self._auto_state_load()
        day = time.strftime("%Y-%m-%d")
        if day != getattr(self, "_auto_day", ""):
            self._auto_day = day
            self._auto_rounds_today = 0
            self._auto_items_today = 0
            self._auto_state_save()
        cap_r = int(getattr(self.settings, "auto_rounds_per_day", 4) or 0)
        cap_i = int(getattr(self.settings, "auto_items_per_day", 8) or 0)
        if cap_r and self._auto_rounds_today >= cap_r:
            return False, f"今天自主刷已经 {self._auto_rounds_today} 轮（上限 {cap_r}）"
        if cap_i and self._auto_items_today >= cap_i:
            return False, f"今天自主刷已经看了 {self._auto_items_today} 条（上限 {cap_i}）"
        return True, ""

    async def _maybe_autonomous_round(self) -> None:
        ok, why = self._auto_budget_ok()
        if not ok:
            return
        now = time.time()
        interval_min = int(getattr(self.settings, "loop_interval_min", 90) or 90)
        since = int((now - (self._last_round_at or 0)) / 60) if self._last_round_at else 9999
        pending = await self.store.inbox_pending(limit=5) if hasattr(self.store, "inbox_pending") else []
        human_recent = (now - (self._last_user_at or 0)) < 1800 if getattr(self, "_last_user_at", 0) else False
        due_by_time = since >= interval_min
        if not (due_by_time or human_recent or pending):
            return
        info = {
            "since_last_min": since, "last_count": self._auto_items_today,
            "today_rounds": self._auto_rounds_today,
            "max_rounds": int(getattr(self.settings, "auto_rounds_per_day", 4) or 0),
            "human_recent": human_recent,
            "pending": ", ".join(str(p.get("kind")) for p in (pending or [])[:3]),
            "why": f"时间到了={due_by_time} 人在={human_recent} 有待处理={bool(pending)}",
        }
        decision = await self.agent.judge_round(info, self._last_umo or "")
        take = int(decision.get("take") or 0)
        if not decision.get("run") or take <= 0:
            logger.info("自主刷：模型判断这轮不用刷（%s）", decision.get("reason") or "没给理由")
            return
        umo = self._push_target or self._last_umo or ""
        if not umo:
            # 没有可推送的会话：照样刷，只是不发简报（别用空 umo 去 send_message，会抛「不合法的 session」）
            logger.info("自主刷：没有推送目标（push_target 没配、也没聊过），这轮只刷不推")
        job = self.jobs.submit("round", umo, take)
        if job is None:
            logger.info("自主刷：队列满了，这轮放弃")
            return
        self._auto_rounds_today += 1
        self._auto_items_today += take
        self._last_round_at = now
        logger.info("自主刷：模型说值得刷 %s 条（%s）", take, decision.get("reason") or "")
        if getattr(self.settings, "notify_start", False):
            await self._ask_help(f"自主判断触发：我刷一轮（{take} 条）。理由：{decision.get('reason') or '时间到了'}")

    async def _handle_dm_text(self, entry: Entry, umo: str) -> str:
        """别人发来一句话：按人格回一句，发回去。"""
        if not getattr(self.settings, "dm_chat_enabled", False):
            return "日常聊天没开（配置里 dm_chat_enabled），只记账不回"
        allow = [x.strip() for x in (self.settings.dm_chat_allow or "").replace("，", ",").split(",") if x.strip()]
        who = entry.who or ""
        if not allow or not any(a == who or a == str(entry.extra.get("from_uid") or "") for a in allow):
            return f"「{who}」不在日常聊天白名单里，不回"
        free = [
            x.strip()
            for x in (getattr(self.settings, "dm_chat_no_limit", "") or "")
            .replace("，", ",")
            .split(",")
            if x.strip()
        ]
        # 0.10.0：自己人（dm_targets）也算免限流；但静默时段仍然生效
        free += [
            x.strip()
            for x in (getattr(self.settings, "dm_targets", "") or "")
            .replace("，", ",")
            .replace("、", ",")
            .split(",")
            if x.strip()
        ]
        unlimited = any(a == who or a == str(entry.extra.get("from_uid") or "") for a in free)
        if unlimited:
            if self.limits.in_quiet_hours(getattr(self.settings, "quiet_hours", "")):
                return "静默时段，先不回"
        else:
            why = await self.limits.check_async("dm", self.settings)
            if why:
                return f"先不回：{why}"
        # 0.10.0：先问模型「该不该回、回什么」
        # 0.15.0：这个人在 QQ 上有号的话，把 QQ 那侧的近况也交给模型
        qq_id = self._qq_of(who)
        text0 = ""
        if getattr(self.settings, "agent_enabled", True):
            decision = await self.agent.handle_douyin_event(
                {
                    "type": "dm",
                    "from_nick": who,
                    "content": entry.text or "",
                    "time": entry.when or "",
                    "event_id": entry.extra.get("event_id") or entry.key,
                    "raw_frame_id": entry.extra.get("raw_frame_id", ""),
                },
                umo,
                extra_history=self.memory.from_host(qq_id, 6) if qq_id else None,
            )
            if decision.get("model") is False:
                # 判断用的模型没就绪：不退化成哑巴，走下面「日常聊天」那条路照样回
                logger.info("私信：判断模型没就绪，改用日常聊天回复")
            elif not decision.get("reply"):
                self.memory.remember(who, "them", entry.text or "")
                return f"模型判断不回：{decision.get('reason') or '没给理由'}"
            else:
                text0 = str(decision.get("text") or "")
                if text0:
                    self._event_judged_reply = getattr(self, "_event_judged_reply", 0) + 1
                    self.memory.remember(who, "them", entry.text or "")
                    ok0, detail0 = await self.send_dm(who, text0, allow=[], check_limit=False)
                    if ok0:
                        self.memory.remember(who, "me", text0)
                        if not unlimited:
                            await self.limits.hit_async("dm")
                        return f"回了：「{text0}」（模型判断：{decision.get('reason') or '—'}）"
                    return f"回了但没发出去：「{text0[:20]}」（{detail0}）"
        memory = self.memory.background(
            person=who,
            qq_id=qq_id,
            limit=int(getattr(self.settings, "dm_chat_history", 6) or 6),
        )
        self.memory.remember(who, "them", entry.text or "")
        text = await self.voice.chat(their=entry.text or "", umo=umo, memory=memory)
        if not text:
            return "没想出合适的回法（模型没给出合格的句子），不硬发"
        ok, detail = await self.send_dm(who, text, allow=[], check_limit=False)
        if not ok:
            return f"回了但没发出去：「{text[:20]}」（{detail}）"
        self.memory.remember(who, "me", text)
        if not unlimited:
            await self.limits.hit_async("dm")
        return f"回了：「{text}」"

    async def _handle_mention(self, page, entry: Entry, umo: str) -> str:
        # 0.10.0：同一个人挂在队列里没回完的评论，直接丢新事件（一次针对一个用户只回一条）
        uid = str(entry.extra.get("from_uid") or entry.who or entry.key)
        if uid in self._mention_seen:
            return f"「{uid}」已经排了一条没回完的，这条丢掉"
        self._mention_seen[uid] = entry.key
        try:
            return await self._mention_work(page, entry, umo)
        finally:
            self._mention_seen.pop(uid, None)

    def _entry_event(self, entry):
        return {"type": {"dm_text": "dm", "dm_share": "share"}.get(entry.kind, entry.kind),
                "from_nick": entry.who, "content": entry.text, "time": entry.when,
                "event_id": entry.extra.get("event_id") or entry.key,
                "raw_frame_id": entry.extra.get("raw_frame_id", ""),
                "from_uid": entry.extra.get("from_uid", ""), "aweme_id": entry.aweme_id}

    async def _mention_work(self, page, entry: Entry, umo: str) -> str:
        # IM 私信里的「评论卡」和群聊 @ 都是 IM 语义，不能拿去公开评论区回复。
        if str(entry.extra.get("channel") or "") == "im" or str(entry.extra.get("route") or "") == "dm":
            return "IM 里的评论卡/群内@，不当公开评论处理"
        if not getattr(self.settings, "inbox_mention_reply", True):
            return "评论回复开关关闭，只记账"
        text0 = ""
        if getattr(self.settings, "agent_enabled", True):
            decision = await self.agent.handle_douyin_event(self._entry_event(entry), umo)
            if decision.get("model") is False:
                text0 = ""  # 没有可用模型：退回 voice.reply 那条老路，别哑掉
            elif not decision.get("reply"):
                return "模型判断不回：" + (decision.get("reason") or "无有效判断")
            else:
                text0 = str(decision.get("text") or "")
                if not text0:
                    return "模型说回但没给话，按不回处理"
                self._event_judged_reply = getattr(self, "_event_judged_reply", 0) + 1
        if not text0:
            try:
                text0 = await self.voice.reply(their=entry.text or "",
                                               title=getattr(entry, "src", "") or "", umo=umo)
            except Exception as exc:  # noqa: BLE001
                logger.warning("回评论时写句子失败：%s", exc)
                text0 = ""
            if not text0:
                return "没想出合适的回法，不硬发"
        if not entry.aweme_id:
            return "视频 id 待补，保留通知记录"
        mode = self._reply_mode()
        if mode == "off":
            return "评论回复模式 off，只保留模型判断"
        uid = str(entry.extra.get("from_uid") or entry.who)
        blocked = await self._reply_blocked(uid, entry.who or "")
        if blocked:
            return blocked
        pending = await self.store.inbox_pending("comment_reply", 200)
        for row in pending:
            payload = json.loads(row.get("payload") or "{}")
            if payload.get("uid") == uid:
                return "同用户已有待确认评论，这条跳过"
        payload = {"uid": uid, "who": entry.who, "aweme_id": entry.aweme_id,
                   "comment_id": str(entry.extra.get("comment_id") or ""),
                   "text": text0, "created_at": time.time()}
        if mode == "ask":
            await self.store.inbox_add("comment_reply", entry.key, src=entry.who,
                                      payload=json.dumps(payload, ensure_ascii=False))
            await self._notify_text(umo, f"待确认评论回复 · {entry.who}\n{text0}\n/dy confirm 确认（12 小时有效）")
            return "评论回复已入待确认队列"
        # 计数在 _send_comment_reply / _send_mention_comment 里落，这里别再记一遍
        return (await self._send_comment_reply(payload)
                if payload["comment_id"] else await self._send_mention_comment(payload))

    def _insider(self, target: str, uid: str = "") -> bool:
        """这个人算不算「自己人」：私信/日常聊天/分享/@ 白名单里出现过的都算。

        自己人回评论不限次数；不在名单里的外人只回一次（避免跟陌生人吵起来）。
        """
        t = str(target or "").strip()
        u = str(uid or "").strip()
        if not t and not u:
            return False
        names = []
        for attr in ("dm_targets", "dm_chat_no_limit", "dm_allow", "dm_self_names"):
            raw = getattr(self.settings, attr, "") or ""
            names += [x.strip() for x in str(raw).replace("，", ",").replace("、", ",").split(",") if x.strip()]
        try:
            names += list(self.settings.share_allowlist or [])
        except Exception:  # noqa: BLE001
            pass
        for n in names:
            if not n:
                continue
            if t and (n == t or n in t or t in n):
                return True
            if u and n == u:
                return True
        # uid 反查名字，再比一次（对方昵称带后缀、我们记录的是短名时也能对上）
        if u:
            by_uid = self._name_of(u) if hasattr(self, "_name_of") else ""
            if by_uid and t and (by_uid == t or by_uid in t or t in by_uid):
                return True
        return False

    def _reply_mode(self) -> str:
        """评论/私信回复怎么发：auto 直接发，ask 进队列，off 只记判断。"""
        mode = str(getattr(self.settings, "comment_reply_mode", "auto") or "auto").lower()
        return mode if mode in {"auto", "ask", "off"} else "auto"

    async def _reply_blocked(self, uid: str, who: str) -> str:
        """非自己人只回一次的闸门：返回拒绝理由，空串表示放行。"""
        if not getattr(self.settings, "comment_reply_once_others", True):
            return ""
        if self._insider(who, uid):
            return ""
        try:
            used = await self.store.reply_count(uid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("查评论回复次数失败（放行）：%s", exc)
            return ""
        if used >= 1:
            return f"「{who or uid}」不是自己人，已经回过 {used} 次，不再回"
        return ""

    async def _send_mention_comment(self, payload):
        """@ 提醒的公开回法：在视频下发一条评论 @ 回对方。

        没有原始评论 id 时不能冒充「回复某条评论」，但对方是 @ 了我才来的，
        回给这个人本身不需要再过一遍白名单（不做无差别群发）。
        """
        aweme = str(payload.get("aweme_id") or "")
        text = str(payload.get("text") or "")
        if not aweme or not text:
            return "视频 id 或内容缺，先不发"
        who = str(payload.get("who") or "").strip()
        why = await self.limits.check_async("comment", self.settings)
        if why:
            return "评论暂缓：" + why
        res = await self.social.comment(aweme, text, [who] if who else [])
        if res.ok:
            await self.limits.hit_async("comment")
            await self.store.reply_hit(str(payload.get("uid") or who or ""), who, "",
                                       aweme)
            return "回了：" + text
        return "评论发送未确认：" + str(getattr(res, "detail", ""))

    async def _send_comment_reply(self, payload):
        why = await self.limits.check_async("comment", self.settings)
        if why:
            return "评论暂缓：" + why
        cid = str(payload.get("comment_id") or "")
        if not cid:
            return "原评论 id 待补，未发布顶层评论替代回复"
        res = await self.social.reply_comment(payload["aweme_id"], cid, payload["text"])
        if res.ok:
            await self.limits.hit_async("comment")
            await self.store.reply_hit(str(payload.get("uid") or payload.get("who") or ""),
                                       str(payload.get("who") or ""), cid,
                                       str(payload.get("aweme_id") or ""))
            return "回了：" + payload["text"]
        return "评论发送未确认：" + res.detail

    async def _confirm_comment_replies(self):
        if self._reply_mode() == "off":
            return ["评论回复模式 off，队列保留"]
        lock = getattr(self, "_comment_confirm_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._comment_confirm_lock = lock
        async with lock:  # 两个人同时 /dy confirm 也只发一次
            out = []
            for row in await self.store.inbox_pending("comment_reply", 20):
                payload = json.loads(row.get("payload") or "{}")
                blocked = await self._reply_blocked(str(payload.get("uid") or ""),
                                                    str(payload.get("who") or ""))
                if blocked:
                    await self.store.inbox_done("comment_reply", row["key"], blocked)
                    out.append(blocked)
                    continue
                if time.time() - float(payload.get("created_at") or 0) > 43200:
                    line = "评论确认已过期"
                else:
                    line = (await self._send_comment_reply(payload) if payload.get("comment_id")
                            else await self._send_mention_comment(payload))
                # Failed or rate-limited confirmations remain pending until TTL; never blind retry.
                if line.startswith("回了：") or "过期" in line:
                    await self.store.inbox_done("comment_reply", row["key"], line)
                out.append(line)
            return out

    # ---------------------------------------------------------------- 私信发送
    #
    # 为什么走界面而不是私信接口：imapi 那套要走 imsdk（wss + 自签 token），
    # 而且发消息的风控比评论更严。界面发虽然没有服务端回执，但可以「发完回读
    # 最后一条气泡」来确认 —— 这就是 dm_readback 的作用。

    def _dm_targets(self, config_attr: str = "dm_targets") -> list[str]:
        raw = getattr(self.settings, config_attr, "") or ""
        return [x.strip() for x in raw.replace("，", ",").replace("、", ",").split(",") if x.strip()]

    def _dm_allowed(self, target: str, names: list[str]) -> bool:
        t = (target or "").strip()
        return any(a and (a == t or a in t or t in a) for a in names)

    def _dm_limit_kind(self) -> str:
        """主动发私信算哪一类配额：分开了就是 dm_out，否则和「回私信」共用 dm。"""
        return "dm_out" if getattr(self.settings, "dm_quota_separate", True) else "dm"

    async def send_dm(self, who: str, text: str, *, allow=None, check_limit: bool = True) -> tuple:
        """发一条私信，返回 (成功?, 说明)。allow=None 表示不校验白名单。"""
        if not text or not text.strip():
            return False, "内容是空的"
        if allow is None:
            allow = self._dm_targets()
        if check_limit:
            why = await self.limits.check_async(self._dm_limit_kind(), self.settings)
            if why:
                return False, f"先不发：{why}"
        ok, detail = await self.social.send_dm(who, text, allow=allow)
        if ok and check_limit:
            await self.limits.hit_async(self._dm_limit_kind())
        return ok, detail

    async def _dm_send_queued(self, row_id: int, who: str, text: str, umo: str = "") -> str:
        """执行发件箱里的一条（失败会通知主人）。"""
        ok, detail = await self.send_dm(who, text)
        if ok:
            await self.store.dm_mark(row_id, "sent")
            logger.info("私信已发：%s（%s）", who, detail)
            return f"✅ 发给「{who}」：{text[:40]}" + ("" if len(text) <= 40 else "…")
        await self.store.dm_mark(row_id, "failed", detail)
        logger.warning("私信没发出去：%s（%s）", who, detail)
        try:
            await self._ask_help(
                f"私信没发出去：「{who}」\n原因：{detail}\n"
                f"内容：{text[:80]}\n要看怎么办：/dy dm 状态",
                umo=umo,
            )
        except Exception:  # noqa: BLE001
            pass
        return f"❌ 发给「{who}」失败：{detail}"

    async def _dm_flush(self, umo: str = "", limit: int = 10) -> list[str]:
        """把待发队列里到期的都发出去（/dy confirm 走这条路）。"""
        rows = await self.store.dm_pending(limit)
        if not rows:
            return []
        out = []
        for row in rows:
            out.append(
                await self._dm_send_queued(
                    int(row["id"]), str(row.get("target_uid") or ""), str(row.get("text") or ""), umo
                )
            )
        return out

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

    def _template_comment(self, item, summary: str = "") -> str:
        """模板兜底：模型写不出来 / voice_enabled=false 时才用。"""
        tpl = self.settings.comment_template or "看完记一笔：{summary}"
        values = {
            "title": item.title or "",
            "summary": (summary or "")[:60],
            "keywords": "",
            "author": item.author or "",
            "url": item.share_url,
        }
        try:
            text = tpl.format(**values)
        except Exception:
            text = "看完记一笔：" + (values["summary"] or "这条信息量挺大")
        return text.strip()[:120]

    async def _comment_text(self, item, analysis, umo: str = "") -> str:
        """评论文案：优先让对话模型按当前人格写，写不出来才退回模板。

        模型看到的是「标题 + 作者 + 摘要 + 关键词 + 蒙太奇拼图」，所以它说的是这条视频，
        而不是把摘要截断套进固定句式。禁词自检不过会重写一次，仍不过就退模板。
        """
        if getattr(self.settings, "voice_enabled", True):
            frames: list[str] = []
            montage = getattr(analysis, "montage", None)
            if montage:
                frames.append(str(montage))
            try:
                text = await self.voice.comment(
                    title=item.title or "",
                    author=item.author or "",
                    summary=getattr(analysis, "summary", "") or "",
                    keywords=list(getattr(analysis, "keywords", []) or []),
                    transcript=getattr(analysis, "transcript", "") or "",
                    frames=frames,
                    umo=umo,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("模型写评论文案失败，退回模板：%s", exc)
                text = ""
            if text:
                return text
        return self._template_comment(item, getattr(analysis, "summary", "") or "")

    def _share_text(self, item) -> str:
        """分享时一起发出去的文字（默认只发链接）。可用 {title} {author} {url}。"""
        tpl = self.settings.share_text or "{url}"
        try:
            text = tpl.format(title=item.title or "", author=item.author or "", url=item.share_url)
        except Exception:
            text = item.share_url
        return text.strip()[:300]

    async def _human_pause(self) -> None:
        """动作之间歇一会儿，且时间不固定。

        固定间隔本身就是指纹：每 6.0 秒一次，连着十次，比任何行为都像脚本。
        所以取 action_delay_s 的 0.6~1.8 倍随机，偶尔再长一点（看手机的停顿）。
        """
        base = max(0.0, float(self.settings.action_delay_s or 0))
        if base <= 0:
            return
        if not getattr(self.settings, "action_jitter", True):
            await asyncio.sleep(base)
            return
        delay = random.uniform(base * 0.6, base * 1.8)
        if random.random() < 0.15:  # 偶尔走神更久
            delay *= random.uniform(1.6, 2.4)
        await asyncio.sleep(delay)

    async def _retire(self) -> None:
        """按 keep_days 清旧数据：库里的行 + work/media 里的视频帧图。"""
        days = int(getattr(self.settings, "keep_days", 30) or 0)
        if days <= 0:
            return
        try:
            stats = await self.store.prune(days)
        except Exception as exc:  # noqa: BLE001
            logger.warning("清旧记录失败：%s", exc)
            return
        try:
            await asyncio.to_thread(self.limits.prune, days * 86400)
        except Exception:  # noqa: BLE001
            pass
        cutoff = time.time() - days * 86400
        removed = 0
        for path in sorted(self.work_dir.glob("*")):
            try:
                if path.is_file() and path.suffix.lower() not in {".json", ".db"} and path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        total = sum(stats.values())
        if total or removed:
            logger.info("清理保留期外的记录：%s，文件 %d 个", stats, removed)

    async def _do_actions(self, pairs: list[tuple], kinds: set[str] | None = None) -> list[str]:
        """对已分析的视频执行点赞/分享/评论，返回给人看的回执。

        `kinds` 为 None 表示三种都做；给了就只做里面列的（`like` / `share` / `comment`），
        用于「评论自动发、分享仍要问」这种按种类放行的场景。
        """
        want = None if kinds is None else {str(k).lower() for k in kinds}
        do_like = want is None or "like" in want
        do_share = want is None or "share" in want
        do_comment = want is None or "comment" in want
        if not (do_like or do_share or do_comment):
            return []
        lines: list[str] = []
        if getattr(self.settings, "rate_enabled", True) and self.limits.in_quiet_hours(
            getattr(self.settings, "quiet_hours", "") or ""
        ):
            return [f"　静默时段（{self.settings.quiet_hours}），这轮不动手"]
        budget = max(0, self.settings.max_actions_per_round)
        for item, analysis in pairs[:budget]:
            # 点赞（最低风险的那一档，先做）
            if do_like and not await self.store.acted(
                item.aweme_id, "like", "digg"
            ):
                why = await self.limits.check_async("like", self.settings)
                if why:
                    lines.append(f"　跳过点赞：{why}")
                else:
                    res = await self.social.like(item.aweme_id, True)
                    await self.store.record_action(
                        item.aweme_id, "like", "digg", res.ok, res.detail
                    )
                    if res.ok:
                        await self.limits.hit_async("like")
                    lines.append(f"　{res}")
                    await self._human_pause()
            # 分享
            wanted, _rejected = (
                self.settings.check_targets(list(self.settings.share_allowlist), kind="share")
                if do_share
                else ([], [])
            )
            todo = [t for t in wanted if not await self.store.acted(item.aweme_id, "share", t)]
            why_share = await self.limits.check_async("share", self.settings) if todo else ""
            if todo and why_share:
                lines.append(f"　跳过分享：{why_share}")
                todo = []
            if todo:
                results = await self.social.share(item.aweme_id, todo, self._share_text(item))
                for res in results:
                    await self.store.record_action(
                        item.aweme_id, "share", res.target, res.ok, res.detail
                    )
                    if res.ok:
                        await self.limits.hit_async("share")
                    lines.append(f"　{res}")
                await self._human_pause()
            # 评论
            text = await self._comment_text(item, analysis) if do_comment else ""
            mentions, _blocked = self.settings.check_targets(
                list(self.settings.mention_allowlist), kind="mention"
            )
            key = "@".join(mentions) or "评论"
            why_comment = await self.limits.check_async("comment", self.settings)
            if text and why_comment:
                lines.append(f"　跳过评论：{why_comment}")
            elif text and not await self.store.acted(item.aweme_id, "comment", key):
                res = await self.social.comment(item.aweme_id, text, mentions)
                await self.store.record_action(item.aweme_id, "comment", key, res.ok, res.detail)
                if res.ok:
                    await self.limits.hit_async("comment")
                lines.append(f"　{res}")
                await self._human_pause()
        return lines

    def _auto_kinds(self) -> set[str]:
        """这一轮不用问、直接做的动作种类。

        `action_mode` 是总闸门：`off` 一律不动；`auto` 三种都自动；`ask` 时再看单项
        开关 —— `auto_comment`（评论）/ `auto_like`（点赞）勾了就自己发，剩下的
        （分享）仍然推给用户确认。「刷完自动评论」走的就是这条。
        """
        mode = (getattr(self.settings, "action_mode", "off") or "off").lower()
        if mode == "off":
            return set()
        if mode == "auto":
            return {"like", "share", "comment"}
        kinds: set[str] = set()
        if getattr(self.settings, "auto_comment", False):
            kinds.add("comment")
        if getattr(self.settings, "auto_like", False):
            kinds.add("like")
        if getattr(self.settings, "auto_share", False):
            kinds.add("share")
        return kinds

    async def _auto_actions(self, result: RoundResult) -> str:
        """按 action_mode + 单项开关决定要不要动真格；返回要追加到简报后面的文字。"""
        mode = (getattr(self.settings, "action_mode", "off") or "off").lower()
        if mode == "off" or not result.analyzed:
            return ""
        auto = self._auto_kinds()
        rest = {"like", "share", "comment"} - auto
        if mode == "ask" and rest:
            self._pending = result.analyzed[: max(1, self.settings.max_actions_per_round)]
            self._pending_at = time.time()
            self._pending_kinds = rest
        lines: list[str] = []
        if auto:
            try:
                lines = await self._do_actions(result.analyzed, kinds=auto)
                self._last_action_lines = list(lines or [])
                self._last_action_lines = list(lines or [])
            except HumanVerifyNeeded:
                raise                  # 交给 _run_job：搬窗口、叫人、验过再重试
            except Exception as exc:  # noqa: BLE001
                logger.warning("写操作失败：%s", exc)
                return f"\n\n写操作失败：{exc}"
        if mode == "ask" and rest:
            todo = "/".join(sorted(rest))
            tail = f"\n\n还有 {todo} 没做：回 /dy do 执行，或 /dy do 跳过"
            if not lines:
                return tail
            return "\n\n写操作回执：\n" + "\n".join(lines) + tail
        self._last_action_lines = list(lines or [])
        self._last_action_lines = list(lines or [])
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
        self._last_user_at = time.time()  # 自主刷拿它判断「人在不在」
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
                await self.start_dm_watch()
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
                (
                    f"  评论文案：模型按人格写（{st.voice_max_chars} 字内"
                    f"{'，带一张拼图' if st.voice_frames else '，只看文字'}）"
                    if st.voice_enabled
                    else f"  评论文案：模板（{st.comment_template or '看完记一笔：{summary}'}）"
                ),
                f"  人格：{st.voice_persona or '（当前会话默认人格）'}"
                + (f"｜禁词 {st.voice_banned}" if st.voice_banned else ""),
                f"  分享文案：{st.share_text or '（默认：只发链接）'}",
                f"  每轮上限：{st.max_actions_per_round} 条｜动作间隔：{st.action_delay_s}s",
                f"  频率：{self.limits.summary(st) if st.rate_enabled else '关（不限）'}"
                + (f"｜静默 {st.quiet_hours}" if st.quiet_hours else ""),
                f"  待执行动作过期：{st.pending_ttl_hours} 小时",
                f"  内核：{st.channel or 'chromium'}"
                + ("（严格：起不来就报错）" if st.channel_strict else "（起不来会退回 chromium）")
                + ("｜profile 钉死" if st.profile_pin else ""),
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
                row = rows[0] if rows else {}
                summary = (row.get("summary") or "") or ""
                keywords = [k for k in str(row.get("keywords") or "").replace("，", ",").split(",") if k.strip()]
                if getattr(self.settings, "voice_enabled", True):
                    yield event.plain_result("正在按人格写一句评论…")
                    try:
                        text = await self.voice.comment(
                            title=row.get("desc") or item.title or "",
                            author=row.get("author") or item.author or "",
                            summary=summary,
                            keywords=keywords,
                            transcript=(row.get("transcript") or "")[:300],
                            frames=[],
                            umo=getattr(event, "unified_msg_origin", "") or "",
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("模型写评论失败：%s", exc)
                        text = ""
                if not text:
                    text = self._template_comment(item, summary)
            why = await self.limits.check_async("comment", self.settings)
            if why:
                yield event.plain_result(f"没发：{why}")
                return
            yield event.plain_result(
                f"正在评论「{item.title[:24] or item.aweme_id}」：{text[:40]}"
                f"{'　@' + ' @'.join(mentions) if mentions else ''}…"
            )
            res = await self.social.comment(item.aweme_id, text, mentions)
            if res.ok:
                await self.limits.hit_async("comment")
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

        if sub in {"imapi", "私信接口"}:
            yield event.plain_result("问一下 imapi（只读，别急着关窗口）…")
            data = await self.imapi.probe()
            if not data.get("ok"):
                yield event.plain_result("私信 HTTP 通道不通：" + str(data.get("error") or ""))
                return
            lines = [
                f"私信 HTTP：{data.get('conversations')} 个会话（私聊 {data.get('private')}）",
                f"读到消息 {data.get('messages')} 条，带时间戳 {data.get('messages_with_timestamp')} 条"
                f"（{'齐' if data.get('timestamps_all_present') else '不齐'}）",
                f"耗时 {data.get('ms')} ms —— DOM 那条路要十几秒",
            ]
            sample = data.get("sample") or {}
            if sample:
                who = "我发的" if sample.get("mine") else f"对方 {sample.get('from_uid')}"
                lines.append(f"最新一条：{sample.get('text') or '(没文字)'} · {who} · {sample.get('create_time')}")
            yield event.plain_result("\n".join(lines))
            return

        if sub in {"noticeapi", "通知接口"}:
            yield event.plain_result("问一下通知接口（只读）…")
            data = await self.noticeapi.probe()
            if not data.get("ok"):
                yield event.plain_result("通知 HTTP 通道不通：" + str(data.get("error") or ""))
                return
            lines = [
                f"通知 HTTP：{data.get('items')} 条，@我 {data.get('at_me')} 条，需要回的 {data.get('needs_reply')} 条",
                f"带 comment_id 的 {data.get('with_comment_id')} 条 —— 有它才能按 cid 精确回复",
                f"耗时 {data.get('ms')} ms；未读计数 {data.get('counts')}",
            ]
            for item in (data.get("sample") or [])[:3]:
                lines.append(
                    f"· {item.get('type_name')}｜{item.get('from_nick') or '(无名)'}｜"
                    f"{(item.get('content') or '')[:40]}｜cid={item.get('comment_id') or '无'}"
                )
            if data.get("warnings"):
                lines.append("（有告警：" + "; ".join(str(w) for w in data["warnings"][:3]) + "）")
            yield event.plain_result("\n".join(lines))
            return

        if sub == "do":
            first = args[1].lower() if len(args) > 1 else ""
            if first in {"跳过", "skip", "取消", "cancel", "no"}:
                self._pending = []
                self._pending_kinds = set()
                yield event.plain_result("已取消这轮写操作")
                return
            if not self._pending:
                dm_wait = await self.store.dm_pending(10)
                if dm_wait:
                    yield event.plain_result(f"开始发 {len(dm_wait)} 条待发私信…")
                    out = await self._dm_flush(event.unified_msg_origin)
                    yield event.plain_result("\n".join(out) if out else "发完了。")
                    return
                yield event.plain_result(
                    "没有待执行的动作。action_mode=ask 时刷完一轮会问你一次；"
                    "也可以直接用 /dy share、/dy comment、/dy dm"
                )
                return
            pairs = self._pending
            ttl_h = int(getattr(self.settings, "pending_ttl_hours", 12) or 0)
            if ttl_h and self._pending_at and time.time() - self._pending_at > ttl_h * 3600:
                age_h = (time.time() - self._pending_at) / 3600
                self._pending = []
                yield event.plain_result(
                    f"这些动作已经放了 {age_h:.0f} 小时（超过 {ttl_h} 小时），作废了。"
                    "重新 /dy start 刷一轮再确认吧——放着不动的分享/评论，执行时视频可能都凉了"
                )
                return
            self._pending = []
            self._pending_at = 0.0
            kinds = getattr(self, "_pending_kinds", None) or {"like", "share", "comment"}
            self._pending_kinds = set()
            yield event.plain_result(f"开始执行 {len(pairs)} 条视频的{'/'.join(sorted(kinds))}…")
            lines = await self._do_actions(pairs, kinds=kinds)
            yield event.plain_result("\n".join(lines) or "没有需要执行的（这些视频可能都做过了）")
            dm_lines = await self._dm_flush(event.unified_msg_origin)
            if dm_lines:
                yield event.plain_result("待发的私信：\n" + "\n".join(dm_lines))
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

        if sub in {"inbox", "dms", "mentions"}:
            await self._ensure_ready()
            do = any(a in {"处理", "handle", "run", "do", "yes"} for a in args[1:])
            yield event.plain_result("去翻一遍私信和提醒，大概 30 秒…")
            try:
                entries = await self._inbox_scan(event.unified_msg_origin)
            except HumanVerifyNeeded as exc:
                yield event.plain_result(f"翻的时候撞上人机验证（{exc}），发 /dy verify 我把它挪到桌面上。")
                return
            pending = await self._pending_entries() if do else []
            if not entries and not pending:
                yield event.plain_result("没有新的：私信里没人发视频过来，提醒里也没有新的 @。")
                return
            if entries:
                lines = [f"新收 {len(entries)} 条："]
                for idx, e in enumerate(entries, 1):
                    lines.append(f"{idx}. {e.describe()}")
                yield event.plain_result("\n".join(lines))
            if not do:
                if entries:
                    yield event.plain_result("要我现在就一条条看、写总结/回评论，发 /dy inbox 处理")
                else:
                    yield event.plain_result(f"手里还压着 {len(pending)} 条没处理，发 /dy inbox 处理")
                return
            todo = pending or entries
            report = await self._inbox_handle(todo, event.unified_msg_origin)
            yield event.plain_result("\n".join(report) if report else "处理完了，没什么可说的。")
            return

        if sub in {"dm", "message", "发私信", "send"}:
            await self._ensure_ready()
            if len(args) >= 2 and args[1] in {"状态", "status", "日志", "log"}:
                stats = await self.store.dm_stats()
                rows = await self.store.dm_pending(10)
                lines = ["私信发件箱："]
                for k in ("pending", "sent", "failed", "skipped"):
                    lines.append(f"　{k}：{stats.get(k, 0)}")
                if rows:
                    lines.append("待发：")
                    for r in rows:
                        lines.append(f"　#{r['id']} → {r['target_uid']}：{str(r['text'])[:40]}")
                    lines.append("发 /dy confirm 就发出去")
                yield event.plain_result("\n".join(lines))
                return
            if len(args) < 3:
                yield event.plain_result(
                    "用法：/dy dm 昵称 要说的话（或 /dy send 昵称 话）\n"
                    "查发件箱：/dy dm 状态"
                )
                return
            if not getattr(self.settings, "dm_send_enabled", True):
                yield event.plain_result("主动发私信是关的（配置 → 私信 → dm_send_enabled）")
                return
            who, text = args[1], " ".join(args[2:])
            targets = self._dm_targets()
            if not targets:
                yield event.plain_result("私信白名单是空的（私信 → dm_targets），谁都不发")
                return
            if not self._dm_allowed(who, targets):
                yield event.plain_result(f"「{who}」不在私信白名单里（私信 → dm_targets）")
                return
            mode = (getattr(self.settings, "action_mode", "ask") or "ask").lower()
            if mode == "off":
                yield event.plain_result("action_mode=off，写操作一律不执行")
                return
            if mode == "ask":
                row_id = await self.store.dm_queue(who, text)
                yield event.plain_result(
                    f"先放着（ask 模式）：#{row_id} → 「{who}」\n内容：{text}\n"
                    f"确认发出去就发 /dy confirm；12 小时内不发就作废。"
                )
                return
            row_id = await self.store.dm_queue(who, text)
            yield event.plain_result(await self._dm_send_queued(row_id, who, text, event.unified_msg_origin))
            return

        if sub in {"confirm", "确认", "发出", "发出去"}:
            await self._ensure_ready()
            comments = await self._confirm_comment_replies()
            if comments:
                yield event.plain_result("\n".join(comments))
            stats = await self.store.dm_stats()
            if not stats.get("pending"):
                yield event.plain_result("没有待发的私信（/dy dm 状态 可以看发件箱）")
                return
            yield event.plain_result(f"开始发 {stats.get('pending')} 条待发私信…")
            out = await self._dm_flush(event.unified_msg_origin)
            yield event.plain_result("\n".join(out) if out else "发完了。")
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

    def _chain_text(self, result: RoundResult, text: str) -> MessageChain:
        """简报只换文案、保留拼图（actions 模式下只发一行也带图没意义，就不带）。"""
        if text == (result.text or ""):
            return self._chain(result)
        return MessageChain([Plain(text or "（没有内容）")])

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

    def _ensure_dm_watch(self) -> None:
        """同步版：在非 async 的启动路径里也能把守护挂上。"""
        # 自主刷和私信守护解耦：私信关着也照样能自己刷
        self._ensure_auto_think()
        if self._dm_task and not self._dm_task.done():
            return
        # 0.10.0：事件模式不依赖 dm_chat_poll_s（它是兜底间隔），只要日常聊天开着就起
        if not getattr(self.settings, "dm_chat_enabled", False):
            return
        try:
            self._dm_task = asyncio.create_task(self._dm_watch_loop())
        except RuntimeError:  # 没有运行中的事件循环
            self._dm_task = None

    def _ensure_auto_think(self) -> None:
        if getattr(self, "_think_task", None) and not self._think_task.done():
            return
        if not getattr(self.settings, "autonomous_round", True):
            return
        try:
            self._think_task = asyncio.create_task(self._auto_think_loop())
        except RuntimeError:
            self._think_task = None

    async def start_dm_watch(self) -> None:
        if self._dm_task and not self._dm_task.done():
            return
        if not getattr(self.settings, "dm_chat_enabled", False):
            return
        self._dm_task = asyncio.create_task(self._dm_watch_loop())
        self._ensure_auto_think()

    async def stop_dm_watch(self) -> None:
        if self._dm_task and not self._dm_task.done():
            self._dm_task.cancel()
            try:
                await self._dm_task
            except Exception:  # noqa: BLE001
                pass
        self._dm_task = None

    async def terminate(self) -> None:
        if getattr(self, "watcher", None) is not None:
            await self.watcher.close()
        await self.stop_dm_watch()
        think = getattr(self, "_think_task", None)
        if think and not think.done():
            think.cancel()
            try:
                await think
            except Exception:  # noqa: BLE001
                pass
        self._think_task = None
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
