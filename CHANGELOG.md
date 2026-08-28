# Changelog

所有正式发布使用 `vA.B.C`：`A` 为整体性或不兼容更新，`B` 为向后兼容的新功能，`C` 为向后兼容的修复。

## v1.1.1 — 2026-08-28

- 新增 `bld steam covers complete` 与对应 UI 任务，使用公开 Steam Store 元数据和官方 CDN 为当前占位图补齐纵向封面，并保留本地图片优先级。
- 修复 HTML 安全表单把 POST 错发到当前 GET 页面而产生 `405 Method Not Allowed` 的问题。
- 修复终态任务详情每秒重复刷新，使关联计划与人工审核入口可正常点击。
- 为浏览器表单增加带状态、请求信息和排查建议的本地错误页；JSON API 保持结构化错误。
- 作品筛选栏优化列宽与响应式换行；评分和探索队列明确展示 Bangumi 收藏状态。
- Steam 人工审核现在保留筛选/页码，提交时立即锁定按钮并显示结果；重复提交会返回既有 successor，不再把正常的旧草稿取消误报为失败。
- 计划页提供“审阅并开始 Preflight”快捷操作，Apply 使用确认弹窗和自动绑定 ID/nonce；长结果限制在可滚动区域，确认入口移动到页面顶部。

## v1.1.0 — 2026-08-28

### 新功能

- 将任务与计划合并为统一工作台，持久记录双向关联、实时进度和人工审核入口。
- Steam 批量匹配支持最多 250 项的单计划、逐项进度、安全取消及 `fail_fast|continue` 失败策略。
- 任务和计划可批量归档、恢复；满足严格限制时可在备份后永久删除。
- 作品、Steam 库、未匹配列表和 Steam 分类增加实际使用导向的筛选、排序及分页。
- 状态增加中文解释，评分队列提交后统一前往下一待处理项。

### 修复

- 修复 Steam 人工审核按钮未随表单提交、导致“Choose exactly one revision decision”的问题。
- 修复长批量搜索不更新进度、不响应取消以及传输层错误缺少明确分类的问题。
- UI 重启时会把遗留的 `cancel_requested` 收敛为 `cancelled`，不自动重放任务。

## v1.0.0 — 2026-08-28

Bangumi Local Database 的首个稳定公开版本。

### 主要功能

- 覆盖书籍、动画、音乐、游戏和三次元的本地 SQLite 收藏镜像。
- 本机 Web UI，包含作品、同步、Tag、Steam、计划、任务、评分、探索和图片缓存管理。
- BASE / LOCAL / REMOTE 三方比较、不可变计划、fresh preflight、备份、写后验证、审计和 reverse draft。
- 通用批量个人 Tag、公开 Tag 条件和游戏分类人工审核。
- Steam 本地分类导入、标题补全、多语言 Bangumi 候选、批量匹配和状态计划。
- 固定且可恢复的评分队列，以及不会扫描全库的有界游戏探索队列。
- wheel 安装和 `bld init` 首次配置。

### 安装

```powershell
uv tool install https://github.com/visIon147/bangumi-local-database/releases/download/v1.0.0/bangumi_local_database-1.0.0-py3-none-any.whl
bld init
bld db upgrade
bld ui serve
```

### 已知限制

- Bangumi 公共 `/v0/` API 当前不支持取消收藏；恢复新建收藏需要网页手动取消后再执行本地核对。
- `/v0/search/subjects` 仍是实验性搜索接口，候选匹配需要人工审核。
- Bangumi TagIndex 可能发生不区分大小写的 Tag 扩展，分类标签建议采用不会碰撞的名称。
- UI 仅供本机回环地址访问，不支持作为公网服务部署。
