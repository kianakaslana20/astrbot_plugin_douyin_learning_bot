"""douyin/agent.py —— 事件交给模型判断（0.10.0）

为什么要这一层：以前「收到私信就按规则回」，规则写死的东西没法判断
「这条值不值得回」「这个分享值不值得看」。这里把判断交给主核心配的对话模型，
人设取 AstrBot 的人格，返回结构化 decision，调用方只照 decision 执行。
"""
from __future__ import annotations

import json
import logging
import re

from astrbot.api import logger as _logger  # noqa: F401  （保证 astrbot 包可用时导得通）

logger = logging.getLogger("astrbot.douyin.agent")

_DECISION_KEYS = ("reply", "text", "reason", "run", "take", "worth")

_EVENT_RULES = """现在是抖音私信/评论的场景。下面是一条事件，你要决定：回不回、回什么。

规则：
- 只有真的需要你回应时才 reply=true。广告、群发、重复的、看不懂的、纯表情的可不回。
- reply=true 时 text 用本人的口吻，一到两句，不用敬语、不用「您好」「希望」「感谢」这类客服腔，不写 @，不用 markdown。
- 白名单里的人（自己人）可以随意一点；不是白名单的陌生人保守一点，能不回就不回。
- reason 用中文一句话说清为什么这么定。

只输出 JSON：{"reply": true/false, "text": "...", "reason": "..."}"""

_ROUND_RULES = """现在是抖音刷视频的场景。你要决定：这一轮值不值得去刷。

规则：
- 刚刷过没多久、或者没什么新东西可看，就 run=false。
- 有人分享了视频、或者刚跟人聊过（说明人在），可以 run=true。
- take 是想刷几条（1~5）。reason 中文一句话。

只输出 JSON：{"run": true/false, "take": 3, "reason": "..."}"""


def _loads(text: str) -> dict:
    """从模型回包里抠出 JSON（模型常带 ```json 围栏或前后废话）。"""
    raw = (text or "").strip()
    if not raw:
        return {}
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    if fence:
        raw = fence.group(1)
    else:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            raw = m.group(0)
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _clean_text(text: str, limit: int = 120) -> str:
    t = (text or "").strip().strip('"“”\'')
    t = re.sub(r"\s+", " ", t)
    return t[:limit]


class DouyinAgent:
    """模型决策层：事件回不回、这一轮值不值得刷。"""

    def __init__(self, voice, settings, store=None, memory=None):
        self.voice = voice
        self.settings = settings
        self.store = store
        self.memory = memory

    # ------------------------------------------------------------ 内部
    async def _ask(self, rules: str, body: str, umo: str = "") -> dict:
        provider = None
        try:
            provider = self.voice.provider(umo) if self.voice else None
        except Exception as exc:  # noqa: BLE001
            logger.info("agent: 拿不到对话模型（%s）", type(exc).__name__)
        if provider is None:
            return {}
        system = rules
        try:
            persona = self.voice.persona_prompt(umo) if self.voice else ""
        except Exception:  # noqa: BLE001
            persona = ""
        if persona:
            system = f"{persona}\n\n{rules}"
        timeout = float(getattr(self.settings, "voice_timeout_s", 60) or 60)
        import asyncio

        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=body, system_prompt=system), timeout=timeout
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("agent: 模型调用失败（%s）", type(exc).__name__)
            return {}
        return _loads(getattr(resp, "completion_text", "") or "")

    def _history(self, who: str, limit: int = 12) -> str:
        if not self.memory:
            return ""
        try:
            rows = self.memory.recent(who, limit=limit)
        except Exception:  # noqa: BLE001
            return ""
        return "\n".join(rows or [])

    def _in_allow(self, who: str) -> bool:
        names = [
            x.strip()
            for x in (getattr(self.settings, "dm_targets", "") or "")
            .replace("，", ",").replace("、", ",").split(",")
            if x.strip()
        ]
        return bool(names) and any(a == who or a in who or who in a for a in names)

    # ------------------------------------------------------------ 对外
    async def handle_douyin_event(self, evt: dict, umo: str = "") -> dict:
        """evt: {type, from_nick, content, time, event_id}"""
        kind = str(evt.get("type") or "dm")
        who = str(evt.get("from_nick") or "")
        content = str(evt.get("content") or "")
        allowed = self._in_allow(who)
        body = "\n".join(
            [
                f"事件类型：{kind}",
                f"对方昵称：{who or '（未知）'}",
                f"是否自己人（白名单）：{'是' if allowed else '否'}",
                f"本次内容：{content[:500] or '（空）'}",
                "最近的对话（旧→新）：",
                self._history(who) or "（没有记录）",
            ]
        )
        data = await self._ask(_EVENT_RULES, body, umo)
        decision = {
            "model": bool(data),  # 没拿到模型/没解析出 JSON 时为 False，调用方据此退回规则
            "reply": bool(data.get("reply")),
            "text": _clean_text(data.get("text") or ""),
            "reason": _clean_text(data.get("reason") or "", 200),
        }
        if decision["reply"] and not decision["text"]:
            decision["reply"] = False
            decision["reason"] = decision["reason"] or "模型说回，但没给话"
        await self._record(evt, decision)
        return decision

    async def judge_round(self, info: dict, umo: str = "") -> dict:
        body = "\n".join(
            [
                f"距上次刷完：{info.get('since_last_min', '未知')} 分钟",
                f"上次刷了几条：{info.get('last_count', 0)}",
                f"今天已经自主刷了几轮：{info.get('today_rounds', 0)} / 上限 {info.get('max_rounds', 0) or '不限'}",
                f"最近有人互动吗：{'有' if info.get('human_recent') else '没有'}",
                f"有新的分享/评论/@ 吗：{info.get('pending', '没有') or '没有'}",
            ]
        )
        data = await self._ask(_ROUND_RULES, body, umo)
        run = bool(data.get("run"))
        try:
            take = int(data.get("take") or 0)
        except Exception:  # noqa: BLE001
            take = 0
        out = {
            "run": run,
            "take": max(0, min(take or int(getattr(self.settings, "want", 3) or 3), 5)),
            "reason": _clean_text(data.get("reason") or "", 200),
        }
        if not run:
            out["take"] = 0
        if self.store:
            try:
                await self.store.judge_add(
                    f"round:{int(__import__('time').time())}",
                    "round",
                    "self",
                    info.get("why", ""),
                    "run" if run else "skip",
                    out["reason"],
                )
            except Exception:  # noqa: BLE001
                pass
        return out

    async def _record(self, evt: dict, decision: dict) -> None:
        if not self.store:
            return
        try:
            await self.store.judge_add(
                str(evt.get("event_id") or ""),
                str(evt.get("type") or "dm"),
                str(evt.get("from_nick") or ""),
                str(evt.get("content") or ""),
                "reply" if decision.get("reply") else "skip",
                str(decision.get("reason") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("agent: 落库失败（%s）", type(exc).__name__)