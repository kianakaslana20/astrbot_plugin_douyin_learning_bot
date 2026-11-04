# AstrBot「抖音学习助手」插件方案 v2

> 借鉴目标：`mjy1113451/astrbot_plugin_b-`（B站学习助手 v3.1.6，Python / MIT，2026-10 仍在更新）
> 目标环境：本机 AstrBot **4.29.3**、Python 3.12.16 venv、Playwright chromium 已就绪
> 调研采样时间：2026-10-31

---

## 0. 结论

**可以做，但「抖音那一半」的选型要按这份调研改三次。** 原项目 80% 的「脑」可以原样搬——它本来就是为「自动刷流 → 理解 → 交互 → 沉淀」写的，`services/platform_adapter.py` 里 `douyin` 已经在 `SUPPORTED_PLATFORMS` 里躺着，只是 `supported_analysis=False`。

真正决定成败的是取流通道。我最初想的三条路里，**有一条已经被抖音废掉了**，但另一条更好的路浮出来了。

| 原设想 | 实测结论 | 替代方案 |
|---|---|---|
| 通道 A：Playwright 拦截 `tab/feed` | 可行但慢、重 | ✅ 保留，用于**需要签名**的接口 |
| 通道 B：自算 `a_bogus` 直连 | 可行但需维护 JS/VM | ✅ 保留，Node 子进程签名器 |
| 通道 C：`iesdouyin` 分享页免签解析 | ❌ **已失效**：302 跳回 douyin.com，不再内联 `RENDER_DATA`，旧 `iteminfo` 接口已下线 | 换成 🔥 **`POST /aweme/v1/web/module/feed/` 免签名推荐流** |

最后这条是本次最大收获：**推荐流有一个不需要 `a_bogus` 的口子**，MediaCrawlerPro 现行实现就在用（`need_sign=False`）。有它，MVP 的含金量立刻不一样。

- MVP（自动刷 + 总结 + 推送）：**3–4 天**
- v0.1（+ 点赞收藏、每日简报、知识库）：**8–12 天**
- v0.2（+ 评论互动、讲解网页）：另计 5–7 天

---

## 1. 原项目拆解：哪些能直接用

| 模块 | 原职责 | 对抖音插件 |
|---|---|---|
| `main.py` | `Star` 子类、命令注册、自动启动 | ✅ 骨架照抄 |
| `api/throttle.py`（2KB） | 请求限速/排队 | ✅ 直接用，节奏参数改一改 |
| `api/client.py`（35KB） | B站 HTTP 封装 + cookie | 🔧 保留壳，签名层换抖音 |
| `brain/_brain_loop.py`（88KB） | 主循环：取流→挑→看→记 | ✅ 流程复用，取流换 adapter |
| `brain/video_analysis.py`（111KB） | 视频理解/摘要/章节 | ✅ 输入换成「ASR 文本 + 关键帧」 |
| `services/interest_engine.py`（30KB） | 兴趣打分 | ✅ 直接复用，指标换抖音维度 |
| `brain/decision.py` | 打分 → 动作 | ✅ 直接复用 |
| `brain/comment.py`、`private_msg.py` | 评论/私信生成 | 🔧 文本侧复用，接口换抖音 |
| `knowledge/*` | 分类、整理、回顾、检索 | ✅ 直接复用 |
| `xingye_bot/asr_engine.py`（45KB） | 本地/远程 ASR | ⚠️ 精简——AstrBot 已有 STT provider |
| `xingye_bot/grid_frames.py`（13KB） | 抽帧拼九宫格 | ✅ 直接复用 |
| `services/video_to_ppt.py`（105KB） | 视频 → 讲解网页 | ✅ 复用（竖屏 9:16 版式调一下） |
| `core/platform_actions.py` | 三个动作开关常量 | ✅ 照抄，扩成四级阶梯 |
| `services/platform_adapter.py` | 多平台识别 + 分派 | ⭐ **就是要在这儿长出来的地方** |

它的适配层现状（原文）：

```python
SUPPORTED_PLATFORMS = ("bilibili", "youtube", "douyin", "kuaishou", "web", "local")
# detect_platform: "douyin.com" in value or "iesdouyin.com" -> "douyin"
# extract_video_id: re.search(r"/video/(\d+)", value)
# fetch_platform_metadata(): douyin 落到 else -> supported_analysis = False
```

作者留了插槽没填。我们的活就是把 `douyin` 分支填满，并把它从「一个文件里的 if/else」升级成「adapter 目录」，以后快手/小红书能顺着加。

---

## 2. 抖音侧技术选型

### 2.1 四条通道（按优先级）

| # | 通道 | 原理 | 能拿到 | 抗变化 | 签名 | 定位 |
|---|---|---|---|---|---|---|
| **①** | **免签推荐流** | `POST /aweme/v1/web/module/feed/`（`need_sign=False`） | 推荐流 `aweme_list`，可按 `tag_id` 分类 | ★★★★ | ❌ 不需要 | **主通道** |
| **②** | Playwright 复用 | 打开 douyin.com，hook `XMLHttpRequest.prototype.send`，把页面自己生成的 `a_bogus` 捞出来或直接读响应 | 所有需签名接口 + 写操作 | ★★★★★ | 由页面生成 | **写操作 & 需签名接口** |
| **③** | Node 签名器 | 本地 `node_signer.js` 算 `a_bogus`，Python 侧 httpx 直连 | 全量接口，可并发 | ★★☆（算法会更新） | 自算 | **批量/加速** |
| **④** | 免签搜索 | `GET /aweme/v1/web/general/search/single/`（免签） | 指定关键词的视频 | ★★★★ | ❌ 不需要 | **冷启动 / 精准订阅** |

> 注意通道 ① 和 ④ 都**不用登录 Cookie** 也能试；写操作（点赞收藏评论关注）必须登录 + 签名 + CSRF 头。

### 2.2 接口全表（签名=需 a_bogus）

| 用途 | 路径 | 方法 | 签名 | 登录 |
|---|---|---|---|---|
| **推荐流（主）** | `/aweme/v1/web/module/feed/` | POST | **否** | 否 |
| 推荐流（旧） | `/aweme/v1/web/tab/feed/` | GET | 是 | 否 |
| 搜索 | `/aweme/v1/web/general/search/single/` | GET | **否** | 否 |
| 视频详情 | `/aweme/v1/web/aweme/detail/` | GET | 是 | 否（建议有） |
| 用户作品 | `/aweme/v1/web/aweme/post/` | GET | 是 | 否 |
| 用户信息 | `/aweme/v1/web/user/profile/other/` | GET | 是 | 否 |
| 评论列表 | `/aweme/v1/web/comment/list/` | GET | 是 | 否 |
| 子评论 | `/aweme/v1/web/comment/list/reply/` | GET | 是 | 否 |
| 登录态检查 | `/aweme/v1/web/history/read/`（`status_code=8` 即未登录） | GET | 是 | 是 |
| 点赞 | `/aweme/v1/web/commit/item/digg/` | POST | 是 | 是 |
| 收藏 | `/aweme/v1/web/aweme/collect/`（`action=1/0`，形态待抓包确认） | POST | 是 | 是 |
| 关注 | `/aweme/v1/web/commit/follow/user/` | POST | 是 | 是 |
| 发评论 | `/aweme/v1/web/comment/publish/` | POST | 是 | 是 |

写操作统一要求：`form-urlencoded` + `Origin: https://www.douyin.com` + `Referer: /discover` + `X-Secsdk-Csrf-Token: DOWNGRADE`，且 **`a_bogus` 要对 body 一起签名**。

### 2.3 推荐流真实参数（通道 ①）

```
POST https://www.douyin.com/aweme/v1/web/module/feed/
device_platform=webapp & aid=6383 & channel=channel_pc_web
module_id=3003101 & count=20 & refresh_index=1
tag_id=0            # 0=全部；300204 美食 / 300205 游戏 / 300207 体育 / 300215 影视
presented_ids=...   # 已推过的 id，去重关键
refer_id= & refer_type=10
awemePcRecRawData={"is_xigua_user":0,"is_client":false}
support_h265=1 & support_dash=1 & pc_client_type=1 & install_time=...
+ 31 个设备指纹参数（screen 2560x1440、browser Chrome/135、os Mac OS 10.15.7、
  cpu_core_num、device_memory、downlink、effective_type、round_trip_time…）
```

拿到 `tag_id` 这张表，就等于能做「按兴趣标签刷」——比如你只爱看影视和游戏，插件就能定向刷，而不是被动吃算法推荐。这是产品上比 B 站版更强的一点。

### 2.4 aweme 关键字段

```python
{
  "aweme_id": "7xxxxxxxxxxxxxxxxxx",
  "desc": "文案 #话题",
  "create_time": 1761xxxxxx,
  "author": {"uid", "sec_uid", "nickname", "unique_id"},
  "video": {
    "duration": 15300,                         # ms
    "width", "height", "ratio",
    "play_addr": {"uri", "url_list": [...]},   # 无水印
    "bit_rate": [{"gear_name", "quality_type", "bit_rate", "is_h265",
                  "play_addr": {"url_list"}}],
    "cover": {"url_list": [...]},
    "cla_info": {"caption_infos": [...]}        # 部分视频带自动字幕
  },
  "images": [...],                              # 图集作品
  "music": {"title", "play_url": {"url_list"}},
  "statistics": {"digg_count", "comment_count", "share_count", "collect_count"},
  "text_extra": [{"hashtag_name": "..."}]
}
```

搜索类响应外面多包一层 `aweme_info`，归一化时要注意。

### 2.5 取直链

- **`video.play_addr.url_list` = 无水印**（web 端）；`download_addr` 是带水印的
- 优先 `bit_rate[]` 里质量最高的一档（`is_h265`/`is_bytevc1`），体积小画质好
- 直链带 `x-expires` 等过期参数 → **必须即时下载，不能缓存 URL**
- 图集走 `images[].url_list`，音轨走 `music.play_url`

### 2.6 签名与风控（写进代码的判断依据）

**a_bogus**：`bdms 1.0.1.19-fix.01`，栈式 VM；SM3（盐 `dhzx`）对 query/body/UA 做 5 次哈希 → 时间戳 + 双周序号 + 环境校验和 + 熵 + pageId + aid + 屏幕尺寸 → XOR/位混合 → base64，**定长 180 字符、非确定性**（含熵字节）。签名输入 = query + UA + Cookie，**参数顺序和编码必须完全一致**。

开源实现（我核过活跃度）：

| 项目 | 形态 | 最近更新 | 备注 |
|---|---|---|---|
| `hanzheng1954/douyin-abogus-analysis` | Node 签名器 + **纯 Python 移植**（与 Node 逐字符 0 差异） | 2026-09-23 | 最完整，含 4K/无水印档位表 |
| `ylcangel/douyin_sign`（brock7 fork） | JS + `decode_vmcode.py`，需 execjs | 2026-08-11 | 对应同一算法版本 |
| `MediaCrawler` | 算法外置成签名服务 `POST /signsrv/v1/douyin/sign` | 2026-10-27 | 适合起个本地小服务 |
| `f2` / `TikTokDownloader` / `Evil0ctal` | 下载/采集为主 | 2026-10 | 不做点赞评论 |

**结论：没有「纯 Python 免浏览器」的长期稳定解。** 工程上最稳的是通道 ②（让页面自己算），③ 只在需要并发时启用。

**风控要素**：

| 项 | 怎么来 |
|---|---|
| `msToken` | `POST https://mssdk.bytedance.com/web/common`（magic 538969122, version 1, dataType 8, strData≈12KB 指纹）取 Set-Cookie；长度须 120/128；降级方案 = 126 随机字符 + `==` |
| `webid` | `POST https://mcs.zijieapi.com/webid?aid=6383&sdk_version=5.1.18_zip`；失败用 19 位伪 id |
| `ttwid` | 首页 Set-Cookie 或 ttwid 注册接口 |
| `verifyFp` | `s_v_web_id` = `"verify_" + base36(ms) + "_" + 36位`（8/13/18/23 位为 `_`，第 14 位为 `4`） |

**失败特征 → 处置**：

| 现象 | 含义 | 处置 |
|---|---|---|
| **HTTP 200 + 空 body** | 典型拦截（我本地实测 `/tab/feed/` 就是空体） | 换通道 ①，或补签名 |
| `status_code != 0` / `aweme_list == []` | 软限流 | 退避 5–15 min，降频 |
| 403 / `blocked` 文本 | 指纹对不上 | 固定 UA 与签名一致，换 Cookie |
| 跳 `/verify` | 验证码 | **立即熔断**，推截图到聊天，等人处理 |

> `-352` 是 App 侧签名错误码，Web 端主要看空体和 `status_code`。

---

## 3. 插件架构

### 3.1 目录

```
astrbot_plugin_douyin_learning_bot/
├── main.py                    # Star 子类：命令 / LLM Tool / 生命周期
├── metadata.yaml              # name/display_name/version/astrbot_version/author/repo
├── _conf_schema.json          # WebUI 配置面板
├── requirements.txt           # playwright>=1.40（其余 AstrBot 已带）
├── adapter/
│   ├── base.py                # BasePlatformAdapter: feed/detail/comment/act
│   ├── dy_module_feed.py      # 通道①：免签推荐流
│   ├── dy_search.py           # 通道④：免签搜索
│   ├── dy_browser.py          # 通道②：Playwright + XHR hook（取 a_bogus / 写操作）
│   ├── dy_signer.py           # 通道③：Node 子进程签名器
│   ├── dy_risk.py             # msToken / webid / ttwid / verifyFp / 熔断
│   ├── dy_download.py         # play_addr 即时下载 + 过期重取
│   └── normalize.py           # aweme -> AwemeItem 统一模型
├── brain/
│   ├── loop.py                # 取流→过滤→理解→决策→执行→记录
│   ├── understand.py          # 下载→抽音→STT→抽帧→视觉→摘要
│   ├── decide.py              # 兴趣打分 → {skip,collect,like,comment}
│   ├── act.py                 # 写操作（开关 + 配额 + dry_run + 熔断）
│   └── report.py              # 早报/周报
├── knowledge/                 # SQLite + FTS5（可选向量）
├── storage/db.py
└── web/panel.py               # register_web_api 看板（可选）
```

### 3.2 数据流

```
[timer / /dy next]
        │
        ▼
 [adapter.feed()] ─ ① module/feed（免签，主）
        │        ├─ ④ search（免签，按关键词订阅）
        │        └─ ② Playwright（需要签名时的兜底）
        ▼
 [dy_risk.preflight]  msToken / webid / ttwid / UA 固定
        ▼
 [normalize]  →  AwemeItem(aweme_id, desc, author, duration, urls…)
        ▼
 [filter]  去重(aweme_id/presented_ids) / 时长 / 关键词 / 作者黑名单 / 日配额
        ▼
 [understand]  即时下载 mp4（直链会过期！）
        ├─ ffmpeg 抽 m4a → STT provider 转写
        └─ ffmpeg 抽 9 帧 → 拼图 → 视觉模型（text_chat image_urls）
        ▼
 [decide]  兴趣打分 0–100 → action
        ▼
 ┌──────┴───────┬──────────┬──────────┐
skip          collect     like      comment（需显式开启）
        ▼
 [db.save] → [report] 定时推到聊天
```

### 3.3 动作分级（沿用原项目思路，扩成四级）

原项目只有三个常量：

```python
ALLOW_PUBLIC_COMMENTS = True
ALLOW_AT_MENTION_REPLIES = True
ALLOW_VIDEO_LIKES = True
```

抖音版做成**四级 + 双层开关**（配置层 + 代码内硬上限）：

| 级别 | 动作 | 默认 | 要求 |
|---|---|---|---|
| **L0** | 观看 + 总结（零对外动作） | ✅ 默认开 | 无 |
| **L1** | 收藏（仅本人可见） | ⬜ | 手动开 |
| **L2** | 点赞 | ⬜ | 手动开 + 日限额 |
| **L3** | 评论 | ⬜ | 手动开 + 日限额 + 抽检 |

---

## 4. AstrBot 对接（本机 4.29.3 源码核实，非凭记忆）

| 能力 | 接口 |
|---|---|
| 命令 | `@filter.command("dy")` + `yield event.plain_result(...)` |
| 主动推送 | `await self.context.send_message(umo, MessageChain([Plain(t), Image.fromFileSystem(p)])) -> bool` |
| 文本模型 | `pid = await self.context.get_current_chat_provider_id(umo=umo)` → `await self.context.llm_generate(chat_provider_id=pid, prompt=...)` |
| **看画面** | `p = self.context.get_using_provider(umo)` → `await p.text_chat(prompt, image_urls=[...], system_prompt=...)` |
| **听声音** | `stt = self.context.get_using_stt_provider(umo)` → `await stt.get_text(audio_path)` ← `STTProvider.get_text(audio_url)` 是抽象方法，**不用自己装 whisper/funasr** |
| AI 自主调用 | `@filter.llm_tool(name="douyin_watch")`，docstring 必须有 `Args:` 段 |
| 多步 Agent | `await self.context.tool_loop_agent(event=..., chat_provider_id=..., prompt=..., tools=ToolSet([...]), max_steps=30)` |
| 配置面板 | `_conf_schema.json`（`bool`/`string`/`int`/`text`/`options`/`template_list`） |
| 存储 | `data/plugin_data/astrbot_plugin_douyin_learning_bot/` |

`@filter.llm_tool` 的坑（官方文档明确警告）：**只解析 docstring 的 `Args:` 段，不读类型注解**，格式必须 `参数名(类型): 描述`，写错参数会被静默丢弃。

```python
@filter.llm_tool(name="douyin_watch")
async def douyin_watch(self, event: AstrMessageEvent, count: int = 3):
    """让助手去抖音刷几条视频并总结。

    Args:
        count(number): 要刷几条，1-10
    """
    ...
```

**本机依赖已就绪**（我查过 AstrBot 的 venv）：

```
playwright ✅ 已装 + chromium-1248/headless_shell 已下载
httpx / aiohttp / Pillow / yt_dlp / numpy ✅
ffmpeg ✅ Playwright 自带 7.0.1 + 系统 WindowsApps 版
cv2 / faster_whisper / funasr ❌（本项目用不到：视觉走 text_chat，语音走 STT provider）
```

也就是说，**不装任何重依赖就能开工**。

### 4.1 通道 ① 的落地写法

```python
async def feed_once(self, count=20, tag_id=0):
    q = {"device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web",
         "module_id": "3003101", "count": str(count), "refresh_index": "1",
         "tag_id": str(tag_id), "refer_id": "", "refer_type": "10",
         "support_h265": "1", "support_dash": "1", "pc_client_type": "1",
         "presented_ids": ",".join(self.recent_ids[-30:]), **self.fingerprint}
    r = await self.http.post("https://www.douyin.com/aweme/v1/web/module/feed/",
                             params=q, headers=self.headers)   # 无 a_bogus
    body = r.json()
    if not body.get("aweme_list"):          # 空体/空列表 = 被拦，走熔断
        raise RiskBlocked("module/feed 返回空列表")
    return body["aweme_list"]
```

### 4.2 通道 ② 的落地写法（需要签名时）

```python
# hook 页面自己的 XHR，把 a_bogus 连请求一起留下来
await page.add_init_script("""
  const _open = XMLHttpRequest.prototype.open, _send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function(b){
    try { window.__lastXhr = {url: this.__url, body: b}; } catch(e){}
    return _send.apply(this, arguments);
  };
  XMLHttpRequest.prototype.open = function(m,u){ this.__url = u; return _open.apply(this, arguments); };
""")
```

拿到真实请求后，用同样的 `a_bogus` 计算规则或直接 `page.evaluate(fetch(...))` 在页面上下文里发请求——**签名永远由页面算，我们只借它的上下文**。这是抗升级的根本原因。

### 4.3 存储

```
<astrbot>/data/plugin_data/astrbot_plugin_douyin_learning_bot/
├── db.sqlite3
├── media/               # 下载的 mp4，默认 7 天清理
├── frames/              # 抽帧拼图临时文件
└── browser_profile/     # Chromium 持久化登录态
```

```sql
CREATE TABLE aweme (aweme_id TEXT PRIMARY KEY, author TEXT, desc TEXT,
  duration INTEGER, tag TEXT, stats_json TEXT, seen_at INTEGER, score REAL,
  action TEXT, summary TEXT);
CREATE TABLE action_log (id INTEGER PRIMARY KEY, aweme_id TEXT,
  action TEXT, ok INTEGER, err TEXT, ts INTEGER);
CREATE TABLE kb (id INTEGER PRIMARY KEY, aweme_id TEXT, title TEXT,
  body TEXT, tags TEXT, created_at INTEGER);
```

---

## 5. 配置面板草案

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | **false** | 总开关（装上不会自己动） |
| `dry_run` | bool | **true** | 只记日志不执行写操作 |
| `owner_umo` | string | "" | 推送到哪个会话（`/dy bind` 自动填） |
| `feed.tag_ids` | string | "0" | 兴趣标签，如 `300207,300215`（体育,影视） |
| `feed.count_per_round` | int | 5 | 每轮刷几条 |
| `feed.interval_minutes` | int | 60 | 两轮间隔 |
| `feed.daily_limit` | int | 80 | 单日上限 |
| `feed.keywords_block` | text | "" | 屏蔽词，逗号分隔 |
| `feed.min_duration_sec` | int | 10 | 太短跳过 |
| `feed.search_keywords` | text | "" | 走通道 ④ 的订阅词 |
| `actions.level` | string | "L0" | `L0`/`L1`/`L2`/`L3` |
| `actions.daily_like_limit` | int | 10 | |
| `actions.daily_comment_limit` | int | 3 | |
| `understand.vision` / `.stt` | bool | true | 抽帧送视觉 / 送 STT |
| `understand.frames` | int | 9 | 拼图帧数 |
| `browser.headless` | bool | true | 登录时改 false |
| `browser.proxy` | string | "" | |
| `signer.enabled` | bool | false | 是否启用通道 ③ |
| `signer.node_path` | string | "" | node 可执行路径 |
| `report.times` | string | "09:00,21:00" | 简报时间 |
| `storage.keep_days` | int | 7 | 媒体保留天数 |

---

## 6. 里程碑

| 阶段 | 内容 | 验收 | 估时 |
|---|---|---|---|
| **M0 打样** | `POST module/feed` 免签拿 20 条 `aweme_list`，打印标题 | 不登录也能拿到数据 | **0.5 天** |
| **M1 单条理解** | `/dy sum <链接或 id>`：`aweme/detail` → 下载 → 抽音抽帧 → STT + 视觉 → 摘要 | 发个链接回一段总结 | 1 天 |
| **M2 自动刷流** | 主循环 + 去重 + 过滤 + 打分 + 落库 + `/dy start|stop|status` | 挂机半天自己刷自己存 | 1.5 天 |
| **M3 简报推送** | 定时汇总（今日 N 条、TOP3、一句话观点） | 早晚各收一条 | 0.5 天 |
| **M4 写操作** | Playwright 通道：收藏/点赞/评论（分级 + 限额 + `X-Secsdk-Csrf-Token` + 熔断） | 按配置执行且不失控 | 2 天 |
| **M5 知识库** | 落库 + `/dy kb <关键词>` + 周报回顾 | 能问「上周刷到讲 XX 的那条」 | 2 天 |
| **M6 打磨** | 配置面板、日志、看板、`metadata.yaml`、发布 | 干净环境装得上、跑得动 | 2 天 |

**MVP = M0–M3 ≈ 3.5 天，其中 M0 只要半天就能证明「这条路通」。**

---

## 7. 风险表

| 风险 | 概率 | 影响 | 对策 |
|---|---|---|---|
| `module/feed` 接口加签名或下线 | 中 | 主通道失效 | 兜底链：① → ② → ③；接口常量化 + 自动降级 |
| `a_bogus` 算法更新 | 高（季度级） | 通道 ③ 失效 | ①② 不依赖自算签名，所以不至于全停 |
| `msToken` 校验变严 | 中 | 免签通道也开始要 token | 预置 mssdk 取 token + 降级随机串 |
| 账号风控/验证码 | 中 | 写操作中断 | 熔断 + 推截图 + 人工处理 + 日限额 |
| 直链过期导致下载失败 | **高** | 视频下载 404 | 拿到 URL 立即下载；失败重取 detail 刷新 URL |
| 登录态过期 | 高 | 写操作全挂 | `storage_state` 持久化；`/dy login` 手动 |
| 视频占盘 | 中 | 磁盘爆 | 7 天清理 + 单文件上限 + 可只留摘要 |
| 竖屏视觉识别差 | 中 | 摘要质量低 | 拼图改 3×3 竖构图；ASR 权重高于画面 |
| 与原 `astrbot_plugin_parser` 重叠 | 低 | 重复解析 | 检测到已装则链路三改为「复用其解析结果」 |

---

## 8. 工程边界（自己给自己立的规矩）

- **默认 L0**：装完只会「看 + 总结 + 推给你」，不会在你不知情时点赞评论
- **双闸**：动作分级 + 日配额，`/dy status` 随时能看到今天用掉多少额度
- **验证码 = 立即熔断**：宁可当天不刷，也不硬刚风控
- **媒体不长期留存**：摘要和元数据才是资产

---

## 9. 需要你拍板的三件事

1. **主通道确认**：直接押 `module/feed` 免签（快，但有被加签的风险），还是主用 Playwright 拦截（慢但最稳）？我建议 **① 主 + ② 兜底**，M0 半天就能告诉你 ① 现在到底通不通。
2. **理解深度**：默认「ASR + 9 帧拼图」；要不要给长视频加「分段 ASR + 章节」？（贵一档，摘要质量高一档）
3. **插件形态**：独立新插件 `astrbot_plugin_douyin_learning_bot`，还是在原 B 站插件里补 `douyin` 分支做双平台版？我建议独立，互不干扰。

拍完我就开 M0——**半天内给你一个「能不能刷到」的确定答案**，再决定要不要投入后面 3 天。

---

## 10. 落地结果（2026-10-31 01:30 更新，方案已被实测改写）

§6 的三个里程碑已经跑完，其中**方案里最关键的一条假设被证伪**，这里如实记录修订：

### 10.1 通道判断修正

| 方案原判断 | 实测结论 |
| --- | --- |
| `POST /aweme/v1/web/module/feed` 免签可用 | **假**。HTTP 200 但 body 0 字节；用页面自己的 cookie 在页内 fetch 同样是 0 字节 |
| 通道 ② Playwright 拦截做兜底 | 升级为**唯一主通道** |
| 拦截 `/aweme/v1/web/tab/feed/` 即可 | 不够。今天首页进的是「抖音精选」变体，走 `/aweme/v2/web/module/feed/`，且回包里多一个 `chime_video_list`，两个数组都得吃 |
| headless 也能跑 | **假**。`headless=True` 直接落到「验证码中间页」，cookie 只剩 `__ac_nonce` |
| 需要登录才刷得到 | **假**。未登录即可刷推荐流（29 个 cookie 含 `ttwid`，无 `sessionid`） |

### 10.2 实测进度（全部一手数据）

| 环节 | 结果 |
| --- | --- |
| 取流 | 20 秒 4 次滚动 → 60 条唯一 aweme，39 条带 `play_addr` |
| 筛选 | 60 → 过筛 33–36 条（丢的是无直链、超 900s、空卡片） |
| 排序 | 重写打分后 Top5 变成「洗钱 / 0 号病人 / 马斯克 3.1 万亿 / 三文鱼刺身 / 俄罗斯远东」，不再是清一色影视解说 |
| 下载 | 52.9MB / 17.8s，`HTTP 206`，`ffprobe` 读数与 aweme 元数据一致 |
| 视觉 | 9 帧九宫格（1151KB）直接送 `deepseek-flash` 多模态，模型读出画面、认出「带货口播」，给出 keywords 与 verdict |
| 语音 | 本机 AstrBot 无任何 STT；插件内建 astrbot/openai/local 三档后端，mock 接口验证切换正确（转写 111 字） |
| 宿主加载 | 已装进 `data/plugins`，日志 `Added llm tool: douyin_watch`，热重载无报错 |

### 10.3 两个必须记住的环境坑

- **ffmpeg**：`shutil.which("ffmpeg")` 会命中 `WindowsApps\ffmpeg.EXE`（0 字节商店占位符），必须按「存在 + 非 windowsapps + 体积 > 64KB」三重校验才认；真实可用的在 `C:\ffmpeg\bin\ffmpeg.exe`，Playwright 自带的 `ffmpeg-win64.exe` 也可用。
- **热加载**：AstrBot 只有设了环境变量 `ASTRBOT_RELOAD=1` 才监视插件目录；否则运行中新增的插件目录不会被发现，必须走面板「重载」（`POST /api/v1/plugins/reload`）。

### 10.4 与方案的差异清单

1. 不实现 a_bogus 签名 —— 拦页面响应，签名交给页面自己。
2. 不加分段 ASR / 章节 —— 按你定的「ASR + 9 帧」执行。
3. 写操作（点赞/评论）仍为占位，未实现。
4. 新增 `douyin/asr.py`（方案里只写了「用 AstrBot 的 STT」，实际必须自带三档降级）。
5. 知识库落在 `data/plugin_data/astrbot_plugin_douyin_learning_bot/kb.sqlite3`，SQLite 关键词检索，未上向量。

开箱即用的说明书见插件目录 `README.md`。


---

## 11. 追加：分享给好友/群 + 评论 @ 人（v0.2.0 已落地）

第 10.4 节那条「写操作仍为占位」已经不算数了。这一节是新增的两个动作怎么做、为什么这么做、以及现在还差什么。

### 11.1 先查接口，再决定走哪条路

动手前先确认「接口到底存不存在」，用的探针是 `E:\工作区\_probe_dy_social.py`（未登录，页内 fetch，带 `credentials:'include'` 与 `x-secsdk-csrf-token: DOWNGRADE`）：

| 探测 | 结果 | 说明 |
| --- | --- | --- |
| `POST /aweme/v1/web/comment/publish/` | `200` + `{"comment":null,"status_code":8}` | 路径正确，8 = 未登录，只差登录态 |
| `GET /aweme/v1/web/comment/list/` | `200` + `status_code=0`，39 条 | 读接口未登录也能用 |
| `GET /aweme/v1/web/user/profile/self/` | `status_code=8` | 同上，路径正确 |
| `/aweme/v1/web/im/*` | `404 Unsupported path(Janus)` | 私信不在这个前缀下，这是网关名 |
| `POST /aweme/v1/web/commit/item/digg/` | `status_code=4` | 点赞接口在，同样要登录 |

结论：**评论接口能调，私信接口不在 web 前缀下**。

### 11.2 评论的真实契约（从线上 bundle 反查出来的）

`client-entry_2.2b0a1b52.js` 里评论发布函数的原文：

```js
POST /aweme/v1/web/comment/publish
query: app_name=aweme, enter_from=…, previous_page=… + 公共参数
form:  reply_id, reply_to_reply_id, aweme_id, text,
       text_extra=JSON.stringify([...]), comment_send_celltime, comment_video_celltime
```

`text_extra` 在 form 里是**字符串化的 JSON**。@ 好友的元素长这样（从真实评论里扒的，可直接照抄）：

```json
{"start":0,"end":18,"user_id":"101946073155","type":0,
 "hashtag_name":"","hashtag_id":"",
 "sec_uid":"MS4wLjABAAAAJziehvnmGlTa3Y-RCNfpro12G5ucqS6E7wyviTauXLw"}
```

`start/end` 是 `@昵称` 这段文字在正文里的字符下标（`@user4475165723312` 占 18 个字符）。

但这条路径和 `/v1/message/send` 一起被列进了 bd-ticket-guard（`certType=cookie`）名单 —— 也就是发评论要过票据校验层。页面自己的请求层会算好，我们照着点就自动带上。

### 11.3 私信为什么只能点界面

前端 JS 里唯一的 IM 域名是 `imapi.douyin.com`，imsdk 初始化参数（`24914.5ca2f421.js` 原文）：

```js
appKey:"e1bd35ec9db7b8d846de66ed140b1ad9", fpId:9, service:5,
apiUrl:"https://imapi.douyin.com",
frontierUrl:"wss://frontier-im.douyin.com/ws/v2", biz:"douyin_web"
```

发送路径是 `/v1/message/send`，要 appKey 级的 token + 票据头。纯 HTTP 复现等于把 imsdk 重写一遍 —— 不划算。

### 11.4 所以实现方式是「在登录态页面里像人一样点」

| 动作 | 路径 |
| --- | --- |
| 分享 | `/video/<id>` → 点 `[data-e2e="video-share-icon-container"]` → 面板里点「分享给朋友」→ 搜昵称 → 点结果 → 点「发送」 |
| 评论 | `/video/<id>` → 点 `[data-e2e="feed-comment-icon"]` → 焦点进编辑框 → 打字 → 打 `@` 点候选 → 点「发送」 |

两个实测细节决定了写法：

1. `feed-comment-icon` 与 `video-player-share` 是 **0×0** 元素，Playwright 的 `locator.click()` 必然超时 —— 必须 `page.evaluate("document.querySelector(sel).click()")` 这种 JS 直点。
2. 未登录时分享面板只有「登录后分享给朋友 / 登录其他账号 / 复制链接 / 抖音 app 扫码观看」，评论图标点开是登录框（`请输入手机号`/`请输入验证码`）。**所以这两个动作没有登录态根本没法验证选择器**，这是当前唯一的空白点。

### 11.5 代码落点

- 新文件 `douyin/social.py`：`Social` 类，`share(aweme_id, targets)`、`comment(aweme_id, text, mentions)`、`probe(aweme_id, kind)`、`logged_in()`、`_dump()`（失败时存 png+html）。
- `douyin/config.py`：新增 `action_mode / share_targets / mention_targets / comment_template / action_delay_s / max_actions_per_round / action_dump`。
- `douyin/store.py`：新表 `social_action`，唯一索引 `(aweme_id, kind, target)` —— 同一条视频对同一个好友只发一次。
- `main.py`：`/dy share`、`/dy comment`、`/dy do`、`/dy act`、`/dy dom`；`_auto_actions()` 按 `action_mode` 决定「不动作 / 问一句 / 自动做」，`start` 与后台循环都会调用它。
- `_conf_schema.json`：`actions` 组从 2 项扩到 9 项。

### 11.6 三种模式

| `action_mode` | 行为 |
| --- | --- |
| `off`（默认） | 只播报。手动命令仍可用 |
| `ask` | 刷完问「要分享/评论吗」，回 `/dy do` 才执行 |
| `auto` | 直接执行，遵守「每轮最多 N 条」「间隔 N 秒」 |

默认 `off` 是有意的：这两个动作是对**你自己账号**的公开行为，不该在装完插件的第一分钟就自动发生。

### 11.7 还差什么（一条）

登录态。当前 `_dy_profile` 里 33 个 cookie 没有 `sessionid`，所以：

1. 分享 / 评论的选择器是**按公开页面结构 + 常见文案写的**，没在登录态下验证过；
2. 兜底手段已经准备好：任一动作失败会把截图 + HTML 存进 `plugin_data/<插件>/social_dumps/`，`/dy dom share` / `/dy dom comment` 会直接把登录态下的 `data-e2e` 列表、可见 input、`contenteditable`、按钮文案打回来。

也就是说：**你 `/dy login` 扫一次码，我按 `/dy dom` 的输出把选择器钉死，这两个功能就从"能跑"变成"稳定跑"。**

## 12、第一次真机校准之后的修正（v0.2.1）

用户在已登录状态下跑了 `/dy dom share`，拿到三份现场（`probe-share` / `probe-share-friend` / `probe-comment`）。
我没有只看截图结论，而是把三份 HTML 用 lxml 拆开逐个核对，结果推翻了原设计里的两个假设。

### 12.1 分享弹层是"空壳"，点开不渲染

`[data-e2e="video-share-container"]` 确实存在，但整棵子树只有 149 字符：

```html
<div data-e2e="video-share-container">
  <div class="XT9zvsxA isDark W6YK5FzT oTLrVnIA DQFMJFVl"></div>
</div>
```

而且它并不是独立弹层，而是**嵌在** `video-share-icon-container`（分享图标）内部的兄弟节点。
三份 HTML 里「分享给朋友 / 发给朋友 / 私信 / 复制链接」出现次数全是 **0**。
结论：面板内容是 hover/点击之后异步挂载的，`click → 立即 dump` 这个节奏永远抓不到；
原方案里「点开分享图标 → 按文案点『发给朋友』」这条路，在真实页面上是不成立的。

### 12.2 私信面板真实可用，而且结构稳定

三份 HTML 里都有 `[data-e2e="im-dialog"]`（6610 字符），并且**带真实会话**：

```
yezheyue 刚刚 · 嗨～ 我是yezheyue，聊聊天吧
洛克王国pvp · 04/06
大肥鱼Yume：本群
```

也就是说：账号已登录，私信面板可读可点，好友与群都在同一个列表里。摸到的结构：

| 用途 | 选择器 |
| --- | --- |
| 面板根 | `[data-e2e="im-dialog"]` |
| 会话行 | `[data-e2e="conversation-item"]` |
| 会话标题 | `[class*="ConversationItemtitle"]` |
| 面板内搜索框 | `input.semi-input[placeholder="搜索"]`（**必须排除** `[data-e2e="searchbar-input"]`） |
| 关闭按钮 | `svg[class*="closeImPage"]` |
| 入口 | `[data-e2e="im-entry"]` |

→ 所以把**主分享通道改成"走私信面板发链接"**：点消息入口 → 搜昵称/群名 → 点会话 → 输入框发链接。
功能上与抖音原生「分享给朋友」等价（原生也是把视频卡片发进会话），但每一步都有稳定锚点。
原生分享面板保留为 `share_mode=panel` 备选。

### 12.3 一个差点埋掉整轮的误点

首版用「按文字点按钮」找分享面板入口，候选词里带了 `朋友`。而顶部导航本来就有一个「朋友」tab，
于是点击落在了导航上，页面直接跳到朋友信息流——dump 里 `data-e2e` 变成 `slideList / feed-item / feed-active-video`，
一眼就能看出来上下文已经不对了。现在所有按文案点击都**限定在面板容器内**，并且先等容器子树非空。

### 12.4 探针改成"写文件"，不再靠聊天里贴文本

`/dy dom` 现在把结果写成 `plugin_data/<插件>/social_dumps/probe-<时间>.json`（每步成败 + 选择器命中 + 面板状态），
同时额外落 `*.region.*.html`（只存 `im-dialog`、`video-share-container`、`comment-list` 三个关键区域），
聊天里只回一段摘要 + 文件路径。这样校准时直接读文件，不会像这次一样被 1800 字符截断。

### 12.5 评论编辑器仍未在真机确认

三份 HTML 里 `contenteditable`、`textarea` 都是 0 个，评论区只有 `comment-list` / `comment-item`。
说明占位符「善语结善缘，恶言伤人心」那一行要么在虚拟列表首屏之外，要么点开才挂载。
现在的实现是：先把评论列表 `scrollTop = 0`，再点含「善语结善缘」的节点，然后轮询等 `[contenteditable="true"]`，
失败就截图 + 只 dump 评论区。这一条要等下一次 `/dy dom comment` 才能定论。


---

## 13. 分享 / 评论的真机校准结果（2026-10-31，v0.3.0）

这一节全部是一手实测，不是推断。校准方式：用插件自己的浏览器 profile（已登录，68 个 cookie 含 `sessionid`）
起 headful Chromium，1440×960，`navigator.webdriver` 擦除，逐层 dump HTML + 截图，再 lxml 解析。

### 13.1 分享面板：hover 打开，不是 click

| 事实 | 证据 |
| --- | --- |
| 弹层由 **mouseover** 触发 | 鼠标移到 `[data-e2e="video-share-icon-container"]` 中心后 innerText 出现「分享给朋友」；`click` 之后反而 `display:none` |
| 弹层根节点自身 rect = **0×0** | `[data-e2e="video-share-container"]` 的 `getBoundingClientRect()` 宽高都是 0，内容在 fixed 子层；判可见只能看 innerText |
| 行内按钮锚点是 `[data-userid]` | `<div class="xu0OjnsQ wv0hD3qc uiL5gOiW" data-userid="2634023404581211">分享</div>`；旁边 class 全是 CSS Modules 哈希，会随构建变 |
| 成功信号 | 点完 2~3 秒后该行文案变成 **「捎句话」**；私信抽屉里能看到「某某 刚刚 分享[视频]」 |
| 行按钮必须 JS 点 | 真实鼠标移出图标区域 → 弹层立即关闭，点击落到视频上 |
| 弹层搜索框不生效 | `fill("大肥鱼Yume")` / 逐字键入「洛克」都不改变列表，还出现过「没有搜索到朋友」 |

实测 5 个可见行（都是「最近私信过的人」）：大肥鱼Yume `4330304429101319`、yezheyue `2634023404581211`、
洛克王国pvp `7625369585096688164`、Osot-酒保 `111241498169`、卯月的理想世界 `1319007669196573`。

### 13.2 评论：能填能 @，发送会撞短信验证

链路全部打通：点底部输入条 `[class*="comment-input-inner-container"]`（rect 约 868×46 @ y=833）
→ Draft.js 编辑器 `div[contenteditable="true"].public-DraftEditor-content`（757×22）
→ `insert_text` 正文 → 打 `@` + 昵称 → 候选 `.atBox-inner-container` 里点选
→ 编辑器内容变成 `这条讲得清楚，学到了 @小喵佳`。

发送按钮没有文字、class 是哈希，定位法是「编辑器右侧 y±34px、x 在 right-4 ~ right+170 之间的 36×36 图标，
取最右那个」（实测 x=1041, y=838）。

**发送后**：editor 被清空 = 提交成功；随后抖音弹出 `second-verify-mask` /
`second-verify-panel` / `uc-ui-verify_sms-verify` —— **手机短信二次验证**。这一次必须人工过一次。

### 13.3 私信抽屉：能开，但搜索不过滤

点 `[data-e2e="im-entry"]`（rect 32×44 @ (1302,6)）→ `[data-e2e="im-dialog"]` 变 336×892 @ (1092,108)，
先显示「私信加载中...」约 1 秒；搜索框 227×32 @ (1144,171)；会话行 324×67 @ (1093,217)；
会话内输入框 `div[contenteditable="true"].messageEditorinputArea`（`data-placeholder="发送消息"`）146×44，
**没有「发送」文字按钮，靠 Enter**。
问题同弹层：搜索框在本版不过滤，只能命中列表里已有的会话 → 因此把它降级为兜底通道。

### 13.4 踩过的三个坑（都会导致"看起来没 bug、实际不生效"）

1. **SVG 没有 `.click()`** → `Page.evaluate: TypeError: e.click is not a function`；要么改用 `dispatchEvent(MouseEvent)`，要么改用真实鼠标。
2. **同名方法重复定义**：重写时旧块没删干净，`comment` / `_share_by_im` / `_open_im` / `_im_open` 各有两个定义，
   Python 取后一个 → 修好的版本从未生效。排查手法：扫 `def ` 行号找重复，或 `inspect.getsource` 打印实际执行的源码。
3. **按行号删块不可靠**：cut 完之后 `comment` 里混进了 `_share_by_im` 的结尾（`NameError: name 'done' is not defined`），
   最后是整文件重写才干净。

### 13.5 与 §12 的差异

- §12 说「默认走 im」，实测后**改成默认 panel**：panel 真的能把视频分享出去，im 的搜索不生效。
- §12 担心「弹层是异步渲染所以脆」，实际是「hover 才渲染 + 行按钮要 JS 点 + 等 2~3 秒」，可控。
- §12 说评论编辑器"未真机确认"，现在确认了，并额外发现短信验证这一关。

## 14. 配置面板化与白名单（v0.4.0）

### 14.1 为什么要改

v0.3.0 之前，写操作的参数全靠命令临时给：`/dy share 老张,兄弟群`、`/dy comment 这条 @小王`。
用起来有三个问题：

1. 每次都要手打名字，抖音昵称还带生僻字，容易打错；
2. 名字写错时是「点开面板搜不到人 → 静默失败」，看不出是自己写错了；
3. 最要命的：**命令里能写任意名字**，意味着任何能发消息的人都能让机器人去分享给任意陌生人。

第 3 条是设计缺陷，不是使用习惯问题。所以把参数搬进配置面板，并加白名单强制。

### 14.2 配置项

`actions` 组现在 12 项，新增/改动三处：

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `share_targets` | `template_list` | `[]` | 分享白名单，每行 `{nick, enabled, note}` |
| `mention_targets` | `template_list` | `[]` | @ 白名单，同上 |
| `enforce_allowlist` | `bool` | `true` | 强制白名单，关掉就退回「谁都能发」 |

`template_list` 的形状照 `astrbot_plugin_kwtrigger` 的 `/rules` 抄的：
`{"type":"template_list","default":[],"templates":{"target":{"name":"对象","items":{...}}}}`。
核心侧 `astrbot_config.py:183` 对 `template_list` 只做 `conf[k] = default`，不递归解析 items，
所以默认值必须是 list，面板保存后会变成 `list[dict]`。

### 14.3 解析层（`douyin/config.py`）

`_as_list()` 现在吃三种输入，兼容老配置：

- 逗号/空格分隔的字符串（v0.3 之前的写法）；
- `list[str]`；
- `list[dict]`（面板 template_list 产物）——按 `_DICT_NAME_KEYS = ("nick","name","value","target","who","text","昵称","名字")`
  取第一个非空字段，且 `enabled is False` 的行直接跳过。

校验在 `Settings.check_targets(names, kind="share"|"mention")`：

```
enforce_allowlist=False → 全放行
名单为空                → 全拒（宁可不动手）
否则按「a == name or a in name or name in a」匹配
```

用「包含」而不是「全等」是有意的：面板里分享对象只能按片段匹配昵称（真机实测弹层搜索框不生效，
只能对着已列出的最近联系人对名字），两道逻辑保持同一套匹配规则，行为才可预期。

### 14.4 命令层（`main.py`）

- `/dy share` 无参 → 用 `share_allowlist`；带参 → 先过 `check_targets`，被拒的单独回一行说明，
  全被拒就直接返回、不开浏览器。
- `/dy comment` 无参 → 用 `comment_template` + `mention_allowlist`；被拒的 @ 会在结果末尾注明
  「xx 不在 @ 白名单里，没 @ 上」。
- 自动动作（`_do_actions`）与 `ask` 模式攒的动作，走的是同一条 `check_targets`，不存在绕过路径。
- 新增 `/dy cfg`（别名 `/dy 配置`、`/dy 参数`、`/dy 白名单`）：打印模式、白名单、模板、节奏、选片参数。
  存在的意义是「确认面板改的到底生效了没有」——面板里改完要 `/dy reload`，而 reload 是否读到新值，
  看这一条最快。

### 14.5 验证

- `_as_list` 三种输入 + `enabled` 过滤：`['A','C']`（B 被 enabled=false 跳过）✓
- `check_targets` 六种组合（开/关白名单、空名单、命中、被拒）全部符合预期 ✓
- `_test_dy_cmd.py` 17 条命令全绿，另加白名单混合场景：
  `/dy share 老张,路人` → 路人被拒、老张照发；`/dy comment 这条 @小王`（小王在白名单）→ 正常 @。
- `_test_dy_actions.py`：`auto` 模式 3 个动作（share×2 + comment×1）落库、第二轮全去重 ✓
- 热重载：`重载成功。 / activated=True version=0.4.0 author=YG / 失败插件：（无）`

### 14.6 面板协议的硬要求：`__template_key`

用面板接口实测踩到的：往 `template_list` 写行时，**每行必须带 `__template_key`**（值 = `templates` 里的键名，本例是 `target`），
否则 `PUT /api/v1/plugins/config` 直接 400：

```
格式校验未通过: ['缺少模板选择 actions.share_targets[0]: 需要 __template_key']
```

面板前端会自动补这个字段，所以正常在网页上点不会遇到；但如果用脚本改配置（就像我这次），得自己带上。
落盘后的行长这样：

```json
{"__template_key": "target", "nick": "大肥鱼Yume", "enabled": true, "note": "账号自己，先拿他试"}
```

解析层不受影响：`_DICT_NAME_KEYS` 里第一个命中的是 `nick`，`__template_key` 会被忽略。

另外一个坑：**插件配置文件一旦生成过，就会覆盖 schema 的新默认值**。
这次升到 0.4.0 后，`share_mode` 在文件里还留着旧的 `"im"`、`share_targets` 还是空字符串 `""`。
空字符串塞进 `template_list` 会让面板渲染异常，所以顺手把落盘文件里的两项改成 `[]`。
换句话说：**改了 schema 默认值，记得回看一眼 `core/data/config/<插件>_config.json`**。

### 14.6 还剩什么

- 白名单目前只覆盖「分享对象」和「@ 对象」。评论正文仍由模板生成，没做敏感词过滤
  （README 里已写明：正文发出去就公开，别用会翻车的模板）。
- 面板里没有「谁在什么条件下可以触发」的权限控制。任何能给机器人发消息的人都能发 `/dy share`，
  只是被白名单约束了目标。真要收口，得在 `dy()` 入口加管理员判断（`event.is_admin()`）。

---

## 15. 后台化与登录通知（v0.5.0）

目标（用户原话）：「让 astrbot 刷视频时在后台进行，需要登录就通知我」。

### 15.1 先把「后台」拆成三件事

| 问题 | 具体表现 | 这一版怎么办 |
|---|---|---|
| 命令阻塞 | `/dy start` 要等 40s~3min 才回话，期间会话像卡住 | 提交到队列立刻返回，结果由 notifier 推送 |
| 窗口碍事 | headless 被风控（验证码中间页），headful 又抢焦点弹窗 | 窗口挪到屏幕外 + 关后台节流 |
| 登录过期没人知道 | 后台循环静默失败，用户不知道为什么没动静 | 主动发二维码 + 回执 + 超时告知 |

### 15.2 队列设计（`douyin/jobs.py`）

- 单 worker 串行：Playwright persistent context 不能双开，手动命令与后台循环必须互斥；
- `Job` 带 `stage`（排队中/取流/解析与筛选/下载视频/抽帧·转写·视觉理解/等扫码登录），
  `/dy status` 与 `/dy jobs` 都读它；
- `submit()` 在积压 ≥ 8 时返回 `None`，调用方据此告诉用户「等它们跑完再提」；
- notifier 在任务结束后调用一次，成功推简报、失败推原因，异常不外溢。

`_run_job` 的顺序：总开关检查 → 登录态检查（`_ensure_login`）→ `pipeline.run_once`
→ 写操作（`_auto_actions`）。`pipeline.on_stage` 用 lambda 把阶段写回当前 job。

### 15.3 静默窗口的实测依据

启动参数追加：

```
--window-position=-32000,-32000
--window-size=1280,900
--disable-background-timer-throttling
--disable-renderer-backgrounding
--disable-backgrounding-occluded-windows
```

后三条是关键：Chrome 对「不可见窗口」会降频定时器，抖音的推荐流靠页面自己滚动触发请求，
一旦降频，取流会明显变慢甚至拿不到。实测静默窗口下 **15.4s / 60 条 aweme**（39 条带直链），
与可见窗口一致 → 藏起来不损失成功率。

### 15.4 二维码这条路踩的六个坑（都已修）

1. **骨架屏**：`goto` 完立刻截图只有灰块 → 先等真实图片节点（`img[src*=douyinpic]`）；
2. **DOM 里按 class 抓不到**：不是 canvas，也不是 `img[src*=qrcode]`（抖音的 class 是哈希）；
3. **CDP `clip` 漏层**：把 clip 交给 `page.screenshot(clip=...)` 截出来只有中间抖音 logo，
   二维码黑块没合成进去 → 改成整屏截图 + PIL 本地裁；
4. **还没画好**：截图时二维码区域仍是白底 → 按暗像素轮询（白底/logo `<5%`，画好后约 30%）；
5. **半登录态根本没有二维码**（v0.5.1 才发现）：账号掉登录但 cookie 还在时，抖音给的是
   **一键登录面板**——一张「登录后免费畅享高清视频」横幅 + 用户头像 + 「一键登录」按钮，
   二维码在这一屏里不存在。必须先点「登录其他账号」，才会切到「扫码登录 / 验证码登录 / 密码登录」
   那一屏。当时的表现是：锚点找不到 → 落到兜底框 → 截出一张 190×190 的白图，暗像素 0.0%。
6. **方框启发式会抓错图**（v0.5.2）：改成「枚举 90~330px 的方形区域 → 量黑占比 + 黑白切换密度」
   之后，第一版挑中的是首页右下角「抖音精选 / 下载 APP」的深色卡片（暗像素 100%），
   第二版挑中的是**用户头像**（160×160，切换密度 0.048 刚好过线）。
   最后发现根本不用猜：**二维码就是一个 `<img src="data:image/png;base64,...">`**，
   直接读 `src` 解 base64 落盘即可（512×512 原图，连截图都省了）。

所以现在的判定顺序是：读 `data:image` 方图（110~360px，`_qr_score` 校验黑占比 0.1~0.7、
切换密度 ≥0.02）→ 拿不到才退回方框候选 → 还拿不到就**不发白图**，改发文字指引。

`_qr_score(img) -> (黑占比, 切换密度)` 的实测基准：二维码 (0.33, 0.072)、深色卡片 (1.0, 0.0)、
头像 (0.22, 0.048)。切换密度 = 二值化后相邻像素不同色的比例，纯色块恒为 0，这是区分
「一格一格的码」和「一块深色卡」最省事的指标。

### 15.5 登录通知的交互

```
需要登录抖音才能继续刷。
二维码在下面这张图里，用抖音 App 扫它（6 分钟内有效）。
扫完不用回话，我会自己发现并继续；超时这轮就先跳过，下轮再叫你。
[图片]
→ 登录成功，继续刷这一轮。
→ [简报]
```

- 判定用 cookie：`sessionid` / `sessionid_ss`；
- 冷却 10 分钟，避免每轮都轰炸；
- `notify_login=false` 时不打扰，任务直接失败并在状态里留痕；
- `/dy login` 手动入口也改成发二维码，不再依赖「去看屏幕外的窗口」。

### 15.6 闲置回收

`notify.idle_close_min`（默认 30）—— 两轮之间的间隔超过这个值就先 `browser.close()`，
下轮自动重开（多花 3~5 秒换几百 MB 内存，间隔 90 分钟的后台循环尤其值得）。

### 15.7 验证

| 测试 | 覆盖 | 结果 |
|---|---|---|
| `_test_dy_jobs.py` | 串行顺序 1→2→3、阶段改写、通知、失败留痕、积压上限 | 全绿 |
| `_test_dy_bg.py` | 没登录→发二维码→扫码→继续→推简报；失败推原因；关掉通知不打扰；status 含后台行 | 4/4 通过 |
| `_test_dy_cmd.py` | 17+ 条命令（新增 `/dy start` 入队、`/dy jobs`） | 全绿 |
| `_test_dy_quiet.py` | 真机：静默窗口取流 60 条 / 15.4s；二维码截图 | 通过 |
| 真机二维码 | `qr-test6.png` 190×190，暗像素 28.8%，肉眼可扫 | 通过 |

### 15.8 一个运维坑：重载后插件可能被留在 disabled

`/api/v1/plugins/reload` 之后出现过 `activated=false`（日志：`Plugin ... is disabled.`），
原因是插件路径进了 `inactivated_plugins`。启用接口是 `PATCH /api/v1/plugins/enabled`，
body 字段是 **`plugin_id`**（不是 `name`），写成 `name` 会 422：

```json
{"plugin_id": "astrbot_plugin_douyin_learning_bot", "enabled": true}
```

`_dy_reload.py` 已带上「重载后自动确保启用」。

### 15.9 还剩什么

- 评论发送仍会撞短信验证（§13.2），后台化没改变这一点；
- 阶段推送（`stage_notify`）默认关，需要盯进度就开；
- 队列不持久化：AstrBot 重启后未跑完的任务丢失（重新 `/dy start` 即可）。

## 16. 人机验证：从这轮失败改成等你（v0.6.0）

### 16.1 触发场景（实测）

滑块不是随机出现的，它有偏好：

| 场景 | 触发概率 | 证据 |
|---|---|---|
| 连续滚 feed（每屏 1 次，20 秒 4~10 屏） | 低 | 多次 40 屏连滚未触发 |
| 页面内调 /aweme/v1/web/general/search/single/（按昵称搜人） | **高** | 第一次搜 uid 就弹了，用户当场看到 |
| 同一接口短时间连发（评论 publish） | 中 | 不弹滑块，但回 200 + 空 body（限流） |

所以设计上做两层：**能走 UI 就不走搜索接口**（@ 人改成编辑器里打 @ + 点候选，不再搜 uid），**弹了就搬窗口叫人**。

### 16.2 三个环节

| 环节 | 实现 | 为什么这么做 |
|---|---|---|
| 检测 | rowser.captcha_visible() — CAPTCHA_JS 同时认容器（#captcha_container / [class*=captcha_verify] / [class*=verify-wrap]，宽>60 高>40）和文案（「拖动滑块」「请完成安全验证」等，宽>40 高>10 且在视口内） | 抖音的验证层有时是纯 canvas + 文案，只认 class 会漏；只认文案会把正文里出现的「安全验证」误判，所以加了尺寸门 |
| 搬窗口 | CDP Browser.getWindowForTarget → Browser.setWindowBounds({left:80, top:60, width:1280, height:900, windowState:normal}) + page.bring_to_front() | 静默窗口在 (-32000,-32000)，不搬回来人连滑块都看不见。Playwright 没有公开的窗口移动 API，只能走 CDP |
| 等 + 重试 | wait_captcha_clear(timeout_s=verify_wait_min*60, poll_s=2)，返回 True 后 _run_job 自动重跑这一轮（ttempts = 2 if verify_retry else 1） | 验证是临时状态，重试通常就过；不重试等于白等一轮 |

### 16.3 异常传播链

HumanVerifyNeeded(RuntimeError) 从 rowser.collect() / social._captcha() 抛出 → pipeline 的 except HumanVerifyNeeded: raise 必须放在 except DouyinBlocked **之前**（否则被吞成普通失败）→ main._run_job 捕获 → _handle_verify() → 重试或记失败。_auto_actions 里也要 except HumanVerifyNeeded: raise，因为那里有个 except Exception 兜底会把验证吞掉。

### 16.4 验证记录

- 桩测试 _test_dy_verify.py 三个用例：正常重试链路（搬窗口 1 次 / 等 1 次 / 跑 2 次 / 出简报）；等超时（「这轮没成：人机验证等了 1 分钟没人处理」，不重试）；关掉 erify_retry（不搬窗口、不重试）。
- 真机 _test_dy_verify_live.py：正常页 captcha_visible=False；show_window=True 且 CDP 回读 ounds={left:80, top:60, width:1280, height:882, windowState:normal}。
- 接口评论限流对照实验：同页面 DIGG_FN 正常、comment/list 正常（15050 字节），只有 comment/publish 回空 —— 定位到「只有 publish 被限流，不是登录态或 csrf 问题」，间隔 35 秒重发成功。

### 16.5 退避重试的写法

`python
tries = 1 + settings.comment_retry      # 默认 3 次
gap = settings.comment_retry_s          # 默认 20 秒
for i in range(tries):
    res = await page.evaluate(self.PUBLISH_FN, [aweme_id, text, extra])
    if res.get(code) == 0 and res.get(cid):
        return True, res[cid], "
    empty = res.get(status) == 200 and not res.get(raw)
    if empty and i + 1 < tries:
        await page.wait_for_timeout(gap * 1000)
        gap = min(90, gap * 2)          # 20 → 40 → 封顶 90
        continue
    break
`

关键判断是「200 + 空 body」——它既不是成功也不是明确失败，抖音用这个表达「你太快了」。把它当失败会让用户以为评论功能坏了。

### 16.6 还剩什么

- **分享送达无法自证**：面板通道走到底只有「捎句话」，接口层没有「对方已读/已收」回执。要么接受「已发出」的说法，要么拿一个自己的小号当接收方做一次目视核对。
- **@ 人依赖界面**：候选卡片的位置/加载时机是前端行为，抖音改版就得重新校准（/dy dom comment 还是那个校准入口）。
- **短信二次验证无法自动**：真弹出来只能人工过一次，插件只能把「弹了什么」如实告诉你。

## 17. 换思路：把号分开（多开 profile），而不是跟验证码硬碰（v0.6.2）

### 17.1 弹验证的真实来源

实测下来验证不是均匀分布的，是跟着行为走的：

| 行为 | 弹验证 | 说明 |
|---|---|---|
| 滚 feed 取流 | 很少 | 40 屏连滚未触发；这是刷流主循环，可以放心跑 |
| `/aweme/v1/web/general/search/single/`（按昵称搜人） | **几乎必弹** | 一次就中；已改成走界面 @ 人，不再用这个接口 |
| 连续写操作（评论 publish 连发） | 不弹滑块，但限流 | 回 200 + 空 body；已用退避重试兜住 |
| 每次换新 profile / 新指纹 | 中 | 新指纹前几次访问更容易被要求验证 |

结论：**主循环本身不脏**，脏的是搜索接口、以及被识别成新设备。所以方案不是换工具，而是「把身份固定下来 + 别碰搜索」。

### 17.2 已落地的多开

「一个 profile 目录 = 一个抖音号」，命令是 `/dy account` / `/dy account new 名字` / `/dy account 名字`。意义：

- 主号不进自动化，验证或限制都落在小号身上；
- 小号这份 profile 的 cookie + 设备指纹长期稳定，**扫码一次就一直在**——反复登录本身最容易判异常；
- 两个号各自独立，不互相顶掉（同一账号在别处登录会挤掉 web 端 sessionid，这也是「突然掉登录」的常见原因）。

### 17.3 安卓沙箱 / 模拟器这条路（评估过，本机暂无）

如果「沙箱多开个抖音」是指安卓模拟器里跑 APK，那驱动方式要整体换：

| 维度 | 现在：web + Playwright | 安卓模拟器 + uiautomator2 |
|---|---|---|
| 本机现状 | 已跑通 | **没有**：`adb` / `scrcpy` / 模拟器实测全无 |
| 登录持久性 | profile 存 cookie，扫码一次 | 装好 APK 登录一次，人设更稳 |
| 风控强度 | 中（web 端查指纹） | 低（但查 root / 模拟器痕迹） |
| 取流方式 | 拦 `/aweme/v2/web/module/feed/` 的 JSON，20 秒 55 条 | 只能滚 UI + dump 节点，慢一个量级 |
| 写操作 | 接口直发，评论能拿到 cid 回执 | 全靠点坐标，拿不到 cid |
| 工程量 | 已完成 | 大：装模拟器 + APK + uiautomator2 + 反检测 + 重写两层 |

建议顺序：先用多开小号跑一周，看验证还剩多少；真的还频繁，再评估模拟器（那时收益是「登录更稳」，代价是取流从 JSON 退化成 UI 滚动）。

### 17.4 实现要点

```
plugin_data/<插件>/
├── browser_profile/        # 升级前的老 profile，自动认作 default，不搬家
├── profiles/
│   └── <名字>/             # 每个号一份，cookie + 指纹都在里面
└── state.json              # {"active_profile": "..."}
```

- 查登录态不起浏览器：把 `Default/Network/Cookies` 复制到临时目录，再 sqlite 查 `sessionid`（直接开会和运行中的浏览器抢锁）。
- 切号必须重建浏览器对象：`DouyinBrowser.close()` → 用新目录重建 → `pipeline` / `social` 一起换，否则 persistent context 还指着老路径。
- 老目录兼容：`_profile_path("default")` 先看有没有 `browser_profile`，有就用它——升级不该让人重新扫码。

### 17.5 切号时自动开窗（v0.6.3）

多开本身只解决了「一份 cookie 一份环境」，但切到空 profile 之后人还得扫码，而
`browser.quiet_window=true` 会把窗口放在 `(-32000,-32000)` —— 用户说「我打不开那个页面扫码」就是这么来的。
所以切号必须临时打破「藏窗口」这条规矩。

流程（`_prompt_login_window`）：

1. `settings.quiet_window = False`（同一份 Settings 对象被 browser 持有，改了就生效）
2. `page.goto("https://www.douyin.com/")` —— 先落地到首页，不要停在 `about:blank`
3. CDP `Browser.getWindowForTarget` → `Browser.setWindowBounds({left:80, top:60, width:1280, height:900})` + `bring_to_front()`
4. 推一条「窗口已经开到你桌面上了……我盯 90 秒」
5. 每 5 秒 `logged_in(launch=False)`；登上 → 推「登录成功」；超时 → 推「想再试」
6. `finally`：还原 `quiet_window` 并 `browser.close()`（窗口消失，回到后台形态）

顺带修掉的判定漏洞：`logged_in()` 原来只看 `sessionid/sessionid_ss` 在不在 cookie 里。
session 过期后 cookie 还在，于是这个函数会一直说「已登录」，直到点赞/评论回 `code 8` 才暴露。
现在改为调 `/aweme/v1/web/user/profile/self/`，`status_code == 0` 且 `nickname` 非空才算登录；
拿不到回包（网络异常）按未登录处理，宁可多开一次窗，也不要带着坏 session 空跑一轮。

实测基准（用全新空 profile 走了一遍，确认「游客 ≠ 已登录」）：

| 状态 | cookie 数 | 有 sessionid | self 回包 |
|---|---|---|---|
| 游客（全新 profile 逛首页） | 9 | 否 | `code: 8` |
| 已登录 | 60+ | 是 | `code: 0` + `nick: 大肥鱼Yume` |

也就是说 cookie 判定在「游客」这一侧不会误判，真正会误判的是「cookie 还在但 session 早过期」。

## 18. 写操作的最终形态（v0.6.4）

### 18.1 三条路，各管一件事

| 动作 | 走什么 | 回执 | 可回滚 |
|---|---|---|---|
| 点赞/取消赞 | 页面内 `POST /aweme/v1/web/commit/item/digg/` | `is_digg`（**操作前**状态） | 是（再调一次 type=0） |
| 评论（不带 @） | 接口 `comment/publish/`，`text_extra=[]` | `cid` | 是（`comment/delete/`） |
| 评论（带 @） | 同一接口，`text_extra=[{start,end,user_id}]` | `cid` | 是 |
| 分享 | 面板 hover → 点好友行 | 行文案变「捎句话」 | 否（分享没有撤回接口） |

### 18.2 @ 的关键：uid 从哪儿来

`text_extra` 要 `user_id`，我们没有好友的 uid 库。实测发现**分享面板里每行都带 `data-userid`**，
那个值就是 `user_id`（例：`yezheyue → 2634023404581211`）。于是评论 @ 和分享共用同一份「好友行」数据：

```js
// 面板行：{uid, name}
Array.from(document.querySelector(panel).querySelectorAll('[data-userid]'))
  .map((a) => ({uid: a.getAttribute('data-userid'),
                name: a.parentElement.innerText.replace(/\s+/g,' ').trim()}))
```

`sec_uid` 不用自己找：发布后回读，服务端把它填上了。

### 18.3 回读要靠 insert_ids

刚发的评论不在 `comment/list/` 第一页（那是按热度排的）。要么加 `insert_ids=<cid>` 强制带回来，要么别指望翻页找到。
加了这个参数后回读稳定：

```
text:      接口@回读测试 请忽略 @yezheyue
text_extra: [{start:12,end:21,user_id:"2634023404581211",sec_uid:"MS4wLjABAAAA7DzmY0GC..."}]
```

### 18.4 「捎句话」到底是什么

分享面板里点完好友行，那一行的按钮文案会从「分享」变成「捎句话」，2~3 秒后才更新。
第一版把它当成功信号是对的，但当时理由写错了——它不是「发完了」，而是「已经分享，可以再补一句话」。
验证方式是去私信里看：会话列表首行变成 `yezheyue 刚刚 分享[视频]`，那就是真到了。

### 18.5 实测记录（v0.6.4 全量跑）

| 项 | 结果 |
|---|---|
| 登录态 | ✅ 已登录 |
| 取流 | ✅ 40 条 / 23.6s |
| 解析归一化 | ✅ 40 条 |
| 筛选打分 | ✅ 36 条过筛 |
| 下载 | ✅ 12.4 MB / 7.6s |
| 抽帧 / 拼图 | ✅ 4 帧 / 177 KB |
| 点赞 → 复原 | ✅ 未赞→已赞→未赞 |
| 评论（含 @） | ✅ `cid=7704027710465180435`，@ 已带上 |
| 删除评论 | ✅ 已删除（同一个 cid） |
| 分享给 yezheyue | ✅ 面板行显示「捎句话」 |

13/13 全过。

## 19. 选片收紧：从「什么都学」到「挑着学」（v0.6.5）

### 19.1 当时的问题

`min_like=500` 是唯一的门。实测 70 条样本过 67 条、58 条样本过 52 条——**等于没筛**。
推荐流里短剧、漫宠、二创占大头，结果就是学习链路天天在嚼这些。

### 19.2 两道门怎么加的

```python
# douyin/scoring.py
hits_know = _hits(blob, KNOWLEDGE_HINTS)          # 只算真知识词
if hits_know < settings.min_knowledge_hits:
    return BLOCKED_SCORE                          # 第 1 道：一个知识词都没有，不学
...
# douyin/scoring.py::rank
scored = [(s, i) for s, i in scored if s > BLOCKED_SCORE and s >= floor]   # 第 2 道：分数下限
```

`KNOWLEDGE_HINTS` 补了一批技术实词：`开源 / 论文 / 算法 / 架构 / 模型 / 大模型 / 推理 / 训练 / 参数 / 评测 / 干货 / 入门 / 进阶 / 踩坑 / 工具 / 插件 / 部署 / 配置`。

补的原因是一次误杀：`突发！DeepSeek开源super-agent！超级Agent要来了！` 被挡了——
它一个"科普腔"的词都没写，但显然是知识内容。教训是**别只认"科普/讲解"这种腔调词**。

### 19.3 把 `AI` 踢出知识词

`AI` 原本在 `KNOWLEDGE_HINTS` 里。问题是它出现在几乎每条 AI 题材娱乐视频标签中：

- `向大肥鱼求婚会发生什么！！ #DeepSeek #AI #拟人 #可爱` → 8.51 分，过了
- `这AI说话咋还老变～ #Deepseek` → 8.32 分，过了

所以 `AI` 现在只在 `interest_keywords` 里做加分项，不再算"知识词"。这一改，这两条立刻被挡。

### 19.4 阈值是量出来的，不是拍的

同一批样本离线扫了 6 组组合（`_calib_score.py` / `_calib_blocked.py`）：

| min_score | min_knowledge_hits | 过筛 | 观察 |
|---|---|---|---|
| 0 | 0 | 63/66 | 旧逻辑，等于没筛 |
| 2.5 | 1 | 28/58 | 标题明显变干净 |
| 4.0 | 1 | 31/55 | 留下的都是「AI 换装原理 / 宋朝历史 / 冷知识」这类 |
| 4.0 | 2 | 14/58 | 只留硬货，偏严 |
| 6.5 | 2 | 14/58 | 再抬分几乎不再筛（分数扎堆在 11~13） |

结论：**`min_score` 在 2.5~6.5 之间几乎不咬人**（因为知识项封顶 9 分 + 热度约 4 分），
真正起作用的是 `min_knowledge_hits`。所以默认取 `min_knowledge_hits=1`、`min_score=4`，
`min_score` 留作最后那一下小幅修剪。

### 19.5 `/dy calib` 自检

切阈值不该靠猜。`/dy calib [条数]` 抓一批样本，按**当前生效配置**算一遍，回话给：
过筛率、门槛一行、会学的 8 条（分数/时长/标题）；一条没过时列出"差一点"的几条；
过筛率 <8% 提示放宽、>75% 提示收紧。真机跑（48 条样本、25% 过筛）见 README。

### 19.6 踩到的坑

`/dy calib` 第一次真机跑报 `NameError: name 'parse_aweme' is not defined`。
原因是 `main.py` 有两套导入：正常被 AstrBot 加载走 `from .douyin...`，
被当顶层模块加载（测试脚本 `import main`）走 `except ImportError` 那段兜底。
我只往第一套里加了 `from .douyin.feed import parse_aweme` 和 `from .douyin import scoring`，
兜底那套没加——测试路径就炸了。两套必须同步加。

## 20. 换内核与 cookie 真相（v0.8.0）

### 20.1 为什么要能换内核

自带 chromium 的 UA 是 `Chrome/156`，本机 Edge 是 `Edg/143` —— 谁更「像人」不好判断，
但用户机器上有 Edge 时，用它至少和日常浏览环境一致（同一套字体、编码、图形栈）。
代价是 profile 不能混用，见 20.2。

### 20.2 profile 必须按内核分目录（实测）

| 组合 | 结果 |
|---|---|
| 空目录 + Edge | 正常，`title='抖音-记录美好生活'` |
| chromium 写过的目录 + Edge | `TargetClosedError`，`exitCode=2147483651` |
| 同上，删 `Local State` | 仍然崩 |
| 同上，删 `Preferences` | 仍然崩 |
| 同上，两个都删 | 仍然崩 |

崩溃日志里的关键行：

```
content_settings_pref.cc:319 Invalid pattern strings: chrome://omnibox-popup.top-chrome/,*
```

结论：`profiles/<号>/` 下再分一层内核名。切换内核时把老目录的 cookie 搬一次
（`raw_cookies()` → `add_cookies()`），实测 57 个 cookie、3.1 秒。

### 20.3 cookie 的三条路，只有一条通

| 路子 | 结论 |
|---|---|
| 解 Edge/Chrome 的 cookie 库 | `v10` 能解（DPAPI + AESGCM），`v20` 解不开 —— 密钥交给浏览器进程了 |
| 浏览器运行时读库 | 库被独占，`WinError 32`；`mode=ro`/`immutable=1` 都是 unable to open |
| 插件自己的 profile | 唯一稳的：扫一次码，之后靠心跳和到期检查续命 |

所以「实时自动拿 cookie」不是偷，是**养**：`cookie_keepalive_min` 定时访问首页，
`sid_guard` 快到期就叫人。

### 20.4 sid_guard 的真实格式

```
0170c8569213f5fde30c56729526a94c|1793743872|5184000|Sat, 02-Jan-2027 22:11:12 GMT
      hash                        | 签发时间  | 有效期 | 过期时刻
```

第 2 段是**签发时间**。按它当过期时间算，刚登录的会话会被判成已过期（我们踩过）。
第 4 段才是过期时刻，`%a, %d-%b-%Y %H:%M:%S GMT`。

### 20.5 会话作废时的样子

- cookie 里 `sessionid` 还在（32 字节）
- `/user/profile/self/` 还能回 `{'code': 0, 'nick': '大肥鱼Yume'}`
- 但 `POST /aweme/v1/web/comment/publish/` 回 `{"comment":null,"status_code":8}`
- 点赞同样 `status_code: 4`

重新扫码后立刻恢复：同一条视频 `publish` 回 `code 0` 并给 `cid`。

### 20.6 打不开的视频

feed 里挑到的卡片不都能开。`/video/<id>` 落到 `data-e2e="error-page"` 时：
`document.querySelector('video')` 可能还在（占位），但 `videoWidth == 0`，
分享图标、评论框、播放器控件全没有。判定用「error-page 且 videoWidth 为 0」。

处理：`/video/<id>` → `/discover?modal_id=<id>` → 再来一次 `/video/<id>`，
三次都不行就跳过这条（上层换下一条）。

### 20.7 uid 缓存

`@人` 的 `text_extra` 只需要 `user_id`，而 `user_id` 只能从分享面板的行里刮
（`data-userid`）。面板要在详情页 hover 才出来，实测有时四轮都不出。

于是刮到就存 `work/uid_cache.json`：

```json
{"yezheyue": "2634023404581211", "大肥鱼Yume": "4330304429101319"}
```

之后发评论直接读缓存，**完全不用开视频页**（实测从首页直接发，回 cid 并可删）。

### 20.8 二次验证也算人机验证

界面发评论被拦时页面会出现 `el:second-verify-mask` / `el:second-verify-panel`。
这跟滑块一样要人点一下，所以现在直接抛 `HumanVerifyNeeded`，走「搬窗口 + 叫你」，
不再只回一句「已发送（可能被拦）」。

### 20.9 实测记录（Edge 内核，真实 profile）

| 项 | 结果 |
|---|---|
| 登录态 | `True`，身份 `大肥鱼Yume` |
| 取流 | 69 条 / 15.2s |
| 下载 | 17.0 MB / 48.0s |
| 抽帧 + 拼图 | 4 帧 / 346 KB |
| 点赞 → 取消 | `未赞 → 已赞` → `已赞 → 未赞` |
| @ 评论（缓存路径） | `已发布（cid=7704067476850328335）`，@ 已带上，随后删除成功 |
| 分享 | 面板行按钮显示「捎句话」（已分享，可再补一句） |

## 21. 窗口前台化（v0.8.2）

### 21.1 问题

`quiet_window` 把窗口放在 `(-32000,-32000)`，后台跑是不抢焦点了，
但**人要动手时找不到窗口**：扫码、拖滑块、看页面报错全做不了。
`/dy login` 早期只发一张截图——而截图里的二维码约 1 分钟就过期，扫不及。

### 21.2 三条路径

| 触发 | 动作 | 配置 |
|---|---|---|
| 需要扫码登录 | `show_window()` + 置顶，然后才截图发图 | `browser.window_on_login` |
| 撞人机验证 | 同上，文案说「去窗口里拖一下」 | `notify.verify_wait_min` |
| 用户主动 | `/dy show` | 无 |

### 21.3 置顶为什么要 Win32

CDP 的 `Browser.setWindowBounds` 只能改位置/尺寸/最小化状态，
**没有 z-order / always-on-top**。浏览器被终端或编辑器挡住时，挪了等于没挪。
所以补了 `focus_window()`：`EnumWindows` → 按 `GetWindowThreadProcessId` +
`QueryFullProcessImageNameW` 认进程（`msedge.exe` / `chrome.exe`）→ 标题含「抖音」
→ `ShowWindow(SW_RESTORE)` → `SetWindowPos(HWND_TOPMOST)` → `SetWindowPos(HWND_NOTOPMOST)`
→ `SetForegroundWindow`。两步 SetWindowPos 的用处：抢到焦点，但不永久钉在最上层。

### 21.4 实测

```
起完了，屏外窗口坐标： (-26214, -26214, 1554, 882)
show_window = True
摆回来之后的坐标：   (80, 60, 1280, 882)
focus_window 命中：  抖音-记录美好生活 - 个人 - Microsoft Edge
```

（用干净 profile 测的：线上 profile 被 AstrBot 里的插件进程占着，Playwright 起第二个会
撞「请在已运行的浏览器会话中打开」然后退回 chromium，窗口机制本身跟登录态无关。）

### 21.5 剩下的坑

- 多显示器、缩放不是 100% 时 `window_rect()` 拿到的是物理像素，跟肉眼位置略有出入
- 窗口被用户手动最小化后，`show_window` 能还原；但如果是被 `moveWindow` 到别的工作区就未必
- 浏览器进程名不在 `msedge.exe` / `chrome.exe` 里（比如 chromium for testing）时认不到，
  这时只靠 CDP 挪位，不置顶

## 22. profile 内核锁（v0.8.3）

### 22.1 事故复盘

时间线（2026-11-04）：

| 时刻 | 事件 |
|---|---|
| 06:28 | `用 msedge 内核启动失败（TargetClosedError），退回 Playwright 自带 chromium` |
| 06:47 | 同上，且 **fallback 落在同一个 `profiles/小号/msedge` 目录** → 目录里混入 chromium 的写入 |
| 07:12 | 该目录又用 msedge 打开（能开），但登录态已失效 |
| 07:26 | `/user/profile/self/` 回 `{"status_code":8,"status_msg":"用户未登录"}`，cookie 一个不少 |

两个独立问题被混在一起：

1. **TargetClosedError**：Edge 不允许同一 `user-data-dir` 开第二个实例。当时有另一个进程占着这份 profile。
2. **登录态失效**：同一 `sessionid` 在多个 profile 副本（`pk/a`、`pk/b`、`pk/c`）里被并发使用，
   抖音服务端吊销了旧会话。

### 22.2 误判复盘（写下来防再犯）

```
sqlite> select name, length(value) from cookies where name='sessionid';
sessionid|0
```

看到 0 就判定「被清零」是错的：Chromium 把明文的 `value` 留空，密文在 `encrypted_value`。
正确读法是 `context.cookies()`（Playwright 解密后返回 32 字符的 sessionid），
或者 `length(encrypted_value)`（v10 前缀 + AES-GCM 密文，几十到几百字节）。

### 22.3 三条防护

```python
# douyin/browser.py
def _guard_kernel(self):
    # 1) headless 不许碰真实 profile
    # 2) lock 里的 channel 与本次要求不同 → raise ProfileKernelMismatch
```

- `channel.lock` 内容：`{"channel": "msedge", "fingerprint": "3e9a98f3b909ffd5", "at": "...", "version": 1}`
- `fingerprint = sha1(sorted(args) + locale + viewport + tz + channel)[:16]`
- 参数微调（比如改 `quiet_window`）不会拦，只看内核；内核不同直接停手

fallback：

```python
fb_dir = self.profile_dir.with_name(self.profile_dir.name + "-chromium")
```

### 22.4 验收数据

```
logged_in = True
session_info: {'has_session': True, 'sessionid_len': 32, 'ttwid': True, 'csrf': True, 'channel': 'msedge', 'expired': False}
whoami: {'code': 0, 'nick': '大肥鱼Yume', 'uid': '4330304429101319'}
取流条数: 51
评论: True | 已发布（cid=7704088579237298994）；@ 已带上（yezheyue）
删除: True | 已删除
```

清空前的备份：`profiles/小号.bak.20261104`（555.9 MB / 4502 文件）。

### 22.5 窗口侧的两个坑（同批修）

1. 隐藏窗口继承：DSH shell 是隐藏的，Playwright 拉起的 Edge 继承 `SW_HIDE`，
   `IsWindowVisible=false` 但 CDP 认为窗口在 `(80,60)`。认窗口不再按可见性过滤，命中后 `ShowWindow(SW_SHOWNORMAL)`。
2. `OpenProcess(QUERY_LIMITED_INFORMATION)` 对浏览器进程可能 `Access denied`，
   取不到 exe 就按标题（含「抖音」）兜底。

### 22.6 还没解决的

- 同一 `sessionid` 仍可能被别的设备/副本顶掉；`channel.lock` 只防本机目录混写，防不了服务端吊销
- 每日 6 小时短会话（`sid_guard` 有效期 21600）确实是抖音会下发的，靠 `keepalive` 续；
  长会话（5184000 = 60 天）要看它给不给

## 23. text_extra 只认纯数字 uid（v0.8.4）

### 23.1 现象与误导性

`comment/publish` 回 `status_code:0`、给了 cid、评论在评论区里看得见 —— 一切正常，
除了那条评论里没有 @。被 @ 的人不会收到任何提醒。

服务端对这个字段是「宽容」的：

```python
text_extra = [{"start": 0, "end": 9, "user_id": "op111014722", "type": 0,
               "hashtag_name": "", "hashtag_id": "", "sec_uid": ""}]
# → {"comment": {"cid": 7704...}, "status_code": 0}   不报错，但谁也没 @
```

抖音号（`unique_id`，如 `op111014722`）和数字 uid（`4330304429101319`）是两套命名空间，
接口只认后者。所以「评论发不出去」和「@ 没生效」是两种完全不同的故障，
前者有错误码，后者完全静默。

### 23.2 缓存结构

```json
{
  "yezheyue":  {"uid": "2634023404581211", "dy_id": ""},
  "主人":      {"uid": "",                "dy_id": "op111014722"}
}
```

- `UID_RE = ^\d{6,20}$`，只有匹配才写 `uid`
- `_load_uid_cache()` 兼容老格式（`{name: str}`），非数字自动迁到 `dy_id`
- `_uid_of()` 只返回 `uid`；`_dy_id_of()` 取抖音号；`_slot_of()` 负责片段匹配
- `_remember_uids()` 抓到非数字时记一行日志，不覆盖已有的 uid

### 23.3 反查与降级

```
有 uid ────────────────────────────────→ 填 text_extra，发
只有 dy_id ──私信搜索──命中且行内写着该抖音号→ 得到 uid，填 text_extra
                    └─没命中────────────→ 不填 @，只发普通评论，回执写明原因
部分人有 uid ──────────────────────────→ 不发送（避免「@到一半」）
```

`_uid_from_dy_id()` 里那句校验是必须的：

```python
if dy_id not in name and dy_id != name.strip():
    continue        # 搜索没过滤时列表是最近会话，照第一个数字 uid 填会 @ 到别人
```

### 23.4 验证

| 用例 | 结果 |
|---|---|
| `@yezheyue`（有 uid） | `已发布（cid=7704092128515736378）；@ 已带上（yezheyue）` |
| `insert_ids` 回读 | `text_extra[0].user_id=2634023404581211`，`sec_uid` 由服务端补全 |
| `@主人`（只有 dy_id） | `@ 没带上（拿不到这些人的纯数字 uid（主人）），改成只发评论` |
| 单测 `_test_dy_uidcache.py` | 14/14（老格式迁移 / 非数字不覆盖 / 部分命中不发 / 反查成功写回） |

### 23.5 遗留

- 反查只有「私信搜索」一条路，且要行内明写抖音号；抖音搜索接口（`discover/search`）
  在这台机器上会挂住（fetch 不返回），暂不接
- 知道昵称、既不是好友也不在会话里的人，目前只能「不 @」，回执里会说清楚

## 24. 文案层：让人格写评论（v0.8.5）

### 24.1 模板为什么会露馅

```python
tpl = "看完记一笔：{summary}"
text = tpl.format(summary=analysis.summary[:60])
```

- 句式固定：几十条评论一个骨架，翻主页就能看出来
- `summary` 是给"知识库"用的压缩摘要，不是人会说的话（信息密度高、没有立场）
- 同一条视频被分享到不同群时前后文案一样

评论区要的不是"摘要"，是**一个人在特定一条视频下面的一句话**，所以需要看内容 + 有立场。

### 24.2 取人格与模型

```python
pm = context.persona_manager
persona = pm.get_persona_v3_by_id(voice_persona) if voice_persona else await pm.get_default_persona_v3(umo)
prov = context.get_using_provider(umo)          # 或 await context.get_using_provider_async(umo)
resp = await prov.text_chat(prompt=..., system_prompt=persona["prompt"] + 规则, image_urls=[...])
```

- 人格是 `Personality`（TypedDict），字段 `prompt / name / begin_dialogs / …`，只取 `prompt`
- `get_default_persona_v3(umo)` 会按会话配置解析默认人格，所以"QQ 上用什么人格，评论区就什么人格"
- `image_urls` 支持本地路径/`file://`/http，内部走 `resolve_image_ref_to_base64_data` 转 data URL
  （实测裸路径、`file:///`、`file:////` 三种都能解析）

### 24.3 自检与重写

```
生成 → 清洗（剥引号/剥 @/压成一行）
     → check()：空？命中禁词？多行？像列表？
        ├─ 干净 → 返回
        └─ 不干净 → 带「重写一句更像人话的」再要一次
                   ├─ 干净 → 返回
                   └─ 仍不干净 → 返回空串（上层退模板）
```

禁词表分两段：内置（首先/其次/再次/综上/总之/值得一提的是/这个视频/内容丰富/干货满满/
值得一看/展现了/体现了…）+ 用户在 `voice_banned` 里加的。内置那份是"AI 腔"特征词，
用户那份通常是"别让我说这个词"。

### 24.4 为什么保留模板

模型挂掉、超时、被限流时，评论还是要发 —— 模板是**兜底**而不是默认。
`voice_enabled=false` 时行为跟 0.8.4 完全一致，谁不喜欢模型写就关掉。

### 24.5 与"人味"还差什么

- 没有随机延迟（现在发出去是"看完立刻评论"，真人很少这样）——排进 0.9.x
- 没有对同一条视频的"第二次评论"策略，也不该有
- 语气只在 prompt 层，没有 few-shot 样本；真正稳的做法是把用户自己写过的评论塞 3 条进去

## 25. 闸门：让写操作像个人（v0.8.6）

### 25.1 为什么计数必须落库

内存计数有个隐蔽的退步：重载插件 = 配额清零。AstrBot 里重载是常态（改配置、更新），
于是「今天最多 8 条评论」会变成「每次重载后 8 条」。`rate_hit(kind, ts)` 落 `dy.db`，
按时间窗 `COUNT(*)`，重载、重启都在。

### 25.2 四档预算，不是一个总数

| 动作 | 默认 | 窗口 | 为什么这么定 |
|---|---|---|---|
| 点赞 | 30 | 天 | 最便宜，但一天几百个同样异常 |
| 分享 | 10 | 天 | 不对外发言，但刷屏式转发会被盯 |
| 评论 | 8 | 天 | 最贵，要过审，还有二级验证 |
| 私信 | 6 | 小时 | 对话要看上下文，按天算会把一场聊天掐死 |

### 25.3 静默时段

`quiet_hours="01:00-08:00"`，`parse_quiet` 支持 `23:00-07:00` 跨午夜（`start > end` 分支）。
判断用本地时间；格式错、空串一律当「不静默」——配置写错不该让功能全停。

### 25.4 随机间隔

固定间隔是最好复现的指纹：每 6.0 秒一次，连续十次。`_human_pause()` 取 0.6~1.8 倍，
另有 15% 概率乘 1.6~2.4（像中途看了眼别处）。`action_jitter=false` 退回固定值。

### 25.5 幂等收件箱

```
inbox(id, kind, key, src, payload, seen_at, handled_at, result)
UNIQUE INDEX idx_inbox_key(kind, key)
```
`inbox_add` 用 `INSERT OR IGNORE`，返回 `rowcount > 0`：重复进来直接 False。
`inbox_pending(kind, limit)` 只取 `handled_at=0`；`inbox_done` 写回执。
这不只是去重——它是 0.9 私信/提醒能安全重跑的前提。

### 25.6 三个「不该自己变」的开关

- `channel_strict`：`_ensure()` 的 fallback 分支前判断，真就 `_shutdown_playwright()` + 抛 `DouyinBlocked`。
  静默换内核 = 换机器指纹 = 登录态和风控画像重来。
- `profile_pin`：`_migrate_kernel_profile` 开头直接 return，只记一次日志。
- `keep_days`：`_retire()` 在每轮 job 开头跑，删 `aweme/event/social_action` 与已处理 inbox，
  再清 `work/` 里超期文件（跳过 `.json`/`.db`，否则会把 uid 缓存和库删了）。

### 25.7 验证

`_test_dy_086.py` 22 项：静默跨午夜 / 到上限被拦 / 关开关放行 / dm 按小时 / inbox 幂等与 stats /
保留期只删旧的 / 随机区间上下界 / 关掉随机回固定值。命令、队列、验证、文案、uid 缓存回归全绿。

## 26. 收件箱：私信与提醒怎么读（v0.9.0）

### 26.1 为什么不走接口

私信消息走 `imapi.douyin.com` 的 imsdk：`appKey=e1bd35ec9db7b8d846de66ed140b1ad9`、`service=5`、`frontierUrl=wss://frontier-im.douyin.com/ws/v2`，收发都在 WebSocket 上，`/aweme/v1/web/im/*` 直接 404（`Unsupported path(Janus)`）。提醒面板同理没有公开读接口。结论：这两条读界面，成本是每次约 20~40 秒，收益是不用逆向一套带状态的协议。

### 26.2 私信面板的四个实测细节

1. `[data-e2e="im-entry"]` 在顶栏 x≈1478,y≈28；**点它必须 hover 再 mouse.click**。`page.evaluate("…click()")` 出来的 `[data-e2e="im-dialog"]` 宽高都是 0，DOM 有内容但 Playwright 认为不可见，后续 locator 全部超时。
2. 面板 336×932，会话行 `[data-e2e="conversation-item"]`，行内三段：`conversationConversationItemtitle`（昵称，含 `\n` 要切）、`ConversationItemDescleft`（预览）、`ConversationItemTagNextToTitle`（时间）。预览里就能看出类型：`分享[视频]`。
3. 会话内 `[data-e2e="msg-item-content"]` 一条消息一个；分享卡片 `div.MessageItemShareAwemecontainer`。
4. **卡片里没有 aweme_id**，只有封面图 URL；点开后地址栏变成 `https://www.douyin.com/jingxuan?modal_id=<id>` → 从 `modal_id=` 取。Esc 关闭后要确认 modal 消失，并把地址栏的 `modal_id` 擦掉，否则下一条会被误认。

`mine` 的判定不能用 class 名猜（`closest('[class*="right"]')` 会把所有气泡判成自己的），用**气泡中心 x 与面板中线**比：`x > dr.x + dr.width*0.62` 即自己的。

### 26.3 提醒面板

点顶栏「通知」→ `[data-e2e="listDlgTest-container"]`（328×696）。`innerText` 直接可读，条目形如：

```
yezheyue 朋友 @大肥鱼Yume 在评论中提到了你05:31
yezheyue 朋友 模型里面有敏感限制吗[看]我想给我家鱼安排上@大肥鱼Yume 在评论中提到了你05:29
```

`parse_row` 剥零宽字符 → 抽时间 → 去「在评论中提到了你」尾巴 → 滤掉 `朋友/互相关注/关注了你/·/|` → 首段当昵称、其余当评论正文。条目本身不带 aweme_id，点开才跳转，取 `modal_id`。

坑：图标字体没加载完时，文字是「通知」的最外层是 `ul`（32×44），内层 `span` 还没渲染 —— 选择器要求 `children.length === 0` 会永远找不到。改成取**面积最小**的那个候选。

### 26.4 幂等与队列

`inbox(kind, key, src, payload, seen_at, handled_at, result)`，`UNIQUE(kind, key)`：

- 私信视频 `key = dm:<aweme_id>`
- 提醒 `key = mt:<aweme_id>:<正文前16字>`（没拿到 id 时退化）

`/dy inbox` 只扫不收钱（只读），`/dy inbox 处理` 才动手；处理的对象是 `inbox_pending()`（库里所有没处理的），不是本轮新扫到的 —— 否则「扫一次不处理、第二次就没得处理」。`payload` 存 JSON 字符串（`sqlite3` 不接受 dict，这条踩过一次）。

### 26.5 处理链路

| 来源 | 链路 |
|---|---|
| 私信视频 | `fetch_item`（`aweme/detail/`）→ `pipeline._handle_one`（下载/抽帧/转写/理解）→ `voice.summary` → 推给主人 |
| @ 提醒 | `fetch_item` → `_handle_one` → `limits.check_async("comment")` → `voice.reply` → `social.comment(aid, text, [who])` → `limits.hit_async("comment")` |

写操作仍受 `action_mode`/频率/静默/随机延迟约束；撞 `HumanVerifyNeeded` 就地停下并提示 `/dy verify`。

### 26.6 遗留

- 私信只处理「别人发来的分享卡片」，纯文本聊天还没接（`DirectMessages.send_text` 已具备，缺的是轮询与语气样本）。
- 会话列表只翻最近 8 个、每个会话最多点开 3 张卡，超过的漏掉；要更全就把 `inbox_max_conversations` 调大，代价是扫描时间线性增长。
- `_share_aweme_id` 依赖点开卡片 → 有 2 秒/张的硬成本，没有更快的路。

## 27. 私信日常聊天与「点错会话」的坑（v0.9.1）

### 27.1 为什么先关着
回私信比回评论更私人。评论错了顶多被人当杠精，私信错了是往人家对话框里塞错话。所以默认 `dm_chat_enabled=false`，白名单为空等于谁都不回 —— 两个开关都打开才会动手。

### 27.2 读的边界
只读最后 `dm_chat_max` 条、只读 `mine=false` 的纯文本，分享卡片走原本那条「取 aweme_id」的路。界面占位文字（`打个招呼吧`、`发一条消息`）直接滤掉，不然会把空会话当成有人说话。

### 27.3 点错会话（这次最大的坑）
第一版按 `conversation-item` 的坐标 `page.mouse.click(x, y)`。症状：`open_conversation('yezheyue')` 返回 True，读出来的却是「大肥鱼Yume」的消息，`collect_dm` 永远是 0 条。

原因两层：
1. 点进一个会话后，聊天视图**盖在列表上面**，列表行还在 DOM 里但不可见；坐标点要么点到聊天区、要么点到行与行之间的缝。
2. 判断「进去了没有」用的是 `document.querySelectorAll('[data-e2e="msg-item-content"]').length > 0` —— 上一个会话的消息节点还没卸载，条件立刻为真。

修法：
- 新增 `CHAT_NAME_JS` 读聊天头部标题（`[class*="ChatHeadertitleContainer"]`），点完必须核对标题和名字对得上；
- 点击改成 `ROW_CLICK_JS`：按名字找列表行、逐个派发 `pointerover/mouseover/mousedown/mouseup/click`。跨视图也能点到，不依赖坐标；
- 会话名晚渲染会拿到纯数字 uid（`_looks_uid`），等 600ms 重读，最多 4 次；
- `conversations()` 里原来「回首页重开面板」的兜底会打断已打开的会话，而且面板会恢复到上次的聊天视图 —— 去掉，改成等两轮 + 重开一次面板。

### 27.4 验证
| 项 | 结果 |
|---|---|
| 依次点 yezheyue / 大肥鱼Yume / 卯月的理想世界 | 全部进对，标题一致，`msgs=11 / 5 / 0` |
| `collect_dm` 读出别人文字 | `111`、`肥鱼` 两条（yezheyue） |
| 单测 `_test_dy_inbox.py` | 21 通过 / 0 失败（含白名单外不理、每小时上限拦住） |
| 分享卡与 @ 提醒老链路 | 仍正常（5 张卡、2 个 id；提醒 2 条） |

### 27.5 遗留
- 只有一轮扫描，没做「守着私信实时回」；间隔还是跟着 `loop_interval_min` 走
- 没有 few-shot：语气全靠人格提示词，长对话里容易跑味
- 群聊（`洛克王国pvp`）没单独处理，按昵称匹配，群名写进白名单才会读

## 28. 私聊不限次数与跨平台记忆（v0.9.4）

### 28.1 两个不同的"记得"

| 层 | 存在哪 | 谁写 | 用来干什么 |
| --- | --- | --- | --- |
| 抖音这边 | 插件自己的 `dy.db` 的 `chat_log` | 插件（收到写 them、发出写 me） | 接上句、去回声 |
| QQ 那边 | AstrBot 的 `data_v4.db` → `platform_message_history` | 宿主 | 让你换平台也接得上 |

不去抄宿主的库，是因为抄一份迟早两边不一致；只读不写，宿主升级表结构时也只是这段记忆暂时空着。

### 28.2 为什么自聊会话要特殊处理

抖音私聊自己那个窗口，`isFromMe` 对两边都是 true（DOM 上确实如此）。
原本 `_incoming_texts` 只收 `not mine`，于是自聊会话里一条都收不到——表现就是"它不理我"。
现在：`dm_self_names` 里的会话，把 `mine` 但**不在自己发送记录里**的那些当对方说的。
代价是"你复述插件刚说过的话"会被当回声吞掉，这个取舍比自问自答好。

### 28.3 不限次数怎么做的

`limits.check_async("dm")` 前面先过一遍 `dm_chat_no_limit`；命中就跳过计数与写计数。
也就是说免限流的人**不占**每小时配额，也不会把配额吃光影响别人。

### 28.4 私信守护

`dm_chat_poll_s > 0` 时 `_start_loop_if_needed` / `/dy start` 会顺带挂上 `_dm_watch_loop`：
每 N 秒 `inbox.collect(include_dm=True, include_mentions=False)` → 新条目进 inbox（幂等）→ 逐条回。
撞到人机验证只暂停这一轮，不退出循环。`terminate()` 里 `stop_dm_watch()` 收尾。

### 28.5 验证与遗留

- `_test_dy_memory.py` 14/0；自聊回声过滤手工验证通过；`_test_dy_inbox` 21/0 等回归全过。
- 遗留：QQ 历史只取纯文本（图片/语音不进 prompt）；守护期间浏览器常开，耗电与占内存；没有 few-shot 语料，语气仍靠人格 prompt。

### 28.6 首轮只记账（v0.9.5）

守护起来的第一轮如果直接回，会把「这几天你发给自己的每句话」一次性回一遍。
所以第一轮标记 `_dm_warmed` 之前，条目只 `inbox_done(..., "backlog")`，不回。

## 29. 三件事一个根：窗口 / 私信 / 登录（v0.9.6）

### 29.1 症状与真因对照

| 报上来的现象 | 第一层解释 | 真因 |
|---|---|---|
| 窗口配了 `quiet_window:false` 仍看不见 | 「窗口被藏到屏幕外」 | 进程压根没起：`profile_dir` 是 chromium 目录，`channel=msedge`，`channel.lock` 直接拒 |
| 抖音私信不回 | 「守护没在跑」 | 同一个拒启动，每 60 秒抛一次，只写日志 |
| 登录态老掉 | 「cookie 没存住」 | 同一 `sessionid` 被多份 profile / 多个核心同时使用，服务端按新设备吊销 |

### 29.2 窗口位置的三次翻车

1. 无 `--window-position` → 继承 profile 上次位置（`-26214`）；
2. 给了 `-32000`（静默模式）→ 人找不到；
3. 改 `quiet_window=false` 但仍无坐标 → 继承上次的 `-1525`（左边界外）；再试又变成 `-25600`（最小化态，rect 只剩标题栏 159×27）。
   最终解法：**显式坐标 + 启动后 `show_window()`**（CDP `setWindowBounds` + Win32 `ShowWindow(SW_RESTORE)`）。

### 29.3 登录被顶号的三个来源（都实测过）

- 复制 profile 多开：三份副本同一 `sessionid` → `profile/self` 回 `status_code:8`；
- 两个核心进程：launcher 起的 + 手工起的，各开一个浏览器共用一份 profile；
- 混内核：chromium 写、msedge 读 → `exitCode=2147483651 STATUS_BREAKPOINT`。

对策是三条硬约束：一份 profile、一个内核（`channel_strict`）、一个核心（`profile_pin`）。

### 29.4 重载语义

`/dy reload` 走 AstrBot 的插件重载，`main.py` 会重新加载，但 `douyin.*` 子模块沿用 `sys.modules` 里的旧对象。改子模块 → 重启核心。判断方法：日志文案与源码不一致，就是旧对象还在跑。

### 29.5 验收

- rect `(80, 60, 1280, 882)`、`vis=True`、`iconic=False`；
- `私信守护：本轮读到 3 条私信内容，新 0 条`（0 新是因为已处理过）；
- `私信守护：回了：「…」` 两条（修复前就已回成功，说明登录与写入都正常）。

## 30. 主动发私信（v0.9.7）

### 30.1 为什么不走接口
私信在 imsdk 里：`appKey e1bd35ec9db7b8d846de66ed140b1ad9`、`frontierUrl
wss://frontier-im.douyin.com/ws/v2`、发送走 `/v1/message/send` 且要 SDK token。
纯 HTTP 复现成本高、风控还严。走界面则一切都由页面自己算。

### 30.2 界面路径
顶栏 `[data-e2e="im-entry"]`（im SDK 起来才挂上，实测 11~15 秒）→ 抽屉
`[data-e2e="im-dialog"]`（336×932）→ 会话行 `[data-e2e="conversation-item"]`
→ 点行进会话（用 `ROW_CLICK_JS` 派发事件，坐标点在聊天视图里会点空）
→ 头部标题 `ChatHeadertitleContainer` 核对是不是进对人了
→ 输入区 `div.editor-kit-container`（`msg-input` 只是外壳）→ 打字 → Enter。

### 30.3 回读
`sent_ok()`：输入框已清空 **且** 抽屉 innerText 里出现这句话。
不能用「最后一条消息节点」——分享卡会排在最后，读到的是卡片作者名（实测踩过）。

### 30.4 幂等与配额
`dm_outbox` 与 `inbox` 分表；配额 `rate_dm_out_*` 独立，可用 `dm_quota_separate`
并回 `dm` 池。发送前四道关：白名单 → 开关 → action_mode → 配额。

### 30.5 验证
真机 `send_dm → True | 已发给「大肥鱼Yume」并回读确认`；
单测 `_test_dy_dm_send.py` 23/0；`_test_dy_inbox.py` 23/0。
