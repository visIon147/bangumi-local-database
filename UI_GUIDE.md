# Bangumi Local Database UI 使用指南

这份指南面向使用本机 Web UI 管理 Bangumi 收藏和 Steam 游戏库的用户。UI 是现有安全同步引擎的可视化入口：它不会绕过不可变计划、人工审阅、fresh preflight、备份、远端验证和审计流程。

## 1. 启动与关闭

首次使用或项目升级后，先同步依赖并升级数据库：

```powershell
uv sync
uv run bld db upgrade
```

启动 UI：

```powershell
uv run bld ui serve
```

默认地址为 `http://127.0.0.1:8765/`。不希望自动打开浏览器时：

```powershell
uv run bld ui serve --no-open-browser
```

需要编辑非默认环境文件时：

```powershell
uv run bld ui serve --env-file path\to\.env
```

在运行 UI 的终端按 `Ctrl+C` 可正常停止服务。UI 只能绑定 `127.0.0.1`、`::1` 或 `localhost`，不要通过反向代理、端口转发等方式暴露到局域网或公网。

## 2. UI 的安全模型

请先理解以下四点：

1. 打开页面不会访问 Bangumi 或 Steam 网络接口。
2. 联网读取必须通过明确按钮进入后台任务；需要联网的表单会在界面中说明。
3. 修改 Bangumi 的操作必须先生成不可变计划，不存在“一键同步全部并写入”的按钮。
4. UI 中的 Agent、推荐或候选结果不能直接 apply，最终写入仍由确定性 application service 完成。

Bangumi 写入流程固定为：

```text
选择目标
→ 生成不可变计划
→ 查看“将修改 / 本次不修改”
→ Review 并输入完整 Plan ID
→ Fresh preflight
→ 再次核对最终清单
→ 输入完整 Plan ID 并使用短时一次性确认
→ 写前备份
→ 串行 POST/PATCH
→ GET 验证
→ 审计与反向草稿
```

如果 LOCAL、REMOTE 或来源证据在计划生成后变化，条目会标为 stale，不会静默覆盖。

## 3. 推荐的首次使用顺序

1. 打开“设置”，确认数据库 schema、Bangumi 和 Steam 配置状态。
2. 执行“联网验证 Bangumi 认证”。
3. 打开“同步”，选择 subject 类型与图片策略后生成两阶段 Pull 计划。
4. 在“任务”中打开计划工作台，逐项检查更新/冲突，再独立 review、fresh preflight 与 apply。
5. 通过“作品”浏览本地镜像。
6. 如需 Steam 功能，先在“Steam → 检测”确认本地数据源，再执行导入预览。
7. 在小批量条目上熟悉 Plan Review/Apply，再进行更大范围操作。

## 4. 导航与页面说明

### 概览

首页显示本地作品、Bangumi 收藏、待审阅/可执行计划、活跃评分队列和探索会话数量。该页面只读取 SQLite。

### 作品

“作品”是统一的本地收藏浏览页，覆盖书籍、动画、音乐、游戏和三次元，也可包含没有 Bangumi subject 的 Steam-only 游戏。

可执行的操作包括：

- 按标题、类型、来源、收藏状态以及个人 Tag 组合筛选。
- 标题、类型、来源和每页数量默认显示；收藏状态与 Tag 条件位于“高级筛选”，存在高级条件时会保持展开。
- 标准多选可使用 Ctrl/Command 或 Shift；启用 JavaScript 后，再次添加已选 Tag 即可取消，也可以点击 Tag chip 上的 ×。
- 个人 Tag 支持“命中全部/任一”和“排除任一”；匹配精确且区分大小写。
- 每页可选 12/24/48/96 项，支持页码和指定页跳转；筛选条件会随分页保留。
- 查看 Bangumi identity、收藏状态、个人 Tag 和已缓存封面。
- 编辑 Bangumi 收藏的 LOCAL rating/status/comment/private 字段。
- 编辑游戏专属的本地 profile、游玩描述和私有备注。

这些编辑只修改 LOCAL。要写回 Bangumi，必须再从“同步”生成 sync plan。

### 同步

“同步”页面包含以下入口：

- Pull Plan：fresh-read Bangumi 收藏并生成 v5 本地合并计划；此时不改变收藏镜像。
- Cached Status：只用当前 LOCAL/BASE 查看状态。
- Fresh Status：通过后台任务读取 REMOTE 后重新比较。
- Shadow Bootstrap：安全建立缺失的同步基线。
- LOCAL Collection Edit：编辑本地收藏字段。
- Sync Plan：按 subject ID 或全部本地变化生成 v2 计划。
- 冲突处理：对每个字段选择保留 LOCAL、采用 REMOTE 或输入自定义 JSON 值。

冲突处理本身不写 Bangumi。选择保留 LOCAL 或自定义值后，需要另行生成 sync plan。

### 通用计划工作台

计划中心按类型展示封面、个人/公开 Tag、Steam 分类、规则与 `BASE / LOCAL / REMOTE / INTENDED` 字段差异。draft 的批量 Tag、分类、sync、Steam/Discovery 状态和 Pull 计划可逐项排除或恢复；分类人工项还可选择两个已配置的个人分类 Tag。保存调整只会创建新的 successor draft，并在同一事务取消旧 draft，不会 review、apply 或访问 Bangumi。

任务完成后若产生 plan/session，任务页会显示直接入口；原始 JSON 仅保留在折叠的诊断区。reverse、recovery 以及已 review/applied 的计划保持只读。

### Tags

Tag 页面可以生成以下不可变计划：

- 批量添加个人 Tag。
- 批量删除精确匹配的个人 Tag。
- 批量重命名并保持原有顺序和稳定去重。
- 通过 IDs、当前全部收藏或自定义 public Tag 选择目标。
- 为通用操作选择全部、书籍、动画、音乐、游戏或三次元范围。
- 对尚未分类的游戏生成 `Galgame分类` 候选计划。

计划生成前后都会保留“将修改”和“本次不修改”的完整对账清单。

#### TagIndex 大小写碰撞提醒

Bangumi 服务端 TagIndex 对大小写可能执行扩展。例如个人 Tag 与公开 Tag 发生 `Galgame` / `galgame` 碰撞时，服务端可能返回两个 Tag，导致写后 canonical verify 失败。

建议个人分类 Tag 使用不与纯英文公开 Tag 碰撞的名称，例如：

- `Galgame分类`
- `普通Game分类`

发生异常时不要扩大批量范围，应先检查计划审计和远端实际值，再通过独立恢复计划处理。

### 计划

“计划”是所有 Bangumi mutation 的核心入口。

计划详情会展示：

- 计划类型、format version、状态和 content hash。
- “将修改”条目及精确字段/payload。
- “本次不修改”条目及原因。
- apply run、逐项 remote operation、HTTP 状态和时间。

#### Review

Review 只把 immutable draft 标记为 reviewed，不会写 Bangumi。必须输入当前页面显示的完整 Plan ID。

#### Fresh preflight 与 Apply

1. 在计划安全操作页启动 Fresh preflight。
2. 转到“任务”查看最终将修改、stale 和读取失败项。
3. 任务成功后点击“核对结果并继续”。
4. 在短时确认页再次查看最终清单。
5. 输入完整 Plan ID 后提交 Apply worker。

确认只绑定当前浏览器 session、当前 plan content hash，默认五分钟有效且只能使用一次。Apply worker 开始后还会再次 fresh preflight。

#### Recovery 与 Reverse

- 成功写入会为实际成功项生成独立 reverse draft。
- uncertain 审计可通过 Recovery 入口生成恢复草稿。
- reverse 必须重新 review、preflight 和确认，绝不自动执行。

#### 取消 Bangumi 收藏

当前 Bangumi 公共 `/v0/` API 没有收藏 DELETE endpoint。UI 不会尝试未公开路由，也不会把 404 猜测为删除成功。

需要恢复“新建收藏”时：

1. 先在 Bangumi 网页手动取消收藏。
2. 回到对应 v3+ reverse 计划的安全操作页。
3. 运行“Fresh 验证网页已取消”。
4. 确认任务验证远端为 404。
5. 输入完整 Plan ID，执行 LOCAL/shadow reconciliation。

该流程只验证远端已经不存在，并对齐 LOCAL/shadow；不会发送 Bangumi 写请求。

### Steam

Steam 页面分为检测、导入、分类、库、未匹配、匹配计划和状态计划。

#### 检测与导入

- “检测”只检查本机 Steam 数据源是否可读，不显示账户 ID 或绝对路径。
- “导入预览”默认 dry-run，不写 SQLite，也不访问 Bangumi。
- 勾选 LOCAL apply 后通过持久任务更新本地 Steam inventory 和分类，仍不写 Bangumi。
- 只有显式允许网络时才使用 Steam Web 补全；本地自定义分类仍以本地文件为准。
- “标题补全”可对未知标题显式读取 Steam Store，也可保存人工标题覆盖；后续导入不会覆盖人工值。

#### 单条匹配

在未匹配列表中打开 AppID，可查看 Steam 标题、分类和 Bangumi 候选。联网搜索会显示候选标题、subject ID、URL、得分和理由。

可以：

- 确认一个 Bangumi subject。
- 输入其他 subject ID 重新验证。
- 标记明确无条目。
- 暂缓处理。
- 重新打开 no-subject/deferred 条目。

搜索无结果不等于“不存在”；只有明确执行 no-subject 才保存此决定。

#### 批量匹配计划

批量匹配支持 AppIDs、分类、分类正则和全部未解决选择器。页面会解释四种范围；全部未解决默认覆盖 eligible 项，硬上限 250，超过时要求缩小范围。`no_subject` 与 deferred 默认不重复出现，也可显式重新纳入。

计划页会冻结候选简介、封面、公开 Tag、链接、得分、首二分差和判定依据。候选图片可选择只登记或仅补齐缺图；自动建议仍可在 apply 前通过 successor draft 改成其他 subject、人工审核、no-subject 或 deferred。

Steam match plan 的 apply 只建立本地 identity 映射，不写 Bangumi。

#### Bangumi 状态计划

已确认 identity 后，状态计划可以根据 Steam 分类规则生成 Bangumi POST/PATCH 草稿：

- 名称包含“完结”的分类默认映射为 `done`。
- 精确“在用”默认映射为 `doing`。
- 未命中项默认只留本地；只有显式指定 remaining status 才加入计划。
- 可选 Tag 会生成独立 Tag draft，不与状态写入混合。

设置页可保存默认 `exact|contains|regex` 规则、关键词、目标状态和大小写设置。状态计划可以使用默认规则，也可以仅对本次计划覆盖规则和未命中策略。规则与 hash 会进入不可变计划；一个条目命中不同目标状态时只列为冲突。

### 评分

评分队列是固定数据集、固定顺序、可恢复的本地工作流。

创建时可选择：

- subject 类型和收藏状态。
- recently-updated、release-date、title、subject-type 等排序。
- 带 seed 的可复现 random 排序。
- 是否包含 deferred。
- 最大条目数和可选联网 enrichment。

在卡片中可评分、跳过或暂缓：

- 私有理由只保存在 SQLite，不进入计划或页面响应。
- 发布理由默认只在现有 comment 为空时进行。
- 替换已有 comment 必须填写完整公开 comment，并显式确认替换。
- item 建立后若 LOCAL rating/comment 被其他操作改变，会标为 stale 并拒绝覆盖。

评分完成后，“生成评分同步计划”只生成 v2 rate/comment draft，仍需独立 review/apply。

### 探索

Discovery 用于建立有界候选队列和持久审核决定，不会扫描整个 Bangumi 游戏库。

支持三种来源：

- Steam 本地游玩、分类、安装和 owned 证据。
- 带关键词的 Bangumi game search。
- 至少指定 year 或 platform 的 Bangumi browse。

可以记录 `played|not_played|unsure|deferred`。这些决定只保存在本地，并会在未来 session 中抑制重复候选，除非显式 reopen。

`played` 不会自动推断为 `done`。Promotion 分为两个独立动作：

1. 明确建立或确认 work/Bangumi identity。
2. 用户另选 `wish|done|doing|on-hold|dropped` 后生成状态草稿。

### 图片

Steam 本地封面扫描位于 Steam 子导航，Bangumi 封面策略位于同步页面；缓存校验与清理保留为高级工具。打开这些页面不会自动扫描磁盘或联网。

图片策略：

- `none`：完全跳过图片。
- `metadata`：只登记安全的远端引用或 Steam 逻辑定位信息。
- `missing`：只补齐尚无 blob 的来源，已有缓存不会重复 GET。
- `refresh`：显式强制重新下载远端图片。

可用操作：

- 扫描 Steam `librarycache` 中的本地封面。
- 显式联网下载最多 200 个已登记的远端图片来源。
- 校验缓存文件的 SHA-256、MIME、大小和路径。
- 输入 `PRUNE` 后清理项目私有缓存中的未固定项。

Steam 原始文件不会被删除；SQLite 只保存可移植逻辑定位，不保存机器绝对路径或图片二进制。

### 任务

所有长时间或联网操作都会进入持久任务。任务页展示：

- queued/running/succeeded/failed/cancelled/interrupted 状态。
- 当前阶段、进度和脱敏事件。
- 脱敏结果，例如新 plan/session ID。
- queued/running 任务的取消请求。

程序重启后，遗留的 running job 会标记为 interrupted，不会猜测远端是否成功。请检查审计、fresh status 或重新生成计划后再继续。

### 设置、健康状态与帮助

- “设置”可维护 Bangumi Token/username/User-Agent/网页域名、Steam 路径/account ID/Steam ID64/API Key、图片与重试默认值和 Steam 状态规则。Steam 手机令牌、API Key 注册及账户 ID 查找请使用独立的 [STEAM_SETUP.md](STEAM_SETUP.md)。
- Secret 输入始终为空：留空保持，只有显式勾选才清除；Token/API Key 不进入页面响应、数据库、任务、日志或计划。
- 进程环境变量管理的字段只读；保存 `.env`/TOML 后必须重启 UI，后台 worker 不会热切换配置。
- 数据库 URL、plan/backup/media 目录不会从网页修改。
- “健康状态”检查应用与 SQLite 是否可访问。
- “帮助”在 UI 内展示公开 README、本指南和 Steam 配置指南，内容来自当前安装包。

## 5. 常见问题

### UI 提示数据库 schema 太旧

停止 UI 后执行：

```powershell
uv run bld db upgrade
```

升级会先创建 SQLite online backup 和 manifest。

### Bangumi 认证返回 401/403

检查本地 `.env` 中的 token、username 和 User-Agent。不要把 `.env` 内容粘贴到 issue、日志或 Agent prompt。401/403 会立即终止远端批次。

### 计划条目变成 stale

这表示计划生成后的 LOCAL、REMOTE 或 Steam 来源证据发生变化。旧计划不会自动扩大或重写；先 pull/fresh status，再生成新计划。

### 图片没有显示

常见原因：

- 当前策略是 `none` 或 `metadata`。
- 远端来源尚未执行显式下载。
- Steam `librarycache` 中没有对应文件。
- 图片被大小、MIME、像素、host 或路径保护拒绝。

可在“图片”页面查看统计并提交校验任务。

### 任务显示 interrupted 或 failed

先查看任务的脱敏错误与事件。对于远端写操作，还应检查计划审计和 fresh status。不要通过重复点击假设请求失败；不确定响应必须先 GET 验证。

### 表单按钮无法工作

安全 mutation 表单依赖本地 JavaScript 添加 CSRF header。确认浏览器没有禁用本页 JavaScript，并使用 UI 显示的 `127.0.0.1`/`localhost` 地址访问。

### 打开 Bangumi 后没有登录

外链会在当前浏览器的新标签页打开，并只能复用同一浏览器配置文件、同一 Bangumi 域名已有的 Cookie。先在设置页选择你实际登录的 `bgm.tv`、`bangumi.tv` 或 `chii.in`。如果 UI 被系统默认浏览器打开，而登录状态在另一浏览器，请用 `--no-open-browser` 启动，再在已登录浏览器中手动打开 `http://127.0.0.1:8765/`。本项目不会读取或迁移浏览器 Cookie。

## 6. CLI 仍然可用

UI 与 CLI 调用相同的 application service。需要脚本化、详细 JSON/CSV 或故障诊断时仍可使用 CLI，例如：

```powershell
uv run bld status --refresh-remote
uv run bld plan show <PLAN_ID>
uv run bld media verify
uv run bld steam unmatched
```

CLI 同样遵守 dry-run、完整 Plan ID、fresh preflight、备份、验证和审计规则。

## 7. 本地数据与凭据

以下内容应始终只保存在本机：

- `.env` 和 token/credential。
- 真实 SQLite、WAL、backup、plan、导出和日志。
- 图片缓存和 Steam 机器路径。
如需报告问题，请提供脱敏错误码、版本、schema 和可复现步骤，不要提供 Token、数据库、真实计划导出或私有理由。
