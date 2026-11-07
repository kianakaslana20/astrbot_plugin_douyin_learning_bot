"""私信 HTTP 只读通道 —— 不碰 DOM、不开面板，直接问 imapi.douyin.com。

为什么要它：
    现在读私信只有一条路：打开私信面板、点会话、从 DOM 里抠文字。冷启十几秒、
    切会话一秒多，而且 DOM 里没有真实时间戳，谁发的要靠 isFromMe 容器推。
    抖音网页版自己的 IM 走 HTTP + protobuf，只认 Cookie，不需要 a_bogus：
    一次 `cmd=2043` 就能拿到**全部已建立会话**，每个会话带最近若干条消息，
    每条都有**毫秒级 create_time** 和**发送者数字 uid**。

红线：
    · **只读**。这个文件里没有任何发送代码，发私信仍然走 `im.send_dm` 的界面路径。
    · Cookie 只在内存里拼请求头，不落盘、不进日志。
    · 拿不到就如实报错（`ImApiError`），让调用方退回 DOM 路径，绝不编数据。

协议要点（对照页面真实请求逐字节比出来的）：
    请求 body 是信封，字段 1=cmd、2=seq、3="0.1.8"、7=构建标识、8=内层 body、
    15=重复 kv（session_aid/app_name/…）、21="douyin_web"、22="web_sdk"。
    响应 `env[4]` 是状态字符串（必须 "OK"），`env[6]` 是外层，
    再往里 `wrap[cmd]` → `sub[1]` 才是会话列表。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

try:  # 单元测试里没有 astrbot 包，退成标准 logging，行为一致
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

WWW = "https://www.douyin.com"
IM_HOST = "https://imapi.douyin.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
COMMON = "device_platform=webapp&aid=6383&channel=channel_pc_web&pc_client_type=1"

CMD_INIT = 2043       # 拉全部已建立会话 + 每会话最近消息


class ImApiError(RuntimeError):
    """这一通道不可用（没登录 / 没装 curl_cffi / 接口拒了）。调用方该退回 DOM。"""


# --------------------------------------------------------------- protobuf 最小实现
# 只实现 varint 和 length-delimited 两种 wire type —— IM 接口的信封只用到它们。
# Python 的 int 是任意精度，19 位的 short_id 和毫秒时间戳都不会被截断。


def _varint(n: int) -> bytes:
    out = bytearray()
    v = int(n)
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _enc_v(field: int, n: int) -> bytes:
    return _varint((field << 3) | 0) + _varint(n)


def _enc_ld(field: int, data: bytes | str) -> bytes:
    raw = data if isinstance(data, bytes) else str(data).encode()
    return _varint((field << 3) | 2) + _varint(len(raw)) + raw


def _enc_empty(field: int) -> bytes:
    return _varint((field << 3) | 2) + _varint(0)


def _parse(data: bytes, *, max_fields: int = 4096) -> dict[int, list[Any]]:
    """宽容解析：坏字节就停，不抛 —— 响应里允许有我们还不认识的字段。"""
    fields: dict[int, list[Any]] = {}
    i, n, count = 0, len(data), 0
    while i < n and count < max_fields:
        try:
            tag, i = _read_varint(data, i)
        except IndexError:
            break
        field, wire = tag >> 3, tag & 7
        if not field:
            break
        try:
            if wire == 0:
                value, i = _read_varint(data, i)
            elif wire == 1:
                value, i = data[i : i + 8], i + 8
            elif wire == 2:
                length, i = _read_varint(data, i)
                if length < 0 or i + length > n:
                    break
                value, i = data[i : i + length], i + length
            elif wire == 5:
                value, i = data[i : i + 4], i + 4
            else:
                break
        except IndexError:
            break
        fields.setdefault(field, []).append(value)
        count += 1
    return fields


def _read_varint(data: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        b = data[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7
        if shift > 70:            # 非法 varint，当坏字节处理
            raise IndexError


def _one(fields: dict[int, list[Any]], key: int, default: Any = None) -> Any:
    values = fields.get(key)
    return values[0] if values else default


def _text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    return "" if value is None else str(value)


_seq = [10000 + int(time.time() * 1000) % 89]


def _envelope(cmd: int, body: bytes) -> bytes:
    _seq[0] += 1
    parts = [
        _enc_v(1, cmd),
        _enc_v(2, _seq[0]),
        _enc_ld(3, "0.1.8"),
        _enc_empty(4),
        _enc_v(5, 3),
        _enc_v(6, 1),
        _enc_ld(7, "0d50935:feat/pc-im-group"),
        _enc_ld(8, body),
        _enc_ld(9, "0"),
        _enc_ld(11, "douyin_pc"),
        _enc_ld(14, "360000"),
    ]
    for key, value in (
        ("session_aid", "6383"),
        ("session_did", "0"),
        ("app_name", "douyin_pc"),
        ("priority_region", "cn"),
        ("user_agent", UA),
        ("cookie_enabled", "true"),
        ("browser_language", "zh-CN"),
        ("browser_platform", "Win32"),
        ("browser_name", "Mozilla"),
        ("browser_version", UA.split("Mozilla/")[-1]),
        ("browser_online", "true"),
        ("screen_width", "1920"),
        ("screen_height", "1080"),
        ("timezone_name", "Asia/Shanghai"),
        ("deviceId", "0"),
        ("is-retry", "0"),
    ):
        parts.append(_enc_ld(15, _enc_ld(1, key) + _enc_ld(2, value)))
    parts += [_enc_v(18, 1), _enc_ld(21, "douyin_web"), _enc_ld(22, "web_sdk")]
    return b"".join(parts)


# --------------------------------------------------------------- 消息正文整理


def message_text(content_raw: str) -> dict:
    """把 content JSON 压成「有没有字、什么类型」。分享卡也在这里认出来。"""
    text, kind = "", "text"
    try:
        data = json.loads(content_raw or "{}")
    except Exception:
        return {"text": (content_raw or "")[:200], "kind": "raw"}
    if not isinstance(data, dict):
        return {"text": (content_raw or "")[:200], "kind": "raw"}
    text = str(data.get("text") or data.get("content_title") or "").strip()
    awe_type = int(data.get("aweType") or 0)
    if awe_type == 800:
        kind = "share"
    if not text and kind == "text":
        kind = "unsupported"
    return {"text": text[:400], "kind": kind}


def peer_uid(conv_id: str, my_uid: str) -> str:
    """会话 id 形如 `0:1:<uidA>:<uidB>`，去掉自己那个就是对方。"""
    uids = [p for p in str(conv_id or "").split(":") if p.isdigit() and len(p) >= 8]
    return next((u for u in uids if u != my_uid), uids[-1] if uids else "")


class ImApi:
    """私信 HTTP 只读客户端。cookie 从现有浏览器上下文里取（进程内，不落盘）。"""

    def __init__(self, browser, *, timeout: float = 12.0, limit: int = 20):
        self.browser = browser
        self.timeout = timeout
        self.limit = max(1, min(int(limit or 20), 50))

    # ---------------------------------------------------------------- cookie
    async def _cookie_header(self) -> str:
        try:
            raw = await self.browser.raw_cookies()
        except Exception as exc:  # noqa: BLE001
            raise ImApiError(f"取 cookie 失败：{type(exc).__name__}") from exc
        pairs = [
            f"{c.get('name')}={c.get('value')}"
            for c in raw or []
            if c.get("name") and "douyin.com" in str(c.get("domain") or "douyin.com")
        ]
        cookie = "; ".join(pairs)
        if "sessionid" not in cookie:
            raise ImApiError("cookie 里没有 sessionid（浏览器那边没登录）")
        return cookie

    # ---------------------------------------------------------------- 请求
    def _session(self, cookie: str):
        try:
            from curl_cffi import requests as creq
        except ImportError as exc:  # pragma: no cover - 依赖没装
            raise ImApiError("没装 curl_cffi，HTTP 通道不可用") from exc
        session = creq.Session(impersonate="chrome")
        session.headers.update(
            {
                "User-Agent": UA,
                "Cookie": cookie,
                "Referer": WWW + "/",
                "Origin": WWW,
            }
        )
        return session

    def _blocking_my_uid(self, cookie: str) -> str:
        with self._session(cookie) as session:
            resp = session.get(
                WWW + "/aweme/v1/web/user/profile/self/?" + COMMON, timeout=self.timeout
            )
            data = resp.json() or {}
        uid = str((data.get("user") or {}).get("uid") or "")
        if not uid:
            raise ImApiError(
                f"登录态查不出来（status_code={data.get('status_code')}）—— 可能掉登录了"
            )
        return uid

    def _blocking_conversations(self, cookie: str, my_uid: str) -> list[dict]:
        with self._session(cookie) as session:
            resp = session.post(
                IM_HOST + "/v1/message/get_message_by_init",
                data=_envelope(CMD_INIT, _enc_ld(CMD_INIT, b"")),
                headers={
                    "Content-Type": "application/x-protobuf",
                    "Accept": "application/x-protobuf",
                },
                timeout=self.timeout,
            )
        if resp.status_code != 200:
            raise ImApiError(f"IM 接口 HTTP {resp.status_code}")
        env = _parse(resp.content)
        status = _text(_one(env, 4))
        if status != "OK":
            # 例如 "too many requests"：如实报，不重试、不凑数据
            raise ImApiError(f"IM 接口返回 {status or '(空)'}")
        wrap = _parse(_one(env, 6) or b"")
        inner = _parse(_one(wrap, CMD_INIT) or b"")
        out: list[dict] = []
        for blob in inner.get(1) or []:
            if not isinstance(blob, (bytes, bytearray)):
                continue
            conv = _parse(bytes(blob))
            core = _parse(_one(conv, 1) or b"")
            conv_id = _text(_one(core, 1))
            messages = []
            for raw in conv.get(2) or []:
                if not isinstance(raw, (bytes, bytearray)):
                    continue
                field = _parse(bytes(raw))
                sender = _text(_one(field, 7))
                body = message_text(_text(_one(field, 8)))
                at_ms = int(_one(field, 10) or 0)
                if not body.get("text") and body.get("kind") == "unsupported":
                    continue
                messages.append(
                    {
                        "from_uid": sender,
                        "mine": sender == my_uid,
                        "create_time": at_ms,
                        "text": body.get("text", ""),
                        "kind": body.get("kind", "text"),
                        "server_message_id": _text(_one(field, 3)),
                    }
                )
            messages.sort(key=lambda m: m.get("create_time") or 0)
            out.append(
                {
                    "conv_id": conv_id,
                    "short_id": _text(_one(core, 2)),
                    "type": int(_one(core, 3) or 0),      # 1=私聊 2=群聊
                    "peer_uid": peer_uid(conv_id, my_uid),
                    "messages": messages[-self.limit :],
                }
            )
        return out

    async def my_uid(self) -> str:
        cookie = await self._cookie_header()
        return await asyncio.to_thread(self._blocking_my_uid, cookie)

    async def conversations(self, limit: int | None = None) -> list[dict]:
        """全部已建立会话（含每个会话最近 limit 条）。失败抛 ImApiError。"""
        if limit:
            self.limit = max(1, min(int(limit), 50))
        cookie = await self._cookie_header()
        my_uid = await asyncio.to_thread(self._blocking_my_uid, cookie)
        convs = await asyncio.to_thread(self._blocking_conversations, cookie, my_uid)
        for conv in convs:
            conv["mine_uid"] = my_uid
            for message in conv.get("messages") or []:
                message.setdefault("conv_id", conv.get("conv_id", ""))
        return convs

    async def probe(self) -> dict:
        """M0 探针：只回答「通不通、有多少会话、时间戳齐不齐」，不改任何行为。"""
        started = time.time()
        try:
            cookie = await self._cookie_header()
            my_uid = await asyncio.to_thread(self._blocking_my_uid, cookie)
            convs = await asyncio.to_thread(self._blocking_conversations, cookie, my_uid)
            for conv in convs:
                for message in conv.get("messages") or []:
                    message.setdefault("conv_id", conv.get("conv_id", ""))
        except ImApiError as exc:
            return {"ok": False, "error": str(exc), "ms": int((time.time() - started) * 1000)}
        except Exception as exc:  # noqa: BLE001
            logger.warning("私信 HTTP 探针异常：%s", type(exc).__name__)
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "ms": int((time.time() - started) * 1000),
            }
        msgs = [m for c in convs for m in c.get("messages") or []]
        with_ts = [m for m in msgs if m.get("create_time")]
        sample = None
        if msgs:
            newest = max(msgs, key=lambda m: m.get("create_time") or 0)
            sample = {
                "conv_id": newest.get("conv_id", ""),
                "from_uid": newest.get("from_uid"),
                "mine": newest.get("mine"),
                "create_time": newest.get("create_time"),
                "text": newest.get("text", "")[:60],
            }
        return {
            "ok": True,
            "ms": int((time.time() - started) * 1000),
            "my_uid": my_uid,
            "conversations": len(convs),
            "private": len([c for c in convs if c.get("type") == 1]),
            "messages": len(msgs),
            "messages_with_timestamp": len(with_ts),
            "timestamps_all_present": len(with_ts) == len(msgs) and bool(msgs),
            "sample": sample,
            "per_conversation": [
                {
                    "conv_id": c["conv_id"],
                    "type": c.get("type"),
                    "peer_uid": c.get("peer_uid"),
                    "messages": len(c.get("messages") or []),
                    "last_time": max(
                        [m.get("create_time") or 0 for m in (c.get("messages") or [])] or [0]
                    ),
                }
                for c in convs[:10]
            ],
        }