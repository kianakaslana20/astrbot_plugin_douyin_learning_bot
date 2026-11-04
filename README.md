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

## 三点九九、评论让人格自己写（v0.8.5）

### 之前为什么不对

`comment_template = "看完记一笔：{summary}"` 是模板。同一句话会出现在不同视频下面，
`{summary}` 还只是把摘要截断 60 字 —— 一眼就是脚本。

### 现在怎么做

评论文案改由**当前对话模型 + 你在 AstrBot 里配的人格**生成：

1. 取人格：`context.persona_manager` → `get_persona_v3_by_id(voice_persona)`，
   留空则 `get_default_persona_v3(umo)` —— 就是你在 QQ 上跟它说话时那个人格
2. 取模型：`context.get_using_provider(umo)`（就是你正在用的那个对话模型）
3. 一起送过去：系统提示 = 人格 prompt + 评论规则；用户内容 = 标题 / 作者 / 摘要 /
   关键词 / 听写片段，外加**那张 4 格抽帧拼图**（`voice_frames` 控制送不送图）
4. 自检：命中 AI 腔黑名单（首先 / 其次 / 综上 / 这个视频 / 干货满满…）、多行、
   写成列表、空 —— 命中就带着「重写一句更像人话的」再要一次；两次都不行**退回模板**

规则里写死的几条：一句话、`voice_max_chars` 字以内、不复述标题、不总结全片、
emoji 最多一个、不要自己写 @（@ 由发评论那层加）。

### 配置（配置 → 写操作）

| 项 | 默认 | 说明 |
|---|---|---|
| `voice_enabled` | 开 | 关掉退回 `comment_template`（老行为） |
| `voice_persona` | 空 | 人格名，例如「大肥鱼」；空 = 该会话默认人格 |
| `voice_max_chars` | 40 | 一句评论多少字，30~50 最自然 |
| `voice_frames` | 1 | 送不送那张抽帧拼图；0 = 只看文字，便宜快 |
| `voice_timeout_s` | 60 | 等模型多久，超时退模板，不卡整轮 |
| `voice_banned` | 空 | 额外禁词，逗号分隔（内置那份黑名单一直生效） |

### 实测

```
① 人格拼进 system ✅   带一张 file:/// 拼图 ✅   默认人格按 umo 取 ✅
② 第一版「首先…其次…」→ 命中禁词 → 重写 → 通过 ✅
③ 两版都不合格 → 返回空 → 上层退模板 ✅
④ 模型自己加的「@张三」和引号被剥掉 ✅
⑤ 指定人格名走 get_persona_v3_by_id ✅
⑥ voice_frames=0 不送图 ✅
⑦ 没有对话模型 → 静默失败不抛 ✅
⑧ 字数上限 40 生效 ✅
⑨ 总结走另一套规则（第一人称、允许说不信） ✅
17 通过 / 0 失败
```

### 本机人格现状

`data_v4.db` 的 `personas` 表里目前两个：`default`（You are a helpful and friendly assistant）和
`大肥鱼`（鲸鱼娘那套）。想让评论区跟 QQ 上一致，`voice_persona` 填 `大肥鱼` 就行；
留空则看该会话在面板里选的是哪个人格。

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

## 三点九九五、闸门：频率、静默、随机、幂等（v0.8.6）

写操作上线前先把「怎么不惹事」做掉。四条规矩，各自解决一类问题。

| 规矩 | 解决什么 | 配置 |
|---|---|---|
| 频率预算 | 一天几十条评论，人不会这么干 | `rate_comment_per_day` / `rate_share_per_day` / `rate_like_per_day` / `rate_dm_per_hour` |
| 静默时段 | 半夜三点秒回评论，是脚本最响的自证 | `quiet_hours`（例 `01:00-08:00`，支持跨午夜） |
| 随机间隔 | 固定 6 秒一次的节奏本身就是指纹 | `action_jitter`（0.6~1.8 倍，15% 概率再拖长） |
| 待执行过期 | 攒了三天的动作再执行，视频都凉了 | `pending_ttl_hours`（默认 12） |

计数落在 `dy.db` 的 `rate_hit` 表，**重载插件、重启 AstrBot 都不清零**——不然「今天已经发了 12 条」这件事一重载就忘了，等于没有限制。四档分开算：点赞便宜、分享中等、评论最贵（要过审）、私信按小时（对话要看上下文，不能按天算）。

`/dy cfg` 会打印当前用量：`频率：评论 4/8每天｜分享 0/10每天｜点赞 0/30每天｜私信 0/6每小时`。

另外三件「不该在后台自己变」的事，也在这里钉住：

- `channel_strict`：Edge 起不来就报错说明原因，不偷偷换 chromium。换内核在抖音眼里等于换了台机器。
- `profile_pin`：钉死当前 profile 目录，关掉自动迁移。
- `keep_days`：超过 N 天的看片记录、动作日志、下载的视频和拼图自动清掉（`0` = 不清）。

收件箱表 `inbox(kind, key, …)` 配唯一索引，同一个 `aweme_id` / `cid` / `msg_id` 只会处理一次——这是 0.9 的私信与提醒功能的地基，重跑、重载、多轮循环都不会重复回复同一条。

## 四点零、私信与 @ 提醒：能读、能回（v0.9.0）

之前它只会「自己刷」。现在它会看你的私信和提醒了。

### 两条入口都走界面，没走接口

私信消息在 `imapi.douyin.com` 的 imsdk（WebSocket frontier），纯 HTTP 拼不出来；提醒面板也没有公开接口。所以两条都靠界面读，好在都能读干净：

| 入口 | 怎么点开 | 读到什么 | 视频 id 从哪来 |
|---|---|---|---|
| 私信 | 顶栏消息图标 **先 hover 再点**（JS 直接 click 出来的是 0×0 隐形面板） | 会话名、预览、时间；会话内分享卡片 | **卡片 DOM 里没有 id**，点开卡片后地址栏变成 `?modal_id=769…` |
| 提醒 | 顶栏「通知」 | 文本直接可读：`yezheyue 朋友 …@大肥鱼Yume 在评论中提到了你05:31` | 点条目才跳转，同样取 `modal_id` |

两个坑记在这里：①私信图标必须 hover 再 mouse.click，否则面板 0×0；②「通知」图标字体没加载完时，文字为「通知」的最外层是个 `ul`（32×44），要取**面积最小的那个**，不能要求「没有子节点」。

### 用法

```
/dy inbox          # 只读：翻一遍，列出新到的东西（不改任何东西）
/dy inbox 处理      # 才真的看视频、写总结、回评论
/dy dm 老张 吃了吗   # 用登录的号发一条私信（dm_allow 白名单）
```

`/dy inbox` 每次扫到的东西会落进 `inbox` 表（`kind` + `key` 唯一），所以同一条私信视频不会重复总结、同一条 @ 不会重复回。扫描到但没处理的会一直留着，下次 `/dy inbox 处理` 一起办。

### 怎么处理的

| 来源 | 动作 | 用的是 |
|---|---|---|
| 私信里的分享视频 | 拉详情 → 下载 → 抽帧/转写 → 按你的人格写一段总结 → 推给你 | `voice.summary` |
| 评论里 @ 我 | 拉详情 → 看完 → 按人格回一句 → 发到他那条视频下面并 @ 他 | `voice.reply` + `social.comment` |

回复走的是和手动评论同一条闸门：`action_mode`（默认 ask）、每小时/每天上限、静默时段、随机延迟。撞人机验证会停下来说一声，让你发 `/dy verify` 把窗口挪到桌面上。

### 白名单分两套

`share_targets` / `mention_targets` 管「分享给谁、评论时 @ 谁」；`dm_allow` 管「私信能发给谁」。混在一起会出事——分享错人只是尴尬，私信发错人是骚扰。

## 四点零一、私信日常聊天：按人格回一句（v0.9.1）

`/dy inbox` 除了「别人分享的视频」和「@ 提醒」，现在也读**别人发来的文字**，并按当前人格回一句。

| 开关 | 默认 | 说明 |
|---|---|---|
| `actions.dm_chat_enabled` | 关 | 打开才回；关着只读不回 |
| `actions.dm_chat_allow` | 空 | **昵称白名单，留空 = 谁都不回** |
| `actions.dm_chat_max` | 2 | 一轮里同一个人最多读到几条、回几次 |
| `actions.rate_dm_per_hour` | 6 | 每小时最多回几条（含 `/dy dm` 手发） |

规矩还是老三条：开关、白名单、限流。回不出来就不发 —— 模型没给出合格句子（撞禁词、太长、空）时宁可沉默，不硬凑。

发出去之前它会先想一遍：接住对方那句话、一到两句、不用敬语、不写 @；不合格就让模型重说一次，还不行就放弃这一条。

```text
/dy inbox            翻私信和提醒（只读，列出有什么）
/dy inbox 处理        真去写总结、回评论、回私信
/dy dm 昵称 要说的话   手动发一条（同样受白名单和每小时上限约束）
```

私信分享来的视频写总结要跑转写和读图，是最贵的一步，所以单独一条预算：`actions.rate_summary_per_day`（默认 20，写 0 就是一份都不写）。超了不会硬写，直接告诉你今天到量了。

## 四点零二、私聊不限次数 + 记得你在 QQ 说过什么（v0.9.4）

抖音私信里想一直聊下去，两件事得先解决：**次数闸门**和**记忆断层**。

### 一、不限次数

| 配置（inbox 组） | 作用 |
| --- | --- |
| `dm_chat_no_limit` | 填在这的昵称不受「私信每小时」上限约束，想聊多久聊多久 |
| `dm_chat_allow` | 谁能触发回复（不在名单里的人，说了也不回） |
| `dm_chat_poll_s` | 多久看一次私信（60 = 每分钟；0 = 跟着主循环，可能几十分钟才看一眼） |
| `dm_chat_history` | 回复时把最近几句一起给它，别聊着聊着忘了上句 |

`dm_chat_poll_s > 0` 会起一个「私信守护」：只扫私信、不碰提醒，扫到新的一句就回一句。
代价是浏览器基本常开——不想一直开着就设 0。

### 二、自己跟自己聊（自聊会话）

抖音里私聊自己那个窗口，两边的消息**都标成"我发的"**，机器分不出谁在说。
`dm_self_names` 填上这些会话的昵称后，插件改用「我说过什么」来分辨：
只要那句话不在它自己发过的记录里，就当成你在说。这样一问一答能接下去，也不会自己回自己。

### 三、记得你在 QQ 说过什么

`dm_identity` 一行一条：`抖音昵称=QQ号`，例如 `大肥鱼Yume=1931855425`。填了之后：

1. 插件去读 AstrBot 自己的库（`data_v4.db` 的 `platform_message_history`），把这个 QQ 号最近的纯文本捞出来；
2. 顺带带上抖音这边最近的几句（存在插件自己的 `dy.db` 里）；
3. 一起塞进 system prompt —— 只作为"你记得的事"，不混进"对方刚说的"。

所以你在 QQ 上聊过的话题，换到抖音私信里它能接上。图片、语音、表情包这类不计入（纯文本段里的 `[Image]` 占位符也会被滤掉）。

### 四、刚启动那一下不会补回历史

守护第一次跑的时候只把已有的消息记进收件箱、**不回复**（标成 backlog）。
不然你一加载插件，它会把这几天你发的每句话都回一遍。

### 五、验证

- `E:\工作区\_test_dy_memory.py` 14 通过 / 0 失败：只拿这个人的记录、顺序正确、真库能读到 QQ 历史、占位符被滤、身份映射三种写法、免限流名单、`voice.chat` 支持 memory。
- 自聊去回声实测：`普通会话（全 mine）→ []`、`自聊 echo={插件发过的}→ ['在吗', '那你说说']`。
- 回归：`_test_dy_inbox` 21/0、`_test_dy_086` 22/0、`_test_dy_voice` 17/0、jobs / cmd / verify 全过。

## 三点九八、窗口看不见、私信收不到、登录老掉：三件事同一个根（v0.9.6）

这三件事是连着报的，查下来根子都在「浏览器到底有没有真的起来」。

### 一、窗口明明配了 false 还是看不见

三层原因，逐层：

| 层 | 现象 | 真实原因 |
|---|---|---|
| 1 | 进程列表里根本没有我们的 msedge | `browser.profile_dir` 被填成了 `profiles\小号`（这是 chromium 写的老目录），内核锁里记着 `chromium`，而 `browser.channel` 要 `msedge` → 启动前就被 `channel.lock` 拦住，**一次都没起来** |
| 2 | 起来了但在屏幕外 | Edge 会从 profile 恢复上次的窗口位置。我们之前把它放在 `-32000`，恢复出来落在左边界外（实测 rect `-1525`） |
| 3 | 起来了、最小化 | profile 记住的是最小化状态（rect `-25600`），人看不见但进程在 |

修法：`profile_dir` 指向 `profiles\小号\msedge`；`quiet_window=false` 时**显式给 `--window-position=80,60 --window-size=1280,900`**；启动成功后再 `show_window()` 还原一次（防最小化）。实测 rect 从 `-25600` 变成 `(80, 60, 1280, 882)`。

另外加了兜底：`profile_dir` 万一又被人填到基目录，插件自己会认锁、自动落到 `<内核>` 子目录并记一条日志，不再直接拒绝启动。

### 二、抖音私信收不到

同一个根（浏览器没起来 → `私信守护` 每 60 秒抛一次异常，但只写日志，人看不到）。修完启动问题后还有两个坑：

- **页面停在视频页/空白页时找不到「消息」入口**：私信抽屉只挂在首页顶栏。守护每轮先确认 `page.url` 里有 `douyin.com`，不是就先回首页。
- **守护崩了要出声**：`ProfileKernelMismatch` / `DouyinBlocked` 现在会私聊你一次（10 分钟节流），而不是只写日志。

实测（修完）：`私信守护：本轮读到 3 条私信内容`，并且真的回了话。

### 三、登录态老是掉

不是插件"忘了存"，是**同一个账号被多处同时使用**，抖音按新设备登录把旧会话吊销：

1. 排查时把 `profiles\小号\msedge` 复制成三份、拿同一个 `sessionid` 同时开 → 服务端立刻回 `{"status_code":8,"status_msg":"用户未登录"}`；
2. 有一段时间**两个 AstrBot 核心同时在跑**（launcher 自己起的一个 + 手工起的一个），两套插件共用一个 profile；
3. chromium / msedge 混写同一目录（`STATUS_BREAKPOINT` 那次）。

现在的约定：**一个号一份 profile、一个内核、一个核心进程**。`profile_pin=true` 钉住目录不自动迁移，`channel_strict=true` 起不来就报错、绝不偷偷换 chromium。

### 四、一个容易踩的工程坑

插件「重载」不会重新 import 子模块（`douyin.browser` 还是老的代码对象）。所以改完 `douyin/*.py` 后：

- 只发 `/dy reload`：`main.py` 的改动生效，**子模块的改动可能不生效**；
- 改到 `douyin/browser.py`、`douyin/inbox.py` 这类：**重启 AstrBot 核心**最稳。

症状很好认：日志里的报错文案跟你刚改的代码对不上。

## 四点零三、主动发私信（v0.9.7）

以前只能读私信，现在能发。`/dy dm 昵称 话`，默认 ask：先进发件箱，`/dy confirm` 才真发。

发之前会做四道关：白名单（`inbox.dm_targets`，名单空 = 谁都不发）、开关
（`dm_send_enabled`）、`action_mode`（off 一律不发 / ask 进发件箱 / auto 直接发）、
配额（`rate_dm_out_per_hour` 3 条、`rate_dm_out_per_day` 20 条；`dm_quota_separate`
决定是独立算还是跟日常回话共用 `rate_dm_per_hour` 那一池）。

发完一定回读：输入框清空了 **且** 抽屉里出现了这句话才算成功。只等到文字出现还不够
——字可能还留在输入框里没发出去。确认没发才补一次；确认发了就不重发，避免两条一样的话。

走界面而不是接口，三个原因：私信接口在 imsdk 里（`frontier-im.douyin.com` 的 WebSocket），
拼不出纯 HTTP；`/v1/message/send` 需要 SDK token；界面上看得见的东西你能直接核对。

「为什么每次都要十几秒」：私信的入口 `data-e2e="im-entry"` 是 im SDK 起来之后才挂上去的，
实测 11~15 秒。在那之前点顶栏那块地方没有任何反应，所以代码会等到入口出现再点，
最多等 25 秒。

发件箱落在 `dy.db` 的 `dm_outbox`（`target_uid / text / status / created_at / sent_at / error`），
`/dy dm 状态` 可以看排队与成败。失败会私聊告诉你，不会静默。

## 四点零四、从「定时轮询」改成「事件触发 + 模型自己判断」（v0.10.0）

以前所有东西都靠定时器：私信每 60 秒扫一次，刷视频每 90 分钟一轮。能跑，但两条硬伤——
消息来了要等最多一分钟，刷不刷由时间决定而不是由「有没有值得看的」决定。

### 一、先看抖音自己推不推

挂 `page.on("websocket")` 看了 60 秒，页面上 5 条长连接：

| 连接 | 空闲期帧 | 用途 |
|---|---|---|
| `wss://frontier-im.douyin.com/ws/v2` | 3~4 帧，**全是文本 `hi` 心跳** | **私信主通道** |
| `wss://frontier-pc.douyin.com/ws/v2` | 2 帧 `hi` | PC 端另一路 |
| `wss://bytelink100-ws.douyin.com/...` | 12 帧二进制 | 埋点上报，跟私信无关 |

结论：**推送是真的，但业务帧是 protobuf**。所以不解析——把「frontier 上出现非 `hi`、长度≥32 的帧」当成
**有动静的信号**，信号一到立刻走一次界面读取。比纯轮询快，比解 protobuf 稳。

### 二、四层结构

| 层 | 做什么 | 代码 |
|---|---|---|
| 信号 | 监听 WS，心跳过滤，置脏标记 | `douyin/im.py::ImWatcher` |
| 判断 | 该不该回、回什么、这轮值不值得刷 | `douyin/agent.py::DouyinAgent` |
| 执行 | 发私信（回读校验）、发评论、刷一轮 | `social.send_dm` / `jobs.submit` |
| 兜底 | WS 静默 5 分钟就退回 10 分钟一次轮询 | `_wait_signal` 超时 |

日志每轮都标来源，能一眼看出是推的还是兜的：

```
私信守护(ws)：读到 3 条，新 1 条，回了 1 条，跳过 0 条
私信守护(poll)：读到 3 条，新 0 条，回了 0 条，跳过 0 条
```

### 三、模型判断，不是规则直出

`DouyinAgent.handle_douyin_event` 把「事件类型／对方昵称／是否自己人／本次内容／最近 12 条历史」拼成一段，
交给你在 AstrBot 里配的那个对话模型（复用主核心 provider，不另配 key），人设取当前人格，
只要一个 JSON：

```json
{"reply": true, "text": "在的在的，怎么了", "reason": "自己人打招呼"}
```

`reply:false` 就什么都不发，只记 `dm_judge` 表。没有可用模型时**退回原来的规则路径**，不会哑掉。

自主刷同理：`judge_round` 返回 `{"run":true,"take":3,"reason":"有人分享"}` 才排一轮，
刷前先问，刷后简报标「自主判断触发」，跟手动 `/dy start` 分得开。

### 四、白名单与限流怎么相处

- `dm_targets` 里的人（自己人）：**不计数、不限流**，但**静默时段照样生效**。
- 其他会话：走 `rate_dm_per_hour` / `rate_dm_per_day`。
- 评论区一次只针对一个人回一条：同一 uid 还没回完，新事件直接丢。
- 自主刷另有两道上限：`auto_rounds_per_day`、`auto_items_per_day`。

### 五、配置（面板「私信与提醒」组）

| 项 | 默认 | 说明 |
|---|---|---|
| `agent_enabled` | 开 | 事件交给模型判断；关了退回规则直出 |
| `event_ws` | 开 | 用 WS 帧当触发信号 |
| `poll_fallback_min` | 10 | WS 静默时的兜底间隔（分钟） |
| `autonomous_round` | 开 | 不用 `/dy start` 也能自己刷 |
| `auto_rounds_per_day` | 4 | 自主刷每天最多几轮 |
| `auto_items_per_day` | 8 | 自主刷每天最多看几条 |

### 六、改完必须重启核心

`/dy reload` 只重载 `main.py`，`douyin/*` 子模块还是旧对象。这次动了 `im.py`、新增了 `agent.py`，
所以只能重启 AstrBot 核心才生效。

## 四点零五、把推送真的解开：业务帧解析与评论区接入（v0.11.0）

0.10.0 是「听到动静再去读界面」，0.11.0 是「直接读懂推送」。

**协议层解出来了什么**

| 层 | 字段 | 拿到的 |
| --- | --- | --- |
| Frame | `payload=8` `encoding=6` `type=7` | 原始帧 / 是否压缩 / 帧类型 |
| Response | `cmd=1` `body=6` | 命令号与回包体 |
| `cmd=500` | `body500.message=5` | 新消息通知（正常私信） |
| `cmd=1099` | `body503.message=2` | 陌生人私信 |
| MessageBody | `sender=7` `content=8` `type=6` `server_message_id=3` `create_time=10` | 谁、内容、类型、消息 id、时间 |

内容层按类型解：文本(7)、作品分享(8/77)、评论卡(16/75/105)、`richTextInfos` 里的 @（偏移按 UTF-16 算）。

**这些设计是为了不出错**

- **不当真**：`hi` / `ok` 这类 ACK 不算业务帧；未知命令、未知编码、缺身份信息的帧只留诊断，不硬编。
- **不崩**：帧 1 MiB、解压后 4 MiB、字段 8192、嵌套 32 层 —— 畸形帧丢一条，守护循环继续跑。
- **不重复**：事件 id 用「会话 + 服务端消息 id」，同一条被两个通道各报一次时按帧 sha256 去重。
- **不越界**：IM 里的评论卡、群内 @ 一律不当公开评论；没有原始评论 id 时不冒充「回复某条评论」，
  改成在视频下发一条 @ 回对方。平台互动通知(frontier-pc)的 schema 还没实测到，那一路退回界面读取。
- **不哑**：判断模型没就绪时，私信走日常聊天、@ 走 `voice.reply`，照样回。

**挂得更早**：浏览器交出页面时先回调，WS 监听在导航之前就挂上，页面加载期间建立的连接也接管。

**日志长这样**

```
事件守护(ws)：解码 3 条，读到 1 条，新 1 条，模型判断 reply 1 条，发出 1 条，跳过 0 条；无
```
