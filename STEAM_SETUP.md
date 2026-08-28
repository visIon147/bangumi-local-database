# Steam 本地库与 Web API 配置指南

这份文档说明如何为 Bangumi Local Database 配置 Steam 本地读取和可选的联网补全。Steam 自定义分类始终以本地文件为准；Web API 只用于补齐拥有状态、标题和游玩时间，不能替代本地分类。

## 配置前须知

- `.env`、`config/steam.toml`、API Key 和机器路径不会进入公开仓库。
- 默认关闭 Steam 联网。只有显式选择“允许网络”时才调用 Web API 或商店接口。
- Steam 路径、账户 ID、Steam ID64 和 API Key 不写入计划、任务事件或导出文件。
- 不要把 `.env`、Steam 原始配置文件或带真实账户信息的截图发到 issue。

## 1. 找到 Steam 根目录

Windows 常见位置是：

```text
C:\Program Files (x86)\Steam
```

也可能安装在其他磁盘。根目录下通常能看到 `steam.exe`、`userdata` 和 `config`。在 `.env` 中填写实际路径；路径包含空格时无需额外转义：

```dotenv
BLD_STEAM_ROOT=C:\Program Files (x86)\Steam
```

也可以先留空，让 Windows 自动探测。显式配置的路径优先。

## 2. 找到 userdata account ID

打开：

```text
{Steam 根目录}\userdata
```

其中以数字命名的账户目录就是本地 `account ID` 候选。例如目录为 `123456789`，则配置：

```dotenv
BLD_STEAM_ACCOUNT_ID=123456789
```

只有一个账户目录时程序通常可以自动选择。存在多个账户时必须明确填写，否则导入会拒绝继续并列出脱敏候选。

## 3. 找到 Steam ID64

打开：

```text
{Steam 根目录}\config\loginusers.vdf
```

文件结构类似：

```text
"users"
{
    "76561198xxxxxxxxx"
    {
        "AccountName"    "xxxx"
        "PersonaName"    "xxxx"
        ...
    }
}
```

`users` 下方的 17 位数字键就是 Steam ID64。填写：

```dotenv
BLD_STEAM_ID64=76561198xxxxxxxxx
```

可使用下式与 account ID 交叉检查：

```text
SteamID64 = accountID + 76561197960265728
```

如果 `loginusers.vdf` 有多个用户，请结合 `AccountName`、`PersonaName`、最近登录信息和 `userdata` 目录确认目标账户，不要仅凭第一条记录猜测。

## 4. 启用 Steam 手机令牌

第一次打开 [Steam Web API Key 页面](https://steamcommunity.com/dev/apikey) 时，可能提示当前账户无法注册。通常需要先安装 Steam 手机应用并配置手机令牌：

1. 在手机 Steam 应用登录目标账户。
2. 打开“菜单 → Steam 令牌”。
3. 按应用提示完成手机令牌配置和安全验证。
4. 等待账户安全设置生效后，再打开 API Key 页面。

Steam 的界面文字可能随版本变化；关键是目标账户必须完成 Steam Guard 手机验证。

## 5. 注册 Steam Web API Key

1. 在已登录目标账户的浏览器中打开 [Steam Web API Key 页面](https://steamcommunity.com/dev/apikey)。
2. “域名名称”填写 `localhost`。本项目只在本机 loopback UI 中使用该凭据。
3. 接受 Steam Web API 条款并提交注册。
4. 手机 Steam 应用出现确认请求后，打开“菜单 → 确认”，核对并批准注册。
5. 回到网页读取生成的 API Key，写入本地 `.env`：

```dotenv
STEAM_WEB_API_KEY=replace-with-your-key
```

不要把真实 Key 写入 README、命令行历史、截图、数据库或计划。UI 设置页的密码框不会回显已保存的值：留空表示保持，只有显式勾选“清除”才删除。

Steam 的 GetOwnedGames 能力说明见 [IPlayerService 文档](https://partner.steamgames.com/doc/webapi/IPlayerService)。如果账户隐私设置阻止返回游戏详情，先检查 Steam 个人资料中的游戏详情可见性。

## 6. 配置分类规则

复制公开示例文件：

```powershell
Copy-Item config/steam.example.toml config/steam.toml
```

`config/steam.toml` 是机器私有配置并被 Git 忽略。规则支持 `exact`、`contains`、`regex`、大小写设置和目标 Bangumi 收藏状态。建议先在 UI 设置页保存默认规则，再用 Steam 状态计划预览命中、冲突和未命中清单。

分类命中不会直接写 Bangumi。它只生成不可变计划，仍必须经过 review、fresh preflight、完整 plan ID 确认、apply 和验证。

## 7. 验证配置

保存设置后必须重启 UI，使网页请求和后台 worker 使用同一份配置快照。然后依次执行：

```powershell
uv run bld steam detect
uv run bld steam import
```

`steam import` 默认 dry-run。确认来源、账户、分类和数量合理后，才使用 `--apply-local` 写入本地 SQLite；该操作仍不会写 Bangumi。

需要联网补全时再显式执行：

```powershell
uv run bld steam import --allow-network
```

## 常见问题

### API Key 页面仍提示无法注册

确认手机 Steam Guard 已在正确账户启用，并检查手机应用“确认”中是否有待处理请求。账户限制、Steam 服务状态或新安全设置的等待期也可能阻止注册；这时可以继续使用离线本地导入，Web API 不是必需项。

### 检测到多个账户

从 `userdata` 和 `loginusers.vdf` 核对目标账户，在 `.env` 同时填写 `BLD_STEAM_ACCOUNT_ID` 与对应的 `BLD_STEAM_ID64`，重启 UI 后再检测。

### 本地分类为空或不完整

程序优先读取新版 cloud-storage 分类缓存，无有效记录时才回退旧 `sharedconfig.vdf`。缺少某个辅助来源不表示游戏已移除；只有成功解析的完整分类快照才会停用旧 membership。

### Web API 没有补齐全部标题

先确认 Steam ID64、API Key 和游戏详情隐私设置。商店中不可见、下架、区域限制或非标准 AppID 仍可能没有标题；这不影响保留 AppID，也不应被自动判断为“不存在”。

### 路径含空格

`.env` 可以直接保存完整值。PowerShell 命令若显式传递路径，应使用引号，例如：

```powershell
uv run bld ui serve --env-file ".env"
```
