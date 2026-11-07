"""配置读取：把 AstrBotConfig 的松散 dict 收敛成带默认值的 Settings。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_SPLIT = re.compile(r"[,，;；\s]+")


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "开", "是"}:
        return True
    if text in {"0", "false", "no", "off", "关", "否", ""}:
        return False
    return default


def _as_int(value: Any, default: int, lo: int | None = None, hi: int | None = None) -> int:
    try:
        num = int(float(str(value).strip()))
    except Exception:
        num = default
    if lo is not None and num < lo:
        num = lo
    if hi is not None and num > hi:
        num = hi
    return num


def _as_float(value: Any, default: float) -> float:
    try:
        return float(str(value).strip())
    except Exception:
        return default


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


_DICT_NAME_KEYS = ("nick", "name", "value", "target", "who", "text", "昵称", "名字")


def _section(config: Any, name: str) -> dict:
    """安全取一段配置，取不到就给空 dict。"""
    try:
        value = config.get(name) if hasattr(config, "get") else None
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _as_list(value: Any, default: list[str] | None = None) -> list[str]:
    """把配置项收敛成 list[str]。

    支持三种来源：逗号/空格分隔的字符串、`list[str]`、以及面板 template_list 产生的
    `list[dict]`（每个 dict 是一个模板行，取其中的昵称字段）。
    """
    if value is None:
        return list(default or [])
    items: list[str] = []
    if isinstance(value, (list, tuple, set)):
        for entry in value:
            if isinstance(entry, dict):
                if entry.get("enabled") is False:      # 面板里没勾「启用」的行直接跳过
                    continue
                for key in _DICT_NAME_KEYS:
                    raw = entry.get(key)
                    if raw is not None and str(raw).strip():
                        items.append(str(raw).strip())
                        break
            else:
                items.append(str(entry).strip())
    else:
        items = _SPLIT.split(str(value).strip())
    return [v for v in items if v]


_GROUP_PREFIXES = ("群", "group", "g")
_PRIVATE_PREFIXES = ("私聊", "私", "private", "dm", "友")

_UMO_SAMPLE = "大肥鱼:FriendMessage:1931855425"


def resolve_umo(
    spec: str,
    *,
    default_platform: str = "",
    current_umo: str = "",
) -> tuple[str, str]:
    """把用户写的推送目标补全成 unified_msg_origin。

    支持三种写法：

    - 完整 umo：`大肥鱼:GroupMessage:482755644`（含两个冒号，原样返回）
    - 群号：`群482755644` / `group482755644`，补成 `<平台>:GroupMessage:<群号>`
    - QQ 号：`1931855425` / `私聊1931855425` / `dm1931855425`，补成 `<平台>:FriendMessage:<号码>`
    - `me` / `当前`：返回当前会话

    Returns:
        (umo, 人类可读的说明)。解析失败时 umo 为空字符串，说明里带原因。
    """
    text = (spec or "").strip()
    if not text:
        return "", ""
    if text.count(":") >= 2:
        return text, "完整 umo"
    platform = default_platform
    if not platform and current_umo and ":" in current_umo:
        platform = current_umo.split(":", 1)[0]
    low = text.lower()
    if low in {"me", "this", "当前", "当前会话", "这里"}:
        return current_umo, "当前会话"
    kind = ""
    body = text
    for prefix in _GROUP_PREFIXES:
        if low.startswith(prefix):
            kind, body = "group", text[len(prefix):].strip()
            break
    if not kind:
        for prefix in _PRIVATE_PREFIXES:
            if low.startswith(prefix):
                kind, body = "private", text[len(prefix):].strip()
                break
    body = body.lstrip(":#：").strip()
    if not body.isdigit():
        return "", (
            f"认不出「{text}」：群号写 群482755644，私聊写 1931855425，"
            f"或者直接写完整 umo（形如 {_UMO_SAMPLE}）"
        )
    if not platform:
        return "", "还没有平台信息，先在任意会话里发一次 /dy push"
    if not kind:
        kind = "private"
    msg_type = "GroupMessage" if kind == "group" else "FriendMessage"
    label = "群聊" if kind == "group" else "私聊"
    return f"{platform}:{msg_type}:{body}", label


def _section(config: Any, key: str) -> dict[str, Any]:
    try:
        value = config.get(key, {}) if hasattr(config, "get") else {}
    except Exception:
        value = {}
    return value if isinstance(value, dict) else {}


@dataclass
class Settings:
    """插件全部可调项。默认值与 _conf_schema.json 保持一致。"""

    enabled: bool = True

    # 后台循环
    loop_enabled: bool = False
    loop_interval_min: int = 90
    push_target: str = ""

    # 浏览器
    headless: bool = False
    quiet_window: bool = True      # 把浏览器窗口挪到屏幕外，后台跑不抢焦点（headless 会被风控，只能这样）
    profile_dir: str = ""
    channel: str = "msedge"          # msedge = 用系统 Edge 内核（默认）；"" = Playwright 自带 chromium
    channel_strict: bool = False     # True = Edge 起不来就直接报错，不偷偷退回 chromium
    profile_pin: bool = False        # True = 钉死当前 profile 目录，自动迁移一律不做
    cookie_source: str = "auto"      # auto = profile 没登录时先试着从本机 Edge/Chrome 借一次
    window_on_login: bool = True    # 需要人扫码/过验证时，自动把窗口摆到桌面上（不用再找）
    cookie_string: str = ""          # 粘贴整条 cookie 串（浏览器 F12 里原样复制，只填 k=v; k=v）
    cookie_keepalive_min: int = 20   # 每隔多少分钟摸一下抖音让站点自己续 cookie（0 = 不保活）

    # 取流
    want: int = 8
    max_scroll: int = 6
    scroll_wait_ms: int = 2800
    min_duration_s: int = 45
    max_duration_s: int = 900
    min_like: int = 500
    min_score: float = 4.0          # 打分低于这个就不学（收紧选片的主旋钮）
    min_knowledge_hits: int = 1     # 至少要命中几个"真知识词"（科普/原理/开源/教程…），0 = 不要求
    interest_keywords: list[str] = field(
        default_factory=lambda: [
            "AI", "编程", "历史", "科普", "商业", "效率", "English", "数学",
            "物理", "金融", "写作", "英语", "教程", "原理", "经济", "复盘",
        ]
    )
    block_keywords: list[str] = field(
        default_factory=lambda: [
            "纯享", "小姐姐", "擦边", "带货", "直播回放", "抽奖", "求关注", "广告",
            "短剧", "漫剧", "追剧", "全集", "连载", "第1集", "第一季",
        ]
    )

    # 理解
    frames: int = 9
    frame_width: int = 480
    asr_max_seconds: int = 480
    stt_provider: str = ""
    vision_provider: str = ""

    # 语音转写：astrbot = 用 AstrBot 里配好的 STT；openai = 直连 OpenAI 兼容 /audio/transcriptions；off = 不转写
    asr_backend: str = "auto"
    asr_api_base: str = ""
    asr_api_key: str = ""
    asr_model: str = "FunAudioLLM/SenseVoiceSmall"
    asr_language: str = "zh"

    # 媒体
    max_download_mb: int = 220
    keep_media: bool = False
    ffmpeg_path: str = ""

    # 写操作
    auto_like: bool = False
    auto_comment: bool = True
    auto_share: bool = False      # ask 模式下分享要不要也自动做（默认仍问）
    comment_reply_mode: str = "auto"   # auto / ask / off
    comment_reply_once_others: bool = True   # 非自己人只回一次

    # 写操作（分享 / 评论 @）：off=只播报，ask=推送后问一句，auto=自动执行
    action_mode: str = "off"
    share_mode: str = "panel"          # panel = 点视频右侧分享面板（实测可用）；im = 走私信抽屉发链接
    share_targets: list[str] = field(default_factory=list)
    share_text: str = ""            # 分享时附带的文案，留空则只发链接；可用 {title} {url}
    mention_targets: list[str] = field(default_factory=list)
    comment_template: str = ""
    action_delay_s: int = 6
    action_jitter: bool = True      # 间隔随机化（固定节奏本身就是指纹）
    max_actions_per_round: int = 2
    action_dump: bool = True

    # 文案层：让对话模型 + AstrBot 人格写评论，而不是套模板
    inbox_dm: bool = True
    inbox_mentions: bool = True
    inbox_max_conversations: int = 8
    inbox_max_cards: int = 3
    inbox_share_summary: bool = True
    inbox_mention_reply: bool = True
    dm_allow: str = ""
    dm_chat_enabled: bool = False
    dm_chat_allow: str = ""
    dm_chat_max: int = 2
    dm_chat_no_limit: str = "",  # 这些人私聊不限次数（逗号分隔）
    dm_chat_poll_s: int = 0,    # >0：每隔这么多秒盯一次私信（0=跟着主循环）
    dm_chat_history: int = 6,   # 带几句上下文
    # 主动发私信（0.9.7）：默认开，但仍受 action_mode 约束（off 一律不发）
    agent_enabled: bool = True        # 事件回不回交给模型判断
    event_ws: bool = True             # 用 IM WebSocket 帧当触发信号
    poll_fallback_min: int = 10       # WS 静默时的兜底轮询间隔（分钟）
    autonomous_round: bool = True     # 不需要 /dy start 也能自己刷
    auto_rounds_per_day: int = 4      # 自主刷每天最多几轮
    auto_items_per_day: int = 8       # 自主刷每天最多看几条
    dm_send_enabled: bool = True
    dm_targets: str = ""            # 只允许发给名单里这些人（空 = 谁都不发）
    dm_quota_separate: bool = True  # True：主动私信单独配额；False：和「回私信」共用一个池
    dm_readback: bool = True
    dm_http: bool = True
    dm_http_limit: int = 20
    notice_http: bool = True        # 发完回读校验，false 只是不校验
    dm_identity: str = ""           # 抖音昵称=QQ号，一行一条（抖音侧靠它读 QQ 历史）
    memory_bridge: bool = True      # QQ 侧注入「抖音私聊 + 最近看的视频」
    memory_bridge_lines: int = 6    # 注入几句抖音私聊
    memory_bridge_items: int = 4    # 注入几条看过的视频
    dm_self_names: str = ""    # 自聊会话（自己跟自己）：这些会话里靠"我说过什么"来分辨谁在说,     # 抖音昵称=QQ号，一行一条
    voice_enabled: bool = True     # 关掉就退回 comment_template（老行为）
    voice_persona: str = ""        # 人格名；留空 = 用该会话的默认人格
    voice_max_chars: int = 40      # 一条评论多少字以内
    voice_frames: int = 1          # 给模型看几张图（0=只给文字，1=蒙太奇拼图）
    voice_timeout_s: int = 60      # 等模型多久
    voice_banned: str = ""         # 额外禁词，逗号分隔（内置一份 AI 腔词表）
    # 白名单：开了以后只允许对 share_targets / mention_targets 里的人动手，命令里临时写别的名字会被拒
    enforce_allowlist: bool = True

    # 频率与静默时段（写操作的闸门，计数落 sqlite，重载不清零）
    rate_enabled: bool = True
    rate_comment_per_day: int = 8     # 每天最多发几条评论
    rate_share_per_day: int = 10      # 每天最多分享几次
    rate_like_per_day: int = 30       # 每天最多点几个赞
    rate_dm_per_hour: int = 6         # 每小时最多回几条私信
    rate_dm_out_per_hour: int = 3     # 主动发私信：每小时
    rate_dm_out_per_day: int = 20     # 主动发私信：每天
    rate_summary_per_day: int = 20    # 每天最多写几份私信视频总结
    quiet_hours: str = ""             # 例 "01:00-08:00"，这段时间不发任何东西
    pending_ttl_hours: int = 12       # ask 模式攒下的待执行动作，超过这么久作废
    keep_days: int = 30               # 记录与临时文件保留多少天，0 = 不清

    # 后台 / 通知
    notify_start: bool = False     # 一轮开跑时先说一句（后台循环容易刷屏，默认关）
    notify_done: bool = True       # 跑完推结果
    brief_mode: str = "actions"  # off / actions（只有真动手才说） / all
    notify_error: bool = True      # 失败推原因
    notify_login: bool = True      # 需要登录时主动叫（发二维码截图）
    login_wait_min: int = 6        # 叫了之后等扫码多少分钟
    login_qr: bool = True          # 是否把二维码截图发到聊天里
    idle_close_min: int = 30       # 后台循环两轮之间闲置超过这么久就关浏览器省内存（0=不关）
    stage_notify: bool = False     # 是否推送阶段变化（取流→下载→转写），默认不推，免得刷屏
    verify_notify: bool = True     # 撞上人机验证时是否叫你
    verify_wait_min: int = 5       # 叫你之后等多久（分钟）
    verify_retry: bool = True      # 验证过了是否自动重试这一轮
    login_window_s: int = 90       # 切到空 profile 后把窗口摆到桌面等扫码多少秒

    # 出事时敲谁的门（unified_msg_origin 或 QQ 号简写）；空 = 自动找管理员/最近会话
    help_target: str = ""

    # 接口动作的重试（评论接口会短暂限流：回 200 + 空 body）
    comment_retry: int = 2         # 空回包时重试几次
    comment_retry_s: int = 20      # 第一次重试前等几秒（之后翻倍）

    @classmethod
    def load(cls, config: Any) -> "Settings":
        br = _section(config, "browser")
        fd = _section(config, "feed")
        un = _section(config, "understand")
        md = _section(config, "media")
        ac = _section(config, "actions")
        # 0.9.3 起这两组从 actions 里拆出来了；老配置文件（键还在 actions 里）照样能读
        ibx = {**ac, **_section(config, "inbox")}
        rt = {**ac, **_section(config, "rate")}

        def top(key: str, default: Any = None) -> Any:
            try:
                return config.get(key, default)
            except Exception:
                return default

        out = cls(
            enabled=_as_bool(top("enabled"), True),
            loop_enabled=_as_bool(top("loop_enabled"), False),
            loop_interval_min=_as_int(top("loop_interval_min"), 90, 5, 24 * 60),
            push_target=_as_str(top("push_target"), ""),
            headless=_as_bool(br.get("headless"), False),
            quiet_window=_as_bool(br.get("quiet_window"), True),
            profile_dir=_as_str(br.get("profile_dir"), ""),
            channel=_as_str(br.get("channel"), "msedge"),
            channel_strict=_as_bool(br.get("channel_strict"), False),
            profile_pin=_as_bool(br.get("profile_pin"), False),
            cookie_source=_as_str(br.get("cookie_source"), "auto").lower(),
            window_on_login=_as_bool(br.get("window_on_login"), True),
            cookie_string=_as_str(br.get("cookie_string"), ""),
            cookie_keepalive_min=_as_int(br.get("cookie_keepalive_min"), 20, 0, 24 * 60),
            want=_as_int(fd.get("want"), 8, 1, 60),
            max_scroll=_as_int(fd.get("max_scroll"), 6, 1, 50),
            scroll_wait_ms=_as_int(fd.get("scroll_wait_ms"), 2800, 500, 15000),
            min_duration_s=_as_int(fd.get("min_duration_s"), 45, 0, 7200),
            max_duration_s=_as_int(fd.get("max_duration_s"), 900, 10, 14400),
            min_like=_as_int(fd.get("min_like"), 500, 0, 100_000_000),
            min_score=_as_float(fd.get("min_score"), 4.0),
            min_knowledge_hits=_as_int(fd.get("min_knowledge_hits"), 1, 0, 9),
            interest_keywords=_as_list(fd.get("interest_keywords")),
            block_keywords=_as_list(fd.get("block_keywords")),
            frames=_as_int(un.get("frames"), 9, 1, 25),
            frame_width=_as_int(un.get("frame_width"), 480, 120, 1920),
            asr_max_seconds=_as_int(un.get("asr_max_seconds"), 480, 5, 7200),
            stt_provider=_as_str(un.get("stt_provider"), ""),
            vision_provider=_as_str(un.get("vision_provider"), ""),
            asr_backend=_as_str(un.get("asr_backend"), "auto").lower(),
            asr_api_base=_as_str(un.get("asr_api_base"), "").rstrip("/"),
            asr_api_key=_as_str(un.get("asr_api_key"), ""),
            asr_model=_as_str(un.get("asr_model"), "FunAudioLLM/SenseVoiceSmall"),
            asr_language=_as_str(un.get("asr_language"), "zh"),
            max_download_mb=_as_int(md.get("max_download_mb"), 220, 1, 4096),
            keep_media=_as_bool(md.get("keep_media"), False),
            ffmpeg_path=_as_str(md.get("ffmpeg_path"), ""),
            auto_like=_as_bool(ac.get("auto_like"), False),
            auto_comment=_as_bool(ac.get("auto_comment"), True),
            auto_share=_as_bool(ac.get("auto_share"), False),
            comment_reply_mode=(
                _as_str(ac.get("comment_reply_mode"), "auto").lower()
                if _as_str(ac.get("comment_reply_mode"), "auto").lower() in {"auto", "ask", "off"}
                else "auto"
            ),
            comment_reply_once_others=_as_bool(ac.get("comment_reply_once_others"), True),
            action_mode=_as_str(ac.get("action_mode"), "off").lower(),
            share_mode=_as_str(ac.get("share_mode"), "panel").lower(),
            share_targets=_as_list(ac.get("share_targets")),
            share_text=_as_str(ac.get("share_text"), ""),
            mention_targets=_as_list(ac.get("mention_targets")),
            comment_template=_as_str(ac.get("comment_template"), ""),
            action_delay_s=_as_int(ac.get("action_delay_s"), 6, 2, 120),
            action_jitter=_as_bool(rt.get("action_jitter"), True),
            max_actions_per_round=_as_int(ac.get("max_actions_per_round"), 2, 0, 20),
            action_dump=_as_bool(ac.get("action_dump"), True),
            inbox_dm=_as_bool(ibx.get("inbox_dm"), True),
        inbox_mentions=_as_bool(ibx.get("inbox_mentions"), True),
        inbox_max_conversations=_as_int(ibx.get("inbox_max_conversations"), 8, 1, 30),
        inbox_max_cards=_as_int(ibx.get("inbox_max_cards"), 3, 1, 10),
        inbox_share_summary=_as_bool(ibx.get("inbox_share_summary"), True),
        inbox_mention_reply=_as_bool(ibx.get("inbox_mention_reply"), True),
        dm_allow=_as_str(ibx.get("dm_allow"), ""),
        dm_chat_enabled=_as_bool(ibx.get("dm_chat_enabled"), False),
        dm_chat_allow=_as_str(ibx.get("dm_chat_allow"), ""),
        dm_chat_max=_as_int(ibx.get("dm_chat_max"), 2, 0, 10),
        dm_chat_no_limit=_as_str(ibx.get("dm_chat_no_limit"), ""),
        dm_chat_poll_s=_as_int(ibx.get("dm_chat_poll_s"), 0, 0, 3600),
        dm_chat_history=_as_int(ibx.get("dm_chat_history"), 6, 0, 20),
        dm_send_enabled=_as_bool(ibx.get("dm_send_enabled"), True),
        agent_enabled=_as_bool(ibx.get("agent_enabled"), True),
        event_ws=_as_bool(ibx.get("event_ws"), True),
        poll_fallback_min=_as_int(ibx.get("poll_fallback_min"), 10, 1, 240),
        autonomous_round=_as_bool(ibx.get("autonomous_round"), True),
        auto_rounds_per_day=_as_int(ibx.get("auto_rounds_per_day"), 4, 0, 100),
        auto_items_per_day=_as_int(ibx.get("auto_items_per_day"), 8, 0, 200),
        dm_targets=_as_str(ibx.get("dm_targets"), ""),
        dm_quota_separate=_as_bool(ibx.get("dm_quota_separate"), True),
        dm_readback=_as_bool(ibx.get("dm_readback"), True),
        dm_identity=_as_str(ibx.get("dm_identity"), ""),
        memory_bridge=_as_bool(ibx.get("memory_bridge"), True),
        memory_bridge_lines=_as_int(ibx.get("memory_bridge_lines"), 0, 30),
        memory_bridge_items=_as_int(ibx.get("memory_bridge_items"), 0, 20),
        dm_self_names=_as_str(ibx.get("dm_self_names"), ""),
        voice_enabled=_as_bool(ac.get("voice_enabled"), True),
            voice_persona=_as_str(ac.get("voice_persona"), ""),
            voice_max_chars=_as_int(ac.get("voice_max_chars"), 40, 6, 200),
            voice_frames=_as_int(ac.get("voice_frames"), 1, 0, 4),
            voice_timeout_s=_as_int(ac.get("voice_timeout_s"), 60, 5, 300),
            voice_banned=_as_str(ac.get("voice_banned"), ""),
            enforce_allowlist=_as_bool(ac.get("enforce_allowlist"), True),
            rate_enabled=_as_bool(rt.get("rate_enabled"), True),
            rate_comment_per_day=_as_int(rt.get("rate_comment_per_day"), 8, 0, 500),
            rate_share_per_day=_as_int(rt.get("rate_share_per_day"), 10, 0, 500),
            rate_like_per_day=_as_int(rt.get("rate_like_per_day"), 30, 0, 999),
            rate_dm_per_hour=_as_int(rt.get("rate_dm_per_hour"), 6, 0, 500),
            rate_dm_out_per_hour=_as_int(rt.get("rate_dm_out_per_hour"), 3, 0, 200),
            rate_dm_out_per_day=_as_int(rt.get("rate_dm_out_per_day"), 20, 0, 999),
        rate_summary_per_day=_as_int(rt.get("rate_summary_per_day"), 20, 0, 500),
            quiet_hours=_as_str(rt.get("quiet_hours"), ""),
            pending_ttl_hours=_as_int(rt.get("pending_ttl_hours"), 12, 0, 168),
            keep_days=_as_int(rt.get("keep_days"), 30, 0, 3650),
        )

        bg = _section(config, "notify") or _section(config, "background")
        out.notify_start = _as_bool(bg.get("notify_start"), False)
        out.notify_done = _as_bool(bg.get("notify_done"), True)
        out.brief_mode = (_as_str(bg.get("brief_mode"), "actions").lower() if _as_str(bg.get("brief_mode"), "actions").lower() in {"off", "actions", "all"} else "actions")
        out.notify_error = _as_bool(bg.get("notify_error"), True)
        out.notify_login = _as_bool(bg.get("notify_login"), True)
        out.login_wait_min = _as_int(bg.get("login_wait_min"), 6, 1, 60)
        out.login_qr = _as_bool(bg.get("login_qr"), True)
        out.idle_close_min = _as_int(bg.get("idle_close_min"), 30, 0, 1440)
        out.stage_notify = _as_bool(bg.get("stage_notify"), False)
        out.verify_notify = _as_bool(bg.get("verify_notify"), True)
        out.verify_wait_min = _as_int(bg.get("verify_wait_min"), 5, 1, 60)
        out.verify_retry = _as_bool(bg.get("verify_retry"), True)
        out.login_window_s = _as_int(bg.get("login_window_s"), 90, 20, 600)
        out.help_target = _as_str(bg.get("help_target"), "")
        out.comment_retry = _as_int(bg.get("comment_retry"), 2, 0, 5)
        out.comment_retry_s = _as_int(bg.get("comment_retry_s"), 20, 5, 120)
        return out

    # ---------- 白名单 ----------

    @property
    def share_allowlist(self) -> list[str]:
        return [t for t in (self.share_targets or []) if t]

    @property
    def mention_allowlist(self) -> list[str]:
        return [t for t in (self.mention_targets or []) if t]

    def check_targets(self, names: list[str], *, kind: str = "share") -> tuple[list[str], list[str]]:
        """按白名单过滤目标，返回 (允许的, 被拒的)。

        `enforce_allowlist=False` 时不过滤（全放行）。匹配规则是「配置里写的是请求里名字的
        子串」也算命中——和面板里按片段匹配昵称的行为一致。
        """
        wanted = [n for n in names if n]
        if not self.enforce_allowlist or kind not in {"share", "mention"}:
            return wanted, []
        allow = self.share_allowlist if kind == "share" else self.mention_allowlist
        if not allow:
            return [], wanted
        allowed: list[str] = []
        rejected: list[str] = []
        for name in wanted:
            hit = any(a == name or a in name or name in a for a in allow)
            (allowed if hit else rejected).append(name)
        return allowed, rejected

    @property
    def max_download_bytes(self) -> int:
        return self.max_download_mb * 1024 * 1024
