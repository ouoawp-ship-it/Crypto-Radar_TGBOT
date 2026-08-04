# FinalShell 中文运维菜单

`paopao` 和 `pp` 是服务器中文运维入口。主项目现已只保留四个市场雷达：

- 启动预警；
- 资金摘要；
- 资金费率警报；
- 五因子资金流雷达。

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
6. 五因子资金流雷达候选清单；
7. 数据库、备份与清理；
8. 日志与故障诊断；
9. 高级运维。

## 服务

主项目生产服务只有：

- `paopao-radar.service`：四个市场雷达的扫描、评分和 Telegram 投递；
- `paopao-market-stream.service`：实时成交与清算数据采集。

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
paopao providers
paopao backup
paopao check-update
paopao update
paopao version
paopao flow-candidates --all
```

`paopao radar-status` 只读取本地状态，分别显示四个雷达的最近运行、下次调度、
候选数量、最近投递结果和投递门禁，不访问网络。

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
限制。每轮按预算深度扫描 24 个并确定性轮换；完整候选清单可通过：

```bash
paopao flow-candidates --all
```

Telegram 卡片继续展示本轮扫描、下一轮优先队列和按流通市值分层的完整候选。

## 安装、更新与回滚

```bash
bash scripts/install_server.sh
bash scripts/update_server.sh --check
bash scripts/update_server.sh --yes
```

更新前会执行工作树、分支和版本门禁。生产数据库、日志、真实配置和历史备份
不会进入 Git。链上历史数据在服务器上只做归档保留，不再由主项目写入。
