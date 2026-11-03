"""后台任务队列。

为什么要这层：一次「刷一轮」= 开浏览器 + 取流 + 下载 + 抽帧 + 转写 + 视觉理解，
真实耗时 40s ~ 3min，期间如果让命令原地等，会话就卡住了，而且后台循环和手动命令会
同时抢同一个浏览器 profile（Playwright 的 persistent context 不允许双开）。

所以这里做两件事：
1. **串行化**：全局只有一个 worker，谁来都排队，队列里的任务按提交顺序跑；
2. **可观测**：每个任务带 stage（排队中 / 取流 / 下载 / 抽帧 / 转写 / 理解），
   `/dy status` 直接读它，用户随时知道「现在到底在干什么」。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger("astrbot")

STAGE_IDLE = "空闲"
STAGE_QUEUED = "排队中"


@dataclass
class Job:
    """一个后台任务。stage 由 runner 随时改写，队列只负责搬运。"""

    id: int
    kind: str                 # round = 刷一轮；login = 登录
    umo: str                  # 结果推到哪
    take: int = 1
    stage: str = STAGE_QUEUED
    created: float = field(default_factory=time.time)
    started: float = 0.0
    finished: float = 0.0
    ok: bool = False
    error: str = ""
    result: Any = None

    @property
    def elapsed(self) -> float:
        end = self.finished or time.time()
        return max(0.0, end - (self.started or self.created))

    def describe(self) -> str:
        tail = ""
        if self.error:
            tail = f"｜失败：{self.error[:80]}"
        return f"#{self.id} {self.kind}·{self.stage}（{self.elapsed:.0f}s）{tail}"


class JobQueue:
    """单 worker 的串行队列。

    runner: `async def (job: Job) -> Any`，抛异常即为失败；
    notifier: `async def (job: Job) -> None`，任务结束时调一次（成功/失败都调）。
    """

    def __init__(
        self,
        runner: Callable[[Job], Awaitable[Any]],
        notifier: Callable[[Job], Awaitable[None]] | None = None,
        *,
        max_pending: int = 8,
        history: int = 20,
    ) -> None:
        self._runner = runner
        self._notifier = notifier
        self._max_pending = max(1, max_pending)
        self._q: asyncio.Queue[Job] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._seq = 0
        self.current: Job | None = None
        self.history: deque[Job] = deque(maxlen=max(1, history))
        self.last_error = ""

    # ------------------------------------------------------------ 生命周期

    def _ensure_worker(self) -> None:
        if self._worker is not None and not self._worker.done():
            return
        try:
            self._worker = asyncio.create_task(self._run_forever())
        except RuntimeError:      # 当前没有事件循环（插件加载早期）
            self._worker = None

    async def stop(self) -> None:
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
            try:
                await self._worker
            except (asyncio.CancelledError, Exception):
                pass
        self._worker = None

    # ------------------------------------------------------------ 提交

    def pending(self) -> list[Job]:
        return list(self._q._queue)  # noqa: SLF001 - asyncio.Queue 没有只读接口

    def submit(self, kind: str, umo: str, take: int = 1) -> Job | None:
        """排队一个任务；队列积压超过上限时返回 None（让调用方告诉用户先等着）。"""
        if len(self.pending()) >= self._max_pending:
            return None
        self._seq += 1
        job = Job(id=self._seq, kind=kind, umo=umo, take=take)
        self._ensure_worker()
        self._q.put_nowait(job)
        return job

    def set_stage(self, job: Job, stage: str) -> None:
        job.stage = stage
        logger.debug("任务 %s 阶段 → %s", job.id, stage)

    # ------------------------------------------------------------ 执行

    async def _run_forever(self) -> None:
        while True:
            job = await self._q.get()
            self.current = job
            job.started = time.time()
            job.stage = "启动中"
            try:
                job.result = await self._runner(job)
                job.ok = not job.error
            except asyncio.CancelledError:
                job.error = job.error or "已取消"
                raise
            except Exception as exc:  # noqa: BLE001
                job.ok = False
                job.error = f"{type(exc).__name__}: {exc}"
                logger.warning("后台任务 #%s 失败：%s", job.id, job.error)
            finally:
                job.finished = time.time()
                job.stage = "完成" if job.ok else "失败"
                if job.error:
                    self.last_error = f"#{job.id} {job.error}"
                self.history.append(job)
                self.current = None
                self._q.task_done()
            if self._notifier is not None:
                try:
                    await self._notifier(job)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("后台任务 #%s 通知失败：%s", job.id, exc)

    # ------------------------------------------------------------ 状态

    def status_text(self) -> str:
        if self.current is None and not self.pending():
            line = "后台任务：空闲"
            if self.history:
                last = self.history[-1]
                line += f"｜上一个 {last.kind} {last.stage}（{last.elapsed:.0f}s）"
        elif self.current is not None:
            line = f"后台任务：{self.current.describe()}"
            wait = len(self.pending())
            if wait:
                line += f"｜另有 {wait} 个排队"
        else:
            line = f"后台任务：排队 {len(self.pending())} 个"
        if self.last_error:
            line += f"\n最近一次失败：{self.last_error[:120]}"
        return line