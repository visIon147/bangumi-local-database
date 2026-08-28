# Changelog

所有正式发布使用 `vA.B.C`：`A` 为整体性或不兼容更新，`B` 为向后兼容的新功能，`C` 为向后兼容的修复。

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
