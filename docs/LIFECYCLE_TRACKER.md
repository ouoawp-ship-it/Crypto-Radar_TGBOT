# Binance-Centric Signal Lifecycle Tracker

## v1.78.0 intelligence / replay 扩展

v1.78.0 保留 v1.77.0 的采集、评分和状态机，新增独立预计算层：`intelligence_score` 用于研究生命周期质量，replay frames 用于回放事件演化，analytics/similarity 用于历史统计和相似案例。新层只读取既有 lifecycle/events/snapshots 与 outcomes，不覆盖 `lifecycle_score`、`risk_score`，不自动改变 decision model 或生命周期阈值。

数据库迁移仅新增表、索引和兼容版本元信息；既有三张生命周期表及历史记录不删除、不重建。完整说明见 `LIFECYCLE_INTELLIGENCE.md` 与 `LIFECYCLE_REPLAY.md`。

系统仅用于信号整理、研究和风险提示，不构成投资建议，不执行自动交易。

## 范围与边界

v1.77.0 为首次有效单币信号建立生命周期档案，并持续合并同币后续信号。系统记录首信号级别（15m、1h、4h、24h 或 unknown），识别同级确认、周期升级、短线冷却、风险升高、启动失败与生命周期关闭。

生命周期数据保存在独立的 `data/lifecycle.db`，包含 `signal_lifecycles`、`lifecycle_events` 和 `lifecycle_metric_snapshots`。初始化可重复执行；symbol、事件 dedup key 和快照唯一键防止重复写入；批量更新在单个事务内完成，并对 SQLite lock 使用 retry/backoff。既有 `signals.db`、`outcomes.db` 和 `jobs.db` schema 不变。

仅用于信号整理和风险提示，不构成投资建议，不执行自动交易。

## 数据源口径

核心指标只来自 Binance：

- Futures 当前价格、K 线/成交量、当前与历史 OI、taker buy/sell 近似 futures CVD、funding rate。
- Spot 当前价格、K 线/成交量、aggTrades 近似 spot CVD。
- 24h OI 在直接周期不可用时由 4h 或 1h 历史窗口聚合，局部数据不可用会标记 `unavailable`，不会使整轮失败。

OKX、Bybit 和 Hyperliquid 只采集当前价格与资金费率，并计算相对 Binance 的价格/资金费率偏差。旁路数据只写入 `exchange_context_json` 并用于页面和辅助提醒，不进入生命周期评分、风险评分或状态流转。系统不会全市场拉取逐笔成交；spot aggTrades 仅用于活跃生命周期和单币详情，并明确标记采样状态。

所有 Binance 请求复用 HTTP session、超时和 TTL cache。同轮 `(symbol, timeframe)` 去重；批量预取最多使用 8 个 worker。HTTP 418、429、timeout 或单组件失败会降级为 `unavailable`，不阻断主服务。

## 状态与评分

状态包括 `warming`、`launching`、`upgraded_1h`、`upgraded_4h`、`trend_confirmed`、`cooling`、`risk_warning`、`failed` 和 `closed`。15m → 1h → 4h → 24h 的更高级信号生成稳定的升级事件；同级信号生成确认事件。成交量、OI、futures/spot CVD 与 funding 只补充生命周期强度或风险，不改变既有 decision model。

风险信号包括 OI 增长而价格下跌、合约 CVD 增强但现货不跟、资金费率过热、快速拉升、短时间信号密度过高以及明确走弱关键词。大周期走弱与启动失败会和短线冷却区分处理。

## CLI

```bash
python main.py lifecycle-backfill --lookback-hours 168 [--dry-run]
python main.py lifecycle-scan --lookback-hours 24 --limit-symbols 80 [--symbol BTCUSDT] [--push] [--dry-run]
python main.py lifecycle-status --symbol BTCUSDT [--dry-run]
```

`--dry-run` 不写数据库，也不会发送或标记 Telegram 消息。主进程仅在 `LIFECYCLE_TRACKER_ENABLE=true` 时按 `LIFECYCLE_SCAN_INTERVAL_SEC` 调度；异常与主 Bot 隔离。

## Telegram 辅助提醒

`LIFECYCLE_TELEGRAM_ENABLE=false` 为默认值。开启后只处理重要生命周期事件，并执行三层限制：同 symbol/event/level 至少四小时一次、同 symbol 每小时最多两条、全局每小时最多三十条。只有真实发送成功才写入 pushed 标记。该能力不修改既有 Telegram 主推送流程或 topic 语义。

## API 与前端

公开只读端点位于 `/public-api/lifecycle/*`，使用字段投影和递归脱敏；不返回 Telegram 标识、dedup key、原始 payload/text、配置、鉴权信息或本机路径。私有 `/api/lifecycle/*` 沿用既有登录与 CSRF 逻辑。

Next.js `/lifecycle` 展示概览、筛选与生命周期列表；首页显示跟随摘要；`/coin/[symbol]` 显示首次信号、最高周期、Binance 指标、事件时间线和旁路交易所观察。
