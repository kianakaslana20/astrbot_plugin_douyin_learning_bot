"""面板（WebUI）后端：把插件状态摊平成 JSON，把面板上改的几项写回配置。

只暴露明确列在 EDITABLE 里的字段 —— 面板能调的都在这里，别的一律不给改。
"""
from __future__ import annotations

import time
from typing import Any

# path -> (类型, 说明)。类型只用来在写回时做一次收敛，不是给前端用的。
EDITABLE: dict[str, tuple[str, str]] = {
    # 刷视频
    "loop_enabled": ("bool", "定时刷"),
    "loop_interval_min": ("int", "刷的间隔（分钟）"),
    "inbox.autonomous_round": ("bool", "自己决定要不要刷"),
    "inbox.auto_rounds_per_day": ("int", "每天自主刷最多几轮"),
    "inbox.auto_items_per_day": ("int", "每天自主刷最多看几条"),
    "feed.want": ("int", "每轮挑几条"),
    # 写操作
    "actions.action_mode": ("enum:off,ask,auto", "总闸门"),
    "actions.auto_like": ("bool", "自动点赞"),
    "actions.auto_comment": ("bool", "刷完自动评论"),
    "actions.auto_share": ("bool", "自动分享"),
    "actions.comment_reply_mode": ("enum:auto,ask,off", "回别人的评论"),
    "actions.comment_reply_once_others": ("bool", "外人只回一次"),
    "actions.action_delay_s": ("int", "动作间隔（秒）"),
    "actions.max_actions_per_round": ("int", "一轮最多动几条"),
    "actions.comment_template": ("str", "评论模板"),
    # 额度
    "rate.rate_comment_per_day": ("int", "评论 / 天"),
    "rate.rate_like_per_day": ("int", "点赞 / 天"),
    "rate.rate_share_per_day": ("int", "分享 / 天"),
    "rate.rate_dm_per_hour": ("int", "回私信 / 小时"),
    "rate.rate_dm_per_day": ("int", "回私信 / 天"),
    "rate.rate_dm_out_per_hour": ("int", "主动私信 / 小时"),
    "rate.rate_dm_out_per_day": ("int", "主动私信 / 天"),
    "rate.quiet_hours": ("str", "静默时段（如 23:00-08:00）"),
    # 简报与推送
    "notify.brief_mode": ("enum:off,actions,all", "QQ 简报"),
    "notify.notify_done": ("bool", "跑完推结果"),
    "push_target": ("str", "简报推给谁"),
    # 私信
    "inbox.dm_chat_enabled": ("bool", "会回私信"),
    "inbox.dm_chat_allow": ("str", "允许聊的人"),
    "inbox.dm_http": ("bool", "私信走接口"),
    "inbox.notice_http": ("bool", "通知走接口"),
    "inbox.agent_enabled": ("bool", "让模型判断回不回"),
    "inbox.dm_targets": ("str", "允许主动发私信给谁"),
    "inbox.dm_self_names": ("str", "自己的号（回声判定）"),
    "inbox.dm_identity": ("str", "抖音昵称=QQ号"),
    # 学习
    "inbox.knowledge_enabled": ("bool", "记知识点卡片"),
    "inbox.knowledge_cards_in_prompt": ("int", "每次注入几张卡片"),
    "inbox.knowledge_review_daily": ("bool", "每天写一段复盘"),
    "inbox.memory_bridge": ("bool", "两边记忆互通"),
    "inbox.memory_bridge_lines": ("int", "互通带几句对话"),
    "inbox.memory_bridge_items": ("int", "互通带几条视频"),
    "understand.asr_backend": ("enum:auto,astrbot,openai,chat,local,off", "语音转写通道"),
    "understand.asr_model": ("str", "转写模型"),
    "understand.asr_api_base": ("str", "转写接口地址"),
}

RATE_KINDS = {
    "rate_comment_per_day": "comment",
    "rate_like_per_day": "like",
    "rate_share_per_day": "share",
    "rate_dm_per_day": "dm",
    "rate_dm_out_per_day": "dm_out",
}

_LIMIT_LABEL = {
    "comment": "评论",
    "like": "点赞",
    "share": "分享",
    "dm": "回私信",
    "dm_out": "主动私信",
    "mention": "评论回复",
}


def _get_path(cfg: Any, path: str, default: Any = None) -> Any:
    node = cfg
    for part in path.split("."):
        if not isinstance(node, dict):
            return default
        node = node.get(part)
        if node is None:
            return default
    return node


def _coerce(kind: str, value: Any) -> Any:
    if kind == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    if kind == "int":
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    if kind.startswith("enum:"):
        allowed = kind.split(":", 1)[1].split(",")
        text = str(value or "").strip()
        return text if text in allowed else None   # None = 不认识这个值，交给调用方拒掉
    return "" if value is None else str(value)


def editable_fields(cfg: Any) -> dict[str, Any]:
    return {path: _get_path(cfg, path) for path in EDITABLE}


def apply_updates(cfg: Any, updates: dict[str, Any]) -> tuple[list[str], list[str]]:
    """把面板传上来的值写进配置对象。返回 (改了的, 被拒的)。"""
    changed: list[str] = []
    rejected: list[str] = []
    for path, value in (updates or {}).items():
        spec = EDITABLE.get(path)
        if spec is None or not isinstance(cfg, dict):
            rejected.append(path)
            continue
        node = cfg
        parts = path.split(".")
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        coerced = _coerce(spec[0], value)
        if coerced is None:            # 枚举值不认识：整条拒掉，不要悄悄改成第一个选项
            rejected.append(path)
            continue
        node[parts[-1]] = coerced
        changed.append(path)
    return changed, rejected


async def snapshot(bot) -> dict:
    """面板一次拉完要显示的东西。任何一块查挂了都不影响其它块。"""
    out: dict[str, Any] = {
        "ok": True,
        "now": time.strftime("%Y-%m-%d %H:%M:%S"),
        "version": _version(bot),
        "fields": editable_fields(getattr(bot, "config", {}) or {}),
        "spec": {k: {"type": v[0], "label": v[1]} for k, v in EDITABLE.items()},
    }
    out["browser"] = await _safe(_browser_state, bot)
    out["limits"] = await _safe(_limits_state, bot)
    out["auto"] = await _safe(_auto_state, bot)
    out["queue"] = await _safe(_queue_state, bot)
    out["cards"] = await _safe(_cards_state, bot)
    out["actions"] = await _safe(_actions_state, bot)
    out["notes"] = _notes_text(bot)
    out["events"] = _event_state(bot)
    return out


def _version(bot) -> str:
    """插件版本号：优先 AstrBot 注入的 metadata，退回到包里的 metadata.yaml。"""
    for attr in ("version", "star_version"):
        value = getattr(bot, attr, "")
        if value:
            return str(value)
    try:
        from pathlib import Path
        import re

        text = (Path(__file__).resolve().parents[1] / "metadata.yaml").read_text("utf-8-sig")
        m = re.search(r"^version:\s*(\S+)", text, re.M)
        return m.group(1) if m else ""
    except Exception:  # noqa: BLE001
        return ""


async def _safe(fn, bot) -> dict:
    try:
        result = fn(bot)
        if hasattr(result, "__await__"):
            result = await result
        return result
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


async def _browser_state(bot) -> dict:
    state = {"logged_in": None, "window": None, "profile": ""}
    try:
        state["logged_in"] = await bot.browser.logged_in(launch=False)
    except Exception as exc:  # noqa: BLE001
        state["logged_in"] = None
        state["login_error"] = type(exc).__name__
    try:
        state["window"] = bot.browser.window_rect()
    except Exception:  # noqa: BLE001
        state["window"] = None
    try:
        state["profile"] = str(getattr(bot.settings, "profile_dir", "") or "")
    except Exception:  # noqa: BLE001
        pass
    return state


async def _limits_state(bot) -> dict:
    rows = []
    for setting_name, kind in RATE_KINDS.items():
        cap = int(getattr(bot.settings, setting_name, 0) or 0)
        try:
            used = int(await bot.limits.count_async(kind, 86400))
        except Exception:  # noqa: BLE001
            used = -1
        rows.append(
            {
                "kind": kind,
                "label": _LIMIT_LABEL.get(kind, kind),
                "used": used,
                "cap": cap,
            }
        )
    try:
        quiet = bool(bot.limits.in_quiet_hours(bot.settings.quiet_hours))
    except Exception:  # noqa: BLE001
        quiet = False
    return {"rows": rows, "quiet_now": quiet}


def _auto_state(bot) -> dict:
    bot._auto_state_load()  # noqa: SLF001 - 面板只读
    return {
        "day": getattr(bot, "_auto_day", ""),
        "rounds": getattr(bot, "_auto_rounds_today", 0),
        "items": getattr(bot, "_auto_items_today", 0),
        "last_round_at": getattr(bot, "_last_round_at", 0.0),
        "max_rounds": int(getattr(bot.settings, "auto_rounds_per_day", 0) or 0),
        "max_items": int(getattr(bot.settings, "auto_items_per_day", 0) or 0),
    }


async def _queue_state(bot) -> dict:
    out = {"comment_pending": [], "dm_pending": [], "inbox": {}}
    try:
        out["comment_pending"] = [
            {"key": row.get("key", ""), "src": row.get("src", ""),
             "payload": (row.get("payload") or "")[:160]}
            for row in await bot.store.inbox_pending("comment_reply", 20)
        ]
    except Exception as exc:  # noqa: BLE001
        out["comment_error"] = type(exc).__name__
    try:
        out["dm_pending"] = [
            {"id": row.get("id"), "to": row.get("target_uid", ""),
             "text": (row.get("text") or "")[:120], "at": row.get("created_at", 0)}
            for row in await bot.store.dm_pending(20)
        ]
    except Exception as exc:  # noqa: BLE001
        out["dm_error"] = type(exc).__name__
    try:
        out["inbox"] = await bot.store.inbox_stats()
    except Exception:  # noqa: BLE001
        out["inbox"] = {}
    return out


async def _cards_state(bot) -> dict:
    cards = []
    try:
        for row in await bot.store.card_recent(limit=40, since=0.0):
            cards.append(
                {
                    "id": row.get("id"),
                    "topic": row.get("topic", ""),
                    "take": row.get("take", ""),
                    "aweme_id": row.get("aweme_id", ""),
                    "at": row.get("created_at", 0),
                }
            )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}", "items": []}
    return {"items": cards, "total": len(cards), "count": len(cards)}


async def _actions_state(bot) -> dict:
    rows = []
    try:
        for row in await bot.store.actions_recent(limit=25):
            rows.append(
                {
                    "ts": row.get("ts", ""),
                    "kind": row.get("kind", ""),
                    "target": row.get("target", ""),
                    "ok": bool(row.get("ok")),
                    "detail": (row.get("detail") or "")[:140],
                    "aweme_id": row.get("aweme_id", ""),
                }
            )
    except Exception as exc:  # noqa: BLE001
        return {"error": type(exc).__name__, "items": []}
    return {"items": rows}


def _notes_text(bot) -> str:
    try:
        path = bot._notes_path()  # noqa: SLF001 - 面板只读
        if path.exists():
            return path.read_text("utf-8")
    except Exception:  # noqa: BLE001
        pass
    return ""


def _event_state(bot) -> dict:
    watcher = getattr(bot, "watcher", None)
    stats = {}
    if watcher is not None:
        try:
            stats = watcher.stats()
        except Exception:  # noqa: BLE001
            stats = {}
    return {
        "watcher": stats,
        "watcher_alive": bool(getattr(watcher, "alive", lambda: False)()) if watcher else False,
        "dm_task": bool(getattr(bot, "_dm_task", None)),
        "paused": bool(getattr(bot, "_dm_paused", False)),
    }