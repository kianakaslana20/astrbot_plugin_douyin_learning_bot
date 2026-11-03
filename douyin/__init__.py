"""抖音刷视频学习助手 —— 内部实现包。

模块划分：
- config    ：把 AstrBotConfig 收敛成带默认值的 Settings
- media     ：ffmpeg 探测、抽音轨、抽帧、九宫格拼图
- browser   ：headful Playwright，拦截 tab/feed 拿推荐流，下载无水印源
- feed      ：aweme 字段归一化
- store     ：SQLite 去重与知识库
- scoring   ：兴趣/热度打分
- understanding：ASR + 视觉理解
- pipeline  ：串起一轮完整流程
"""

__all__ = [
    "config",
    "media",
    "browser",
    "feed",
    "store",
    "scoring",
    "understanding",
    "pipeline",
]
