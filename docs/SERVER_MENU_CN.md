# FinalShell 中文运维菜单

`paopao` 和 `pp` 是服务器中文运维入口。主项目现有五个独立市场雷达：

- 脉冲雷达；
- 资金摘要；
- 资金费率警报；
- 五因子资金流雷达；
- 公告风险。

链上监控雷达已迁移到独立项目，不再由本仓库的菜单、服务或配置管理器维护。

## 使用方法

```bash
paopao
# 或
pp
```

只有交互式终端会打开菜单；自动化命令和管道不会进入交互界面。

## 主菜单

1. 总览与健康检查；
2. 服务管理；
3. 检查更新与版本；
4. Telegram 配置；
5. Telegram 设置与测试；
6. 数据库、备份与清理；
7. 日志与故障诊断；
8. 高级运维。

“Telegram 配置”只设置 Bot Token、群号并查看脱敏配置状态。脉冲雷达已直接
替换旧启动预警，默认开启；旧评分、旧多周期方向、旧 AI 解读和切回入口均已
移除。15 分钟异动与 2 小时背离共用原预警话题号。

“Telegram 设置与测试 → 管理员私聊菜单”用于绑定管理员并显式启停独立私聊
服务。私聊中可以查看五雷达、健康、推送额度、话题、最近信号、推送记录和
中文故障说明；经二次确认后，可以开关主动故障提醒及五个雷达的自动调度。
详细边界见
[Telegram 管理员私聊菜单](TELEGRAM_PRIVATE_CONTROL.md)。

## 服务

主项目长期生产服务为：

- `paopao-radar.service`：五个市场雷达的独立调度和共享 Telegram 投递；
- `paopao-market-stream.service`：实时成交与清算数据采集。

另有默认关闭的 `paopao-private-control.service`，只负责已绑定管理员的 Bot
私聊菜单。它与两个长期服务隔离，安装和更新不会自动启动它。

主 BOT 默认使用安全 Dry-run：

```text
MAIN_BOT_DELIVERY_MODE=dry_run
MAIN_BOT_REAL_SEND=false
MAIN_BOT_REAL_SEND_ACK=
```

Dry-run 启动参数为 `main.py loop`，不会调用 Telegram HTTP。真实发送仍必须
同时满足 Real 模式、真实发送开关、固定确认短语、Telegram 配置和
`main.py live --send --confirm-real-send` 的既有双门禁。

## 常用命令

```bash
paopao status
paopao doctor
paopao readiness
paopao radar-status
paopao stable-check
paopao backup
paopao check-update
paopao update
paopao version
```

`paopao radar-status` 只读取本地状态，分别显示五个雷达的最近运行、下次调度、
候选数量、最近投递结果和投递门禁，不访问网络。

Telegram 话题和置顶说明仅允许手工创建或修复。普通雷达推送只复用已保存的
专属话题；缺少路由时安全阻断，不会自动新建，也不会退回群主界面。菜单入口：
“Telegram 设置与测试 → 手工创建/修复话题并置顶说明”。

## 哪些功能仍保留在 FinalShell

- 管理员绑定、Telegram Token、群号；
- 主 BOT 真实发送、真实测试消息、话题创建/修复/删除/置顶；
- 安装、更新、部署、Git 版本切换和回滚；
- 主 BOT、市场数据服务的启停和 systemd 管理；
- 数据库备份、恢复、清理、完整日志和原始诊断。

私聊菜单承担日常查看和低风险开关，不提供真实发送、服务器或
部署等高风险入口。关闭某个雷达只暂停该雷达的自动调度，不停止共享市场快照、主进程、市场数据服务、清理或结果
追踪，也不会切换真实推送模式。

## 配置安全

所有 FinalShell 输入按部署者要求明文显示，便于提交前核对。配置值不进入命令
参数或 Shell History；保存后状态、日志和报告继续脱敏。配置管理器继续使用：

- 字段白名单；
- 文件锁；
- 修改前备份；
- 原子替换；
- 权限 600；
- 写后校验；
- 失败恢复。

自适应日线盘整产品只将以下四个布尔门禁加入低风险配置白名单：

- `CONSOLIDATION_DAILY_PRODUCT_ENABLE`；
- `CONSOLIDATION_DAILY_SHADOW_MODE`；
- `CONSOLIDATION_DAILY_DIGEST_ENABLE`；
- `CONSOLIDATION_DAILY_BOUNDARY_EVENTS_ENABLE`。

脱敏配置状态会显示这四项，默认依次为关闭、开启、关闭、关闭。历史K线长度、
日报条数、重试/等待参数和状态文件路径不接受菜单或 `paopao_config.py set` 动态
修改，防止日常运维误改识别口径或覆盖状态；这类调整只能走受审查的发布维护流程。

上线时先开启 `PRODUCT_ENABLE` 并保持 `SHADOW_MODE=true`，至少验收一个目标日 K
的完整全市场覆盖；随后在影子模式中开启 `DIGEST_ENABLE` 和
`BOUNDARY_EVENTS_ENABLE`，最后才关闭影子模式。北京时间 08:00 只是日 K 收线参考
点，日报要等轮转覆盖完成后才生成，不应按“08:00 是否收到消息”判断服务状态。

回滚时先重新开启影子模式，再关闭日报和边界事件；需要时再关闭产品总开关。
三个日线相关状态边界与旧盘整状态彼此独立，不要从菜单外手工删除状态文件。
恢复影子模式会停止新产品真实流量，但保留尚未成功投递的事件供后续重放。

输入 Token 时请关闭共享屏幕和录屏。状态只显示
`configured/not_configured`，不会打印实际凭据。

## 五因子资金流雷达

候选池覆盖当前全部符合条件的 Binance USDT 永续合约，不受固定 60 个数量
限制。每轮按预算深度扫描 24 个并确定性轮换；Telegram 卡片展示本轮扫描、
下一轮优先队列和按流通市值分层的完整候选。

## 安装、更新与回滚

```bash
bash scripts/install_server.sh
bash scripts/update_server.sh --check
bash scripts/update_server.sh --yes --refresh-pulse-topic-intro
```

最后一个选项只在明确需要按版本刷新现有“脉冲雷达”置顶说明时使用；它不会
创建缺失的话题，真实发送仍经过双门禁。

正式版本不得由菜单或普通分支更新代替 Tag 发布。确认对应 Tag 的 GitHub
Actions 成功后，使用 `scripts/deploy_tag.sh --check-tag/--tag`；部署备份和
回滚方法见 `docs/ALTCOIN_CONTRACT_ANOMALY_FINAL_CN.md`。

更新前会执行工作树、分支和版本门禁。生产数据库、日志、真实配置和历史备份
不会进入 Git。链上历史数据在服务器上只做归档保留，不再由主项目写入。
