# 通知采集证据审查（2026-11-05）

## 结论与验证等级

- **真实通知 API 响应 schema：尚未获得。** 本次没有连接浏览器、读取账号或线上消息，也没有触发写操作。
- **历史本地报告**：`docs/DESIGN.md:1560-1569` 记录通知面板 selector、两条 `innerText` 示例与点击后 `modal_id`；没有原始通知网络响应、完整 DOM、data 属性或评论 ID 的证据。
- **当前实现的扩展字段与分支均为 synthetic**：`data-uid/data-from-uid/data-user-id`、`data-comment-id`、`data-aweme-id/data-item-id`、`data-event-id` 和链接查询参数的测试数据来自人工构造，不能据此宣称线上一定提供这些字段。

## 只读代码核对

### 原通知 DOM schema

原 `ROW_JS` 仅回传 `text/x/y/h/cls`，无 `attrs/nodes/links`。历史文字与坐标既不能恢复 `comment_id`，也不能恢复可信的发送者 `uid`。昵称不是 uid，用户主页 `/user/MS4w...` 是 sec_uid 类型的路径值，不应标为数字 uid。

扩展实现把每条候选行自身的属性及其后代属性、链接保存在 `attrs/nodes/links`。当前约束：

- 不向父面板提取用户 ID，不从裸文本里的数字猜 ID。
- 发送者后代节点/用户链接文本须与该行提取的昵称一致；正文 `@另一人` 的链接不归给发送者。
- 同字段出现不同值时保留空值，不使用首个命中。
- 多条通知合并的容器跳过；此规则和行尺寸启发式仍需真实 DOM 验证。
- URL 仅接受相对链接或 douyin.com 及其子域；显式支持 `comment_id` 与 `aweme_id/modal_id/item_id`。**尚未支持 `cid` 别名**，因为没有证据证明该位置的 cid 总是当前通知评论 ID。

### 点击通知后的 URL

`Notices.open_mention()` 当前仅从 `AWEME_RE` 获取视频 ID（`modal_id`、`/video/`、`/note/`、`aweme_id`），**没有读取点击后的 `comment_id` 或 `cid`**。本次修复比较点击前后视频 ID，旧页面 ID 未变化时返回空值，避免把旧 modal 归给当前通知。

即使将来观察到 URL 有评论 ID，也应核对：它是否来自当前通知点击、是否与旧 URL 不同、是否对应本条通知而不是回复父评论/页面高亮状态。当前缺 ID 的通知应保持待补；不得用顶层评论冒充精确回复。

## 公开网页与本地探针查证

| 来源/动作 | 结果 | 能否证明通知 schema |
|---|---|---|
| 插件目录 `.har` / `*probe*` 文件检索 | 未找到 | 否 |
| `E:\工作区\_probe_notice.py:37-59,86-99` | 读取面板后的全页 URL/部分链接，只 print；保存截图，未点击具体通知 | 否；探针源码不是抓包结果 |
| `E:\工作区\_probe_notice2.py:39-55,88-95` 与 `E:\工作区\_probe_notice3.py:24-51,75-86` | 只提取面板文字/class/data-e2e；没有 comment_id、cid、链接或通知响应采集 | 否 |
| `E:\工作区\_probe_dm5.py:105-127` | 私信分享卡片点击后 URL 输出，不是通知点击；未解析 comment_id | 否 |
| `E:\工作区\_live` 中通知命名产物 | 找到 notice2.png、notice3.png、notice-diag.png；未找到通知专属文本/JSON/HAR 响应。未读取浏览器 profile | 否；仅文件清单核对，没有把截图当成结构证据 |
| `docs/DESIGN.md:1545-1571` | 有历史 DOM 文本与 modal_id 叙述，无响应原文 | 否 |
| `https://www.douyin.com/` 普通无账号 HTTP GET | HTTP 200、72914 字符，命中 `window.byted_acrawler.init` 与 nonce/signature 逻辑；没有外链 script src | 否；收到前置签名页面，而非已登录通知应用 |
| web_search | 缺少 `DEEPSEEK_API_KEY`，未执行有效搜索 | 否 |
| 公开开源参考 `raw.githubusercontent.com/Johnserf-Seed/f2/main/f2/apps/douyin/api.py` | DNS `getaddrinfo ENOENT raw.githubusercontent.com` | 否；未读到源码，不推断其字段 |

## 可复现 synthetic 检验

```powershell
python -m unittest discover -s tests -p test_notice_events.py -v
```

18 项通过：文本兼容/评论回复分类、时间位置、明确属性和链接、sec_uid 与外域过滤、冲突留空、发送者隔离、旧 modal 回归、事件 key 优先级和缺字段待补，以及 Node 执行实际 `ROW_JS` 配合最小 DOM facade。

**这些是已运行的 synthetic 单元测试，不是浏览器/线上通知验证。** 此审查没有新增任何基于猜测的通知 API 地址或响应字段。
