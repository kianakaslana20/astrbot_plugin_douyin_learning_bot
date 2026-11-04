"""文案层：让 AstrBot 的对话模型 + 当前人格写话，而不是套模板。

为什么要单独一层：

- 模板（`comment_template = "看完记一笔：{summary}"`）一多就露馅——同一句话反复出现在
  不同视频下面，等于告诉所有人这是机器人。
- 评论要的是「一个人刷完随手留一句」，这需要模型看过内容（抽帧 + 转写摘要）再说，
  而不是把摘要截断 60 字套进固定句式。
- 语气应该跟 AstrBot 里配的人格一致：同一个号在 QQ 上怎么说话，在抖音评论区就怎么说话。

所以这里做三件事：
1. 从 `context.persona_manager` 取人格 prompt（留空则用该会话的默认人格）；
2. 用 `context.get_using_provider(umo)` 拿当前对话模型，图文一起发（蒙太奇那张图）；
3. 生成后自检——命中禁词/太像总结就重写一次，仍不合格就退回模板（宁可平淡，不要 AI 腔）。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from astrbot.api import logger

# 模型爱用的"总结腔"。出现在评论区就非常出戏，命中即重写。
DEFAULT_BANNED = (
    "首先",
    "其次",
    "再次",
    "最后一点",
    "综上",
    "总之",
    "总的来说",
    "值得一提",
    "不得不说",
    "希望对你有帮助",
    "作为一个",
    "该视频",
    "这个视频",
    "这条视频",
    "本视频",
    "视频中",
    "内容丰富",
    "干货满满",
    "值得一看",
    "意义在于",
    "展现了",
    "体现了",
    "不仅",
    "而且",
)

_COMMENT_RULES = """你现在不是助手，是一个刚刷完这条视频、顺手在评论区留一句话的普通人。

只输出那一句话本身，不要解释、不要引号、不要换行、不要列表、不要标题。
要求：
- 一句话，{max_chars} 字以内，越短越像人；
- 可以有态度、可以口语、可以只说一个点；不要复述标题，不要总结全片；
- 不要出现"首先/其次/综上/总之/值得一提/这个视频/内容丰富"这类腔调；
- emoji 最多一个，能不写就不写；
- 不要 @ 任何人，也不要写"@某某"——@ 由外面加；
- 不确定的地方就含糊带过，不要编造具体数字和人名。"""

_SUMMARY_RULES = """你要用第一人称给"自己"写一条看完视频后的记录，像发给朋友的消息，不是给读者的文章。

要求：
- {max_chars} 字以内，口语，允许不完整句；
- 说清三件事：讲的是什么、有没有新东西、我信不信/想不想再看；
- 不要分点、不要小标题、不要"首先其次"；
- 没有信息量的地方就直说"没看懂"或"这条没什么"，不要硬凑；
- emoji 最多两个。"""


_REPLY_RULES = """有人在你刷到的视频下面 @ 了你，回他一句，像平时聊天。

要求：
- 一句话，{max_chars} 字以内，可以先答他的问题再补半句
- 直接回应他说的事，别绕开、别答非所问
- 不用敬语，不写「感谢分享」「说得对」这类场面话
- 不写 @昵称、不写引号、不复述视频标题
"""


_CHAT_RULES = """你在用抖音私信跟人聊天。回一句，像本人打字。

要求：
- {max_chars} 字以内，一到两句，可以只回半句
- 接住对方那句话的意思，别答非所问、别自说自话
- 不用敬语、不解释自己是谁、不写「有什么可以帮你」
- 不写 @昵称、不加引号、不用 markdown
- 对方只是丢个表情/「嗯」这类，就短回，别硬找话题
"""


class Voice:
    """把「说什么」交给模型和人格，把「怎么发出去」留给 social。"""

    def __init__(self, context: Any, settings: Any) -> None:
        self.context = context
        self.settings = settings

    # ------------------------------------------------------------------ 配置

    def _banned(self) -> tuple[str, ...]:
        extra = getattr(self.settings, "voice_banned", "") or ""
        words = [w.strip() for w in re.split(r"[,，、;；\s]+", extra) if w.strip()]
        return tuple(DEFAULT_BANNED) + tuple(words)

    def _max_chars(self, default: int = 40) -> int:
        try:
            n = int(getattr(self.settings, "voice_max_chars", default) or default)
        except (TypeError, ValueError):
            n = default
        return max(6, min(200, n))

    # -------------------------------------------------------------- 人格/模型

    async def persona_prompt(self, umo: str = "") -> str:
        """取 AstrBot 里的人格设定；拿不到就空着（模型仍会按规则写）。"""
        pm = getattr(self.context, "persona_manager", None)
        if pm is None:
            return ""
        name = (getattr(self.settings, "voice_persona", "") or "").strip()
        persona = None
        try:
            if name:
                persona = pm.get_persona_v3_by_id(name)
                if persona is None:
                    logger.warning("人格「%s」不存在，改用当前会话默认人格", name)
            if persona is None:
                persona = await pm.get_default_persona_v3(umo or None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("取人格失败：%s", exc)
            return ""
        if not persona:
            return ""
        prompt = (
            persona.get("prompt", "")
            if isinstance(persona, dict)
            else getattr(persona, "prompt", "")
        )
        return (prompt or "").strip()

    async def provider(self, umo: str = "") -> Any:
        getter = getattr(self.context, "get_using_provider", None)
        prov = None
        if callable(getter):
            try:
                prov = getter(umo or None)
            except TypeError:
                prov = getter()
            except Exception as exc:  # noqa: BLE001
                logger.warning("取对话模型失败：%s", exc)
        if prov is None:
            async_getter = getattr(self.context, "get_using_provider_async", None)
            if callable(async_getter):
                try:
                    prov = await async_getter(umo or None)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("取对话模型失败（async）：%s", exc)
        return prov

    async def _chat(
        self,
        system: str,
        prompt: str,
        images: list[str] | None = None,
        umo: str = "",
    ) -> str:
        prov = await self.provider(umo)
        if prov is None:
            logger.warning("没有可用的对话模型，文案退回模板")
            return ""
        timeout = getattr(self.settings, "voice_timeout_s", 60) or 60
        try:
            resp = await asyncio.wait_for(
                prov.text_chat(
                    prompt=prompt,
                    system_prompt=system,
                    image_urls=images or None,
                ),
                timeout=float(timeout),
            )
        except asyncio.TimeoutError:
            logger.warning("对话模型超时（%ss），文案退回模板", timeout)
            return ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("对话模型调用失败：%s", exc)
            return ""
        text = getattr(resp, "completion_text", "") or ""
        return text.strip()

    # ------------------------------------------------------------------ 自检

    def check(self, text: str) -> str:
        """返回不合格的原因；空串表示可用。"""
        if not text:
            return "空"
        low = text.lower()
        for word in self._banned():
            if word.lower() in low:
                return f"命中禁词「{word}」"
        if text.count("\n") >= 2:
            return "多行了"
        if re.match(r"^\s*([-*·]|\d+[.、])", text):
            return "像列表"
        return ""

    @staticmethod
    def _clean(text: str) -> str:
        t = text.strip()
        t = re.sub(r"^[\"'“”‘’「」]+|[\"'“”‘’「」]+$", "", t)
        t = re.sub(r"\s*\n\s*", " ", t)
        t = re.sub(r"^@\S+\s*", "", t)          # 模型自己加的 @ 一律去掉
        return t.strip()

    # ------------------------------------------------------------------ 产出

    def _images(self, frames: list[str] | None) -> list[str]:
        n = int(getattr(self.settings, "voice_frames", 1) or 0)
        if n <= 0 or not frames:
            return []
        out: list[str] = []
        for raw in list(frames)[:n]:
            p = str(raw)
            out.append(p if p.startswith(("http://", "https://", "file://", "data:")) else f"file:///{p.lstrip('/')}")
        return out

    async def comment(
        self,
        *,
        title: str,
        author: str = "",
        summary: str = "",
        keywords: list[str] | None = None,
        transcript: str = "",
        frames: list[str] | None = None,
        umo: str = "",
    ) -> str:
        """写一条评论区留言；写不出来返回空串，调用方退回模板。"""
        max_chars = self._max_chars(40)
        persona = await self.persona_prompt(umo)
        system = (persona + "\n\n" if persona else "") + _COMMENT_RULES.format(
            max_chars=max_chars
        )
        facts = [f"标题：{title or '（无）'}"]
        if author:
            facts.append(f"作者：{author}")
        if summary:
            facts.append(f"内容要点：{summary[:300]}")
        if keywords:
            facts.append("关键词：" + "、".join(keywords[:6]))
        if transcript:
            facts.append("听写片段：" + transcript[:200].replace("\n", " "))
        prompt = "\n".join(facts) + "\n\n写那句话："

        for attempt in range(2):
            raw = await self._chat(
                system if attempt == 0 else system + "\n（上一次写得太像总结了，重写一句更像人话的）",
                prompt,
                self._images(frames),
                umo,
            )
            text = self._clean(raw)
            if not text:
                break
            text = text[: max_chars * 2]
            why = self.check(text)
            if not why:
                return text[:120]
            logger.info("评论文案不合格（%s）：%s", why, text[:40])
        return ""

    async def summary(
        self,
        *,
        title: str,
        author: str = "",
        analysis_summary: str = "",
        keywords: list[str] | None = None,
        transcript: str = "",
        frames: list[str] | None = None,
        umo: str = "",
    ) -> str:
        """写一条"像人写的"看完记录；失败返回空串。"""
        max_chars = max(60, self._max_chars(160))
        persona = await self.persona_prompt(umo)
        system = (persona + "\n\n" if persona else "") + _SUMMARY_RULES.format(
            max_chars=max_chars
        )
        facts = [f"标题：{title or '（无）'}"]
        if author:
            facts.append(f"作者：{author}")
        if analysis_summary:
            facts.append(f"内容摘要：{analysis_summary[:500]}")
        if keywords:
            facts.append("关键词：" + "、".join(keywords[:8]))
        if transcript:
            facts.append("听写：" + transcript[:600].replace("\n", " "))
        prompt = "\n".join(facts) + "\n\n写这条记录："
        text = self._clean(await self._chat(system, prompt, self._images(frames), umo))
        return text[: max_chars * 2]

    async def reply(
        self,
        *,
        their: str,
        title: str = "",
        analysis_summary: str = "",
        frames: list[str] | None = None,
        umo: str = "",
    ) -> str:
        """回一条别人 @ 我的评论。要接住对方那句话，不像客服。"""
        max_chars = self._max_chars(40)
        persona = await self.persona_prompt(umo)
        system = (persona + "\n\n" if persona else "") + _REPLY_RULES.format(
            max_chars=max_chars
        )
        facts = []
        if their:
            facts.append(f"对方说的：{their[:200]}")
        if title:
            facts.append(f"视频标题：{title[:120]}")
        if analysis_summary:
            facts.append(f"视频讲了什么：{analysis_summary[:300]}")
        prompt = "\n".join(facts) + "\n\n回他一句："
        for attempt in range(2):
            raw = await self._chat(
                system if attempt == 0 else system + "\n（上一条太客气了，像聊天那样再说一句）",
                prompt,
                self._images(frames),
                umo,
            )
            text = self._clean(raw)
            if not text:
                break
            why = self.check(text)
            if not why:
                return text[:120]
            logger.info("回评不合格（%s）：%s", why, text[:40])
        return ""


    async def chat(
        self,
        *,
        their: str,
        history: list[str] | None = None,
        umo: str = "",
    ) -> str:
        """私信里回一句。语气跟着人格走，不套客服腔。"""
        max_chars = self._max_chars(60)
        persona = await self.persona_prompt(umo)
        system = (persona + "\n\n" if persona else "") + _CHAT_RULES.format(
            max_chars=max_chars
        )
        parts = []
        if history:
            parts.append("刚才聊的：\n" + "\n".join(history[-6:]))
        parts.append(f"对方刚说：{their[:200]}")
        parts.append("回他：")
        prompt = "\n".join(parts)
        for attempt in range(2):
            raw = await self._chat(
                system if attempt == 0 else system + "\n（上一条太像客服了，用平时说话的方式重说）",
                prompt,
                None,
                umo,
            )
            text = self._clean(raw)
            if not text:
                break
            why = self.check(text)
            if not why:
                return text[:200]
            logger.info("聊天回复不合格（%s）：%s", why, text[:40])
        return ""
