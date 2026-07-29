# FinalShell 中文运维菜单

`paopao` 是现有服务器命令的兼容入口。在 FinalShell 的交互式终端中运行：

```bash
paopao
# 或
paopao menu
# 可选短命令
pp
```

只有 stdin 和 stdout 都是 TTY 时才进入中文菜单。非交互 SSH 命令、SCP、SFTP、管道或自动化任务只显示帮助并确定性退出，不会打开菜单。安装脚本不会修改 `.bashrc`，也不会在登录时自动启动菜单。

## 首页

首页只读取本地轻量状态：

- Version 与当前 Commit
- `paopao-radar`、`paopao-market-stream`、`paopao-oar-watch`
- Base RPC 是否配置
- DeepSeek 是否配置/启用
- 链上 Topic 是否配置
- Registry、Active Watch、Open Unresolved
- 最近扫描与 Automation DB 状态

打开菜单不会自动运行 `git fetch`、RPC、AI、Telegram、Provider Check 或完整测试。

## 主菜单

1. 总览与健康检查
2. 服务管理
3. 检查更新与版本
4. API、Token 与密钥
5. AI 模型与提示词
6. 链上活动雷达
7. Telegram 设置与测试
8. 数据库、备份与清理
9. 日志与故障诊断
10. 高级运维

所有原命令继续兼容，例如：

```bash
paopao status
paopao doctor
paopao readiness
paopao stable-check
paopao providers
paopao backup
paopao telegram-test
paopao check-update
paopao update
paopao version
```

## API、Token 与密钥

菜单不会使用 `sed` 修改环境文件，而是调用：

```bash
python scripts/paopao_config.py status
python scripts/paopao_config.py set <白名单字段>
python scripts/paopao_config.py enable OAR_AI_ENABLE
python scripts/paopao_config.py disable OAR_AI_ENABLE
python scripts/paopao_config.py validate
```

Secret 不作为命令行参数。TTY 中使用无回显输入；非 TTY 只能从 stdin 读取。管理器使用字段白名单、文件锁、写前备份、原子替换、权限 600 和写后校验；失败时自动恢复原文件，并保留未知字段和注释。

管理器还会离线校验 Bot Token、Chat ID、链上 Topic ID、DeepSeek 模型/Base URL 和 CEX 标签安全相对路径；校验不发起 Telegram、Provider 或 RPC 请求。每个环境文件最多保留 30 个 `.bak.*` 备份，不会删除其他文件。

状态只显示 configured/not_configured 或安全枚举，不打印完整 RPC URL、Token、Chat ID、Topic ID、API Key 或 Authorization。

## AI 与 Prompt

AI 菜单可以：

- 查看脱敏配置并原子应用 DeepSeek V4 Pro 推荐 Profile
- 设置 API Key、Base URL、Model、Thinking Mode、Reasoning Effort、Max Tokens
- 查看 Prompt 状态与 Hash
- 显示、编辑、校验、恢复默认 Prompt
- 查看历史并回滚
- 执行 `ai-provider-check` 和合成 `ai-smoke`
- 查看或只清理 AI 结果 Cache（小时调用预算保留）
- 在 Provider Check 与 AI Smoke 之后显式 enable/disable

核心安全 Prompt 不可由菜单编辑。Operator Prompt 只影响业务分析风格；完整规则见 [OAR_DEEPSEEK_V4_PRO.md](OAR_DEEPSEEK_V4_PRO.md)。

## OAR 管理

OAR 菜单复用现有 CLI：

- Registry list/add/verify/disable
- Watch list/add/remove
- `bridge-once`
- `watch-once`
- Unresolved 本地摘要
- `token-activity`
- `token-report`
- `labels-check`

Registry 验证和 Token 查询仍要求显式 `--allow-network`。验证时必须选择“设为 Primary”或“仅验证为 Secondary”；只有前者传递 `--set-primary`。Symbol 不一致时默认停止，显示链上 Symbol 与 Market Symbol，并且只有输入 `接受Symbol不一致` 后才传递 `--accept-symbol-mismatch`。菜单不会按 Symbol 猜合约，也不会自动验证 Pending Token；禁用 Registry 还要求输入 `禁用Registry`。

## Telegram

普通菜单只提供脱敏配置、链上 Topic 设置、规则报告 dry-run 和 readiness。Dry-run 不带 `--send` 或 `--confirm-real-send`。

真实 Telegram 测试只在“高级运维”中出现，并要求输入完整短语：

```text
发送真实测试
```

代码仍保留现有真实发送双门禁；菜单不是绕过通道。

## 更新、日志与备份

- 更新检查：`scripts/update_server.sh --check`
- 安全更新：确认 `执行安全更新` 后运行 `scripts/update_server.sh --yes`
- 日志：按主 BOT、Market Stream、OAR 或 error 过滤
- 诊断：组合现有 `doctor` 和脱敏配置状态
- 备份：复用 `database-backup` 的一致性备份与恢复验证
- 清理：复用现有 bounded cleanup

菜单不猜测数据库恢复目标。真实恢复必须先核对备份清单，并输入完整短语：

```text
恢复数据库
```

## 高风险确认

以下操作不能只用 `y/N`：

- 停止主 BOT：`停止主BOT`
- 重启主服务：`重启主服务`
- 安全更新：`执行安全更新`
- 真实 Telegram 测试：`发送真实测试`
- 清理 AI 结果 Cache：`清理AI缓存`
- 恢复 Prompt：`恢复提示词`
- 配置回滚：`回滚配置`
- 数据库恢复：`恢复数据库`
- 禁用 Registry：`禁用Registry`

短语不完全一致时操作取消。

## 安装

`scripts/install_shortcuts.sh` 是安装流程使用的幂等入口；它不访问网络、不启动菜单或服务。`scripts/install_server.sh` 会确保：

- `/usr/local/bin/paopao`
- 可选 `/usr/local/bin/pp -> /usr/local/bin/paopao`

可以用 `INSTALL_PP_SHORTCUT=0` 跳过 `pp`。安装只验证脚本可执行，不会启动交互菜单。
