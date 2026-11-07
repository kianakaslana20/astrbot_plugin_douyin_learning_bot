"""语音转写：三条后端，按「AstrBot 里配好的 STT → 自填 OpenAI 兼容接口 → 本地 faster-whisper」的顺序挑。

为什么要自带后端而不是只用 AstrBot 的 STT：
实测这套 AstrBot 4.29.3 的 provider 列表里只有 chat / embedding / TTS，一条 STT 都没配，
而 AstrBot 支持的 STT 实现（whisper_api / whisper_selfhosted / sensevoice_selfhosted /
xinference_stt / mimo_stt）都需要用户自己再加一个。插件不能因为「机器上没配 STT」就整条
理解链路瘫掉，所以：

- ``astrbot``：用 context.get_using_stt_provider()，零配置，前提是用户配过；
- ``openai``：直接打 ``{base}/audio/transcriptions``，硅基流动 SenseVoice、OpenAI Whisper、
  各类 one-api 中转都通；
- ``chat``：把音频 base64 塞进 chat/completions 的 input_audio，千问系
  ``qwen3-asr-flash`` 这类模型直接回文字（实测 2026-11-08 在 maas 中转上可用）；
- ``local``：装了 faster-whisper 就在本地转，不花 API 额度；
- 都没有：不转写，只靠 9 帧画面理解，并在简报里注明「未获得语音转写」。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from astrbot.api import logger

from .config import Settings

_TIMEOUT = httpx.Timeout(180.0, connect=20.0)


class ASRError(RuntimeError):
    pass


async def _openai_transcribe(wav: Path, settings: Settings) -> str:
    """POST {base}/audio/transcriptions（multipart）。"""
    base = (settings.asr_api_base or "").rstrip("/")
    if not base:
        raise ASRError("未填 asr_api_base")
    if not base.endswith("/audio/transcriptions"):
        base = f"{base}/audio/transcriptions"
    headers = {}
    if settings.asr_api_key:
        headers["Authorization"] = f"Bearer {settings.asr_api_key}"

    data: dict[str, Any] = {"model": settings.asr_model or "whisper-1"}
    if settings.asr_language:
        data["language"] = settings.asr_language

    files = {"file": (wav.name, wav.read_bytes(), "audio/wav")}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(base, headers=headers, data=data, files=files)
    if resp.status_code != 200:
        raise ASRError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    if isinstance(body, dict):
        text = body.get("text") or body.get("transcription") or ""
        if not text and isinstance(body.get("segments"), list):
            text = "".join(str(seg.get("text") or "") for seg in body["segments"])
        return str(text).strip()
    return ""


async def _chat_audio_transcribe(wav: Path, settings: Settings) -> str:
    """POST {base}/chat/completions，把整段音频当 input_audio 传进去。

    为什么需要这条：中转站（maas / DashScope 兼容模式）不暴露
    ``/audio/transcriptions``（一律 404），但 ``qwen3-asr-flash`` 走 chat 接口就能转写，
    返回的是 ``choices[0].message.content``。文件大小受 base64 膨胀 4/3 影响，
    所以调用方要先按 ``asr_max_seconds`` 把音频截短。
    """
    import base64
    import json

    base = (settings.asr_api_base or "").rstrip("/")
    if not base:
        raise ASRError("未填 asr_api_base")
    if not base.endswith("/chat/completions"):
        base = f"{base}/chat/completions"
    raw = wav.read_bytes()
    if len(raw) > 24 * 1024 * 1024:
        raise ASRError(f"音频太大（{len(raw) // 1024} KB），先截短")
    payload = {
        "model": settings.asr_model or "qwen3-asr-flash",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": "data:audio/wav;base64,"
                            + base64.b64encode(raw).decode(),
                            "format": "wav",
                        },
                    }
                ],
            }
        ],
        "stream": False,
    }
    headers = {"Content-Type": "application/json"}
    if settings.asr_api_key:
        headers["Authorization"] = f"Bearer {settings.asr_api_key}"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(base, headers=headers, content=json.dumps(payload))
    if resp.status_code != 200:
        raise ASRError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    try:
        msg = (body.get("choices") or [{}])[0].get("message") or {}
    except Exception:  # noqa: BLE001
        msg = {}
    text = msg.get("content")
    if isinstance(text, list):
        text = "".join(str(part.get("text") or "") for part in text if isinstance(part, dict))
    return str(text or "").strip()


async def _astrbot_transcribe(wav: Path, settings: Settings, context, umo: str) -> str:
    provider = None
    try:
        if settings.stt_provider:
            provider = context.get_provider_by_id(settings.stt_provider)
        if provider is None:
            provider = context.get_using_stt_provider(umo)
    except Exception as exc:
        logger.warning("取 STT provider 失败：%s: %s", type(exc).__name__, exc)
        return ""
    if provider is None:
        return ""
    text = await provider.get_text(str(wav))
    return (text or "").strip()


async def _local_transcribe(wav: Path, settings: Settings) -> str:
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except Exception:
        return ""

    def run() -> str:
        model = WhisperModel(settings.asr_model or "small", device="cpu", compute_type="int8")
        segments, _info = model.transcribe(str(wav), language=settings.asr_language or None)
        return "".join(seg.text for seg in segments).strip()

    try:
        return await asyncio.to_thread(run)
    except Exception as exc:
        logger.warning("本地 faster-whisper 转写失败：%s: %s", type(exc).__name__, exc)
        return ""


async def transcribe(wav: Path, settings: Settings, context, umo: str) -> tuple[str, str]:
    """返回 (转写文本, 实际使用的后端)。失败一律返回空文本，不抛异常。"""
    backend = (settings.asr_backend or "auto").lower()

    order: list[str]
    if backend == "astrbot":
        order = ["astrbot"]
    elif backend == "openai":
        order = ["openai"]
    elif backend == "chat":
        order = ["chat"]
    elif backend == "local":
        order = ["local"]
    elif backend == "off":
        order = []
    else:  # auto
        order = ["astrbot", "openai", "chat", "local"]

    for name in order:
        try:
            if name == "astrbot":
                text = await _astrbot_transcribe(wav, settings, context, umo)
            elif name == "openai":
                if not (settings.asr_api_base and settings.asr_api_key):
                    continue
                text = await _openai_transcribe(wav, settings)
            elif name == "chat":
                if not (settings.asr_api_base and settings.asr_api_key):
                    continue
                text = await _chat_audio_transcribe(wav, settings)
            else:
                text = await _local_transcribe(wav, settings)
        except ASRError as exc:
            logger.warning("ASR[%s] 失败：%s", name, exc)
            continue
        except Exception as exc:
            logger.warning("ASR[%s] 异常：%s: %s", name, type(exc).__name__, exc)
            continue
        if text:
            logger.info("ASR 使用后端 %s，转写 %d 字", name, len(text))
            return text, name
    logger.info("没有可用的 ASR 通道，跳过语音转写")
    return "", ""
