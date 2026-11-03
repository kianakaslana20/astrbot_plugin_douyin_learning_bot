# 更新日志

版本号规则：`主.次.修订`。次版本号进位 = 有新能力或有不兼容的配置变化，修订号 = 修 bug / 调参数。

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