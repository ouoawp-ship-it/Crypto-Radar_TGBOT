# FinalShell 中文运维菜单

`paopao` 和 `pp` 是服务器中文运维入口。主项目现有五个独立市场雷达：

- 启动预警；
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
4. API、Token 与密钥；
5. Telegram 设置与测试；
6. 数据库、备份与清理；
7. 日志与故障诊断；
8. 高级运维。

“API、Token 与密钥”里还可以设置启动预警的 AI Key、AI 接口、模型、超时、
多周期方向雷达开关、AI白话解读开关和每轮深度候选数量。输入时内容可见，
保存后密钥和接口地址只显示是否已配置；修改配置不会自动重启服务。

启动预警新版默认保持关闭。打开多周期方向雷达后，原有15分钟发现逻辑仍然
不变，只对入选候选追加有界深查：每个候选最多5次合约K线和1次现货K线
请求。五组时间角色负责大方向、主结构、确认、触发和入场观察；最终规则只按
四组证据计分，避免多周期重复加分。

若币安没有同名现货交易对，系统会明确降级为“仅合约观察”，不得形成方向
确认，也不得调用AI。假强或假弱背离只提示行情质量风险，不代表已经反转。
AI开关同样默认关闭；启用后AI只把规则翻译成白话，不改方向、分数、失效位
或目标。同一个观察最多调用一次，重复卡片优先复用已校验缓存。

“Telegram 设置与测试 → 管理员私聊菜单（第一版）”用于绑定管理员并显式启停
独立私聊服务。私聊中可以查看五雷达、健康、推送额度和话题状态，也可以二次
确认后开关方向雷达与 AI 解读。详细边界见
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

- 管理员绑定、全部 Token/API Key、群号、接口地址和模型；
- 主 BOT 真实发送、真实测试消息、话题创建/修复/删除/置顶；
- 安装、更新、部署、Git 版本切换和回滚；
- 主 BOT、市场数据服务的启停和 systemd 管理；
- 数据库备份、恢复、清理、完整日志和原始诊断。

私聊菜单只承担日常查看和两个安全开关，不提供上述高风险入口。

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

输入 API Key 或 Token 时请关闭共享屏幕和录屏。状态只显示
`configured/not_configured`，不会打印实际凭据。

## 五因子资金流雷达

候选池覆盖当前全部符合条件的 Binance USDT 永续合约，不受固定 60 个数量
限制。每轮按预算深度扫描 24 个并确定性轮换；Telegram 卡片展示本轮扫描、
下一轮优先队列和按流通市值分层的完整候选。

## 安装、更新与回滚

```bash
bash scripts/install_server.sh
bash scripts/update_server.sh --check
bash scripts/update_server.sh --yes
```

更新前会执行工作树、分支和版本门禁。生产数据库、日志、真实配置和历史备份
不会进入 Git。链上历史数据在服务器上只做归档保留，不再由主项目写入。
