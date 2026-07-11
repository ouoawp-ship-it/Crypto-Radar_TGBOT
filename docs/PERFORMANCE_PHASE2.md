# Performance Optimization Phase 2

本报告记录 v1.76.3 的离线、可复现合成基准，并作为 v1.76.4 Runtime Cache & File Lock Hardening 的性能回归基线。所有数据均写入临时 SQLite，Funding 和行情请求使用受控延迟的本地 fake，不访问交易所；结果用于比较相同机器上的相对变化，不代表生产环境 SLO。

## Funding scan

命令：

```bash
python scripts/benchmark_funding_scan.py --symbols 120 --latency-ms 2 --concurrency 8
```

工作量为 120 symbols × 5 exchanges，共 600 次请求。

| 指标 | 串行基线 | Phase 2 |
|---|---:|---:|
| 总耗时 | 1.7793 s | 0.2729 s |
| 加速比 | 1.00× | 6.52× |
| 成功率 | 100% | 100% |
| 失败率 | 0% | 0% |
| 平均响应 | 2.966 ms | 3.594 ms |
| 峰值并发 | 1 | 8 |

并发实现只创建固定数量 worker，任务源为惰性迭代器。每个 exchange-symbol job 的 current/history 请求共享 `FUNDING_REQUEST_TIMEOUT_SEC` deadline；单交易所异常只记为该 job 失败。

## Outcome scan

命令：

```bash
python scripts/benchmark_outcome_scan.py --symbols 100 --request-delay-ms 2
```

工作量为 100 symbols × 4 horizons，共 400 个 outcome。

| 指标 | 逐 horizon 基线 | Phase 2 | 变化 |
|---|---:|---:|---:|
| 总耗时 | 1.1685 s | 0.7089 s | -39.33% |
| 行情请求 | 400 | 200 | -50% |
| decision 计算 | 400 | 100 | -75% |
| SQLite 写事务 | 400 | 1 | -99.75% |

`1h/4h` 复用合并后的 1m K 线窗口，`24h/72h` 复用 5m 窗口；结果按 `open_time` 切回原 horizon，且合并跨度不超过交易所 1000 根 K 线限制。

## Lifecycle scan

命令：

```bash
python scripts/benchmark_lifecycle_phase2.py --symbols 100 --repeats 2 --provider-delay-ms 0.5
```

工作量为 100 symbols × 4 timeframes × 每格 2 条信号，共 800 条信号。逐条基线保留真实 SQLite connect/commit 成本，因此在 Windows 上运行约 2.5 分钟。

| 指标 | 逐条基线 | Phase 2 | 变化 |
|---|---:|---:|---:|
| 总耗时 | 150080.31 ms | 1511.17 ms | 99.31× |
| provider calls | 800 | 400 | -50% |
| SQLite connections | 1599 | 2 | -99.87% |

Phase 2 在扫描前批量跳过已有 lifecycle event 的 signal，按 `(symbol, timeframe)` 缓存指标，并在行情预取后以一个 `BEGIN IMMEDIATE` 事务写入 lifecycle、event 和 snapshot。

## Web API

命令：

```bash
python scripts/benchmark_api_phase2.py
```

默认数据集为 10 symbols × 每币 8 条 signal、每个大字段 4 KB；每个变体预热 1 次并采样 12 次。延迟包含 service 执行与 UTF-8 JSON 序列化，P95 使用 nearest-rank 计算。

| 接口 | P95 基线 | P95 Phase 2 | P95 变化 | 连接/请求 | JSON bytes |
|---|---:|---:|---:|---:|---:|
| `/signals` | 117.619 ms | 19.185 ms | -83.69% | 1 → 1 | 1,344,758 → 165,402 (-87.70%) |
| `/decision` | 814.771 ms | 182.791 ms | -77.57% | 11 → 1 | 74,949 → 74,949 |
| `/outcomes` | 31.795 ms | 30.494 ms | -4.09% | 2 → 1 | 59,269 → 59,269 |
| `/lifecycle` detail | 35.139 ms | 27.348 ms | -22.17% | 3 → 1 | 128,836 → 128,834 |

Signals 列表保留既有字段键，但延迟加载 `payload_json`、`text_html` 和长 excerpt；signal detail 仍读取完整内容。Lifecycle 列表/summary top 使用相同策略，detail 保持完整。Outcome stats 使用一条 CTE 聚合，Decision 对同批 symbols 共用一个请求连接和批量读取路径。

## v1.76.4 回归验收

v1.76.4 不重写 Phase 2 扫描和数据库查询路径。发布前使用较小工作量执行同一组离线 benchmark，确认结构性指标没有回退：

```bash
python scripts/benchmark_funding_scan.py --symbols 30 --latency-ms 2 --concurrency 8
python scripts/benchmark_outcome_scan.py --symbols 30 --request-delay-ms 2
python scripts/benchmark_lifecycle_phase2.py --symbols 30 --repeats 2 --provider-delay-ms 0.1
python scripts/benchmark_api_phase2.py --symbols 6 --rows-per-symbol 4 --blob-bytes 1000 --samples 5
```

验收关注以下不随机器绝对耗时变化的结构性指标：

- Funding 峰值并发接近 8，固定 worker 与单请求 timeout 继续生效。
- Outcome 同一 symbol 的 decision 只计算一次，行情窗口复用，SQLite 写入仍为单次批量事务。
- Lifecycle provider 调用保持合并，SQLite 连接数保持批量路径的低连接数量。
- `/signals` 列表 payload 不重新带回 `payload_json`、`text_html` 等大字段。
- `/decision` 每请求数据库连接数保持为 1，Outcomes/Lifecycle 的聚合和请求级连接复用继续生效。

Runtime hardening 的短缓存不进入上述业务结果：Dashboard 仅缓存服务状态、Git/版本和只读摘要；Next.js 仅缓存公开只读 `/public-api/*`。缓存 loader 失败不写入缓存，TTL 到期自动刷新，变更操作主动失效。JSON lock 与原子替换只改变持久化方式，不改变业务数据 contract。完整说明见 `RUNTIME_HARDENING.md`。

## v1.77.0 结构性回归

v1.77.0 在相同小规模命令下完成回归。Funding 150 个合成 exchange-symbol 请求的峰值并发为 8，耗时从 0.4452 秒降至 0.0593 秒，成功率 100%。Outcome 的行情请求保持 120 → 60、decision 计算 120 → 30、事务 120 → 1。

Lifecycle 的 240 条合成信号保持 provider 调用 240 → 120、SQLite 连接 479 → 2，批量路径耗时 1036.56 ms；新增生命周期数据预取同样使用最多 8 个 worker，并按 `(symbol, timeframe)` 去重。API 回归继续保持 `/signals` payload 减少 62.75%、`/decision` 连接 7 → 1、`/outcomes` 连接 2 → 1、`/lifecycle` detail 连接 3 → 1。绝对耗时受机器负载影响，验收以并发上限、请求/计算次数、事务数、连接数和字段投影等结构性指标为准。

## 验收边界

- 未修改 Telegram 推送内容或发送规则。
- 未修改信号算法和 decision model 规则。
- 未新增或删除业务表/业务字段，未改变数据库核心 schema。
- API 顶层结构及既有字段键保持不变；大字段正文改由详情接口加载。
- 不包含自动交易或交易所下单能力。

## v1.78.0 Lifecycle Intelligence / Replay 基准

生命周期智能评价与回放均为预计算读取路径。离线基准会在临时目录生成确定性
SQLite 数据，不访问交易所，也不会读取或写入生产数据库：

```bash
python scripts/benchmark_lifecycle_intelligence.py --lifecycles 120 --frames 100 --samples 25
```

基准覆盖 intelligence 列表、replay 摘要、100 帧分页、analytics 缓存命中和
similarity top 10；耗时包含 UTF-8 JSON 序列化。目标分别为 P95 `<100ms`、
`<100ms`、`<150ms`、`<50ms` 和 `<200ms`。列表不读取完整 `metrics_json`，
Replay API 不现场重算，Analytics 从 `lifecycle_analytics_cache` 读取。

本机 120 lifecycle / 100 frames / 25 samples 验收结果：

| 读取路径 | P95 | JSON bytes | 目标 |
|---|---:|---:|---:|
| intelligence list（50 条） | 25.390 ms | 40,932 | < 100 ms |
| replay summary | 3.537 ms | 799 | < 100 ms |
| replay frames（100 条） | 28.945 ms | 55,366 | < 150 ms |
| analytics cached | 2.745 ms | 395 | < 50 ms |
| similarity top 10 | 15.665 ms | 3,046 | < 200 ms |

结果为当前本地环境的可复现相对基线，不作为生产绝对 SLO。新 API 只保留
标准 `ok/data` envelope，避免列表和回放帧在响应根重复一份。

同轮 v1.76 结构回归保持：Funding 峰值并发 8、150/150 成功；Outcome 行情
请求 120 → 60、decision 计算 120 → 30、事务 120 → 1；Lifecycle provider
调用 240 → 120、SQLite 连接 479 → 2；`/signals` payload 仍减少 62.75%，
`/decision`、`/outcomes`、`/lifecycle` 请求连接数分别保持 1、1、1。
