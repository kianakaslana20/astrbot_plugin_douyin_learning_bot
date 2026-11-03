"""aweme 字段归一化：从超大 JSON 里只抠出要用的十几个字段。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HASHTAG = re.compile(r"#([^\s#]{1,30})")
_EMOJI = re.compile(r"\[[^\[\]]{1,8}\]")


@dataclass
class AwemeItem:
    aweme_id: str
    desc: str = ""
    author: str = ""
    author_id: str = ""
    duration_ms: int = 0
    width: int = 0
    height: int = 0
    play_url: str = ""
    digg: int = 0
    comment_count: int = 0
    share_count: int = 0
    create_time: int = 0
    is_images: bool = False
    has_caption: bool = False
    tags: list[str] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return self.duration_ms / 1000.0

    @property
    def title(self) -> str:
        text = _EMOJI.sub("", self.desc or "").replace("\n", " ").strip()
        return text[:60] or f"aweme {self.aweme_id}"

    @property
    def share_url(self) -> str:
        return f"https://www.douyin.com/video/{self.aweme_id}"

    @property
    def orientation(self) -> str:
        if not self.width or not self.height:
            return "?"
        if self.height > self.width * 1.1:
            return "竖屏"
        if self.width > self.height * 1.1:
            return "横屏"
        return "方形"


def _urls_of(addr) -> list[str]:
    if isinstance(addr, dict):
        return [u for u in (addr.get("url_list") or []) if isinstance(u, str) and u.startswith("http")]
    return []


def _pick_play_url(video: dict) -> str:
    """按「无水印优先」取直链。

    实测不同片源字段填充不一样：多数有 play_addr，少数只有 play_addr_h264/265，
    长视频有时只在 bit_rate[] 里带地址。download_addr 系列一律不取（带水印）。
    """
    for key in ("play_addr", "play_addr_h264", "play_addr_265"):
        urls = _urls_of(video.get(key))
        if urls:
            return urls[0]
    for rate in video.get("bit_rate") or []:
        if not isinstance(rate, dict):
            continue
        for key in ("play_addr", "play_addr_265", "play_addr_h264"):
            urls = _urls_of(rate.get(key))
            if urls:
                return urls[0]
    # 最后兜底：带水印也能做转写和画面理解，总比整条丢掉强
    for key in ("new_download_addr", "download_addr"):
        urls = _urls_of(video.get(key))
        if urls:
            return urls[0]
    return ""


def _caption_hint(video: dict) -> bool:
    """是否自带字幕轨（有的话转写可以省一次 STT）。"""
    info = video.get("cla_info") or {}
    captions = info.get("caption_infos") if isinstance(info, dict) else None
    return bool(captions)


def parse_aweme(raw: dict) -> AwemeItem | None:
    if not isinstance(raw, dict):
        return None
    aweme_id = str(raw.get("aweme_id") or "").strip()
    if not aweme_id:
        return None

    video = raw.get("video") or {}
    author = raw.get("author") or {}
    stats = raw.get("statistics") or {}
    images = raw.get("images") or []

    desc = str(raw.get("desc") or "")
    tags = [m.group(1) for m in _HASHTAG.finditer(desc)]

    def _int(value) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    return AwemeItem(
        aweme_id=aweme_id,
        desc=desc,
        author=str(author.get("nickname") or ""),
        author_id=str(author.get("uid") or author.get("sec_uid") or ""),
        duration_ms=_int(video.get("duration")),
        width=_int(video.get("width")),
        height=_int(video.get("height")),
        play_url=_pick_play_url(video),
        digg=_int(stats.get("digg_count")),
        comment_count=_int(stats.get("comment_count")),
        share_count=_int(stats.get("share_count")),
        create_time=_int(raw.get("create_time")),
        is_images=bool(images),
        has_caption=_caption_hint(video),
        tags=tags[:8],
    )
