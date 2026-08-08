# 山寨合约异动雷达：生产运行与发布手册

本文描述精简 Final 范围：自动刷新候选池、在现有单一 Binance WebSocket
中实时确认、固定 Telegram 话题推送、幂等冷却、重启恢复、健康检查和
正式 Tag 发布。该模块不提供 0～100 分数、交易成功率、涨跌概率、
CoinGlass 数据、独立回测服务或第二条全市场 WebSocket。

## 1. 生产边界

- `paopao-market-stream.service` 仍是唯一实时行情进程。每个进程只有一个
  `BinanceRealtimeMarketService`、一个连接 runner、一套 pipeline/aggregator
  和一份 `!forceOrder@arr`。
- 普通 `python main.py market-stream` 永远保持原有采集语义。仅当 systemd
  包装器读取到独立生产开关后，才显式增加 `--altcoin-production`。
- P2 人工限时 Dry-run 与生产服务共用进程锁，不能同时运行。配置错误、
  Manifest 不可信或锁冲突都会在新 WebSocket 创建前失败关闭。
- 生产能力默认关闭；真实 Telegram 发送还必须同时通过配置确认和
  `--send --confirm-real-send` 双重命令门禁。

## 2. 候选池长期刷新

候选扫描在 WebSocket 回调之外的单任务 worker 中运行，复用 P1 的 CMC-ID
可信映射、Binance 合约发现和 OI 采集。默认每 1800 秒刷新一次，失败后
60 秒再试，Manifest 最大年龄为 2400 秒。一次只允许一个刷新任务；成功
结果继续使用 P1 的原子替换、规则指纹和双哈希校验。

刷新失败时保留上一份已验证订阅，避免无谓退订；一旦 Manifest 超龄，
实时服务继续采集但停止产生依赖候选上下文的新信号。CMC 全量查询和候选
OI REST 请求绝不在 WebSocket 消息回调内执行。P1 单轮 OI 请求预算默认
600，P2 实时候选 OI 的单会话预算默认 50；两者口径和统计保持隔离。

健康状态文件默认是：

```text
data/altcoin_contract_anomaly_production_status.json
```

其中只记录刷新成功/失败、Manifest 年龄和哈希、候选/订阅覆盖、事件与
发送统计以及脱敏错误类型，不记录 CMC Key、Bot Token、Chat ID 或 Topic ID。

## 3. 安全配置

先复制并编辑 `config/.env.oi`。不要把真实值提交 Git，也不要在工单或日志
中粘贴这些值；服务器文件权限必须保持为 `600`。

第一次生产同构观察使用：

```dotenv
ALTCOIN_CONTRACT_ANOMALY_ENABLE=true
ALTCOIN_CONTRACT_ANOMALY_REALTIME_ENABLE=true
ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_ENABLE=true
ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_SEND_ENABLE=false
ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_SEND_CONFIRM=
ALTCOIN_CONTRACT_ANOMALY_CMC_API_KEY=<仅保存在服务器>
TG_ALTCOIN_CONTRACT_ANOMALY_TOPIC_ID=<人工预建话题的数字ID>
```

保持 `PRODUCTION_SEND_ENABLE=false` 时，生产同构链路会刷新 Manifest、建立
唯一实时连接、形成闭合特征并持久化预览/outbox，但不会调用 Telegram API。
确认至少形成连续 5 分钟窗口、两个 OI 时间点、有效 funding 窗口且 readiness
通过后，才允许打开真实发送：

```dotenv
ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_SEND_ENABLE=true
ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_SEND_CONFIRM=ENABLE_ALTCOIN_ANOMALY_REAL_SEND
```

生产默认值：

- 候选刷新/重试/最大年龄：1800 / 60 / 2400 秒；
- 单币冷却：3600 秒；
- 全局发送保护：每小时 20 条、每天 50 条；
- 生产队列：256；健康状态刷新：30 秒；
- OI 请求预算窗口：300 秒，每个闭合 5 分钟窗口最多 50 次；请求、成功、失败和 429/418 统计保持会话累计；429/418 在 `DATA_SOURCE_FUSE_SECONDS` 到期前禁止继续请求；
- P2 实时数据最大年龄：120 秒；OI 最大年龄：600 秒；
- 成交量基线 5 个闭合桶、至少 4 个真实样本、覆盖率 80%；
- Funding 相邻更新最大间隔 15 秒。

状态、观察事件、生产 outbox 和共享数据库使用独立路径。配置加载会解析
并比较规范化绝对路径；冲突时在网络和状态写入前退出。

## 4. 固定 Telegram 话题

话题名称固定为“山寨合约异动”，模板为
`TG_ALTCOIN_CONTRACT_ANOMALY`。必须由管理员先在目标群中人工创建，再把
数字 Topic ID 写入配置。模块不会自动创建该话题，也不会回退到主群、
测试话题或其他雷达话题。

配置 Topic ID 后，可使用现有显式话题设置入口发布并尝试置顶说明：

```bash
.venv/bin/python main.py telegram-topic-setup \
  --topic-template TG_ALTCOIN_CONTRACT_ANOMALY \
  --send --confirm-real-send
```

没有置顶权限时命令会如实报告失败，不能据此声称置顶成功。说明正文为：

```text
【山寨合约异动｜说明】

候选依据：
市值、Binance OI/市值、资金费率等基础条件，用于决定监控哪些合约。

实时确认因子共6类：
1. 价格动量
2. 成交量放大
3. 主动买卖与CVD
4. OI变化
5. 资金费率变化
6. 多空爆仓

“实时确认：3项”表示当前有3类独立证据达到阈值，
不代表综合分数、成功率或涨跌概率。

候选依据与实时确认分开展示。
所有信号均附带数据时间和完整度。
```

## 5. 正式消息与最小信号管理

同一 Symbol、同一闭合窗口中的多个事件会合并为一条中文消息；确认项数
只统计六类独立因子，不是分数。缺失字段显示“缺数据”，不会补零。所有
时间明确标为北京时间，文本经过 HTML 转义并按完整行分页。

```text
🚨【山寨合约异动｜首次确认】

ACEUSDT
候选依据：潜在逼空 + 高合约杠杆
实时确认：3项
确认依据：价格动量｜成交量放大｜OI增加

市值：$11.77M
Binance OI：$9.01M
OI/市值：76.6%
资金费率：-0.1356%

1分钟价格：+1.8%
5分钟价格：+3.2%
5分钟OI：+4.7%
主动买入占比：68.2%
成交量：基线的2.6倍
空头爆仓：$120K

数据时间：2026-08-08 20:00:00（北京时间）
数据完整度：完整
```

生产状态和 P2 Dry-run 状态完全分离。事件先原子写入模块 outbox/WAL，发送
成功后才推进生产游标；失败保持待发。确定性 event ID、窗口幂等、单币冷却
和全局频率保护共同防止重复。进程重启会恢复未完成批次，已成功页面不会
重发；Telegram 失败不会提交“已发送”状态。失败批次把下一次尝试时间写入
WAL，采用 5 秒起步、最长 900 秒的指数退避；Telegram 返回更长的
`Retry-After` 时以服务端时间为准，重启后继续遵守原截止时间。

真实发送关闭时，预览使用独立的 `*.preview.json` 状态和 WAL，只记为
`previewed`，不会推进真实发送、冷却或“曾确认”游标。如果进程崩溃点恰好位于
Telegram 已受理而本地尚未确认之间，结果属于不可判定副作用：批次会永久进入
`quarantined` 并停止自动重试，等待人工核对目标话题后处置，绝不冒险重复发送。

## 6. systemd 与验收

`paopao-market-stream.service` 通过 `scripts/run_market_stream.sh` 启动。
包装器只接受严格的 `true` / `false` 值；真实发送缺少固定确认、Token、
Chat ID 或人工 Topic ID 时以退出码 2 停止，systemd 的
`RestartPreventExitStatus=2` 会阻止配置错误重启风暴。

```bash
sudo bash scripts/install_market_stream_service.sh
sudo systemctl restart paopao-market-stream
systemctl status paopao-market-stream --no-pager
.venv/bin/python main.py readiness
.venv/bin/python main.py stable-check --no-save
journalctl -u paopao-market-stream -n 200 --no-pager
```

真实发送仍关闭时，至少观察 12～15 分钟并检查：只有一个 market-stream
进程；候选订阅和 markPrice 覆盖完整；连续 1m/5m 桶、funding 窗口和相邻
OI 点有效；没有旧 epoch 数据触发；Telegram 发送数为 0。零事件只有在上述
数据质量全部成立时才是合法结果。

打开真实发送前，先执行一次显式话题设置命令验证路由和说明消息确实进入
“山寨合约异动”。若 Topic ID、权限或路由不确定，保持真实发送关闭，绝不
改投主群。

说明消息确认无误后，可在仍未开启模块真实推送时发送一条明确标记、非信号的
验收测试消息：

```bash
.venv/bin/python main.py telegram-test \
  --topic-template TG_ALTCOIN_CONTRACT_ANOMALY \
  --send --confirm-real-send
```

该入口只接受测试话题或“山寨合约异动”人工预建话题；固定话题未配置时会在
网络请求前失败关闭。

## 7. 正式 Tag 发布、备份与回滚

`VERSION` 为 `v2.0.1`：仓库在 BOT-only 重构后已经声明 v2.0.0，本次是在
兼容现有服务的前提下补齐生产运行、恢复和发布安全边界，因此使用补丁版本，
而不是扩大为新的功能大版本。正式 Tag 必须是位于远端 `main` 上的 annotated
tag，且 Tag 名必须与 Tag 中的 `VERSION` 完全一致。Tag push 会重新运行完整 CI。

只有 Tag CI 成功后才执行：

```bash
bash scripts/deploy_tag.sh --check-tag v2.0.1
bash scripts/deploy_tag.sh --tag v2.0.1 --yes
```

发布入口拒绝跟踪文件脏改、分叉/伪造 Tag、轻量 Tag、版本不一致、多个
market-stream 或并行 P2 Dry-run。停止服务后，它会以 0700/0600 权限备份：

- `config/.env.oi`（不打印、不移动、不改写真实凭据）；
- `data/` 顶层运行状态、JSON/JSONL 和 SQLite 文件；
- 一份经过 SQLite 恢复校验的数据库备份报告；
- 相关 systemd service/timer 定义及部署前启停状态；
- 前一 commit、目标 Tag 和逐文件 SHA-256 清单。

默认备份目录为 `backups/releases/<UTC时间>-<前一commit>`。切换 Tag 后，
脚本复用安装器更新依赖、编译并运行全量测试，安装服务，再验证 readiness、
stable-check、服务存活和进程冲突。任一步失败会自动恢复前一 commit、原配置、
状态、数据库、systemd 定义和原启停状态。

人工回滚：

```bash
bash scripts/deploy_tag.sh \
  --rollback /home/ubuntu/paopao-crypto-radar/backups/releases/<备份目录> \
  --yes
```

回滚先验证备份路径和全部 SHA-256，不删除 Telegram 历史，也不猜测或重建
密钥。回滚后重新执行 readiness、stable-check、服务状态和日志检查。

## 8. 故障处理

- Manifest 刷新失败但仍新鲜：保留最后有效订阅并告警；超龄后停止新信号。
- CMC Key、Topic ID、Token 或 Chat ID 缺失：保持真实发送关闭并修复唯一缺项。
- Binance 429/418：停止施压，按现有退避恢复，不提高并发或预算。
- Telegram 发送失败：保留 outbox，确认路由/权限后重试；不要手工修改生产游标。
- WebSocket 断流或 epoch 切换：等待当前 epoch 的完整闭合窗口，不使用旧数据。
- 部署验收失败：真实发送保持关闭，使用发布备份回滚，不删除候选池、数据库或
  Telegram 历史。
