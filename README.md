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

## 三点九八、评论发出去了，@ 是空的（v0.8.4）

### 症状

评论正常发布、能拿 cid，但那条评论里**没有蓝色 @**，被 @ 的人也没收到提醒。

### 根因：`text_extra.user_id` 只认纯数字 uid

服务端对 `text_extra[].user_id` 不做校验：把抖音号（形如 `op111014722`）填进去，
接口照样回 `status_code:0` 和 cid，评论也真的发出去了 —— 只是那个 @ 从未指向任何人。

我的 uid 缓存曾经是「昵称 → 一个字符串」，两种值混着存：

```json
{"yezheyue": "op111014722", "大肥鱼Yume": "4330304429101319"}
```

于是 `text_extra.user_id = "op111014722"` → 静默无效。

### 怎么修的

1. **缓存分两格**（`uid` / `dy_id`），只让 `^\d{6,20}$` 进 `uid`：

   ```json
   {"yezheyue": {"uid": "2634023404581211", "dy_id": ""},
    "主人":     {"uid": "",               "dy_id": "op111014722"}}
   ```

   老格式（纯字符串）在读入时自动迁移；非数字值一律搬去 `dy_id`。
   `_remember_uids()` 抓到非数字时**只写 dy_id**，并在日志里点明「「X」给的 op… 不是纯数字，按抖音号存」。

2. **反查**：只有抖音号的人，先按抖音号去私信搜索里查一次（`_uid_from_dy_id`）。
   关键约束——**搜索结果那一行必须明写着这个抖音号**才算命中；不校验的话，
   搜索没过滤时列表还是最近会话，照第一个数字 uid 填就会 @ 到完全不相干的人。

3. **查不到就降级，不拼假 @**：宁可不 @，也不发「看起来 @ 了、其实没点到人」的评论。

   ```
   True | 已发布（cid=7704101963566318373）；@ 没带上（拿不到这些人的纯数字 uid（主人）），改成只发评论
   ```

   只 @ 到一部分同样不发：`_api_comment_with_at` 发现有人没 uid 直接返回失败，
   由上层退成普通评论 —— 一条评论里「@到一半」比不 @ 更容易误导。

### 回读证据（insert_ids 拉回刚发的评论）

```json
{"cid": "7704089996513100602",
 "text": "回读验证：这条应该 @ 到 yezheyue @yezheyue",
 "extra": [{"start": 23, "end": 32,
            "user_id": "2634023404581211",
            "sec_uid": "MS4wLjABAAAA7DzmY0GCgnm-uU8lEwOQ6x7Ck13CfmZvw2QdacCF7MtmO-gKBNtyQ_8y3SktxUwz"}]}
```

`sec_uid` 是服务端自己补的 —— 说明这条 @ 真的解析到了人。

### 手上有脏缓存怎么办

清掉再重拉一次就行：抓一次分享面板会把好友的纯数字 uid 补回来，抖音号会落在 `dy_id` 里。

    # 面板是好友/群才有；拿不到的会留在 dy_id，发评论时自动降级
    /dy comment 一句话 @某人

## 三点九七、profile 内核锁与「登录态丢了」的真相（v0.8.3）

### 先说结论：cookie 从来没被清空

排查时我读到 `Cookies` 表里 `sessionid` 的 `length(value) = 0`，据此判断「被清零」。
**这是读错了列**：Chromium 的明文列 `value` 本来就是空的，真实值在 `encrypted_value`。
正确判断方式是拿 Playwright 的 `context.cookies()`（它会解密），或者查 `encrypted_value` 长度。

真正丢了登录态的原因是另一件事：**同一个 `sessionid` 被多个 profile 副本同时用**，
抖音服务端把会话顶掉了。表象很迷惑——cookie 一个不少、`sid_guard` 也没到期，
但 `/user/profile/self/` 回 `"status_code":8, "status_msg":"用户未登录"`。

### profile 是怎么被写坏的

```
Edge 拒绝在同一 user-data-dir 上开第二个实例
   → 报「请在已运行的浏览器会话中打开」
   → Playwright 抛 TargetClosedError
   → 旧代码 fallback：**在同一个目录**改用自带 chromium 打开
   → 目录里同时有 Edge 的 Local State 契约和 chromium 的写入
   → 下次谁开都崩（exitCode=2147483651 / STATUS_BREAKPOINT）
```

所以「写坏」不是单次 launch 参数不一致，而是**失败的 fallback 往同一目录里换了内核**。

### 三条防护（v0.8.3）

| 防护 | 行为 |
|---|---|
| `channel.lock` | 每份 profile 记 `{channel, fingerprint, at}`；下次发现内核不同就**拒绝启动**，把两边内核、时间、替代方案一起说清 |
| fallback 换目录 | 内核起不来 → 改用 `profiles/<号>/msedge-chromium`，不再复用原目录 |
| headless 禁令 | 真实 profile（有 Cookies 库或已存在 lock）一律不许 headless；headless 只放行临时目录 |

`/dy` 里没有新增命令，触发时后台会直接把原因推给你，例如：

```
profile 内核不一致：...\profiles\小号\msedge
这份 profile 上次是用「msedge」内核打开的（2026-11-04 07:26:11），现在要求用「chromium」。
两种内核混写同一目录会直接崩（TargetClosedError / exitCode=2147483651）。
要换内核：新建一份 profile（/dy account new <名字>）后重新扫码；
或者把本目录整体复制成 chromium 再用新内核打开。
```

### 顺手修的两个窗口坑

1. **隐藏窗口继承**：DSH 的 shell 是隐藏窗口，Playwright 拉起的 Edge 会继承 `SW_HIDE`，
   窗口建出来 `IsWindowVisible=false`，CDP 却说它在 `(80,60)`。现在认窗口时**不按可见性过滤**，
   命中后先 `ShowWindow(SW_SHOWNORMAL)`。
2. **OpenProcess 被拒**：浏览器进程常在不同完整性级别，`QueryFullProcessImageNameW` 可能取不到进程名。
   取不到时按标题（含「抖音」）兜底，不再因为拿不到 `msedge.exe` 就放弃置顶。

## 三点九五、窗口调到前台（v0.8.2）

后台跑久了会忘掉浏览器长什么样；更麻烦的是 `quiet_window` 把窗口放在
`(-32000, -32000)`，**人在桌面上根本找不到它**，扫码、拖滑块、看报错全没法做。

现在三条规矩：

1. **要你动手时，窗口自己上来**：需要扫码登录、或者撞上人机验证，插件会
   `show_window()` 把窗口搬回 `(80, 60)`、还原最小化、再用 Win32
   `SetWindowPos` 真置顶（CDP 没有置顶能力，浏览器被别的窗口挡住就白搬了）。
   配置项：`browser.window_on_login`（默认开）。
2. **随时手动调出来**：`/dy show`（别名 `/dy 窗口`、`/dy 打开`、`/dy 前台`）。
   它会回报窗口在屏幕上的坐标和当前登录身份。
3. **窗口里的二维码会自己刷新**：`/dy login` 现在是「先摆窗口 + 再发一张图」，
   聊天里那张约 1 分钟过期，窗口里那张抖音自己换——**优先在窗口里扫**。

实测（干净 profile，`quiet_window=True` 起：

```
起完了，屏外窗口坐标： (-26214, -26214, 1554, 882)
show_window = True
摆回来之后的坐标：   (80, 60, 1280, 882)
focus_window 命中：  抖音-记录美好生活 - 个人 - Microsoft Edge
```

置顶用的是「先 `HWND_TOPMOST` 再 `HWND_NOTOPMOST`」两步：抢到焦点，但不会永久
钉在最上层挡你别的活。窗口是按进程名（`msedge.exe` / `chrome.exe`）+ 标题含「抖音」
认的，所以任务栏里你自己开的 Edge 不会被误提。

## 三点九、换 Edge 内核 + cookie 到底从哪来（v0.8.0）

### 内核

`browser.channel` 默认 `msedge`，用本机 `msedge.exe`（实测 143.0.3650.66）跑。
起不来会自动退回 Playwright 自带的 chromium，不影响功能：

```
用 msedge 内核启动失败（…），退回 Playwright 自带 chromium
```

**profile 必须按内核分目录**：`profiles/<号>/msedge`、`profiles/<号>/chromium`。
Chromium 写过的目录直接喂 Edge 会崩，`exitCode=2147483651`（STATUS_BREAKPOINT），
日志里是 `content_settings_pref.cc:319 Invalid pattern strings: chrome://omnibox-popup.top-chrome/,*`。
删 `Local State` / `Preferences` 都救不回来，只有空目录才行。老目录里的登录态会自动搬一次，
搬完日志写「已搬 N 个 cookie 到 …（下次直接用，不用扫码）」。

### cookie 从哪来

| 路子 | 能不能用 | 说明 |
|---|---|---|
| 直接读 Edge/Chrome 的 cookie 文件 | 只能读 `v10` | `v20`（App-Bound Encryption）密钥绑在浏览器进程上，解不开 |
| 浏览器开着时读库 | 不行 | Cookies 库被独占，`mode=ro` / `immutable=1` 都报 unable to open |
| 插件自己的 profile | **主路** | 扫一次码，之后靠 `sid_guard` 到期 + 定时心跳维持 |
| 从本机浏览器借 | 兜底 | `cookie_source=auto` 时先试 edge / chrome，借到了就不用扫码 |

所以「实时自动获取 cookie」的答案是：不是去偷浏览器的，而是**让插件自己的会话活着**。
`/dy cookie` 能看到它现在什么状态：

```
登录态：是（大肥鱼Yume）
sessionid：32 字节，sid_guard 剩 1415.6 小时
ttwid：有｜csrf：有｜内核：msedge
上次复核：06:13｜上次保活：06:10｜保活间隔：20 分钟
```

### 会话过期的表现（重要）

过期后 cookie 里的 `sessionid` **还在**，`/user/profile/self/` 甚至**还能回昵称**，
但所有写操作一律回 `status_code: 8`：

```json
{"comment":null,"status_code":8}
```

这就是「AstrBot 说发评论和艾特都需要 cookie」的真相 —— 不是缺 cookie，是会话被服务端作废了。
现在判定会带上 `sid_guard`（`hash|签发时间|有效期秒数|过期日期`，**第 4 段才是过期时间**），
快到点就按「需要登录」处理，走搬窗口 + 发二维码那条求助流程。

### 手上只有 cookie 串？贴进来就行

面板 → 写操作上方的 **browser.cookie_string**（`浏览器` 分组）：浏览器登录 douyin.com 后，
F12 → Network → 点任意请求 → Request Headers 里 `cookie:` 那整行，复制过来贴上去。

```
ttwid=1%7C...; sessionid=...; sessionid_ss=...; sid_tt=...; passport_csrf_token=...; ...
```

只需要 `k=v; k=v` 这一段。**不用管 `msToken` / `X-Bogus` / `a_bogus`** —— 评论、点赞、
分享都是插件在页面里 `fetch` 发的，签名由抖音自己的 JS 现算，签名参数不在请求头里也能过。

贴完发 `/dy cookie` 看「登录态：是」就成。`none` / `null` / `-` 这种占位值会被跳过。

### 两个新坑

- **打不开的视频**：feed 里会挑到直播卡、带货卡，`/video/<id>` 打开后是 `data-e2e="error-page"`，
  播放器控件和分享面板都不存在。现在先验页面，不行换 `?modal_id=` 入口重试，再不行跳过。
- **uid 只能从分享面板刮**：面板只在详情页有、还要 hover 才展开。刮到就落盘
  `work/uid_cache.json`，之后 @ 人直接读缓存，不用为了一条评论去开视频页。

## 许可

MIT，见 [LICENSE](LICENSE)。