"""一轮完整流程：取流 → 归一化 → 去重 → 打分 → 下载 → 理解 → 落库 → 出简报。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api import logger

from . import scoring
from .browser import DouyinBlocked, DouyinBrowser, HumanVerifyNeeded
from .config import Settings
from .feed import AwemeItem, parse_aweme
from .store import Store
from .understanding import Analysis, Understander


@dataclass
class RoundResult:
    text: str = ""
    montage: Path | None = None
    analyzed: list[tuple[AwemeItem, Analysis]] = field(default_factory=list)
    candidates: int = 0
    error: str = ""


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        browser: DouyinBrowser,
        understander: Understander,
        media_dir: Path,
    ):
        self.settings = settings
        self.store = store
        self.browser = browser
        self.understander = understander
        self.media_dir = media_dir

    async def run_once(self, umo: str, want: int | None = None, take: int = 1) -> RoundResult:
        result = RoundResult()
        self._stage("取流")
        try:
            raw = await self.browser.collect(want)
        except HumanVerifyNeeded:
            raise                      # 交给上层去搬窗口、叫人、等滑块
        except DouyinBlocked as exc:
            result.error = str(exc)
            await self.store.log("collect_blocked", str(exc))
            return result
        except Exception as exc:
            result.error = f"取流异常：{type(exc).__name__}: {exc}"
            await self.store.log("collect_error", result.error)
            return result

        self._stage("解析与筛选")
        items = [it for it in (parse_aweme(r) for r in raw) if it]
        if not items:
            result.error = "解析后没有可用条目"
            return result

        seen_ids = await self.store.seen([it.aweme_id for it in items])
        fresh = [it for it in items if it.aweme_id not in seen_ids]
        ranked = scoring.rank(fresh, self.settings)
        result.candidates = len(ranked)
        if not ranked:
            result.error = (
                f"这轮 {len(items)} 条全被筛掉或已看过（已看过 {len(seen_ids)} 条）；"
                "可以调低 min_like / 放宽时长，或者等推荐流换一批"
            )
            await self.store.log("no_candidate", result.error)
            return result

        await self.store.log(
            "round",
            f"抓到 {len(items)} 条，未看过 {len(fresh)} 条，过筛 {len(ranked)} 条",
        )

        done = 0
        for rank, (score, item) in enumerate(ranked):
            if done >= take:
                break
            try:
                self._stage(f"处理第 {done + 1}/{take} 条")
                analysis = await self._handle_one(item, score, umo)
            except DouyinBlocked as exc:
                await self.store.mark(item.aweme_id, "failed", error=str(exc))
                await self.store.log("download_failed", f"{item.aweme_id}: {exc}")
                continue
            except Exception as exc:
                await self.store.mark(item.aweme_id, "failed", error=f"{type(exc).__name__}: {exc}")
                await self.store.log("analyze_failed", f"{item.aweme_id}: {exc}")
                logger.warning("处理 %s 失败：%s", item.aweme_id, exc)
                continue
            if analysis is None:
                continue
            result.analyzed.append((item, analysis))
            if result.montage is None and analysis.montage:
                result.montage = analysis.montage
            done += 1

        if not result.analyzed:
            result.error = "候选都处理失败了，看日志里的 download_failed / analyze_failed"
            return result

        result.text = self._render(result.analyzed)
        return result

    def _stage(self, text: str) -> None:
        """把当前阶段报给上层（后台队列会显示在 /dy status 里）。"""
        cb = getattr(self, "on_stage", None)
        if cb is None:
            return
        try:
            cb(text)
        except Exception:
            pass

    async def _handle_one(self, item: AwemeItem, score: float, umo: str) -> Analysis | None:
        await self.store.upsert(item, status="picked", score=score)
        suffix = ".mp4"
        dest = self.media_dir / f"{item.aweme_id}{suffix}"
        self._stage("下载视频")
        size = await self.browser.download(item.play_url, dest, self.settings.max_download_bytes)
        await self.store.mark(item.aweme_id, "downloaded", score=score)
        logger.info("已下载 %s（%.1f MB）", item.aweme_id, size / 1024 / 1024)

        self._stage("抽帧/转写/视觉理解")
        analysis = await self.understander.analyze(dest, item, umo)
        await self.store.mark(
            item.aweme_id,
            "analyzed" if analysis.ok else "failed",
            transcript=analysis.transcript[:8000],
            visual=analysis.visual,
            summary=analysis.summary,
            keywords="、".join(analysis.keywords),
            verdict=analysis.verdict,
            error=analysis.error,
        )
        if not self.settings.keep_media:
            try:
                dest.unlink(missing_ok=True)
            except Exception:
                pass
        return analysis if analysis.ok else None

    def _render(self, pairs: list[tuple[AwemeItem, Analysis]]) -> str:
        blocks: list[str] = []
        for item, analysis in pairs:
            lines = [
                "🎬 抖音刷到一条",
                f"{item.title}",
                f"@{item.author or '未知'}｜{int(item.duration_s)}s｜{item.orientation}"
                f"｜赞 {item.digg}｜评 {item.comment_count}",
            ]
            if analysis.summary:
                lines.append(f"\n讲了什么：{analysis.summary}")
            if analysis.keywords:
                lines.append(f"知识点：{' / '.join(analysis.keywords)}")
            if analysis.verdict:
                lines.append(f"结论：{analysis.verdict}")
            if analysis.visual:
                lines.append(f"画面：{analysis.visual}")
            if analysis.transcript:
                snippet = analysis.transcript.replace("\n", " ")[:180]
                lines.append(f"转写摘录：{snippet}…")
            lines.append(f"原片：{item.share_url}")
            blocks.append("\n".join(lines))
        return "\n\n———\n\n".join(blocks)
