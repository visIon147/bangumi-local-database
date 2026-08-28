# scripts/

这里未来只放辅助/迁移脚本，不应把核心业务逻辑藏在一次性脚本里。

建议允许：

- 数据检查
- 一次性 legacy import
- 数据库统计

不建议：

- `bulk_patch_bangumi.py` 这种绕过 plan/apply 的远端写脚本
- 含 token 的 curl 文件

任何长期功能应进入 `src/bangumi_local/services/` 和 CLI。

## 公开发布

从本地 `development-private` 且 tracked worktree clean 时运行：

```powershell
.\release_public.ps1 -Version 1.0.0 -FreshRoot
.\release_public.ps1 -Version 1.0.0 -FreshRoot -Push
```

包装脚本调用 `scripts/publish_public_release.py`。后者从净化 tree 在临时目录运行 pytest/Alembic、secret/path 扫描和 wheel/sdist 内容检查，并生成 SHA-256 校验文件。`-FreshRoot` 只用于需要重建公开历史的版本；`-Push` 使用精确 force-with-lease 原子推送 main 和版本 Tag。禁止 `git push --all` 或显式推送私有开发分支。
