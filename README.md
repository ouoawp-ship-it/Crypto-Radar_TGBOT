# Crypto Radar Telegram Bot

这是一个只面向 Telegram 信号推送的加密市场监控项目。v2.5.0 在不改变旧版 4H/1D/1W 箱体与三推规则的前提下，新增独立的自适应 1D 盘整产品：日线冻结结构、4H 闭合预警、1D 收盘确认和全市场日线地图各自标明周期与覆盖状态。盘整突破与三推信号不再展示未经历史校准的 `/100` 分数，而是给出可解释的质量标签、判断依据和原始数值。脉冲雷达的严格加密资产池、全量 15 分钟覆盖以及跨服务统一缓存/限流保持不变。

## 核心功能

- 脉冲雷达：严格排除传统金融、稳定币和未知资产；对全部合格加密合约执行 15 分钟价格/OI/CVD 六分类异动提醒，加上每 2 小时对成交额前 200 个合约的持仓价格背离汇总。
- 资金流雷达：组合现货/合约主动流、OI、费率和价格变化生成多因子信号。
- 资金摘要：定时输出负费率、综合、埋伏、动量与新币候选榜。
- 资金费率警报：监控多交易所极端费率、分歧、衰减与结束状态。
- 盘整突破雷达：默认关闭；旧路径继续扫描 4H、日线、周线的 24/72/240 根冻结箱体。独立的自适应 1D 产品从 20–500 根候选长度选择最长合格箱体，用冻结日线边界接收 4H 闭合预警，并等待 1D 收盘确认；完成同一目标日 K 的全市场覆盖后再汇总一次日线地图。每条信号附带价格 K 线、成交量和 MACD 图，并以质量标签和具体理由代替 `/100` 评分。
- 公告风险：独立解析并推送 Binance 官方上新、下架和活动公告，不参与脉冲雷达分类。
- 山寨合约异动雷达：P1 生成候选池，P2 在现有唯一 WebSocket 内做多因子确认，Final 提供可恢复的生产运行；生产调度和真实发送均默认关闭。
- 信号有效性：按 15m、1h、4h、24h 追踪已发送信号的方向收益、命中率、质量门控和评分分层；只生成复盘数据，不自动修改生产参数。
- 脉冲跟随与复盘：同币首次触发立即发送，2 小时内只在强度升级或分类反转时再发，最多 3 次；15 分钟信号回填 1h/4h 结果，2 小时背离回填 2h 结果并回复原卡片。
- 推送安全：默认 dry-run，真实发送必须同时提供 `--send --confirm-real-send`，并经过 readiness 门禁、去重、冷却、限流和重试。

方向信号和 Telegram 市场数据确认统一以 Binance 原生公开行情为事实源：现货使用 Binance Spot，合约使用 Binance USDⓈ-M Futures。价格、OI、主动成交净额和费率只允许使用实时数据或已闭合窗口，推送会明确显示来源、窗口覆盖和计算口径。

## 项目目录

```text
radars/   六个市场雷达及默认关闭的山寨合约异动候选/确认模块
shared/   Telegram、行情访问、存储等公共能力
runtime/  调度、健康检查、备份和运维命令
config/   配置读取、真实配置和配置示例
data/     本机运行数据（不提交 Git）
scripts/  安装、更新和运维脚本
tests/    按雷达、公共能力、运行管理和配置分类的测试
```

每个主要目录和雷达目录内都有中文说明文件；源码不再包在
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

脉冲雷达直接使用原预警话题，因此服务器原有的
`TG_LAUNCH_ALERT_TOPIC_ID` 不需要改话题号。旧启动预警代码、评分、生命周期和
切换入口已经移除，不存在切回旧雷达的开关。主要配置如下：

```dotenv
PULSE_RADAR_ENABLE=true
SIMPLE_ALERT_SCAN_LIMIT=0
SIMPLE_ALERT_MIN_QUOTE_VOLUME=1000000
DIVERGENCE_SCAN_LIMIT=200
BINANCE_SHARED_CACHE_ENABLE=true
BINANCE_GLOBAL_RATE_LIMIT_ENABLE=true
```

`SIMPLE_ALERT_SCAN_LIMIT=0` 表示不设人工数量上限：先按交易所合约元数据严格识别
加密资产，再保留 24 小时成交额不少于 100 万美元的合约。每轮按照市值高/中/低/
待补全四档和流动性高/中/低三档计算独立价格、OI、OI金额和CVD门槛；默认 8 个
受控 worker 完成细算。细算预算按实际合约数动态分配，触发后图表额外预留 40 次
K线与OI请求；所有本机服务通过同一个 SQLite 账本共享公开行情缓存和 Binance
限流额度。详细口径、降级方式和诊断字段见
[脉冲雷达全量资产池说明](docs/PULSE_UNIVERSE_CN.md)。

Dry-run 不写入脉冲跟随状态、复盘记录或回复状态。只有真实发送成功后才提交这些
状态；若一张新卡分段发送到一半失败，只撤回本次未完整发送的新消息，不会删除
此前成功的历史消息。

常用命令：

```text
python main.py status
python main.py doctor
python main.py readiness
python main.py stable-check
python main.py database-backup
python main.py telegram-test
python main.py telegram-topic-setup --topic-template TG_RADAR_SUMMARY --send --confirm-real-send
python main.py telegram-topic-refresh --topic-template TG_LAUNCH_ALERT --send --confirm-real-send
python main.py telegram-topic-refresh --topic-template TG_CONSOLIDATION_BREAKOUT --send --confirm-real-send
python main.py private-control
python main.py once
python main.py pulse
python main.py pulse-review-report
python main.py pulse-review-report --review-days 30 --review-top 10 --json
python main.py announcement-risk
python main.py flow-radar
python main.py funding-alert
python main.py consolidation-breakout
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

盘整突破雷达使用独立模板 `TG_CONSOLIDATION_BREAKOUT` 和严格话题路由，升级后
默认关闭，不会改变现有生产流量；三推和自适应日线产品分别使用默认关闭的子
开关，自适应日线还默认保持影子模式。首次启用、创建专属话题、20–500 日自适应
箱体、全市场轮转、假突破/三推状态机和回滚方式见
[盘整突破雷达说明](docs/CONSOLIDATION_BREAKOUT_RADAR_CN.md)。

旧版 4H/1D/1W 的 24/72/240 根识别逻辑保持不变。自适应 1D 产品分别尝试
20/30/40/50、60/90/120/150、180/240/300/360/420/500 根锚点，并在每档选择
最长的合格结构；默认读取 620 根日 K，足以保留 500 日箱体的稳定性和 ATR 上下文。
日线边界形成后冻结：4H 闭合 K 线越界只属于早期预警，只有 1D 闭合 K 线满足
条件才属于日线级确认，两者不会被混写成同一个周期或自动互相升级。

北京时间 08:00 只是 Binance UTC 日 K 的收线参考点，不是日报固定推送时刻。
日报围绕同一个目标日 K 持续累计稳定轮转结果，完成预期全市场覆盖后只推送一次；
失败标的按配置重试，最长等待默认 3 小时后才以明确的“不完整覆盖”状态降级完成。
默认正文展示最多 20 个重点结构，完整结构保存在最近 7 份有界日报快照中；市场
没有合格结构时也会如实报告零结果。失败投递从 5 分钟开始指数退避，待发队列只
保留最新日报，较旧日报转入快照而不会在恢复后集中补推。

三推 rule v2 要求三次价格推进分别匹配三个独立、已确认的同周期 MACD 枢轴；
两段价格推进各不少于 `0.10 ATR`，两段 MACD 各至少弱化 `5%`，第三推被后续
新高/新低取代时旧结构立即作废。盘整箱体和三推 Telegram 卡片均不再显示未经
历史校准的 `/100` 分数。自适应日线结构使用“强 / 标准 / 观察”，三推使用
“强 / 一般”，并直接列出完整 K 线与收盘覆盖、触碰簇、路径效率、箱宽、价格
推进、MACD 枢轴、量能、箱体位置或颈线状态等适用依据，同时保留原始数值供人工
复核。质量标签解释结构条件，不代表历史成功率、胜率或涨跌概率。

图表只是 Telegram 表达层：旧版同周期箱体事件继续标出冻结上沿、下沿和事件
K 线；自适应 1D 图最多保留 620 根日 K，可完整展示最长 500 日结构。由 4H 收线
触发的日线边界事件仍绘制 1D 结构，并单独标出 4H 事件价格与时间，避免把日线
箱体误画成纯 4H 箱体。三推事件同时标出价格 P1/P2/P3、三个独立 MACD 枢轴、
颈线和失效位。所需闭合 K 线在受控扫描预算内获取；渲染或 Telegram caption
校验失败时自动降级为原文字信号。加图不改变信号判断、专属话题路由或
`--send --confirm-real-send` 双门禁。

`telegram-topic-refresh` 只刷新已经存在的话题，不会创建新话题；说明版本和
正文都未变化时不会重复发送。服务器更新脚本只有显式增加
`--refresh-pulse-topic-intro` 才会执行这项真实 Telegram 操作，内部仍同时使用
`--send --confirm-real-send`；它只刷新脉冲雷达，不会刷新盘整突破话题。已有盘整
突破话题升级到 v2.5.0 自适应日线说明时，需显式执行上面的
`TG_CONSOLIDATION_BREAKOUT` 刷新命令。`pulse-review-report` 只读取已回填的本地复盘数据，
显示真实样本数、各窗口命中率和本周 TOP 榜，不访问 Telegram，也不会自动刷榜。

脉冲示例卡片可单独验证当前正式的“K线图 + 文案”链路：

```text
python -m radars.pulse.simple_alert --test-push --send --confirm-real-send
```

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
bash scripts/update_server.sh --yes --refresh-pulse-topic-intro
# 正式生产版本只从通过 CI 的 annotated Tag 部署：
bash scripts/deploy_tag.sh --check-tag v2.5.0
bash scripts/deploy_tag.sh --tag v2.5.0 --yes --refresh-pulse-topic-intro
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
现有五个核心雷达可分别经二次确认暂停或恢复自动调度；盘整突破雷达先通过
`CONSOLIDATION_BREAKOUT_ENABLE` 独立启用，三推背离再由
`CONSOLIDATION_BREAKOUT_THREE_PUSH_ENABLE` 单独控制。自适应日线按
`PRODUCT_ENABLE → SHADOW 验收 → DIGEST/BOUNDARY → 关闭 SHADOW` 的顺序上线；回滚时
先恢复影子模式或关闭日报和边界事件，不删除状态文件。公共市场快照和主进程不会
随单个雷达关闭，真实 Telegram 推送门禁也不能由私聊菜单修改。
