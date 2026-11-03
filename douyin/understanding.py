"""理解链路：ffmpeg 抽音轨 → STT 转写；抽 9 帧拼九宫格 → 视觉模型看画面；最后合成一段简报。

为什么是「9 帧 + 全片转写」而不是「分段章节」：
- 分段要多次调用视觉模型，成本和时延都翻几倍，收益主要在超长视频上；
- 9 帧按时间均匀分布，配合全片转写已经能覆盖「讲了什么 / 有没有干货」。
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api import logger

from . import asr, media
from .config import Settings
from .feed import AwemeItem

_JSON_BLOCK = re.compile(r"\{.*\}", re.S)

SYSTEM_PROMPT = (
    "你是短视频内容分析员。用户会给你：一条抖音视频的标题、话题、时长，"
    "以及该视频的语音转写文本和一张按时间顺序排列的 9 帧画面拼图。"
    "你的任务是用中文判断这条视频「讲了什么、有没有真东西、值不值得看」。"
    "只输出 JSON，不要输出任何解释、前后缀或 markdown 代码块。"
)


@dataclass
class Analysis:
    ok: bool = False
    transcript: str = ""
    visual: str = ""
    summary: str = ""
    keywords: list[str] = field(default_factory=list)
    verdict: str = ""
    montage: Path | None = None
    error: str = ""
    asr_backend: str = ""


class Understander:
    def __init__(self, context, settings: Settings, workdir: Path):
        self.context = context
        self.settings = settings
        self.workdir = workdir
        self.asr_backend = ""

    # ------------------------------------------------------------------ provider

    def _vision(self, umo: str):
        try:
            if self.settings.vision_provider:
                prov = self.context.get_provider_by_id(self.settings.vision_provider)
                if prov is not None:
                    return prov
                logger.warning("配置的视觉 provider 不存在：%s", self.settings.vision_provider)
            return self.context.get_using_provider(umo)
        except Exception as exc:
            logger.warning("取对话 provider 失败：%s", exc)
            return None

    # ------------------------------------------------------------------ 主流程

    async def analyze(self, video: Path, item: AwemeItem, umo: str) -> Analysis:
        result = Analysis()
        folder = self.workdir / item.aweme_id
        folder.mkdir(parents=True, exist_ok=True)

        duration = item.duration_s or await media.probe_duration(video, self.settings)
        if duration <= 0:
            result.error = "拿不到视频时长"
            return result

        # 1) 抽帧 → 九宫格
        montage = await self._make_montage(video, folder, duration)
        result.montage = montage

        # 2) 音轨 → 转写
        result.transcript = await self._transcribe(video, folder, umo)

        # 3) 视觉 + 汇总
        result.visual, result.summary, keywords, verdict = await self._describe(
            item, montage, result.transcript, umo
        )
        result.keywords = keywords
        result.verdict = verdict
        result.asr_backend = self.asr_backend
        result.ok = bool(result.summary or result.visual or result.transcript)
        if not result.ok and not result.error:
            result.error = "STT 与视觉都没产出内容（检查 provider 配置）"
        return result

    async def _make_montage(self, video: Path, folder: Path, duration: float) -> Path | None:
        count = max(1, self.settings.frames)
        step = duration / (count + 1)
        ffmpeg = media.find_ffmpeg(self.settings)
        frames: list[Path] = []
        labels: list[str] = []
        for idx in range(count):
            ts = step * (idx + 1)
            out = folder / f"f{idx:02d}.jpg"
            got = await media.extract_frame(
                video, ts, out, self.settings.frame_width, self.settings, ffmpeg
            )
            if got:
                frames.append(got)
                labels.append(f"{int(ts // 60):02d}:{int(ts % 60):02d}")
        if not frames:
            return None
        target = folder / "montage.png"
        made = await asyncio.to_thread(media.build_montage, frames, target, labels)
        if made:
            for path in frames:
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass
        return made

    async def _transcribe(self, video: Path, folder: Path, umo: str) -> str:
        if (self.settings.asr_backend or "").lower() == "off":
            return ""
        wav = folder / "audio.wav"
        got = await media.extract_audio(
            video, wav, self.settings, self.settings.asr_max_seconds
        )
        if not got:
            logger.warning("ffmpeg 抽音轨失败，跳过转写")
            return ""
        try:
            text, backend = await asr.transcribe(got, self.settings, self.context, umo)
            self.asr_backend = backend
            return text
        finally:
            try:
                wav.unlink(missing_ok=True)
            except Exception:
                pass

    async def _describe(
        self,
        item: AwemeItem,
        montage: Path | None,
        transcript: str,
        umo: str,
    ) -> tuple[str, str, list[str], str]:
        provider = self._vision(umo)
        if provider is None:
            return "", "", [], ""

        prompt = self._build_prompt(item, transcript)
        image_urls = [str(montage)] if montage else None
        try:
            resp = await provider.text_chat(
                prompt=prompt,
                system_prompt=SYSTEM_PROMPT,
                image_urls=image_urls,
            )
        except Exception as exc:
            logger.warning("视觉调用失败：%s: %s", type(exc).__name__, exc)
            return "", "", [], ""

        raw = getattr(resp, "completion_text", "") or ""
        data = self._parse_json(raw)
        if not data:
            return "", raw.strip()[:400], [], ""
        visual = str(data.get("visual") or "").strip()
        summary = str(data.get("summary") or "").strip()
        verdict = str(data.get("verdict") or "").strip()
        keywords = data.get("keywords") or []
        if not isinstance(keywords, list):
            keywords = [str(keywords)]
        return visual, summary, [str(k).strip() for k in keywords if str(k).strip()][:8], verdict

    def _build_prompt(self, item: AwemeItem, transcript: str) -> str:
        head = (
            f"标题：{item.desc or item.title}\n"
            f"作者：{item.author or '未知'}\n"
            f"话题：{'、'.join(item.tags) or '无'}\n"
            f"时长：{int(item.duration_s)} 秒｜{item.orientation}｜{item.width}x{item.height}\n"
            f"点赞：{item.digg}｜评论：{item.comment_count}\n"
        )
        body = transcript[:6000] if transcript else "（未获得语音转写）"
        tail = (
            "\n\n按要求输出 JSON，字段固定为：\n"
            '{"summary": "120 字以内说清这条视频讲了什么", '
            '"keywords": ["3-6 个知识点/主题词"], '
            '"verdict": "值得看 或 一般 或 跳过", '
            '"visual": "80 字以内描述画面构成与呈现方式"}\n'
            "判断标准：有可迁移的知识、方法、数据、观点 → 值得看；"
            "只有情绪和娱乐 → 一般；纯标题党/广告/擦边 → 跳过。"
        )
        return head + "\n【语音转写】\n" + body + tail

    @staticmethod
    def _parse_json(raw: str) -> dict:
        if not raw:
            return {}
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
        match = _JSON_BLOCK.search(text)
        candidate = match.group(0) if match else text
        try:
            data = json.loads(candidate)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
