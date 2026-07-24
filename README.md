# Crypto Radar Telegram Bot

这是一个只面向 Telegram 信号推送的加密市场监控项目。`v2.0.0` 已移除公开网站、后台管理、用户系统、独立 AI 助手和全部 Web 部署依赖，恢复为小而稳定的 BOT 运行时。

## 核心功能

- 启动预警：基于价格、OI、成交量、资金费率与突破结构识别启动阶段。
- 资金流雷达：组合现货/合约主动流、OI、费率和价格变化生成多因子信号。
- 资金摘要：定时输出负费率、综合、埋伏、动量与新币候选榜。
- 资金费率警报：监控多交易所极端费率、分歧、衰减与结束状态。
- 公告风险：解析 Binance 官方上新、下架、Launchpool、Airdrop 等公告。
- 信号有效性：按 15m、1h、4h、24h 追踪已发送信号的方向收益、命中率、质量门控和评分分层；只生成复盘数据，不自动修改生产参数。
- 启动信号生命周期：启动预警从观察、预警、确认、启动进入降温期；每次推送使用一条“96根以上15m K线图 + 下方动态文字”的 Telegram 消息，保留 CoinGlass、可复制交易对和 TradingView 入口。新消息发送并持久化成功后，自动删除启动话题中的旧信号，只保留置顶说明和最新推送。连续降温 30 分钟后才确认失效；每个完整周期只计一个结果样本，记录最高/最低收盘变动、OI 区间、阶段耗时和结束收益；同口径样本不足 20 轮时不展示比例。
- 推送安全：默认 dry-run，真实发送必须同时提供 `--send --confirm-real-send`，并经过 readiness 门禁、去重、冷却、限流和重试。

方向信号和 Telegram 市场数据确认统一以 Binance 原生公开行情为事实源：现货使用 Binance Spot，合约使用 Binance USDⓈ-M Futures。价格、OI、主动成交净额和费率只允许使用实时数据或已闭合窗口，推送会明确显示来源、窗口覆盖和计算口径。CoinGlass/Coinalyze 不再改写信号值或参与方向门控，只保留为可选的只读研究、诊断与故障排查工具。

Arkham 不属于本阶段：它是链上实体事件层，后续作为独立模块开发，不与当前高频衍生品采集耦合。

## 本地运行

```powershell
Copy-Item .env.oi.example .env.oi
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe main.py doctor
.\.venv\Scripts\python.exe main.py once
```

必须在 `.env.oi` 中填写：

```dotenv
TG_BOT_TOKEN=123456:...
TG_CHAT_ID=-1001234567890
```

可选的外部数据源诊断：

```dotenv
COINGLASS_ENABLE=true
COINGLASS_API_KEY=...
COINALYZE_ENABLE=true
COINALYZE_API_KEY=...
DERIVATIVES_VALIDATION_SYMBOL_LIMIT=8
```

方向信号不需要配置以上 Key。配置后仅供 `provider-check` 等只读诊断命令使用；API Key 仅通过请求头发送，状态和诊断输出不会包含 Key 内容。

启动预警消息清理只作用于启动预警话题，不会删除资金摘要或资金流雷达中的多币聚合消息。话题始终保留当前置顶说明；历史信号在新消息发送并持久化成功后删除，删除窗口内的失败项会在后续更新重试，超过 Telegram 删除时限的记录会标记为不可删除并停止重试。可按需调整：

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
LAUNCH_SMC_V4_ENABLE=false
LAUNCH_SMC_HISTORY_BARS=400
LAUNCH_SMC_SWING_LENGTH=2
LAUNCH_SMC_EQUAL_TOLERANCE_ATR=0.15
LAUNCH_SMC_DISPLACEMENT_BODY_ATR=1.0
LAUNCH_SMC_MAX_ZONE_AGE_BARS=96
LAUNCH_CHART_V2_ENABLE=false
LAUNCH_OUTCOME_V2_ENABLE=false
LAUNCH_OUTCOME_FOLLOW_THROUGH_PCT=3.0
LAUNCH_OUTCOME_MIN_SAMPLES=20
LAUNCH_PACKAGE_SCORE_DELTA=15
LAUNCH_PACKAGE_PRICE_DELTA_PCT=3.0
LAUNCH_PACKAGE_OI_DELTA_PCT=5.0
LAUNCH_MESSAGE_CLEANUP_ENABLE=true
LAUNCH_MESSAGE_CLEANUP_MAX_AGE_SEC=169200
LAUNCH_MESSAGE_CLEANUP_LIMIT=20
```

dry-run/观察模式不会调用 Telegram 删除接口；真实运行每轮最多尝试 20 条，并避开超过 47 小时的消息。删除结果会同时写入推送历史和信号数据库审计字段，但不改变信号效果统计所需的原始发送状态。

P1.2 增加了只读数据源验收命令，可区分 Key 缺失、套餐/权限不足、接口不可用和正常返回，并保证诊断结果不包含 Key：

```text
python main.py provider-check --provider-symbol BTCUSDT
```

常用命令：

```text
python main.py status
python main.py doctor
python main.py readiness
python main.py stable-check
python main.py provider-check
python main.py database-backup
python main.py telegram-test
python main.py once
python main.py flow-radar
python main.py funding-alert
python main.py signal-effectiveness
python main.py market-stream
python main.py live --send --confirm-real-send
```

## 测试

```powershell
.\.venv\Scripts\python.exe -m compileall -q paopao_radar tests scripts main.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

## Linux 服务器

```bash
bash scripts/install_server.sh
bash scripts/update_server.sh --check
bash scripts/update_server.sh --yes
```

生产环境仅保留：

- `paopao-radar.service`：扫描、评分与 Telegram 推送。
- `paopao-market-stream.service`：实时成交和清算采集。
- `paopao-health.timer`：定时执行 BOT、数据库、行情新鲜度和信号结果追踪健康检查。
- `paopao-backup.timer`：每天创建活动 SQLite 数据库的一致性备份，并实际恢复到内存验证可用性。

默认保留 365 天信号效果样本（最多 20,000 条）和 7 天本机数据库备份。备份目录、保留天数与健康检查最大时效可通过 `.env.oi` 调整；本机备份不能替代后续需要单独配置的异机/对象存储灾备。

更完整的模块边界见 [docs/BOT_ONLY_ARCHITECTURE.md](docs/BOT_ONLY_ARCHITECTURE.md)，安装说明见 [docs/INSTALL_CN.md](docs/INSTALL_CN.md)。
