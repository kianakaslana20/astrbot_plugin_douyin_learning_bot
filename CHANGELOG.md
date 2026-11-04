# 更新日志

版本号规则：`主.次.修订`。次版本号进位 = 有新能力或有不兼容的配置变化，修订号 = 修 bug / 调参数。

## 0.8.4 — 2026-11-04

**评论发得出去、@ 是空的：`text_extra.user_id` 塞了抖音号**

- uid 缓存拆成 `{uid, dy_id}` 两格；只有 `^\d{6,20}$` 能进 `uid`，
  非数字一律落到 `dy_id`；老格式读入时自动迁移，脏值不再被当成 uid 用
- 新增 `_uid_from_dy_id()`：按抖音号去私信搜索反查数字 uid，
  且**必须搜索结果里明写该抖音号**才采用（否则会 @ 到不相干的人）
- 只 @ 到一部分时**不发送**：`_api_comment_with_at` 直接返回失败，
  上层退化成「只发普通评论」，回执里写明「@ 没带上（原因），改成只发评论」
- 清理脚本把 `主人 → op111014722` 这类脏值迁到 `dy_id`，`yezheyue` 重拉回真 uid

真机验证（2026-11-04 08:4x）：`@yezheyue` → `cid=7704092128515736378`；
`insert_ids` 回读 `text_extra.user_id=2634023404581211`、服务端补了 `sec_uid`；
只有抖音号的 `主人` → 降级为普通评论 `cid=7704101963566318373`，Alice 与「@ 生效」判定均正确，测试评论已删。

## 0.8.3 — 2026-11-04

**profile 不再被写坏；「登录态丢了」是误判**

- 新增 `channel.lock`：每份 profile 记 `{channel, fingerprint, at}`，换内核启动直接拒绝，
  错误信息里带上次内核、时间与替代方案（`ProfileKernelMismatch`）
- fallback 改用独立目录 `profiles/<号>/<channel>-chromium`（原来复写同一目录，这才是写坏的路径）
- headless 禁令：真实 profile（有 Cookies 库或 lock）拒绝 headless，headless 只放行临时目录
- 修窗口：隐藏窗口继承 `SW_HIDE` 时不再被漏掉（`ShowWindow(SW_SHOWNORMAL)`）；
  `OpenProcess` 被拒时按标题兜底认窗口
- 文档纠正：`Cookies.value` 列本来为空，真实值在 `encrypted_value`；
  「sessionid 被清零」是误读，真正失效是服务端把会话顶掉（`status_code:8`）

真机验收（2026-11-04 07:3x）：`logged_in=True`、`nick=大肥鱼Yume`、取流 51 条、
评论 `cid=7704088579237298994` 并 @yezheyue 成功、随后删除成功。

## 0.8.2 — 2026-11-04

**窗口不再躲着人**

- 需要扫码 / 撞人机验证时，自动把浏览器窗口摆到桌面 `(80,60)` 并置顶
  （`browser.window_on_login`，默认开）。CDP 只能挪位不能置顶，补了 Win32
  `SetWindowPos`（TOPMOST → NOTOPMOST 两步，抢焦点但不永久钉住）
- 新增 `/dy show`（别名 `/dy 窗口`、`/dy 打开`、`/dy 前台`）：随时把窗口调出来，
  回报屏幕坐标与当前登录身份
- `/dy login` 改成「先摆窗口，再发二维码图」：窗口里的码会自己刷新，
  聊天里那张约 1 分钟过期，所以优先在窗口里扫
- 新增 `DouyinBrowser.window_rect()` / `focus_window()`：按进程名 + 标题认窗口

实测：`quiet_window=True` 起在 `(-26214,-26214,1554,882)`，`show_window()` 后
`(80,60,1280,882)`，`focus_window()` 命中「抖音-记录美好生活 - 个人 - Microsoft Edge」。

## 0.8.1 — 2026-11-04

**粘贴式 cookie（`browser.cookie_string`）**

- 新增配置 `browser.cookie_string`：把浏览器 F12 里那条 `cookie:` 原样贴进来，
  插件启动时灌进上下文，域名统一 `.douyin.com`
- 解决「手上没有登录态、又不想扫码」这类情况；`none` / `null` / `-` 这类占位值会被跳过
- 不需要自己实现 `msToken` / `a_bogus`：评论与点赞都是在**页面里** `fetch` 的，
  签名由抖音自己的 JS 现算。实测同一条视频 `POST /aweme/v1/web/comment/publish/`
  回 `{"status_code":0,"comment":{"cid":...}}`，随后可删
- 会话作废时（cookie 还在、`/user/profile/self/` 还能回昵称）写操作一律 `status_code: 8`，
  这是「需要重新登录」的信号，不是缺 cookie

## 0.8.0 — 2026-11-04

**换 Edge 内核 + 把「cookie 从哪来」讲清楚**

- `browser.channel` 默认 `msedge`：用本机 Edge 跑，起不来自动退回自带 chromium
  （日志会写「用 msedge 内核启动失败（…），退回 Playwright 自带 chromium」）
- **profile 按内核分目录**：`profiles/<号>/msedge` 与 `profiles/<号>/chromium`。
  原因：Chromium 写过的目录直接给 Edge 用会以 `exitCode=2147483651`
  （STATUS_BREAKPOINT）崩掉，日志里是
  `content_settings_pref.cc:319 Invalid pattern strings: chrome://omnibox-popup.top-chrome/,*`；
  删 `Local State`、删 `Preferences` 都不救，空目录才正常。老目录里的登录态会自动搬一次。
- 新增 `douyin/cookies.py`：从本机 Edge/Chrome 读 cookie（DPAPI 解 `v10`）。
  `v20`（App-Bound Encryption）解不开，会明确报错而不是静默失败。
- 新增 `/dy cookie`（别名 `cookie` / `登录态` / `ck`）：打印登录态、身份、sessionid 长度、
  `sid_guard` 剩余时间、ttwid/csrf、内核、上次复核与上次保活；`/dy cookie import edge` 现场搬。
- 新增 `browser.cookie_source`（auto/edge/chrome/profile）与 `browser.cookie_keepalive_min`（默认 20）：
  登录态丢了先试着从本机浏览器借，借不到才叫你扫码；平时按间隔做首页心跳。
- **`sid_guard` 解析修正**：`hash|签发时间|有效期秒数|过期日期`，第 2 段不是过期时间，
  第 4 段才是。早先按第 2 段算会把刚登录的会话判成「已过期」。
- 登录态判定现在同时看 `/user/profile/self/` 与 `sid_guard` 到期时间；快到期直接按需要登录处理，
  触发「搬窗口 + 发二维码」那条求助流程。
- `_open_video` 校验页面：`data-e2e="error-page"` 或没有播放器就换 `?modal_id=` 入口重试，
  再不行跳过这条。以前这种情况会一路走到「评论编辑器没出来（视频可能关闭了评论）」，
  让人以为是评论关了。
- 分享面板行名去掉「 分享」后缀（以前缓存键会变成 `yezheyue 分享`）；
  刮到 uid 就落盘 `work/uid_cache.json`，之后 @ 人不用再开视频页。
- 界面发评论时挂 `response` 钩子接 `/comment/publish/` 的回包，把 cid 捡回来（以前界面路径拿不到 cid，删不掉）。
- 评论被要求二次验证（`second-verify-mask`）时按人机验证处理：搬窗口 + 叫你，而不是只回一句失败。
- 实测（Edge 内核，真实 profile）：取流 69 条/15.2s、下载 17.0MB、点赞与取消、分享、@ 评论带 cid 并删除成功。

## 0.7.0 — 2026-11-04

**发布到 GitHub + 自带更新检查**

- 仓库：<https://github.com/kianakaslana20/astrbot_plugin_douyin_learning_bot>，MIT
- 新增 `/dy update`：读本地 `metadata.yaml` 的 `version` 与 `origin/main` 比对，
  列出落后的提交；`/dy update apply` 直接 `git pull --ff-only` 并提示重载
- 不是 git 克隆（解压装）时给出改用 clone 的说明
- 补齐仓库文件：`CHANGELOG.md`、`LICENSE`、`.gitignore`、`.gitattributes`、`docs/DESIGN.md`
- `metadata.yaml` 的 `repo` 指向真实仓库（AstrBot 的插件信息页会用到）

## 0.6.5 — 2026-11-04

**选片收紧：两道门 + 自检命令**

- 新增 `feed.min_knowledge_hits`（默认 1）：标题/话题里至少命中一个「真知识词」，否则不学
- 新增 `feed.min_score`（默认 4）：打分下限
- `KNOWLEDGE_HINTS` 补技术实词：`开源 / 论文 / 算法 / 架构 / 模型 / 大模型 / 推理 / 训练 / 参数 / 评测 / 干货 / 入门 / 进阶 / 踩坑 / 工具 / 插件 / 部署 / 配置`
- **把 `AI` 从知识词里移除**：它几乎出现在每条 AI 题材娱乐视频标签里，等于后门；只保留在兴趣词里加分
- 新增 `/dy calib [n]`：抓一批样本，回报过筛率、会学哪些、阈值建议
- 修：`main.py` 两套导入（包内相对导入 / 顶层模块兜底）不同步导致 `/dy calib` 报 `NameError`

实测：58 条样本 → 16 条过筛（28%），端到端仍正常出片。

## 0.6.4 — 2026-11-04

**评论改走接口，@ 用 `text_extra`**

- 带 @ 的评论也走 `comment/publish/` 接口：正文追加 `@昵称`，`text_extra` 带 `{start, end, user_id}`
- `user_id` 取自**分享面板的 `data-userid`**（评论与分享共用一份好友数据）；`sec_uid` 服务端自动补全
- 回包给 `cid` → 能回读（`insert_ids`）、能删除；界面路径降级为兜底
- 分享面板 hover 加 4 轮重试（一次不展开是常态）

实测：13/13 全过（取流 / 下载 / 抽帧 / 拼图 / 点赞复原 / 评论@ / 删除评论 / 分享）。

## 0.6.3 — 2026-11-04

- 切号后自动开窗（`quiet_window` 场景下扫码需要窗口在桌面上）

## 0.6.2 — 2026-11-04

**多开：一个 profile 一个号**

- 新增 `/dy account`（别名 `账号` / `号` / `多开`）：列 profile、切号、`new <名字>` 新建空 profile
- profile 存 `data/plugin_data/<插件>/profiles/<名字>`，`default` 复用老目录 `browser_profile`
- 切号会重建 `DouyinBrowser` / `Pipeline` / `Social`（Playwright 持久化 context 不能改路径，只能重建）
- `state.json` 记录 `active_profile`

## 0.6.1 — 2026-11-04

**求助路由：出事找人**

- 新增 `notify.help_target`；留空时按「起这一轮的会话 → `push_target` → 管理员私聊 → 最近会话」逐个试
- 掉登录、弹验证、`push_target` 缺失都改走这条路由
- 发不出去时在任务错误里明确说「这条求助没送到任何人」

## 0.6.0 — 2026-11-04

**人机验证：搬窗口 + 叫人**

- `HumanVerifyNeeded` 异常贯穿取流 / 写操作；检测覆盖 `#captcha_container` 等选择器与「请拖动滑块」等文案
- 撞上就用 CDP `Browser.setWindowBounds` 把窗口挪到桌面 `(80,60,1280,900)`，发消息让你拖滑块
- 等你处理完（默认 5 分钟）自动重试本轮
- 新增 `/dy verify`：手动搬窗口 + 盯验证
- 评论接口加空回包指数退避重试（抖音对 publish 有频率限制）

## 0.5.2 — 2026-11-04

**二维码取真图**

- 二维码不是 canvas，是 `<img src="data:image/png;base64,...">` → 直接 base64 解码落盘，不再截图裁剪
- 取不到二维码时**返回 None 而不是发白图**，文案改成「发 /dy login 再试」
- 修：`QR_CANDIDATE_JS` 类属性缺失（兜底分支一踩就炸）

## 0.5.1 — 2026-11-04

- 处理「一键登录半登录面板」（没有二维码的那种屏）：先点「登录其他账号」切到扫码屏
- 二维码候选筛选从「按锚点文字」改成「方框尺寸 + 黑占比 + 黑白切换密度」

## 0.5.0 — 2026-11-04

**后台化**

- 新增 `douyin/jobs.py`：单 worker 串行队列，`/dy start` 立即返回，跑完推送
- 静默窗口：`--window-position=-32000,-32000` 藏到屏幕外，配三条关降频的参数
- 掉登录时抓二维码发聊天、等人扫码后自动继续
- 新增 `/dy jobs`、`/dy stop`；`notify` 配置组（九项）

## 0.4.0 — 2026-11-04

**配置面板化 + 白名单**

- 分享对象 / @ 人 / 文案 / 模式搬进面板（`template_list` 行式编辑）
- 新增 `actions.enforce_allowlist`：开了就只允许对白名单里的人动手
- 新增 `/dy cfg` 查看生效配置
- 坑：面板写 `template_list` 每行必须带 `__template_key`

## 0.3.0 — 2026-11-01

- 写操作三件套：点赞、评论（含 @）、分享给好友/群
- `action_mode` 三档（`off` / `ask` / `auto`）+ `(视频, 动作, 目标)` 级去重
- `/dy dom` 抓面板真实结构，用于校准选择器

## 0.2.0 — 2026-10-31

- 分享走「分享面板」通道（后默认改为私信面板，见 0.6.x）
- 评论支持 @ 候选点选

## 0.1.0 — 2026-10-31

首个可跑版本：

- headful Playwright 页面旁听取流（v1 tab/feed + v2 module/feed）
- 打分筛选、无水印直链下载、ffmpeg 抽帧、九宫格拼图
- STT 转写 + 视觉理解，出简报入 SQLite 知识库
- `/dy start` `/dy loop` `/dy kb` `/dy push` `/dy stop`