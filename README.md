# Bangumi Local Database

Bangumi Local Database 是一个面向个人长期使用的本地收藏管理工具。它将 Bangumi 的书籍、动画、音乐、游戏和三次元收藏同步到 SQLite，并把 Bangumi 与 Steam 数据集中到带封面和详情的本机 Web UI 中管理。

项目的核心特点：

- **本地图像数据库**：按类型、来源、收藏状态和个人 Tag 浏览作品，展示 Bangumi 视觉图、Steam 封面、简介、链接和同步状态。
- **Bangumi 全类型同步**：统一管理书籍、动画、音乐、游戏和三次元收藏，并支持本地编辑评分、状态、评论、公开性和个人 Tag。
- **批量 Tag 管理**：批量添加、删除、重命名自定义个人 Tag，也可按公开 Tag 条件筛选并进行游戏分类。
- **Steam 数据输入与匹配**：读取本地 Steam 分类和库存，补全标题，搜索多语言 Bangumi 候选，并将确认结果转换为收藏状态计划。
- **队列式评分**：用固定、可恢复的卡片队列逐项评分、跳过或暂缓，再统一生成 Bangumi 同步计划。
- **队列式探索**：从 Steam 证据或有界 Bangumi 搜索建立候选队列，永久记录玩过、未玩过、不确定和暂缓决定。
- **任务与计划工作台**：实时查看批量任务进度，直接进入关联计划，预览同步差异并逐项人工调整；底层保留三方比较、备份、验证和审计。

## 快速开始

需要 Windows、Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)。Web UI 只监听本机回环地址。

### 从 GitHub Release 安装 wheel

```powershell
uv tool install https://github.com/visIon147/bangumi-local-database/releases/download/v1.0.0/bangumi_local_database-1.0.0-py3-none-any.whl
bld init
bld db upgrade
bld ui serve
```

如果 `bld` 尚未加入 `PATH`，运行 `uv tool update-shell`，重新打开终端后再试。`bld init` 会在当前目录创建不含凭据的 `.env` 和 `config/steam.toml`；已有文件永远不会被覆盖。也可以用 `--target-directory PATH` 指定工作目录。

### 从源码运行

```powershell
git clone https://github.com/visIon147/bangumi-local-database.git
Set-Location bangumi-local-database
uv sync
uv run bld init
uv run bld db upgrade
uv run bld ui serve
```

默认地址为 <http://127.0.0.1:8765/>。不希望程序自动打开浏览器时使用：

```powershell
bld ui serve --no-open-browser
```

首次打开后进入“设置”，填写 Bangumi Access Token、用户名和自定义 User-Agent。设置会原子写入本地 `.env`，Token 和 Steam API Key 永不回显；保存后需要重启 UI。

详细页面说明见 [UI_GUIDE.md](UI_GUIDE.md)，Steam 手机令牌、Web API Key、account ID 和 Steam ID64 配置见 [STEAM_SETUP.md](STEAM_SETUP.md)。这两份文档也可从 UI 顶部“帮助”离线阅读。

## 配置与本地数据

主要配置均位于被 Git 忽略的 `.env`：

```dotenv
BANGUMI_ACCESS_TOKEN=
BANGUMI_USERNAME=
BANGUMI_USER_AGENT=
BLD_BANGUMI_WEB_BASE_URL=https://bgm.tv

BLD_DATABASE_URL=sqlite:///./data/gamevault.db
BLD_PLAN_DIRECTORY=plans
BLD_BACKUP_DIRECTORY=backups
BLD_MEDIA_CACHE_DIRECTORY=data/media-cache

BLD_STEAM_ROOT=
BLD_STEAM_ACCOUNT_ID=
BLD_STEAM_CONFIG=config/steam.toml
BLD_STEAM_ID64=
STEAM_WEB_API_KEY=
```

默认数据库路径沿用 `data/gamevault.db`。`.env`、SQLite、计划、备份、日志和图片缓存都不应提交到 Git。API Token 只发送到官方 `https://api.bgm.tv`；UI 不提供自定义 API host 输入。

图片策略为 `none|metadata|cache`：

- `none`：不登记图片。
- `metadata`：只保存图片 URL，复用已有缓存。
- `cache`：下载经过 MIME、体积和像素验证的内容寻址副本。

补齐缺图不会重新下载已有缓存；强制刷新必须显式选择。

## Bangumi 收藏与同步

首次同步可以在 UI“同步”页面生成两阶段 Pull 计划，也可以使用兼容的 CLI：

```powershell
bld auth-check
bld pull
bld list
bld status
```

省略类型时覆盖 Bangumi 官方类型 `book|anime|music|game|real`。UI Pull 会先生成可视化本地合并计划；只有经过 review、fresh preflight、SQLite backup 和完整 Plan ID 确认后才更新 LOCAL，不会写 Bangumi。

在本地编辑收藏后，可生成精确的远端同步计划：

```powershell
bld collection edit 123456 --rating 9
bld collection edit 123456 --status doing
bld sync plan --subject-id 123456 --fields rate,type,comment,private,tags

bld plan show <PLAN_ID>
bld plan review <PLAN_ID>
bld plan apply <PLAN_ID>
```

生成计划不会写 Bangumi。Apply 前会 fresh-read LOCAL 和 REMOTE；发生 stale、冲突或读取失败时只会缩小执行范围，不会增加计划外条目。远端写请求串行执行，写后立即 GET 验证并记录审计；成功批次生成独立 reverse draft，绝不自动恢复。

## 批量 Tag 与游戏分类

通用个人 Tag 支持全部 Bangumi 类型，也可按类型缩小范围：

```powershell
bld tags bulk-add --tag "自定义Tag" --ids 123,456
bld tags bulk-remove --tag "旧Tag" --all-current --subject-type anime
bld tags rename --old-tag "旧Tag" --new-tag "新Tag" --subject-type game
bld tags classify-games --public-tag Galgame --galgame-tag "Galgame分类" --game-tag "普通Game分类"
```

计划会同时列出“将修改”和“本次不修改”，并保存个人 Tag 前后顺序、公开 Tag 依据和未修改原因。游戏分类中，公开 Tag 未命中只进入人工判断，不会自动归为普通游戏。

### Bangumi Tag 大小写碰撞

Bangumi 服务端的 TagIndex 查询可能不区分大小写。即使请求只提交 `Galgame`，远端仍可能扩展为 `Galgame` 与 `galgame` 两个个人 Tag。本项目会把这种 canonical snapshot 不一致判为异常并停止，不会静默视为成功。

分类 Tag 建议使用不会与已有英文 Tag 发生大小写碰撞的名称，例如 `Galgame分类` 和 `普通Game分类`。发生异常时先检查审计记录，再生成独立 recovery plan。

## Steam 本地库与 Bangumi 匹配

Steam 自定义分类以本地文件为准，网络默认关闭：

```powershell
bld steam detect
bld steam import
bld steam import --apply-local
bld steam titles complete --all-missing --allow-network
bld steam covers complete --all-missing --allow-network
bld steam unmatched
```

导入预览不会写数据库；`--apply-local` 也不会访问 Bangumi。未知标题可联网补全或人工覆盖，后续导入会保留人工标题。

“Steam → 本地图片”可扫描客户端已有的 `librarycache`，也可显式联网读取公开 Steam Store 元数据并从官方 CDN 补齐当前仍显示占位图的条目。远程补图优先纵向 `library_600x900`，横向 header/capsule 仅作回退；已有本地 Steam 或已缓存 Bangumi 封面默认不会重复请求。该 Store 接口并非 Steam Web API 的稳定公开契约，失败项会单独列出，不会被解释为游戏已移除。

匹配支持单条搜索和批量计划：

```powershell
bld steam match search 12345 --allow-network
bld steam match confirm 12345 --subject-id 67890
bld steam match plan --all-unmatched --candidate-images missing --allow-network
```

候选会冻结标题、别名、日期、简介、公开 Tag、链接、图片、得分和排序理由。高置信候选可以在计划中自动建议，低置信、DLC、Demo、同名版本和映射冲突必须人工确认。搜索无结果不等于不存在，只有显式 `no-subject` 才会记录为已确认无条目。

批量搜索默认最多 250 项并逐项更新任务进度。“终止整批”适合保守重试；“记录失败并继续”会把单项 transport、timeout、HTTP 或认证失败列入不修改区。连续认证失败会触发熔断，避免重复发出大量无效请求。任务完成但仍有人工作业时会显示“等待人工审核”，不会占用 worker。

顶部“工作台”统一提供任务与计划两个视图。任务生成计划后可直接跳转，计划也会显示来源和 apply/preflight 任务。历史记录可归档恢复；永久删除仅限没有计划、审计或 successor 等引用的终态任务及未执行计划，并会先创建 SQLite 备份。

人工匹配筛选会解释“将修改/不修改”与各原因的含义。每次决定都会生成 successor 草稿，保留当前筛选和页码，并在提交期间锁定按钮以避免重复操作。计划执行可从详情页直接“审阅并开始 Fresh Preflight”；完成后通过页面顶部的确认入口继续，无需手工复制 Plan ID、Job ID 或 nonce。

作品页可按标题、评分、发行时间、Bangumi 收藏更新时间或本地更新时间升降序排列；空评分和空日期始终放在末尾。Steam 库另支持按 AppID、游玩时长、最近游玩、首次/最近发现和匹配状态排序。

Steam 分类到 Bangumi 状态的规则位于 `config/steam.toml`，支持 `exact|contains|regex`、大小写设置和未命中策略。状态计划会为已收藏条目生成 PATCH，为未收藏条目生成 POST；Tag 始终作为独立后续计划。

## 评分与探索队列

评分队列保存固定成员和顺序，支持评分、跳过、暂缓、恢复以及私有理由。私有理由不会进入计划导出或 Bangumi comment；评分完成后仍通过普通同步计划写入远端。

```powershell
bld rating queue create --order random --seed 20260828 --max-items 50
bld rating queue list
bld rating sync-plan <SESSION_ID>
```

Discovery 只创建本地候选和永久审核记录，不遍历整个 Bangumi 游戏库，也不会因 `played` 自动创建收藏或推断为 `done`：

```powershell
bld discovery session create-steam --max-items 50
bld discovery session create-search --query "metroidvania" --max-items 50 --allow-network
bld discovery decide <SESSION_ID> <CANDIDATE_ID> --decision played
```

UI 中两类队列均采用单卡片导航，可补全不超过 200 项的公开元数据和图片。删除 Discovery session 前必须输入完整 ID 并创建 SQLite backup；全局决定和审核事件仍会保留。

## 使用注意事项

- UI 只允许绑定 `127.0.0.1`、`::1` 或 `localhost`。不要用反向代理将其暴露到局域网或公网。
- Bangumi、Steam 等外链在新标签页打开，只能复用同一浏览器配置文件和同一域名已有的 Cookie；项目不会读取或迁移浏览器 Cookie。
- Bangumi 搜索使用官方但仍标记为实验性的 `/v0/search/subjects`，匹配结果必须结合候选证据审核。
- 当前 Bangumi 公共 `/v0/` API 没有取消收藏的 DELETE endpoint。新建收藏的 reverse 需要先在 Bangumi 网页手动取消，再执行 `bld plan reconcile-manual-uncollect <REVERSE_PLAN_ID>` 验证并对齐本地状态。
- 遇到 `401/403` 会立即停止整个写入批次；`429/5xx/timeout` 会先 GET 验证远端结果，再决定有限重试。
- 不要在 issue、截图或计划导出中包含 `.env`、Token、Steam API Key、真实数据库或私有理由。

## 版本与许可

Release 使用 `vA.B.C`：`A` 表示整体性或不兼容更新，`B` 表示向后兼容的新功能，`C` 表示向后兼容的修复。变更记录见 [CHANGELOG.md](CHANGELOG.md)。

本项目使用 [MIT License](LICENSE)。主命令为 `bld`；`bgv` 作为兼容别名保留。
