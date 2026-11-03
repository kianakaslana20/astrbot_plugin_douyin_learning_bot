"""打分：知识密度优先，热度只做微调。

为什么热度权重调低：
在这一版实测的 60 条推荐流样本里，点赞最高的几条是「高三生 vlog」「萌宠」「吃播」，
而真正有信息量的「0 号病人」「洗钱怎么洗」「俄罗斯为什么不经营远东」点赞只有 2-5 万。
纯按热度排，学习链路会一路吃娱乐内容，所以：
- 知识信号（科普/原理/解析/为什么/教程…）给主导权重；
- 娱乐信号（短剧/漫剧/电影解说/吃播/萌宠/挑战…）扣分；
- 热度只用对数做小幅加权，避免千万赞霸榜。
"""

from __future__ import annotations

import math
import re

from .config import Settings
from .feed import AwemeItem

BLOCKED_SCORE = -999.0

# 知识密度信号：标题/话题里出现就加分
KNOWLEDGE_HINTS = (
    "科普", "冷知识", "涨知识", "知识", "原理", "解析", "解读", "详解",
    "教程", "教学", "方法论", "复盘", "拆解", "为什么", "怎么", "如何",
    "历史", "经济", "金融", "物理", "数学", "化学", "生物", "医学", "科学",
    "数据", "研究", "实验", "思维", "认知", "效率", "编程", "英语",
    # 注意："AI" 故意不在此列 —— 它几乎出现在每条 AI 题材的娱乐视频标签里，
    # 放在这里等于给「#AI #DeepSeek 配文案」这类内容开后门。它继续留在兴趣词里加分。
    # 技术/方法类实词：补这一批是因为「DeepSeek 开源 super-agent」这种纯新闻标题
    # 一个"科普/讲解"都没写，被误杀了（教训：别只认"科普腔"的词）。
    "开源", "论文", "算法", "架构", "模型", "大模型", "推理", "训练", "参数",
    "评测", "干货", "入门", "进阶", "踩坑", "工具", "插件", "部署", "配置",
)

# 娱乐信号：出现就扣分（不封杀，兜底时仍可能被选中）
FUN_HINTS = (
    "短剧", "漫剧", "动漫", "动画", "解说", "影视", "电影", "电视剧", "综艺",
    "吃播", "美食", "探店", "测评", "萌宠", "猫", "狗", "vlog", "日常",
    "恋爱", "情感", "前任", "搞笑", "挑战", "游戏", "王者", "三角洲",
    "整活", "沙雕", "神回复", "热梗",
)

# 剧集/连载：重罚（抖音精选里占大头，理解成本高、学习收益低）
_SERIES = re.compile(
    r"第\s*[0-9一二三四五六七八九十百千]+\s*[集话期季章]"
    r"|(全|共)\s*\d+\s*集"
    r"|合集|连载|未完待续|第一季|第二季|第\s*\d+\s*期"
)


def _hits(blob: str, words: tuple[str, ...] | list[str]) -> int:
    return sum(1 for word in words if word and word.lower() in blob)


def score_item(item: AwemeItem, settings: Settings, seen_authors: set[str] | None = None) -> float:
    text = f"{item.desc} {' '.join(item.tags)}"
    blob = text.lower()

    if item.is_images:
        return BLOCKED_SCORE          # 图集没有音轨，理解链路跑不通
    if not item.play_url:
        return BLOCKED_SCORE          # 没有直链就下不了
    if not item.desc.strip() and item.digg == 0:
        return BLOCKED_SCORE          # 空壳卡片（实测里真的出现过）
    if item.digg < settings.min_like:
        return BLOCKED_SCORE

    if _hits(blob, settings.block_keywords):
        return BLOCKED_SCORE

    dur = item.duration_s
    if dur < settings.min_duration_s or dur > settings.max_duration_s:
        return BLOCKED_SCORE

    # 真·知识词：必须命中至少这么多个，才算"在讲一件事"。
    # 注意这里**只算 KNOWLEDGE_HINTS**，不把兴趣词算进来——兴趣词（deepseek / AI / 男娘…）
    # 在娱乐视频的标签里满街都是，混在一起会让「#deepseek #claude 配文案」这种混进来。
    hits_know = _hits(blob, KNOWLEDGE_HINTS)
    if hits_know < settings.min_knowledge_hits:
        return BLOCKED_SCORE
    hits_interest = _hits(blob, settings.interest_keywords)

    # 1) 知识密度（主导项）
    knowledge = hits_know + 0.5 * hits_interest
    score = min(9.0, 2.2 * knowledge)

    # 2) 娱乐信号（扣分项）
    fun = _hits(blob, FUN_HINTS)
    score -= min(6.0, 1.4 * fun)
    if fun >= 2 and hits_know <= 1:
        score -= 2.0      # 只沾一个知识词、却堆了一串娱乐词：明显是蹭标签

    # 3) 时长契合：3-10 分钟最适合"讲清一件事"
    if 180 <= dur <= 600:
        score += 1.5
    elif 60 <= dur < 180:
        score += 0.6
    elif dur > 900:
        score -= 2.0

    # 4) 热度：只做微调
    score += 0.6 * math.log10(1 + max(0, item.digg))

    # 5) 横屏更像讲解/纪录
    if item.orientation == "横屏":
        score += 0.5

    # 6) 剧集/连载重罚
    if _SERIES.search(text):
        score -= 6.0

    # 7) 同作者降权，避免一路同一个号
    if seen_authors and item.author in seen_authors:
        score -= 2.5

    return round(score, 2)


def rank(
    items: list[AwemeItem],
    settings: Settings,
    seen_authors: set[str] | None = None,
) -> list[tuple[float, AwemeItem]]:
    scored = [(score_item(item, settings, seen_authors), item) for item in items]
    floor = float(getattr(settings, "min_score", BLOCKED_SCORE) or BLOCKED_SCORE)
    scored = [(s, i) for s, i in scored if s > BLOCKED_SCORE and s >= floor]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored
