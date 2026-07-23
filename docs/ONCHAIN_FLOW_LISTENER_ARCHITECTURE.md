# 独立链上交易所资金流监听系统设计

状态：**设计基线 / Codex 实施输入**  
目标仓库：`Crypto-Radar_TGBOT`  
目标模块：`paopao-onchain-flow`  

## 1. 目标

在不影响现有 `paopao-radar` 与 `paopao-market-stream` 的前提下，新增一个独立常驻系统，实时发现任意启用链上、任意可识别同质化代币与中心化交易所之间的异常资金流，并通过独立 Telegram 话题推送：

- 单笔大额转入交易所；
- 单笔大额从交易所转出；
- 短时间批量流入或流出；
- 多个时间窗口持续流入或流出；
- 多家交易所同步出现同方向流量；
- 在有足够证据时，给出 `偏多 / 中性 / 偏空`、观察周期、分数、置信度和可解释原因。

这里的“所有币”定义为：

> 对启用链上的所有同质化代币，不预先维护 Token watchlist；只要 Transfer 的任一端命中高置信度交易所地址注册表，就进入候选处理链路。

这不是保存所有钱包之间的全量转账，也不是承诺预测必涨必跌。

## 2. 强制隔离边界

### 2.1 进程隔离

新系统必须使用独立入口和独立 systemd 服务：

```text
onchain_main.py
└── paopao-onchain-flow.service
```

禁止把链上采集、重放、聚合或 Telegram 发送挂入现有 `main.py live` / `run_loop()`。新进程崩溃、RPC 限流、数据库锁、标签错误或 Telegram 失败，不得导致以下服务退出、阻塞或重启：

```text
paopao-radar.service
paopao-market-stream.service
```

### 2.2 配置隔离

新增：

```text
.env.onchain.example   # 提交仓库
.env.onchain           # 本机密钥和运行参数，不提交
```

`.env.oi` 仍由现有 BOT 使用。链上服务可以只读复用其中的 `TG_BOT_TOKEN`、`TG_CHAT_ID` 和公开行情端点；`.env.onchain` 的同名配置具有更高优先级。

默认值必须满足：

```dotenv
ONCHAIN_ENABLE=false
ONCHAIN_REAL_SEND=false
```

只有显式执行：

```bash
python onchain_main.py live --send --confirm-real-send
```

才允许真实发送。

### 2.3 存储隔离

链上服务只写入：

```text
data/onchain/
├── onchain_flow.db
├── runtime_status.json
├── tg_push_history.json
├── tg_outbox.json
├── tg_topic_routes.json
└── backups/
```

禁止写入或复用以下现有写路径：

```text
data/signals.db
data/market_snapshots.db
data/realtime_features.db
data/tg_push_history.json
data/tg_outbox.json
```

链上服务允许以 SQLite 只读 URI 打开现有行情数据库，用于补充价格、OI、Funding 和 CVD；必须设置 `mode=ro`、`PRAGMA query_only=ON`、短 `busy_timeout`。读取失败或数据过期时跳过市场确认，不等待、不重试占锁、不影响主 BOT。

### 2.4 Telegram 隔离

新增模板与专用话题：

```text
TG_ONCHAIN_FLOW_ALERT
TG_ONCHAIN_FLOW_TOPIC_ID
```

链上服务使用独立的 push history、outbox、小时限额和冷却状态，不能消耗现有 BOT 的 `TG_GLOBAL_HOURLY_LIMIT`，也不能因为链上消息过多而抑制已有资金流、启动或资金费率信号。

可以复用现有 `TelegramGateway` 的发送契约，但必须通过一份派生 `Settings` 将所有审计和信号存储路径替换为 `data/onchain/` 下的独立路径。

## 3. 总体架构

```text
                 ┌──────────────────────────────────────┐
                 │       EVM Chain Adapters             │
                 │ HTTP backfill + WebSocket hot path   │
                 └───────────────┬──────────────────────┘
                                 │ NormalizedTransfer
                                 ▼
┌──────────────────┐     ┌───────────────────┐
│ CEX Label Registry│────▶│ Flow Classifier   │
│ seed + inferred   │     │ inflow/outflow/...│
└──────────────────┘     └─────────┬─────────┘
                                   │ ClassifiedFlow
                                   ▼
                         ┌────────────────────┐
                         │ SQLite Event Store │
                         │ cursor/idempotency │
                         └─────────┬──────────┘
                                   │
                                   ▼
                         ┌────────────────────┐
                         │ Rolling Aggregator │
                         │ 5m/15m/1h/4h       │
                         └─────────┬──────────┘
                                   │
                 ┌─────────────────┴──────────────────┐
                 ▼                                    ▼
       ┌──────────────────┐                 ┌──────────────────┐
       │ Anomaly Scorer   │                 │ Market Confirmer │
       │ size/batch/trend │                 │ read-only facts  │
       └─────────┬────────┘                 └─────────┬────────┘
                 └─────────────────┬──────────────────┘
                                   ▼
                         ┌────────────────────┐
                         │ Signal Decision    │
                         │ direction/confidence│
                         └─────────┬──────────┘
                                   ▼
                         ┌────────────────────┐
                         │ Dedicated Telegram │
                         │ topic/outbox/limits│
                         └────────────────────┘
```

## 4. 建议代码结构

```text
onchain_main.py
paopao_radar/onchain_flow/
├── __init__.py
├── cli.py
├── config.py
├── models.py
├── constants.py
├── runtime.py
├── health.py
├── metrics.py
├── db.py
├── migrations.py
├── labels.py
├── classifier.py
├── token_metadata.py
├── price_oracle.py
├── aggregator.py
├── detector.py
├── scorer.py
├── formatter.py
├── notifier.py
├── outcome_tracker.py
└── collectors/
    ├── base.py
    ├── evm_http.py
    ├── evm_ws.py
    ├── replay.py
    └── sqd.py              # 后续可选，不作为首个 PR 的硬依赖
scripts/
├── install_onchain_flow.sh
├── onchain_health_check.sh
└── import_cex_labels.py
config/onchain/
├── chains.example.json
└── cex_addresses.example.csv
tests/onchain_flow/
```

首个实现阶段保持 Python 3.12，优先复用仓库已有 `requests`、`websocket-client`、`sqlite3` 和原子 JSON 工具。不要在第一阶段把 Node、Docker、PostgreSQL 或 Arkham API 变成启动必需条件。

## 5. 统一事件契约

### 5.1 NormalizedTransfer

```python
@dataclass(frozen=True)
class NormalizedTransfer:
    event_id: str                 # chain_id:tx_hash:log_index
    chain_id: int
    chain_name: str
    block_number: int
    block_hash: str
    block_time: int
    tx_hash: str
    log_index: int
    token_address: str
    from_address: str
    to_address: str
    amount_raw: int
    removed: bool
    confirmation_status: str      # pending/finalized/orphaned
    source: str                   # evm_http/evm_ws/replay/sqd
```

`event_id` 必须是全局幂等键。Webhook、WebSocket、HTTP 回补或重放命中同一日志时，只更新来源和确认状态，不得重复聚合。

### 5.2 TokenMetadata

```python
@dataclass(frozen=True)
class TokenMetadata:
    chain_id: int
    token_address: str
    symbol: str
    name: str
    decimals: int | None
    token_kind: str               # erc20/non_fungible/unknown
    metadata_status: str          # verified/partial/failed
    updated_at: int
```

EVM `Transfer(address,address,uint256)` 与 ERC-721 的 Topic 相同，不能只凭 Topic 认定 ERC-20。至少通过 `decimals()`、`symbol()`、合约接口探测和缓存进行分类；无法确认时保存原始事件，但不得生成方向性告警。

### 5.3 ClassifiedFlow

```python
@dataclass(frozen=True)
class ClassifiedFlow:
    event_id: str
    flow_type: str                # inflow/outflow/internal/cross_cex/consolidation/non_cex
    exchange_from: str | None
    exchange_to: str | None
    counterparty_address: str
    amount: Decimal | None
    amount_usd: Decimal | None
    label_confidence: float
    price_status: str
```

## 6. EVM 采集策略

### 6.1 热路径

每条启用链维护独立 WebSocket 连接。监听 ERC Transfer Topic：

```text
keccak256("Transfer(address,address,uint256)")
```

以交易所地址分片建立两组订阅：

```text
topic1 in CEX addresses  # from CEX，候选流出
topic2 in CEX addresses  # to CEX，候选流入
```

这样不需要预先知道 Token 合约，可以覆盖所有与已知交易所地址发生 Transfer 的合约。每个 provider 的单订阅地址上限通过配置控制，默认小分片；订阅失败时自动减小分片而不是无限快速重试。

### 6.2 冷路径和补数

WebSocket 只负责低延迟发现，HTTP `eth_getLogs` 才是可回放事实源：

1. 每条链记录 `last_finalized_block`；
2. 启动或重连后，从持久化游标到当前安全高度分块补数；
3. 每批区块范围可动态缩小，处理 provider 的结果上限；
4. 只有区块达到链级确认深度后才标记 finalized；
5. 收到 `removed=true` 或发现 block hash 不一致时，将事件标为 orphaned，并重建受影响窗口。

初始链与默认确认深度建议：

| 链 | chain_id | 默认确认数 | 首轮状态 |
| --- | ---: | ---: | --- |
| Base | 8453 | 20 | 第一条验收链 |
| Ethereum | 1 | 12 | 第二条验收链 |
| Arbitrum | 42161 | 20 | 后续启用 |
| Optimism | 10 | 20 | 后续启用 |
| BSC | 56 | 15 | 后续启用 |
| Polygon | 137 | 64 | 后续启用 |

所有 RPC/WSS 端点必须从 `.env.onchain` 读取；未配置的链显示为 disabled，不算健康失败。

### 6.3 背压

采集线程不得直接写 SQLite。采用有界队列和单写线程：

```text
chain workers -> bounded event queue -> normalizer/classifier -> single DB writer
```

队列满时：

- 记录 dropped/backpressure 指标；
- 立即切换到 HTTP 游标补数模式；
- 不阻塞现有 BOT；
- 不静默丢失 finalized 区块。

## 7. 地址标签注册表

### 7.1 数据结构

```text
chain_id
address
entity_name
entity_type          # cex/market_maker/bridge/custodian/treasury/vesting/unknown
address_type         # hot/cold/deposit/collector/treasury/contract
source
confidence
valid_from
valid_to
first_seen
last_verified
evidence_json
```

地址统一小写并校验长度。每个标签保留来源、置信度和有效时间，不覆盖旧记录。

### 7.2 标签来源

允许：

- 交易所官方披露；
- 已获授权的公开数据；
- 人工核验；
- 基于归集模式推断的本地标签；
- 后续可选的 Arkham/Dune 等外部增强结果。

禁止：

- 抓取 Arkham 页面或逆向私有接口；
- 把来源不明的地址列表直接标为高置信度；
- 因为某地址曾与交易所交互，就直接把它当成交易所控制地址。

Dune Spellbook 的 CEX flow 和 deposit-address 模型只作为算法研究参考；其当前 BSL 条款需要单独审查，不直接复制受限制代码到本项目。

### 7.3 分类规则

```text
同一家 CEX -> 同一家 CEX      internal，方向分 0
CEX A -> CEX B                cross_cex，默认方向分 0
外部 -> CEX                   inflow，潜在卖压先验
CEX -> 外部                   outflow，潜在提币/积累先验
充值地址 -> 同所热钱包         consolidation，不二次计算流入
非 CEX -> 非 CEX              non_cex，不进入常规告警
```

做市商、桥、托管和国库地址作为修正因子，不能和普通巨鲸使用同一解释。

## 8. 数据库

使用独立 SQLite，WAL 模式、短事务、单写线程。核心表：

```text
schema_migrations
chain_cursors
address_labels
token_metadata
transfer_events
flow_events
flow_windows
alerts
alert_deliveries
signal_outcomes
```

关键约束：

- `transfer_events(chain_id, tx_hash, log_index)` 唯一；
- `flow_events.event_id` 唯一；
- `alerts.alert_key` 唯一；
- 金额使用字符串或整数 raw value，避免 float 精度损失；
- 每次聚合都记录算法版本和阈值版本；
- 原始 candidate transfer 默认保留 30 天，聚合窗口和告警保留 365 天；
- 备份目录独立，不能混入现有主 BOT 备份任务。

## 9. 价格与市场确认

### 9.1 价格接口

```python
class PriceOracle(Protocol):
    def quote(self, chain_id: int, token_address: str, symbol: str, at_ts: int) -> PriceQuote | None:
        ...
```

首版优先级：

1. 从现有 Binance 支持的交易对目录映射 `SYMBOLUSDT`；
2. 对触发候选按需调用公开现货价格接口，并设置独立缓存、预算和熔断；
3. 后续增加链上 DEX TWAP 或第三方价格适配器。

价格缺失时：

- 保存事件和原始数量；
- 标记 `price_status=missing`；
- 不使用伪造的 0 美元；
- 不发“偏多/偏空”正式告警，可在诊断中计入 unpriced rate。

### 9.2 只读市场确认

链上事件超过初步门槛后，按需读取现有市场事实：

```text
15m/1h 价格变化
Spot CVD
Futures CVD
OI 变化
Funding
24h 现货成交额
```

市场数据仅用于调整分数和置信度，不得覆盖链上事实。若现有数据库锁定、缺失或过期，告警正文写明“市场确认缺失”，但链上服务继续运行。

## 10. 异常检测

### 10.1 第一版规则

```text
single_large:
  单笔 amount_usd >= 动态单笔阈值

batch_flow:
  15m 内同方向 tx_count >= 5
  且 distinct_counterparties >= 3
  且 total_usd >= 动态 15m 阈值

continuous_flow:
  60m 内至少 3 个不同 15m 桶同方向活跃
  且 tx_count >= 8
  且 total_usd >= 动态 60m 阈值

multi_exchange:
  60m 内至少 2 家交易所同方向异常
```

### 10.2 动态阈值

阈值不能只用固定美元金额：

```text
single_threshold = max(
  absolute_floor,
  historical_single_p99,
  24h_spot_volume * single_volume_ratio
)

window_threshold = max(
  absolute_window_floor,
  historical_window_p99,
  historical_window_median + k * MAD,
  24h_spot_volume * window_volume_ratio
)
```

在历史样本不足时使用保守固定阈值，并标记 `baseline_status=cold_start`。

## 11. 多空倾向评分

输出范围：`-100 ... +100`。

- Token 净流入交易所：负向先验；
- Token 净流出交易所：正向先验；
- Internal / Cross-CEX：默认 0；
- 项目方、vesting、国库流入交易所：增强负向；
- 多个独立普通地址、多所同步、持续多个窗口：增强同方向；
- 做市商：权重折扣；
- 链上偏空且现货 CVD/价格同步走弱：增强偏空置信度；
- 链上偏空但价格不跌、现货 CVD 增强：识别“卖压被吸收”，降低偏空或转为中性观察；
- 缺失价格、标签置信度低、只有单笔未知地址：降低置信度。

建议输出：

| 分数 | 文案 |
| ---: | --- |
| +60 ～ +100 | 强偏多 |
| +30 ～ +59 | 偏多 |
| -29 ～ +29 | 中性 / 证据不足 |
| -30 ～ -59 | 偏空 |
| -60 ～ -100 | 强偏空 |

评分不是概率。只有 `signal_outcomes` 积累足够样本并完成时间外回测、概率校准后，才能展示历史命中率。

## 12. Telegram 消息契约

示例：

```text
🔴 $ABC 异常交易所净流入

判断：未来 1h–4h 偏空
方向评分：-68 / 100
置信度：中高

链上资金流：
• 15m 净流入：$2.4M
• 60m 净流入：$4.1M
• 交易：17 笔 / 11 个独立地址
• 交易所：Binance、OKX
• 高于 30d 同窗口 P99：2.3 倍

市场确认：
• 15m 价格：-1.4%
• Spot CVD：下降
• OI：+5.2%
• Funding：正值

解释：持续多所流入且现货卖压确认。交易所流入代表潜在可售供应，不等于必然下跌。

链：Ethereum
Tx：0x…
数据状态：finalized / 标签置信度 0.94
```

告警键建议：

```text
chain_id:token_address:direction:window_start:severity_version
```

同一窗口只发一次；严重度升级可以追加一次回复链消息。恢复或方向反转必须生成新事件，不能覆盖原始告警。

## 13. CLI 与运维

```text
python onchain_main.py status
python onchain_main.py doctor
python onchain_main.py labels-check
python onchain_main.py db-check
python onchain_main.py replay --fixture tests/fixtures/onchain/...
python onchain_main.py once
python onchain_main.py live
python onchain_main.py live --send --confirm-real-send
```

`doctor` 至少检查：

- 配置解析且不输出密钥；
- 独立数据路径；
- 标签文件格式和重复地址；
- 每条启用链的 HTTP/WSS 状态；
- 当前区块与持久化游标差距；
- SQLite integrity check；
- Telegram 配置格式；
- 是否错误指向现有 BOT 写路径。

运行指标：

```text
last_seen_block
last_finalized_block
cursor_lag_blocks
ws_reconnect_total
rpc_error_total
queue_depth
backpressure_total
duplicate_event_rate
orphaned_event_total
classified_flow_rate
unpriced_flow_rate
alert_total
telegram_delivery_total
```

## 14. systemd 与发布

新增独立脚本：

```text
scripts/install_onchain_flow.sh
```

首阶段禁止修改现有服务的 `ExecStart`。安装脚本只管理：

```text
paopao-onchain-flow.service
paopao-onchain-health.timer
```

默认只写 unit，不 enable、不 start。只有 `.env.onchain` 中 `ONCHAIN_ENABLE=true` 且用户显式传入 `--enable` 时才启用。

建议资源边界：

```text
MemoryHigh=256M
MemoryMax=384M
TasksMax=128
Restart=always
RestartSec=10
```

更新策略：

- 新服务独立重启；
- 现有 `paopao-radar` 和 `paopao-market-stream` 的安装、更新和重启顺序保持不变；
- 进入稳定阶段前，不把新服务加入主安装脚本的自动启动列表。

## 15. 分阶段项目路径

### P3.0：隔离骨架与可重放纵向切片

交付：

- 独立入口、配置、数据目录和 CLI；
- SQLite schema/migrations；
- 标签导入、标签校验、flow classifier；
- HTTP `eth_getLogs` 分块回补接口与 mock；
- synthetic fixture 重放；
- 单笔/批量/持续规则的确定性测试；
- 独立 Telegram dry-run；
- 不安装、不启动真实服务。

验收：现有完整测试零回归，默认配置下新模块不运行。

### P3.1：Base 单链 dry-run

交付：

- Base HTTP 游标补数；
- WSS 热路径、重连和去重；
- token metadata/价格缓存；
- 独立 systemd unit；
- 连续 7 天 dry-run 观测报告。

验收：重连期间由 HTTP 补齐，重复率可解释，无 finalized 漏块证据。

### P3.2：Ethereum + 六条 EVM 链

交付：

- Ethereum、Arbitrum、Optimism、BSC、Polygon；
- 链级确认数、provider 限制和分片配置；
- 多链统一窗口与多所同步检测；
- 独立 Telegram 真实发送开关。

### P3.3：标签质量和充值地址归集

交付：

- 标签来源/置信度/有效时间；
- deposit/collector 地址推断候选；
- consolidation 去重；
- 人工审核和回滚工具；
- bridge、custodian、market maker 降噪。

### P3.4：市场确认、结果追踪和校准

交付：

- 只读市场事实适配器；
- 1h/4h/24h outcome；
- 按流动性层级、链、交易所和信号类型回测；
- 分数到经验概率的校准报告；
- 不自动修改生产阈值。

### P3.5：Solana 与高吞吐适配器

交付：

- Yellowstone gRPC / Geyser 适配器；
- SPL Token 与 Token-2022 统一事件；
- 可选 Subsquid/SQD 或 Substreams 历史数据适配器；
- 数据量达到阈值后评估 PostgreSQL/ClickHouse，不提前迁移。

## 16. 非功能验收门禁

每个 PR 都必须满足：

1. `python -m compileall -q paopao_radar tests scripts main.py onchain_main.py`；
2. `python -m unittest discover -s tests -p "test_*.py"`；
3. 现有测试数量不得下降，现有测试不得为通过而删除或放宽；
4. `ONCHAIN_ENABLE=false` 时，无新网络连接、线程、数据库写入或 Telegram 调用；
5. 新模块故障不能改变现有 `main.py live` 的退出码和运行状态；
6. 新模块不得写现有 BOT 的 push history/outbox/SQLite；
7. RPC、WSS、价格、标签、Telegram 都必须有超时、熔断或降级；
8. 密钥不进入日志、诊断、测试 fixture 或提交历史；
9. 所有外部事件均以幂等键处理；
10. reorg、重复、断线补数、数据库锁、队列背压、价格缺失必须有测试。

## 17. 首个 Codex 实施范围

首个 Codex 任务只实施 **P3.0**，不得提前把真实链上服务加入生产自启动，也不得重构现有 `run_loop()`。

优先完成一个可重放、可测试、默认不运行的完整纵向切片：

```text
fixture Transfer
→ normalize
→ label lookup
→ classify
→ persist
→ aggregate
→ score
→ format
→ dedicated Telegram dry-run
```

P3.0 合并并通过完整回归测试后，再单独开启 P3.1 的真实 Base 链连接 PR。

## 18. 开源研究参考

仅作为实现和数据建模参考：

- `subsquid/squid-sdk`：多链 ETL、游标和历史回补；
- `subsquid-labs/base-transfers-erc20`：全 ERC-20 部署与 Transfer 示例；
- `streamingfast/substreams`：高吞吐和 cursor/reorg；
- `duneanalytics/spellbook`：CEX flow 与充值归集算法思路，注意 BSL；
- `forta-network/starter-kits`：异常 Transfer、时间序列和告警模式；
- `openlabelsinitiative/OLI`：标签 schema、来源和 trust；
- `rpcpool/yellowstone-grpc`：Solana Geyser 实时流。

项目不得引入 Arkham 页面抓取、隐藏 API 或未经授权的标签再分发。