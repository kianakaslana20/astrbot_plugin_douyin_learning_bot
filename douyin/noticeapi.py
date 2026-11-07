"""互动通知 HTTP 只读通道 —— @ / 评论 / 回复 / 点赞 / 关注。

为什么要它：
    `notice.py` 现在是「打开通知面板 → 点开每条 → 从 DOM 抠链接」。慢，而且
    **拿不到 comment_id**，所以「回复某条评论」一直只能退化成「在视频下发一条 @」。
    这个接口直接返回 `comment_id` / 父评论 id / 楼层信息，正好补上那个缺口。

关键（别人踩过的坑，这里照抄结论、不抄代码）：
    · 列表在响应的 **notice_list_v2** 里，不是 notice_list
    · `group` 与 `notice_group` 必须**同时**给，缺一个 status_code=5
    · `min_time=0` 才会把历史拉出来
    · 网页端没有通知中心页面，只能走这个接口

红线：**只读**。`is_mark_read=0` 不把消息标成已读；这里没有任何写操作。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

try:  # 单元测试里没有 astrbot 包，退成标准 logging，行为一致
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

BASE = "https://www.douyin.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
COMMON = (
    "device_platform=webapp&aid=6383&channel=channel_pc_web&pc_client_type=1"
    "&version_code=190500&version_name=19.5.0&cookie_enabled=true"
    "&screen_width=1920&screen_height=1080&browser_language=zh-CN"
    "&browser_platform=Win32&browser_name=Chrome&browser_version=141.0.0.0"
    "&engine_name=Blink&engine_version=141.0.0.0&os_name=Windows&os_version=10"
    "&platform=PC"
)
GROUPS = (700, 960, 401, 601)
TYPE_NAME = {
    31: "回复了评论",
    32: "回复了你的评论",
    33: "关注了你",
    34: "点赞了你的作品",
    35: "评论了你的作品",
    41: "点赞了你的评论",
    45: "@了你（评论区）",
    46: "@了你（作品）",
}
# 这几类才需要「回一句」；点赞/关注只做记录
REPLY_TYPES = {31, 32, 35, 45, 46}


class NoticeApiError(RuntimeError):
    """通道不可用（没登录 / 没装 curl_cffi / 接口拒了）。调用方该退回 DOM。"""


class NoticeApi:
    def __init__(self, browser, *, timeout: float = 20.0):
        self.browser = browser
        self.timeout = timeout

    async def _cookie_header(self) -> str:
        try:
            raw = await self.browser.raw_cookies()
        except Exception as exc:  # noqa: BLE001
            raise NoticeApiError(f"取 cookie 失败：{type(exc).__name__}") from exc
        pairs = [
            f"{c.get('name')}={c.get('value')}"
            for c in raw or []
            if c.get("name") and "douyin.com" in str(c.get("domain") or "douyin.com")
        ]
        cookie = "; ".join(pairs)
        if "sessionid" not in cookie:
            raise NoticeApiError("cookie 里没有 sessionid（浏览器那边没登录）")
        return cookie

    def _session(self, cookie: str):
        try:
            from curl_cffi import requests as creq
        except ImportError as exc:  # pragma: no cover
            raise NoticeApiError("没装 curl_cffi，HTTP 通道不可用") from exc
        session = creq.Session(impersonate="chrome")
        session.headers.update({"User-Agent": UA, "Cookie": cookie, "Referer": BASE + "/"})
        return session

    # ------------------------------------------------------------------ 解析
    @staticmethod
    def _work_url(blk: dict, aweme: dict, aweme_id: str) -> str:
        for source in (blk, aweme):
            for key in ("work_url", "web_url", "share_url"):
                value = source.get(key)
                if not isinstance(value, str):
                    continue
                parsed = urlparse(value)
                if parsed.hostname in ("www.douyin.com", "douyin.com"):
                    return value
        if not aweme_id:
            return ""
        route = "note" if int(aweme.get("aweme_type") or 0) in (68, 150) else "video"
        return f"https://www.douyin.com/{route}/{aweme_id}"

    @staticmethod
    def _d(value) -> dict:
        """服务端偶尔把 user / aweme 给成字符串或 None，统一收成 dict 免得 .get 炸。"""
        return value if isinstance(value, dict) else {}

    @classmethod
    def normalize(cls, item: dict, group: int) -> dict:
        """一条通知压成我们要的形状：谁、说了什么、在哪个作品、哪条评论。"""
        kind = item.get("type")
        rec: dict[str, Any] = {
            "type": kind,
            "type_name": TYPE_NAME.get(kind, f"type_{kind}"),
            "group": group,
            "nid": str(item.get("nid") or ""),
            "create_time": item.get("create_time") or 0,
            "from_nick": "",
            "from_uid": "",
            "content": "",
            "aweme_id": "",
            "comment_id": "",
            "root_comment_id": "",
            "url": "",
        }
        for key in ("at", "comment", "digg", "follow", "fans", "im", "system"):
            blk = item.get(key)
            if not isinstance(blk, dict):
                continue
            user = cls._d(blk.get("user_info")) or cls._d(blk.get("from_user"))
            inner = cls._d(blk.get("comment"))
            if inner:
                inner_user = cls._d(inner.get("user"))
                if inner_user.get("nickname"):
                    user = inner_user
                if inner.get("cid"):
                    rec["comment_id"] = str(inner["cid"])
                if inner.get("text"):
                    rec["content"] = str(inner["text"]).strip()
            rec["from_nick"] = user.get("nickname") or ""
            rec["from_uid"] = str(user.get("uid") or "")
            rec["content"] = rec["content"] or str(blk.get("content") or "").strip()
            schema = str(blk.get("schema_url") or "")
            query = parse_qs(urlparse(schema).query)
            for name in ("aweme_id", "item_id"):
                value = (query.get(name) or [None])[0]
                if value and str(value).isdigit():
                    rec["aweme_id"] = str(value)
                    break
            if "aweme/detail/" in schema:
                rec["aweme_id"] = schema.split("aweme/detail/")[1].split("?")[0].split("&")[0]
                if "cid=" in schema:
                    rec["comment_id"] = rec["comment_id"] or schema.split("cid=")[1].split("&")[0]
            if blk.get("parent_id"):
                rec["root_comment_id"] = str(blk["parent_id"])
            level1 = cls._d(blk.get("level1_comment"))
            if level1:
                if level1.get("cid"):
                    rec["root_comment_id"] = rec["root_comment_id"] or str(level1["cid"])
                rec["root_author"] = cls._d(level1.get("user")).get("nickname") or ""
                rec["root_text"] = str(level1.get("text") or "")[:80]
            reply = cls._d(blk.get("reply_comment"))
            if reply:
                rec["reply_to_nick"] = cls._d(reply.get("user")).get("nickname") or ""
                rec["reply_to_text"] = str(reply.get("text") or "")[:80]
                if reply.get("cid"):
                    rec["reply_to_comment_id"] = str(reply["cid"])
            aweme = cls._d(blk.get("aweme"))
            if aweme.get("aweme_id"):
                rec["aweme_id"] = rec["aweme_id"] or str(aweme["aweme_id"])
                rec["aweme_desc"] = str(aweme.get("desc") or "")[:80]
            if blk.get("aweme_id"):
                rec["aweme_id"] = rec["aweme_id"] or str(blk["aweme_id"])
            rec["url"] = cls._work_url(blk, aweme, rec["aweme_id"])
            break
        rec["needs_reply"] = kind in REPLY_TYPES
        rec["has_cid"] = bool(rec["comment_id"])
        return rec

    # ------------------------------------------------------------------ 取数
    def _blocking_items(self, cookie: str, groups: tuple[int, ...]) -> dict:
        out: dict[str, Any] = {"counts": {}, "items": [], "warnings": []}
        with self._session(cookie) as session:
            try:
                resp = session.get(
                    BASE + "/aweme/v1/web/notice/count/?" + COMMON + "&is_new_notice=1&need_social_count=1",
                    timeout=self.timeout,
                )
                for c in (resp.json() or {}).get("notice_count") or []:
                    out["counts"][str(c.get("group"))] = c.get("count")
            except Exception as exc:  # noqa: BLE001
                out["warnings"].append(f"count 失败：{type(exc).__name__}")
            seen: set[str] = set()
            for group in groups:
                query = (
                    f"is_new_notice=1&is_mark_read=0&count=20&min_time=0"
                    f"&group={group}&notice_group={group}"
                )
                try:
                    data = session.get(BASE + "/aweme/v1/web/notice/?" + COMMON + "&" + query,
                                       timeout=self.timeout).json() or {}
                except Exception as exc:  # noqa: BLE001
                    out["warnings"].append(f"group={group} {type(exc).__name__}")
                    continue
                if data.get("status_code") not in (0, None):
                    out["warnings"].append(f"group={group} status_code={data.get('status_code')}")
                for item in data.get("notice_list_v2") or []:
                    nid = str(item.get("nid") or "")
                    if nid and nid in seen:
                        continue
                    seen.add(nid)
                    try:
                        out["items"].append(self.normalize(item, group))
                    except Exception as exc:  # noqa: BLE001
                        out["warnings"].append(f"normalize {type(exc).__name__}: {exc}")
                time.sleep(1.2)      # 别把接口问急了（它会回 status_code=5 / 空）
        out["items"].sort(key=lambda r: r.get("create_time") or 0, reverse=True)
        return out

    async def items(self, groups: tuple[int, ...] = GROUPS) -> dict:
        cookie = await self._cookie_header()
        return await asyncio.to_thread(self._blocking_items, cookie, tuple(groups))

    async def probe(self) -> dict:
        """M0 探针：只回答「通不通、有几条、有没有 cid」。"""
        started = time.time()
        try:
            data = await self.items()
        except NoticeApiError as exc:
            return {"ok": False, "error": str(exc), "ms": int((time.time() - started) * 1000)}
        except Exception as exc:  # noqa: BLE001
            logger.warning("通知 HTTP 探针异常：%s", type(exc).__name__)
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "ms": int((time.time() - started) * 1000)}
        items = data.get("items") or []
        with_cid = [i for i in items if i.get("has_cid")]
        at_items = [i for i in items if i.get("type") in (45, 46)]
        reply_items = [i for i in items if i.get("needs_reply")]
        return {
            "ok": True,
            "ms": int((time.time() - started) * 1000),
            "counts": data.get("counts"),
            "items": len(items),
            "with_comment_id": len(with_cid),
            "at_me": len(at_items),
            "needs_reply": len(reply_items),
            "warnings": data.get("warnings") or [],
            "sample": items[:5],
        }