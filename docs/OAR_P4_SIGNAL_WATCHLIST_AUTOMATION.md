# OAR-P4 信号 Watchlist 与有界自动扫描

OAR-P4 在现有 OAR-P1/P2/P3 之上增加已验证 Token Registry、只读市场信号桥接、带 TTL 的 Watchlist 和顺序自动扫描。它不改变 Base 实时 CEX Collector、链游标、行为规则、主 BOT 循环或 Telegram 发送器。

## 安全边界

- `OAR_AUTOMATION_ENABLE=false` 是默认值。
- 自动化业务状态只写 `data/onchain/oar_automation.db`。
- Bridge 通过 SQLite `mode=ro` 和 `query_only` 读取 `data/signals.db`，不使用主 SignalEventStore 的写连接。
- 自动扫描不写 `onchain_flow.db` 的 Transfer、Cursor、Processed Block 或 Alert 表。
- 仅在行为或钱包群组通过通知门禁后才构建报告；只有显式 `--with-ai` 才允许创建 AI 调用。
- `watch-live` 必须同时具备 `OAR_AUTOMATION_ENABLE=true` 和 `--allow-network`。
- 真实 Telegram 仍需要 `ONCHAIN_REAL_SEND=true`、`--send`、`--confirm-real-send` 三重门禁。

## 为什么 Registry 必须验证

市场交易对只有 Symbol，不能唯一确定链和合约。同名、伪造、倍数命名 Token 都可能映射错误，因此系统不会按 Symbol 搜索或猜测合约。自动 Watch 只接受：

- `chain_id=8453`；
- 合约地址为 canonical lowercase EVM 地址；
- Base RPC 返回 chain ID 8453；
- 合约有 bytecode；
- `decimals()` 和 `totalSupply()` 可验证；
- metadata 状态为 `verified_erc20`；
- 该市场交易对恰有一个 `verified + is_primary` 映射。

链上 Symbol 与市场交易对前缀不一致时，`registry-verify` 默认返回 `symbol_mismatch_requires_confirmation`；只有人工复核后显式使用 `--accept-symbol-mismatch` 才可继续。

## Registry 命令

添加 pending 记录，不联网、不进入 Watchlist：

```text
python onchain_main.py registry-add --market-symbol XYZUSDT --chain base --contract 0x... --source manual
```

通过 Base RPC 验证并设为 Primary：

```text
python onchain_main.py registry-verify --token-key 8453:0x... --allow-network --set-primary
```

重复验证不会改变当前 Primary/Secondary 身份；只有显式
`--set-primary` 才会原子切换同一市场交易对的 Primary。Token 成为
verified Primary 后，会按 `public_ref` 只读回查仍在 TTL 内的 open
unresolved 信号并恢复 Watch；主信号库暂时不可读时验证仍成功，恢复后
`bridge-once` 会继续有界重试。

只读列表和保留审计的禁用：

```text
python onchain_main.py registry-list --status verified --limit 100
python onchain_main.py registry-disable --token-key 8453:0x...
```

`registry-list` 在数据库不存在时返回 `not_initialized`，不会创建文件。禁用不会硬删除 Registry、来源或扫描审计。

## 手工 Watchlist

```text
python onchain_main.py watch-add --token-key 8453:0x... --ttl-hours 720 --priority 100
python onchain_main.py watch-list --status active --due-only --limit 100
python onchain_main.py watch-remove --token-key 8453:0x...
```

只有 verified Token 可以加入。`watch-remove` 仅取消 manual 来源；仍有未过期市场信号时 Watch 保持 active，否则转为 expired。

## 主信号只读 Bridge

```text
python onchain_main.py bridge-once
```

Bridge 只消费 `launch`、`flow`、`funding`、`announcement` 模块中满足 `sent=1`、`status=sent`、`ingest_mode=structured`、`quality_status=ready` 且 Symbol 非空的记录。`module=onchain` 永远忽略，防止链上信号触发自身。

首次只看最近一小时。后续使用 `last_signal_ts + last_signal_id` 水位，并回看 300 秒重叠窗口以捕获同 ID 的更新。每条信号的 Registry 解析、Source/Unresolved upsert、Watch 更新和水位推进在同一事务完成；失败时水位不会越过该信号。

主数据库不存在或暂时锁定时返回降级状态，不创建、不修改主数据库。Bridge 不读取 `text_html`，只保留结构化最小字段、最多 300 字的 excerpt 和 payload hash。精确 unresolved 回读最多 100 个 public ref；已过 TTL 的来源标记为 expired，成功恢复的来源标记为 resolved，审计记录不会删除。

## TTL、优先级和合并

默认来源策略：

| 来源 | TTL | 优先级 |
|---|---:|---:|
| manual | 30 天 | 100 |
| launch | 24 小时 | 90 |
| flow | 24 小时 | 80 |
| announcement | 3 天 | 75 |
| funding | 12 小时 | 70 |

相同 `token_key` 只有一个 Watch Item。Priority 取所有有效来源的最高值，Expiry 取最大有效过期时间。来源过期后保留历史但不再参与调度；manual 未过期时仍保持 active。达到 active Token 容量时，新候选记录为 `capacity_exceeded`，不删除旧 Watch，也不启动 RPC。

## Claim、Lease 与失败恢复

每轮使用短 `BEGIN IMMEDIATE` 逐个 Claim 到期 Token，并为每次 Claim 生成唯一 `lease_owner/lease_until`。续租、成功、失败和延期完成都必须匹配 Owner；旧 Worker 在 Lease 过期并被新 Worker 接管后只能留下 bounded stale audit，不能覆盖新 Worker 的状态或发送通知。进程崩溃后 Lease 到期即可恢复。

成功后按扫描间隔调度。Partial 或失败按 5、15、30、60 分钟有界退避；连续失败达到阈值后 Watch 转为 paused，保留所有历史等待人工处理。整轮 RPC 预算耗尽后的未扫描 Token 标记为 `deferred_by_cycle_budget`，不增加失败次数、不退避、不暂停，下一轮仍可立即 Claim。扫描审计不保存完整 Transfer 数组，每 Token 保留最近 100 次、全局最多 5000 次。

## 自动扫描

单轮显式查询：

```text
python onchain_main.py watch-once --allow-network
```

默认 observe：执行 Bridge、扫描到期 Token、写审计，不创建 AI Client 或 TelegramGateway。需要检查报告渲染时可使用：

```text
python onchain_main.py watch-once --allow-network --notify-dry-run
```

这会走现有 ReportNotifier，但真实 Telegram HTTP 调用为 0。可选 `--with-ai` 仍只在通知门槛后生效。

有界循环：

```text
python onchain_main.py watch-live --allow-network --duration-minutes 30
```

`watch-live` 不启动 Base WSS Collector，不读取或推进生产 chain cursor。第一版顺序扫描，不并发查询 Token。

## 查询预算与通知门禁

默认每轮最多 5 个 Token、总计 400 个 RPC 请求；每 Token 最多 1000 个事件、100 个 RPC 请求，保留 20 条代表性 Transfer。扫描器根据真实消耗逐个决定是否 Claim 下一 Token；整轮预算归零后不再创建 Analysis、Report、AI 或 Telegram 对象。高密度 Token 或 Provider 限流可产生 Partial；Partial 被审计并退避，但默认不通知。

进入报告/通知阶段必须满足完整 Activity、完整 Analysis、`analysis.status=ok`，并满足以下之一：

- 正式 Behavior 为 accumulation、distribution、wallet consolidation 或 fanout，且规则分数至少 55；
- 最高 Wallet Group 分数至少 60。

`no_activity`、`isolated`、`inconclusive_activity`、标签不足和任何 Partial 都不自动通知。来源市场信号本身只提供 Watch 原因和上下文，不会单独触发链上消息。

## 关联市场信号

报告的 `linked_market_signals` 只包含 public ref、module、symbol、score、stage、severity、结构化方向、timestamp、age 和短摘要。方向只允许 `long`、`short` 或空值，不能从摘要正文猜测。它按来源优先级、时间、public ref 确定性排序，最多 10 条进入 AI Context、3 条进入 Telegram。不会包含完整原话题正文、Message ID、Topic ID、主数据库路径或凭据。

AI Context Schema 为 2，Prompt Version 为 `oar-ai-prompt-v2`；旧 Prompt Cache 自动 miss。自动链上 Signal Record 记录最多 10 个 linked refs、来源模块、Watch priority 和原因数，并只写独立 `data/onchain/onchain_signals.db`。

## 状态与 Doctor

```text
python onchain_main.py status
python onchain_main.py doctor
```

两者均离线、只读。Status 展示 Registry、Watch、Due、Unresolved、Bridge 水位和最近扫描；Doctor 检查自动化数据库完整性、Schema、Primary 冲突、孤儿 Watch、陈旧 Lease、主信号只读能力和配置预算。诊断只显示凭据是否配置，不显示值。

## 数据库 Schema

`oar_automation.db` 使用版本化、事务化 Schema 2：

- `token_registry`：pending/verified/disabled/rejected Token 映射；
- `watch_items`：合并后的调度、TTL、Priority、Lease 和退避；
- `watch_sources`：来源信号幂等审计；
- `bridge_state`：时间水位与 ID tie-breaker；
- `unresolved_signals`：未解析、歧义、未验证和容量拒绝，保留
  open/resolved/expired 状态、解决时间和目标 Token；
- `watch_scan_runs`：有界扫描结果。

Schema 1 → 2 只迁移独立 Automation DB，并使用显式事务，失败整体回滚。旧 unresolved 记录默认变为 open。本阶段不修改主数据库 Schema，也不增加 `onchain_flow.db` Migration。

## 失败隔离与回滚

单个 Token 的 RPC、非 ERC-20、AI 或 Telegram 故障只影响该 Token，并进入有界退避；下一 Token 继续。自动化数据库完整性、Schema、路径隔离或持续 Lease 事务问题应停止自动化进程，但不影响 `paopao-radar` 和 `paopao-market-stream`。

回滚优先设置 `OAR_AUTOMATION_ENABLE=false` 并停止独立自动化启动方式；无需删除 Registry、Watch、审计、AI Cache 或 Telegram 历史。代码回滚使用标准 Git revert，不删除数据库或状态文件。

## OAR-P5 边界

本阶段不部署、不增加 systemd、不启用自动化。服务器灰度、独立进程启动方式、资源观察和回滚演练属于 OAR-P5。
