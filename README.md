# 抖音刷视频学习助手

> 让 AstrBot 自己刷抖音推荐流：挑值得看的 → 下载无水印源视频 → 抽帧 + 语音转写 → 模型产出
> 「讲了什么 / 有没有干货 / 值不值得看」→ 推给你，并沉淀成可检索的知识库。

作者 **YG**　·　适配 AstrBot `>=4.29.1,<5`（4.29.3 实测）　·　当前版本 **0.6.5**　·　MIT

---

## 它凭什么能刷

抖音 Web 端有三条取数路，实测只有第三条走得通：

| 通道 | 做法 | 实测 |
| --- | --- | --- |
| ① 免签 HTTP | 直打 `POST /aweme/v1/web/module/feed` | HTTP 200 但 body **0 字节** |
| ② 自己签名 | 实现 a_bogus（SM3 盐 `dhzx`） | 可行，但风控一改就得重做 |
| ③ 页面旁听 | headful Playwright 打开推荐流，**拦页面自己发的** feed 响应 | 通：20 秒滚动拿到 60 条唯一 aweme |

本插件用 ③：不签名、不猜参数、让页面自己发请求，我们只收响应。

拦两个路径（抖音会按入口在 v1/v2 之间切，都得认）：

- `/aweme/v1/web/tab/feed/`
- `/aweme/v2/web/module/feed/`（「抖音精选」变体，回包还带 `chime_video_list`）

几条硬约束（都写进代码注释了）：

- **必须 headful**。`headless=True` 会被识别成验证码中间页，cookie 只有 `__ac_nonce`。
- **不登录也能刷**推荐流（拿到 29 个 cookie 含 `ttwid`，没有 `sessionid`）；点赞/评论/分享才需要登录。
- **响应体要在页面跳转前读走**，否则 Playwright 抛 `No resource with given identifier found`，整批丢光。
- 无水印直链是 `video.play_addr.url_list`（`download_addr` 带水印），支持 Range 分段，可限量下载。
- 推荐流里约 **65%** 的条目带 `play_addr`，图集没有音轨，直接跳过。

## 功能

**刷 + 学**

- 按兴趣词 / 时长 / 点赞 / 知识密度打分挑片，两道门（`min_knowledge_hits` + `min_score`）挡掉泛娱乐
- 下载无水印源视频 → ffmpeg 抽 9 帧拼九宫格 + 抽 16k 单声道音轨
- 音轨走 STT 转写、拼图走视觉模型，合成简报；结果入 SQLite 知识库，可 `/dy kb` 检索
- 后台队列串行跑（Playwright 持久化 profile 不能双开），命令立即返回

**写操作（可选，默认 `ask` / 部分 `off`）**

| 动作 | 走什么 | 能回滚 |
| --- | --- | --- |
| 点赞 / 取消赞 | 页面内 `POST /aweme/v1/web/commit/item/digg/` | 是 |
| 评论（可 @ 人） | 接口 `comment/publish/`，`text_extra` 表达 @ | 是（回包给 `cid`，可删） |
| 分享给好友 / 群 | 面板 hover → 点好友行 | 否（没有撤回接口） |

**遇到麻烦会叫你**

- 掉登录 → 抓二维码图直接发给你，扫完自己继续（不需要回话）
- 弹人机验证 → 把浏览器窗口挪到桌面中间、发消息让你拖滑块，过了自动接着跑
- 求助目标优先 `notify.help_target`，没填就找管理员和最近会话

## 安装

```bash
# 1) 放进 AstrBot 插件目录
#    <AstrBot>/data/plugins/astrbot_plugin_douyin_learning_bot
# 2) 装依赖（AstrBot 镜像多半已带，缺了再装）
pip install -r requirements.txt
# 3) Playwright 浏览器内核
python -m playwright install chromium
# 4) 重载插件（面板里点，或重启 AstrBot）
```

`ffmpeg` 优先复用 Playwright 自带那份（`ms-playwright/ffmpeg-*/ffmpeg-win64.exe`），
找不到才落到系统 PATH。

## 命令

| 命令 | 作用 |
| --- | --- |
| `/dy` | 状态与用法 |
| `/dy start [n]` | 排一轮到后台，立刻返回，跑完推给你 |
| `/dy loop on\|off` | 后台自动刷（间隔 `loop_interval_min`） |
| `/dy jobs` | 看队列：在跑什么、进度、排了几个 |
| `/dy stop` | 停队列 + 关浏览器 |
| `/dy login` | 发一张登录二维码到聊天 |
| `/dy account` | 多开：列 profile / 切号 / `new <名字>` 新建 |
| `/dy verify` | 弹人机验证时：搬窗口到桌面并等它过去 |
| `/dy push [目标]` | 设简报推送目标（留空 = 当前会话） |
| `/dy like [off]` | 给最近这条点赞 / 取消赞 |
| `/dy share [对象]` | 分享最近这条（受白名单限制） |
| `/dy comment [文本]` | 评论并 @ 白名单里的人 |
| `/dy do` | 执行 `ask` 模式攒下的动作（`/dy do 跳过` 丢弃） |
| `/dy act off\|ask\|auto` | 写操作模式 |
| `/dy cfg` | 看当前生效配置（白名单 / 模式 / 模板） |
| `/dy calib [n]` | 抓 n 条样本，看当前阈值会学哪些、过筛率多少 |
| `/dy kb [n]` | 看最近 n 条知识库 |
| `/dy update` | 检查 GitHub 上的新版本（`/dy update apply` 直接 `git pull`） |
| `/dy dom im\|share\|comment\|both` | 抓面板真实结构（校准选择器用） |

## 配置

全部在「配置 → 抖音刷视频学习助手」面板里，改完发 `/dy reload`。几个关键项：

| 段 | 项 | 说明 |
| --- | --- | --- |
| `feed` | `min_knowledge_hits` / `min_score` | 选片两道门，默认 `1` / `4`；`/dy calib` 可量 |
| `feed` | `interest_keywords` / `block_keywords` | 兴趣词加分、屏蔽词直接淘汰 |
| `understand` | `frames` / `frame_width` / `asr_max_seconds` | 抽几帧、多宽、转写截多长 |
| `media` | `max_download_mb` | 单条视频下载上限 |
| `actions` | `action_mode` / `share_mode` / `share_targets` / `mention_targets` | 写操作模式与白名单 |
| `browser` | `quiet_window` | 窗口藏到屏幕外（默认开）；要自己扫码/拖滑块时临时关掉 |
| `notify` | `notify_login` / `verify_notify` / `help_target` / `idle_close_min` | 什么时候叫你、叫谁、闲置多久关浏览器 |

白名单两条规则：**名单为空 + 强制开启 = 谁都不发**；匹配是「包含」关系，别用太短的词。

## 版本与更新

- 版本号在 `metadata.yaml` 的 `version`，跟随 git tag（`v0.6.5`）
- 每次发版更新 [CHANGELOG.md](CHANGELOG.md)，并打 tag + GitHub Release
- 检查更新：发 `/dy update`（读 `metadata.yaml` 的 `version` 与 `origin/main` 比对，列出落后的提交）
- 直接更新：`/dy update apply` 会 `git pull --ff-only`，然后去面板重载插件
- 也可以手动：`git -C <插件目录> pull`，或看 [Releases](https://github.com/kianakaslana20/astrbot_plugin_douyin_learning_bot/releases) 页

## 已知限制

- 只做**观看侧**，不做养号、批量私信、刷量
- 分享没有撤回接口，发出去就收不回；所以默认 `action_mode=ask`
- 抖音风控会变：选择器写在 `douyin/browser.py`、`douyin/social.py` 顶部常量里，变了先跑 `/dy dom` 抓新结构
- 接口评论会被限流，空回包时按指数退避重试（`comment_retry` / `comment_retry_s`）
- Windows 上若 `ffmpeg` 命中 `WindowsApps` 的 0 字节占位符，会静默失败——代码里按「存在 + 非 windowsapps + 体积 > 64KB」三重校验

## 目录

```
main.py               插件入口：命令、队列、通知、profile 多开
_conf_schema.json     配置面板 schema
metadata.yaml         插件元信息（版本、repo）
douyin/
  browser.py          取流（页面旁听）、登录态、二维码、人机验证
  feed.py             aweme 归一化
  scoring.py          打分与筛选
  pipeline.py         一轮的编排（取流→筛→下载→抽帧→转写→理解）
  media.py            下载、抽帧、拼图、音轨
  asr.py              语音转写
  understanding.py    视觉 + 文本理解，出简报
  social.py           点赞 / 评论 / 分享（接口 + 界面两条路）
  jobs.py             后台单 worker 队列
  store.py            SQLite：知识库、动作去重
  config.py           配置解析
docs/
  DESIGN.md           设计笔记与实测量表（怎么试出来的）
```

## 许可

MIT，见 [LICENSE](LICENSE)。