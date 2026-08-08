# Crypto Radar Telegram Bot

这是一个只面向 Telegram 信号推送的加密市场监控项目。`v2.0.1` 延续 BOT-only 运行时，并为山寨合约异动雷达补齐默认关闭、可恢复的生产运行与正式 Tag 发布边界。

## 核心功能

- 启动预警：基于价格、OI、成交量、资金费率与突破结构识别启动阶段。
- 资金流雷达：组合现货/合约主动流、OI、费率和价格变化生成多因子信号。
- 资金摘要：定时输出负费率、综合、埋伏、动量与新币候选榜。
- 资金费率警报：监控多交易所极端费率、分歧、衰减与结束状态。
- 公告风险：独立解析 Binance 官方上新、下架和活动公告；同时只作为启动预警辅助证据，不参与启动打分。
- 山寨合约异动雷达：P1 按可信 CMC-ID、Binance 单交易所 OI/市值和资金费率生成候选池；P2 在现有唯一 WebSocket 内完成多因子确认；Final 以独立开关提供候选自动刷新、固定话题、幂等冷却和重启恢复。生产与真实发送均默认关闭。
- 信号有效性：按 15m、1h、4h、24h 追踪已发送信号的方向收益、命中率、质量门控和评分分层；只生成复盘数据，不自动修改生产参数。
- 启动信号生命周期：15分钟完整收线负责触发，1小时主图负责看结构；启动预警从观察、预警、确认、启动进入降温期。第一次信号单独发送，同币后续更新回复上一条成功消息，历史卡片不自动删除。每个完整周期只计一个结果样本，记录最高/最低收盘变动、OI 区间、阶段耗时和结束收益；同口径样本不足 20 轮时不展示比例。
- 按需 AI 解读：雷达扫描默认零 AI 调用；管理员需要时点击启动信号下方按钮，机器人只读取该信号发送时的安全快照并在私聊返回解读。重复点击优先复用缓存，AI 不改规则方向、证据分、阶段或失效位。
- 推送安全：默认 dry-run，真实发送必须同时提供 `--send --confirm-real-send`，并经过 readiness 门禁、去重、冷却、限流和重试。

方向信号和 Telegram 市场数据确认统一以 Binance 原生公开行情为事实源：现货使用 Binance Spot，合约使用 Binance USDⓈ-M Futures。价格、OI、主动成交净额和费率只允许使用实时数据或已闭合窗口，推送会明确显示来源、窗口覆盖和计算口径。

## 项目目录

```text
radars/   五个生产雷达及默认关闭的山寨合约异动候选/确认模块
shared/   Telegram、行情访问、存储等公共能力
runtime/  调度、健康检查、备份和运维命令
config/   配置读取、真实配置和配置示例
data/     本机运行数据（不提交 Git）
scripts/  安装、更新和运维脚本
tests/    按雷达、公共能力、运行管理和配置分类的测试
```

每个主要目录和五个雷达目录内都有中文说明文件；源码不再包在
`paopao_radar/` 这一层中。

## 本地运行

```powershell
Copy-Item config/.env.oi.example config/.env.oi
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe main.py doctor
.\.venv\Scripts\python.exe main.py once
```

必须在 `config/.env.oi` 中填写：

```dotenv
TG_BOT_TOKEN=123456:...
TG_CHAT_ID=-1001234567890
```

启动预警话题始终保留置顶说明和全部成功历史消息。同一币种的第一次信号单独发送，后续更新回复上一条；若回复目标已被人工删除，会安全降级为独立发送。旧清理配置仅为兼容旧环境保留，运行时不再自动删除启动消息：

```dotenv
LAUNCH_INVALIDATION_GRACE_SEC=1800
LAUNCH_LIFECYCLE_V2_ENABLE=false
LAUNCH_LIFECYCLE_INVALID_WINDOWS=2
LAUNCH_MESSAGE_PACKAGE_V2_ENABLE=false
LAUNCH_PRICE_ACTION_V3_ENABLE=false
LAUNCH_PA_BOX_LOOKBACK=16
LAUNCH_PA_MAX_BOX_RANGE_PCT=12
LAUNCH_PA_MIN_BODY_RATIO=0.45
LAUNCH_PA_WICK_BODY_RATIO=1.5
LAUNCH_CHART_V2_ENABLE=false
LAUNCH_OUTCOME_V2_ENABLE=false
LAUNCH_OUTCOME_FOLLOW_THROUGH_PCT=3.0
LAUNCH_OUTCOME_MIN_SAMPLES=20
LAUNCH_PACKAGE_SCORE_DELTA=15
LAUNCH_PACKAGE_PRICE_DELTA_PCT=3.0
LAUNCH_PACKAGE_OI_DELTA_PCT=5.0
LAUNCH_MESSAGE_CLEANUP_ENABLE=false
LAUNCH_MESSAGE_CLEANUP_MAX_AGE_SEC=169200
LAUNCH_MESSAGE_CLEANUP_LIMIT=20
```

`LAUNCH_MESSAGE_CLEANUP_*` 仅用于读取旧配置和旧数据库，不再触发 Telegram 删除请求。只有“本次新卡部分发送”或“新卡状态提交失败”时，才会回滚本次尚未完成的新消息；此前已经成功的启动消息始终保留。

常用命令：

```text
python main.py status
python main.py doctor
python main.py readiness
python main.py stable-check
python main.py database-backup
python main.py telegram-test
python main.py telegram-topic-setup --topic-template TG_RADAR_SUMMARY --send --confirm-real-send
python main.py private-control
python main.py once
python main.py announcement-risk
python main.py flow-radar
python main.py funding-alert
python main.py altcoin-anomaly --preview-telegram
python main.py altcoin-anomaly --realtime-duration-sec 900 --json
python main.py signal-effectiveness
python main.py market-stream
python main.py loop
python main.py live --send --confirm-real-send
```

普通推送不会自动建话题或刷新置顶说明。创建/修复话题必须通过中文菜单或
`telegram-topic-setup` 明确执行；任一核心雷达缺少专属话题时，真实发送会
安全阻断，不会退回群主界面。

主 BOT 的 systemd 服务默认使用安全 Dry-run：

```text
main.py loop
```

只有显式切换为 Real 且固定确认、Telegram 配置及现有 readiness
全部通过时，包装器才会启动：

```text
main.py live --send --confirm-real-send
```

相关配置、中文菜单和回滚方式见
[docs/INSTALL_CN.md](docs/INSTALL_CN.md) 与
[docs/SERVER_MENU_CN.md](docs/SERVER_MENU_CN.md)。

山寨合约异动雷达 P1 的 CMC 配置、离线缓存、人工映射覆盖、退出码和安全边界见
[docs/ALTCOIN_CONTRACT_ANOMALY_P1_CN.md](docs/ALTCOIN_CONTRACT_ANOMALY_P1_CN.md)。
P2 的受限时长 Dry-run、实时阈值、停止方式及生产隔离边界见
[docs/ALTCOIN_CONTRACT_ANOMALY_P2_CN.md](docs/ALTCOIN_CONTRACT_ANOMALY_P2_CN.md)。
Final 的生产开关、固定话题、消息语义、systemd、正式 Tag 发布与回滚见
[docs/ALTCOIN_CONTRACT_ANOMALY_FINAL_CN.md](docs/ALTCOIN_CONTRACT_ANOMALY_FINAL_CN.md)。

## 测试

```powershell
.\.venv\Scripts\python.exe -m compileall -q radars shared runtime config tests scripts main.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py"
```

## Linux 服务器

```bash
bash scripts/install_server.sh
bash scripts/update_server.sh --check
bash scripts/update_server.sh --yes
# 正式生产版本只从通过 CI 的 annotated Tag 部署：
bash scripts/deploy_tag.sh --check-tag v2.0.1
bash scripts/deploy_tag.sh --tag v2.0.1 --yes
```

生产环境仅保留：

- `paopao-radar.service`：扫描、评分与 Telegram Dry-run/Real 安全运行。
- `paopao-market-stream.service`：实时成交和清算采集。
- `paopao-private-control.service`：可选的管理员私聊菜单，默认关闭且独立运行。
- `paopao-health.timer`：定时执行 BOT、数据库、行情新鲜度和信号结果追踪健康检查。
- `paopao-backup.timer`：每天创建活动 SQLite 数据库的一致性备份，并实际恢复到内存验证可用性。

默认保留 365 天信号效果样本（最多 20,000 条）和 7 天本机数据库备份。备份目录、保留天数与健康检查最大时效可通过 `config/.env.oi` 调整；本机备份不能替代后续需要单独配置的异机/对象存储灾备。

更完整的模块边界见 [docs/BOT_ONLY_ARCHITECTURE.md](docs/BOT_ONLY_ARCHITECTURE.md)，安装说明见 [docs/INSTALL_CN.md](docs/INSTALL_CN.md)。
FinalShell 的 `paopao` / `pp` 中文运维菜单见
[docs/SERVER_MENU_CN.md](docs/SERVER_MENU_CN.md)。
管理员私聊菜单见
[docs/TELEGRAM_PRIVATE_CONTROL.md](docs/TELEGRAM_PRIVATE_CONTROL.md)。
私聊菜单可只读查看最近信号、推送记录和中文故障说明；主动故障提醒默认关闭。
五个雷达可分别经二次确认暂停或恢复自动调度，公共市场快照和主进程不会随单个
雷达关闭，真实 Telegram 推送门禁也不能由私聊菜单修改。
