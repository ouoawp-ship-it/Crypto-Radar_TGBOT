# OAR-P1 主动 Token 链上活动查询

## 范围

`token-activity` 是独立于后台链上服务的显式只读命令。第一版仅支持：

- Base（chain ID `8453`）；
- 指定 ERC-20 合约；
- 最近 `15m`、`1h`、`4h`、`24h`；
- finalized 区块中的全部标准 `Transfer(address,address,uint256)` 日志；
- 本地 CEX 标签分类；
- 可选的查询时价格估值。

它不包含 Trace、DEX Swap、行为分析、钱包关联、AI、Watchlist 或 Telegram。

## CLI

```bash
python onchain_main.py token-activity \
  --chain base \
  --contract 0x0000000000000000000000000000000000000001 \
  --window 24h \
  --allow-network
```

可选参数：

```text
--max-events N
--max-rpc-requests N
--top N
--with-price
--min-usd DECIMAL
--pretty
--output-file PATH
```

`--allow-network` 是强制门禁。没有该参数时，不创建 RPC Client，也不发起网络请求。
命令不接受 `--send` 或 `--confirm-real-send`，永远不会创建 Telegram
Gateway 或发送消息。

`--min-usd` 只能和 `--with-price` 一起使用。`--output-file` 使用临时文件和
原子无覆盖提交写入完整 JSON，stdout 只输出状态、Transfer 数量和文件路径。
为避免覆盖生产状态和源码，目标文件必须尚不存在，也不能是符号链接。
仓库内仅允许写入已经存在的 `reports/onchain/` 目录；仓库外允许写入已经
存在的父目录。最终提交使用同目录临时文件、`flush`、`fsync` 和原子无覆盖
hard-link，不使用会替换现有文件的 `os.replace`。

## Finalized 与时间窗口

查询不使用“当前区块减固定块数”近似时间：

1. 读取 Base head；
2. `to_block = head - ONCHAIN_BASE_CONFIRMATION_DEPTH`；
3. 读取 `to_block` 的链上时间戳；
4. 计算 `target_from_time = to_block_time - window_seconds`；
5. 以有界二分搜索找到第一个时间戳大于等于目标时间的 finalized 区块；
6. 通过 Token 合约 `address` 和唯一 Topic0 `Transfer` 查询日志；
7. 读取日志所在唯一 Block 的真实时间戳，并再次按时间边界过滤。

Block Header 在单次查询中缓存；不得读取或推进生产 chain cursor。

## Token Filter

Token 查询使用：

```json
{
  "address": "0x...",
  "fromBlock": "0x...",
  "toBlock": "0x...",
  "topics": [
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
  ]
}
```

该路径复用现有 `BaseHttpCollector` Adaptive Range、RPC 错误分类、严格 ERC-20
ABI 校验、canonical event identity 和去重。现有 CEX topic1/topic2 Filter
保持原样。Provider 若返回其他 Token 合约的日志，查询会 fail closed。
4-topic indexed-value/ERC-721 形态只计入跳过诊断，不会被解释为 ERC-20 数量。
每个 Adaptive Range Segment 的返回日志还会在进入 partial、去重、标准化和
Block Header 查询之前验证：

```text
segment_start <= log.blockNumber <= segment_end
```

任何越界日志都会令查询以 `malformed_log` fail closed。

## 查询预算

安全默认值：

| 配置 | 默认值 | 硬上限 |
|---|---:|---:|
| `TOKEN_ACTIVITY_MAX_WINDOW_HOURS` | 24 | 24 |
| `TOKEN_ACTIVITY_MAX_EVENTS` | 5000 | 5000 |
| `TOKEN_ACTIVITY_MAX_RPC_REQUESTS` | 256 | 256 |
| `TOKEN_ACTIVITY_MAX_UNIQUE_BLOCK_HEADERS` | 2000 | 2000 |
| `TOKEN_ACTIVITY_TOP_N` | 50 | 100 |
| `TOKEN_ACTIVITY_BLOCK_SEARCH_MAX_CALLS` | 32 | 32 |

CLI 可以在已配置上限内进一步降低 `max-events`、`max-rpc-requests` 和 `top`，
不能提高上限。RPC 总预算包含 retry attempt、metadata、head、binary search、
`eth_getLogs` 和 Block Header。

## 完整性与退出码

| 退出码 | 状态 | 语义 |
|---:|---|---|
| 0 | `ok` | 链上查询完整 |
| 2 | `partial` | 返回部分可靠事实，但达到预算或 Provider 持续失败 |
| 1 | `failed` | 无法形成可靠查询结果 |

`partial` 必须同时满足：

```json
{
  "complete": false,
  "truncated": true,
  "truncation_reason": "max_events"
}
```

已取得的事实会保留。结果不会把超限、429、timeout 或 Provider range limit
伪装为完整的零 Transfer。后续 OAR 阶段不得把 `complete=false` 当成完整行为样本。

## 输出 Schema

Schema 版本为 `1`，主要结构如下：

```json
{
  "schema_version": 1,
  "status": "ok",
  "complete": true,
  "truncated": false,
  "truncation_reason": null,
  "query": {
    "chain": "base",
    "chain_id": 8453,
    "contract": "0x...",
    "window": "24h",
    "window_seconds": 86400,
    "from_block": 0,
    "to_block": 0,
    "from_time": 0,
    "to_time": 0,
    "confirmation_depth": 20,
    "min_usd": null,
    "usd_filter_applied": false
  },
  "token": {
    "contract": "0x...",
    "symbol": "TOKEN",
    "name": "Token",
    "decimals": 18,
    "metadata_status": "verified_erc20"
  },
  "price": {
    "enabled": false,
    "status": "disabled",
    "price_usd": null,
    "source": "",
    "observed_at": 0,
    "historical_price": false
  },
  "labels": {
    "status": "ok",
    "count": 0,
    "identity_label_count": 0,
    "classification_eligible_cex_count": 0
  },
  "summary": {},
  "largest_transfers": [],
  "transfers": [],
  "limits": {},
  "diagnostics": {},
  "warnings": []
}
```

每条 `transfers` 记录至少包含：

```json
{
  "event_id": "8453:0x...:0",
  "block_number": 0,
  "block_hash": "0x...",
  "block_time": 0,
  "block_time_iso": "2026-01-01T00:00:00Z",
  "tx_hash": "0x...",
  "log_index": 0,
  "explorer_url": "https://basescan.org/tx/0x...",
  "token_contract": "0x...",
  "from": {
    "address": "0x...",
    "known": false,
    "classification_eligible": false,
    "entity_name": "未知钱包",
    "entity_type": "",
    "address_type": "",
    "source": "",
    "confidence": 0
  },
  "to": {},
  "amount_raw": "0",
  "amount": "0",
  "amount_usd": null,
  "price_status": "disabled",
  "flow_type": "unclassified"
}
```

Token 数量和 USD 值以 Decimal 字符串输出，`amount_raw` 保留完整整数语义。
Transfer 按 `block_number → log_index → tx_hash` 确定性排序。

## Metadata、标签与分类

Metadata 复用 `TokenMetadataResolver` 的 bytecode、`decimals()`、
`totalSupply()`、`symbol()` 和 `name()` 校验，但使用 query-local memory cache，
不写 `token_metadata` 表。合约不存在、无法验证 ERC-20 或 decimals 非法时
fail closed；symbol/name 解码失败允许降级。

主动查询把标签分成两个语义层：

- Identity Registry：显示地址名称、类型、来源、置信度；
- Direction Registry：只包含 Base、查询窗口有效、置信度不低于
  `ONCHAIN_MIN_LABEL_CONFIDENCE` 且非 `synthetic_fixture` 的 CEX 标签。

`known=true` 只表示存在有效身份标签，不代表该标签可以用于交易所方向分类。
每个地址的 `classification_eligible` 会明确说明是否进入 Direction Registry。

Direction Registry 覆盖正常时复用 `classify_transfer`，支持：

- `inflow`
- `outflow`
- `internal`
- `consolidation`
- `cross_cex`
- `non_cex`
- `mint`
- `burn`

标签文件不存在时，查询仍运行，所有地址显示“未知钱包”，
`labels.status=missing`。文件格式正确但没有足够的高置信度 Base CEX 标签时，
`labels.status=insufficient_cex_coverage`；Identity 标签继续显示，但依赖 CEX
身份的方向统一为 `unclassified`。低置信度 CEX 标签不会产生 inflow/outflow。

Mint 和 burn 由 zero address 确定，不依赖标签覆盖，因此在 missing 或
insufficient 状态下仍保留。普通钱包间的 `non_cex` 只在覆盖正常且双方都有
有效、非 synthetic 且达到最低置信度的非 CEX Identity 标签时输出。

标签文件格式损坏、字段非法、地址重复，或联网查询使用
`source=synthetic_fixture` 的 CEX 标签时 fail closed。流入交易所不等于已经
卖出，从交易所提出也不等于已经买入或必然上涨。

## 可选价格

默认不调用价格 Provider。只有 `--with-price` 才使用现有配置的 Provider。
价格缺失不会丢弃 Transfer，`amount_usd=null`。USD 是查询时可用价格的估算，
不是历史成交价。

如果指定 `--min-usd` 但价格不可用：

- 保留全部已取得的链上事实；
- `usd_filter_applied=false`；
- 返回 `partial`；
- 输出 `price_unavailable_for_usd_filter`。

## 零业务写入边界

`token-activity` 不调用 migration、`commit_finalized_range()`、
`advance_cursor()`、`update_head_status()`、告警、delivery、SignalEventStore
或 Telegram。它不写：

- `onchain_flow.db`
- `chain_cursors`
- `processed_blocks`
- `transfer_events`
- `flow_events`
- `alerts`
- `alert_deliveries`
- 主 BOT `signals.db`
- Telegram history/outbox

`--output-file` 是唯一显式可选写入，并且只写用户指定的 JSON 文件。

## 常见错误

- `allow_network_required`：缺少显式网络授权；
- `invalid_contract`：不是 20-byte EVM 地址；
- `wrong_chain`：CLI 或 RPC 不是 Base；
- `rpc_not_configured` / `rpc_auth_failed`：HTTP RPC 不可用；
- `token_not_contract` / `token_not_erc20` / `invalid_decimals`：Token 校验失败；
- `label_file_invalid`：标签文件存在但不安全；
- `malformed_log`：Provider 返回不一致或非 canonical 日志；
- `query_budget_exhausted_before_any_result`：形成可靠结果前已耗尽预算。
- `output_file_exists`：输出目标已经存在，不会覆盖；
- `unsafe_output_file`：符号链接或受保护的仓库/状态路径；
- `output_parent_missing`：输出父目录不存在；
- `output_write_failed`：无法完成安全无覆盖提交。

错误输出会脱敏，不包含 RPC URL、API Key、Authorization 或 Telegram Token。

## 可选人工 Smoke

普通测试不访问真实网络。只有人工确认本地已配置 Base HTTP RPC 后，才运行：

```bash
python onchain_main.py token-activity \
  --chain base \
  --contract 0x0000000000000000000000000000000000000001 \
  --window 15m \
  --max-events 20 \
  --max-rpc-requests 50 \
  --top 10 \
  --allow-network \
  --pretty
```

该命令不启用 WSS、数据库或 Telegram。本开发阶段不执行真实 Smoke。

## OAR-P2 边界

OAR-P1 只提供确定性链上事实。持续吸筹/派发、归集/fanout 状态、钱包关联评分、
AI 解释、Telegram 状态卡片和自动 Watchlist 均属于后续独立阶段。
