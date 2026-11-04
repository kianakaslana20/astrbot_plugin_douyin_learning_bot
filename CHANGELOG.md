## 0.11.2 —— 解出来的私信真的会回：WS 事件提升成一等条目

0.11.1 只做到「解对」，还差一步：解出来的私信只落了一条 `ws_evidence` 证据就没了，
`dm_judge` 一直是空表 —— 也就是「能看见、但不会回」。

### 改

- **界面没读到时自己造条目**：协议层已经拿到发送者、内容和时间，就不该因为界面没跟上
  （面板没渲染、对方秒删）而丢掉。现在直接造一条 `Entry(kind=dm_text/dm_share)` 塞进
  同一轮的处理列表，**白名单、限流、静默时段、模型判断、手动确认全部照旧生效** ——
  走的还是原来那条链路，不是旁路。
- **界面里已经对得上就不另造**：`candidates` 非空时只做身份补全（把 `event_id` /
  `from_uid` / `timestamp` / `protocol` 写进那条 DOM 条目），绝不重复回一遍。对上多条
  （同名不同人）时既不当公开评论、也不造条目，保留证据等下一轮。
- **认领去重**：`_ws_claimed(who, text, ttl=180s)` —— WS 已经处理过的同人同文，后面 DOM
  轮次扫到直接记 `ws_dup` 跳过，防同一条私信回两遍。补过 `protocol` 标记的条目不受这条
  约束（它就是本轮要处理的那条）。
- 日志仍然是那五个数：`解码 N 条 / 读到 M 条 / 新 K 条 / 模型判断 reply X 条 / 发出 Y 条 /
  跳过 Z 条`，跳过原因会写出来（`WS 已经认领这条私信`、`刚推过来的，留给 WS 那轮` 等）。

### 实测

- 真机私信「收得到吗」的帧解出 `event_id=im:0:1:2634023404581211:4330304429101319:7704390662654182971`、
  `content` 一字不差、`from_uid=2634023404581211`（uid 缓存反查 → yezheyue）。
- `tests/` **80 项全过**；老回归 38 / 23 / 23 / 22 / 17 全绿。

### 仍缺

- `frontier-pc`（平台互动通知）还是没有任何业务帧证据，那一路继续是「收到帧 → 去界面读」。
- 评论区的「真的回复某条评论」还依赖界面能读到评论 id；读不到时退化成在视频下发一条 @。

## 0.11.1 —— 真机抓包回来改的：50001 不是消息

0.11.0 的 protobuf 解析只有 synthetic fixture 背书。这一版是拿**真机抓到的帧**
（`action_dump` 打开时落盘的 `ws_dumps/frames.jsonl`）回头校出来的：

- **真实帧结构**（1.2 KB，frontier-im 收到）：`1=seqid 2=logid 3=service 4=method
  5=headers 6=encoding(空) 7='pb' 8=payload`；payload 里 `1=cmd(500)`、`6=body`、
  body 的 `500=NewMessageNotify`，其中 `5=MessageBody`。和钉住的第三方 schema 对得上。
- **`message_type=50001` 是红点/已读状态同步**，不是聊天消息：它的 content 是
  `{"command_type":14,...,"mute_read_badge_count_info":[...]}`。这类帧一分钟来好几条，
  0.11.0 会把每一条都当成「有动静」→ 每一帧都去界面读一次。现在归为 control：
  记诊断、不 dirty、不产生事件。
- **信封判定放宽**：原来要求 `1..4` 字段全在，现在只要认得出是带信封的帧就往下走，
  免得少一个字段就被判成未知帧。
- 真实帧 fixture 只留在本地（`tests/_real_frames.txt` 已进 `.gitignore`，里面有本人 uid
  和 `client_message_id`），测试文件在 fixture 缺失时自动 skip。

实测：改完后同一段时间里 `事件守护(ws)` 轮次从「每帧一次」变成 **0**，
4 条真实帧全部 `control_message:50001 / dirty=False`。

## 0.11.0 — 真的把推送解开了：protobuf 业务帧 + 评论区接入

0.10.0 只是「看到 WS 上有动静就去读界面」。这一版直接解析推送里的业务帧，
私信 / 分享 / @ / 评论卡都能从协议层拿到 id、内容、时间。

- **新增 `douyin/wsproto.py`**：解 Frame(`payload=8/encoding=6/type=7`) → Response(`cmd=1/body=6`)，
  支持 `cmd=500`（NewMessageNotify）、`cmd=1099`（Stranger 陌生人私信）；内容层解文本(7)、
  作品分享(8,77)、评论卡(16,75,105)、richTextInfos 里的 @。
- **压缩与编码**：原生 `__lz4` raw block、gzip、JSON 三种都认；`hi/ok` 这类 ACK 不当业务帧。
- **有界**：Frame 1 MiB、解压后 4 MiB、字段 8192、嵌套 32 层、内容 256 KiB。畸形帧只丢那一条，
  不会把整个守护循环带崩。
- **事件 id**：`im:{会话}:{服务端消息 id}`，外加 `raw_frame_id = sha256(帧)`。同一条消息被
  Playwright 和 CDP 各报一次时按帧 sha 去重。
- **接页面更早**：`Browser._page()` 交出页面时先回调 `on_page_ready`，WS + CDP 监听在导航**之前**
  就挂上，页面加载期间建立的连接也能接管。
- **评论区/@ 接进事件流**：`notice.py` 区分「评论 / 回复 / @」，`inbox.py` 的 `event_entry()`
  把事件翻成收件箱条目；同一个人挂着的评论没回完，新事件直接丢（一次只回一条）。
- **保守处**：IM 里的「评论卡」「群内 @」是 IM 语义，不会拿去公开评论区回复；没有原始评论 id 时
  不冒充「回复某条评论」，改为在视频下发一条 @ 回对方；平台互动通知(frontier-pc)的 schema 还没实测到，
  那一路退回界面读取，日志会写明来源。
- **没有判断模型也不哑**：判断模型不可用时，私信走日常聊天、@ 走 `voice.reply`，照样回。
- 测试：`tests/` 78 项（含 wsproto 27、notice 18、event_runtime 33），老回归 38/23/23/22/17 全绿。


版本号规则：`主.次.修订`。次版本号进位 = 有新能力或有不兼容的配置变化，修订号 = 修 bug / 调参数。

## 0.9.5 — 守护首轮不补历史

- 私信守护启动后的第一轮只把已有消息记进收件箱（`backlog`），不逐条回复 —— 避免一装好就把积压的旧话全回一遍。
- 之后每轮照常：新的一句回一句。

## 0.9.4 — 私聊不限次数 + 跨平台记忆

- **不限次数**：`dm_chat_no_limit` 名单里的人私聊不受每小时上限约束。
- **私信守护**：`dm_chat_poll_s` 设成 60 就每分钟看一次私信，只读私信、不碰提醒。
- **自聊会话**：`dm_self_names` 让插件在"自己跟自己聊"的会话里靠「我说过什么」分辨谁在说话，不会自问自答。
- **记得 QQ**：`dm_identity`（抖音昵称=QQ号）填上后，读 AstrBot 的 `platform_message_history`，把你在这个 QQ 号下聊过的纯文本一起带进 prompt。
- 上下文：`dm_chat_history` 带最近几句，聊着不脱节。
- 单测 `_test_dy_memory.py` 14/0。

## 0.9.3 — 总结也要有预算

私信分享来的视频写总结，要跑一遍转写和读图，是这套东西里最贵的一步。

- 新增 `actions.rate_summary_per_day`（默认 20，0 = 不写）：每天最多写几份总结，超了当场说「这条先不写总结：每天已经做了 N 次总结」
- 读不出来（转写和画面都没东西）还是直接说「看不懂这条」，不硬编

## 0.9.2 — 总结也要有预算

私信分享来的视频写总结，要跑一遍转写和读图，是这套东西里最贵的一步。

- 新增 `actions.rate_summary_per_day`（默认 20，0 = 不写）：每天最多写几份总结，超了当场说「这条先不写总结：每天已经做了 N 次总结」
- 读不出来（转写和画面都没东西）还是直接说「看不懂这条」，不硬编

## 0.8.6 — 闸门：频率、静默、随机、幂等

写操作还没放开，先把「怎么不惹事」做掉。

- **频率预算**：评论 8/天、分享 10/天、点赞 30/天、私信 6/小时，计数落 `dy.db`，重载不清零。四档分开——点赞便宜、评论最贵，不能共用配额。
- **静默时段**：`quiet_hours`，支持跨午夜；这段时间一个动作都不做，回执里说明「静默时段，这轮不动手」。
- **随机间隔**：`action_jitter`，取 `action_delay_s` 的 0.6~1.8 倍，15% 概率再拖长到 2.4 倍。固定节奏是最响的指纹。
- **待执行过期**：`pending_ttl_hours`（默认 12），`ask` 模式攒的动作放太久会作废并说明原因。
- **幂等收件箱**：新表 `inbox(kind, key, src, payload, seen_at, handled_at, result)` + 唯一索引。私信视频、@ 提醒、私信消息都从这里记账，同一条只处理一次。
- **`channel_strict`**：内核起不来就报错，不再静默退回 chromium（那等于换机器）。
- **`profile_pin`**：钉死 profile 目录，跳过自动迁移。
- **`keep_days`**：保留期清理，库里的行 + `work/` 里的视频帧图一起清（`0` = 不清；`.json`/`.db` 不动）。

自测 22 项全过（闸门四档、跨午夜、inbox 幂等、保留期、随机区间），命令/队列/验证/文案/uid 缓存回归全绿。

# 更新日志

## 0.8.5 — 2026-11-04

**评论不再套模板：交给对话模型 + AstrBot 人格写**

- 新增 `douyin/voice.py`（`Voice`）：取 `context.persona_manager` 的人格 prompt，
  用 `context.get_using_provider(umo)` 那个对话模型，把**标题/作者/摘要/关键词/听写**
  连同 4 格抽帧拼图一起送过去，要一句人话
- 生成后自检：AI 腔黑名单（首先/其次/综上/这个视频/干货满满…）、多行、列表、空
  → 命中带「重写一句更像人话的」再来一次；两次都不行退回模板（宁可平淡，不要 AI 腔）
- 剥掉模型自己加的 `@某某` 和引号（@ 由发评论那层加，避免 @ 到错的人）
- 新增配置 6 项：`voice_enabled`（默认开）、`voice_persona`（人格名，空=会话默认）、
  `voice_max_chars`（40）、`voice_frames`（1=送拼图，0=只看文字）、
  `voice_timeout_s`（60）、`voice_banned`（额外禁词）
- `/dy comment` 不带正文时也走模型（原来是把摘要塞进模板）；
  `/dy cfg` 里显示「评论文案：模型按人格写 / 模板」与人格名
- 单测 `_test_dy_voice.py` 17 项全过；顺手补齐 `_test_dy_bg.py` 的桩
  （`show_window` / `keepalive` / `session_info` / `raw_cookies`，v0.8.2 之后漏的）

实测：人格确实进了 system prompt（`[大肥鱼]` / 大肥鱼那段鲸鱼娘设定），
图片以 `file:///` 送进 `image_urls` 并被 `resolve_image_ref_to_base64_data` 解成 data URL。

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

## 0.9.1 — 私信日常聊天

别人发来的文字也能读、能按人格回一句。

- 新增 `actions.dm_chat_enabled` / `dm_chat_allow` / `dm_chat_max`：默认关、白名单空 = 谁都不回
- `Inbox.collect_dm` 除了分享会话，也会进白名单里的普通会话读最后几条文字
- 新增 `Voice.chat()`：一到两句、接住对方意思、不用敬语；撞禁词让模型重说一次，仍不合格就不发
- `_handle_dm_text` 串起「开关 → 白名单 → 每小时上限 → 生成 → 发送 → 记一次」
- **修**：私信会话点错了人 —— 原来按坐标点列表行，聊天视图会盖住列表，读到的常是上一个人的消息。
  改成在 DOM 里按名字派发点击事件 + 用顶部标题核对进对了谁，并滤掉「打个招呼吧」这类占位文字

## 0.9.0

**能读你的私信和提醒了。**

- 新增 `douyin/im.py`：私信抽屉。会话列表、会话内消息、分享卡片、发文本。两个硬约束写进代码注释：入口必须 hover 后点击（JS click 出来 0×0）；分享卡片 DOM 里没有 aweme_id，只能点开卡片从地址栏 `modal_id=` 取。
- 新增 `douyin/notice.py`：「通知」面板里的 @ 提醒，文本直接可读；取「面积最小的『通知』」而不是「没有子节点的『通知』」，因为图标字体没加载完时叶子节点还不存在。
- 新增 `douyin/inbox.py`：收件箱。两条来源合成统一 `Entry`，`fetch_item` 用 `aweme/detail/` 拉详情。
- 新增 `douyin/voice.py::reply()`：回别人 @ 我的评论，先答问题再补半句，最多两轮，仍走禁词校验。
- `main.py`：`/dy inbox`（只读扫描 / 加「处理」才动手）、`/dy dm 昵称 文本`；`_inbox_scan` / `_inbox_handle` / `_pending_entries`（处理的是库里攒着的，不只本轮扫到的）。
- 复用 0.8.6 的闸门：`inbox` 表幂等、频率限制、静默时段、`pending_ttl_hours`、`keep_days`。
- 私信白名单 `dm_allow` 与分享/@ 白名单分开：分享错人只是尴尬，私信发错人是骚扰。


## 0.10.0

**从「定时轮询」改成「事件触发 + 模型自己判断」。** 起因是：私信要等最多 60 秒、刷视频只看时间不看内容。

- 新增 `douyin/im.py::ImWatcher`：挂 `page.on("websocket")` 监听 `frontier-im` / `frontier-pc`，
  把非 `hi` 心跳帧当「有动静」的信号。实测空闲期只有心跳，业务帧是 protobuf —— 所以不解包，只当信号。
- 新增 `douyin/agent.py`：`handle_douyin_event()` 返回 `{reply, text, reason}`，
  `judge_round()` 返回 `{run, take, reason}`；人设取 AstrBot 人格、模型复用主核心 provider。
  没有可用模型时返回 `model:false`，调用方退回旧规则路径（不会因为模型挂了就不回话）。
- 私信守护重写：WS 信号即时读 + WS 静默 5 分钟退回 `poll_fallback_min` 兜底轮询，日志标 `(ws)` / `(poll)`。
- 新增 `dm_judge` 表：event_id / type / from_nick / content / decision / reason / created_at。
- 自主刷：`_auto_think_loop` 每 5 分钟看条件（超过 `loop_interval_min` ／有人分享评论 ／你最近 30 分钟有互动），
  问过模型才排任务；受 `auto_rounds_per_day`、`auto_items_per_day` 两道预算约束。
- `dm_targets` 里的人不再受私信条数限制（静默时段仍然拦），别人的评论同一 uid 只回一条。
- 顺手修：`_handle_dm_share_event` 调 `pipeline._handle_one` 少传了 score 参数。

## 0.9.7 — 2026-11-04

**能主动发私信了。**

- 新增 `im.py: send_dm / resolve_target / send_text / sent_ok / editor_text / panel_text`，
  `social.send_dm` 做门面；走界面，不走接口（imsdk 在 WebSocket 里，拼不出纯 HTTP）。
- 新增 `dm_outbox` 表（`target_uid / text / status / created_at / sent_at / error`），
  与 `inbox` 分开：一个是「别人发来的」，一个是「我要发出去的」。
- 配额独立核算：`rate_dm_out_per_hour=3`、`rate_dm_out_per_day=20`；
  `dm_quota_separate=false` 时并回日常回话那一池。
- `action_mode` 三态接入：off 不发 / ask 进发件箱等 `/dy confirm` / auto 直接发。
- 三个实测坑：
  1. `[data-e2e="msg-input"]` 只是外壳，真正能打字的是里面的 `div.editor-kit-container`；
  2. 会话行 `innerText` **没有换行**（`昵称10:37预览`），名字要从 title 元素取再把时间削掉；
     会话行上也没有 `data-userid`（那只在分享面板里），纯数字 uid 先用缓存换昵称；
  3. 回读不能用「最后一条消息节点」——分享卡会盖在最后，读到的是卡片作者名；
     改成「输入框已清空 + 抽屉里出现这句话」。
- 回读策略修正：先等 6 秒看渲染，确认没发才补一次，避免重发导致两条一样。

## 0.9.6

### 修：窗口看不见 / 私信收不到 / 登录老掉（同一个根）

- `browser.profile_dir` 指到 chromium 基目录、`channel` 要 msedge → 内核锁拦启动，浏览器一次都没起来。
  - 新增 `_resolve_profile()`：`profile_dir` 填到基目录时自动认锁、落到 `<内核>` 子目录。
- `quiet_window=false` 时显式 `--window-position=80,60 --window-size=1280,900`，不再继承 profile 里的屏外/最小化位置；启动后补一次 `show_window()`。
- 私信守护每轮先把页面带回首页（私信抽屉只在首页顶栏）；`ProfileKernelMismatch`/`DouyinBlocked` 会私聊主人一次（10 分钟节流）；每轮记录「读到 N 条 / 新 M 条」。
- 文档：三件事的因果链、登录被顶号的三个来源、重载不重导子模块的坑。
