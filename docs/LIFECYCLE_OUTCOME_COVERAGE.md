# Lifecycle Outcome Coverage & Backfill

> v1.78.2 将本页 v1.78.1 的关联层继续扩展为候选资格与数据质量层。新口径和增量状态机见 [LIFECYCLE_OUTCOME_DATA_QUALITY.md](LIFECYCLE_OUTCOME_DATA_QUALITY.md)，模型校准准入见 [LIFECYCLE_CALIBRATION_READINESS.md](LIFECYCLE_CALIBRATION_READINESS.md)。

## 目标与边界

v1.78.1 为生命周期、生命周期事件和 `signal_outcomes` 建立可重复、可审计的确定性关联，补算已到期但缺失的历史 Outcome，并单独统计关联覆盖率与数据成熟度。本功能不修改 Outcome 计算语义、decision model 阈值、生命周期 intelligence 权重或 Telegram 主推送流程，不接交易所下单 API，也不执行自动交易。

系统仅用于信号整理、研究和风险提示，不构成投资建议，不执行自动交易。

## 确定性关联

关联顺序为：

1. `signal_lifecycles.first_signal_id -> signal_outcomes.signal_id`
2. `lifecycle_events.signal_id -> signal_outcomes.signal_id`
3. `signal_lifecycles.latest_signal_id -> signal_outcomes.signal_id`
4. 仅对缺少 signal id 的旧数据执行规范化 symbol、信号时间、module/template 兼容匹配

兼容匹配默认时间容差为 300 秒，并要求 module 或 template 至少一个一致、时间距离最小且候选唯一。多候选记录 `ambiguous_match`；禁止 symbol-only 或“同币最近 Outcome”关联。

一个生命周期可关联首次信号、同级确认、周期升级、风险和走弱事件的多个 Outcome。Primary 稳定持久化：优先首信号；若首信号没有可用 Outcome，再选择最早有效 lifecycle event signal。每个生命周期最多一个 primary，`UNIQUE(lifecycle_id, outcome_id)` 防止重复 link。

## 状态与成熟度

Outcome coverage 使用以下状态：

- `not_due`：当前 UTC 时间尚未达到对应 horizon。
- `pending`：已存在待扫描记录。
- `ready`：满足条件、等待 Outcome Tracker 计算。
- `success`：结果成功计算，可进入成熟收益统计。
- `unavailable`：交易对或历史 K 线不可获得，不是亏损。
- `error`：真实请求、解析或数据库错误。
- `missing`：该 signal/horizon 尚无 Outcome 记录。

候选信号关联覆盖率：已关联 Outcome 的 eligible 候选信号数 / eligible 候选信号数。旧字段 `link_coverage_ratio` 为兼容保留，前台不再用一个模糊“覆盖率”同时代表生命周期和候选信号口径。

数据成熟度：`success` horizon 数 / 已到期 horizon 数。尚未到期的 24h/72h 不进入成熟度分母；pending/not_due 不算失败，unavailable 不算亏损或零收益。

成熟度标签依次为无数据、等待到期、初步成熟（1h success）、部分成熟（4h success）、基本成熟（24h success）、完整成熟（72h success）、数据不可用、计算异常。

## 回填流程

回填按批次执行：批量读取 lifecycle 候选 signal id，批量读取 signals/outcomes，先关联既有 Outcome，再找出已到期缺失项，复用现有 Outcome Tracker 的行情窗口与 decision 缓存补算，最后用单事务写入 links/coverage，并刷新受影响的 replay、intelligence 与 analytics。

默认跳过 `success`、`unavailable`、尚未到期和稳定链接。`--force-relink` 只重建关联；只有显式 `--force-outcome-rebuild` 才允许重算指定 Outcome。默认每批最多 200 个 lifecycle、1000 个 signal/horizon，单币失败不阻断整批。

```bash
python main.py lifecycle-outcome-link [--symbol BTCUSDT] [--lifecycle-id 12] [--dry-run] [--pretty]
python main.py lifecycle-outcome-backfill --limit 50 [--horizon 1h] [--dry-run] [--pretty]
python main.py lifecycle-outcome-status [--symbol BTCUSDT] [--pretty]
python main.py lifecycle-outcome-reconcile [--repair] [--dry-run] [--pretty]
```

建议生产顺序：先 dry-run 和实际 `link`，检查状态；再 dry-run/执行 50 条；reconcile 无异常后扩大至 200 条。不要对全部历史无上限重算。

Web scheduler 在服务启动后会等待一个完整的配置周期才首次提交增量 backfill，避免部署重启时抢跑人工分批验收。后台修改 enable/interval 配置后应重启 `paopao-web` 使调度线程加载新设置。

## 未关联原因

诊断明确区分 `no_signal_id`、`signal_not_in_store`、`no_outcome_row`、`not_due`、`pending_scan`、`outcome_unavailable`、`ambiguous_match`、`symbol_mismatch`、`time_mismatch`、`module_mismatch`、`invalid_signal_time`、`invalid_symbol` 和 `real_error`。报告不会把所有原因折叠为 `not_found`。

运行报告写入：

```text
docs/generated/lifecycle_outcome_coverage_latest.json
docs/generated/lifecycle_outcome_coverage_latest.md
```

报告与 lock 文件均被 Git 忽略，内容经过脱敏，不包含 token、Telegram 标识、内部路径、异常堆栈或数据库路径。

## API 与页面

公开只读端点位于 `/public-api/lifecycle/outcomes/*`，使用预计算结果、字段投影与短缓存；API 请求不会现场补算。私有执行端点沿用后台登录与 CSRF，并通过 jobs system 防重复提交。

`/lifecycle` 分开解释 Outcome 关联覆盖率与数据成熟度；`/lifecycle/replay` 展示 primary Outcome、已成熟/待到期 horizon、关联方式与结果可信度；`/coin/[symbol]` 展示 1h/4h/24h/72h 状态、最终涨跌、最高涨幅、最大回撤和关联来源。公开页面不展示内部 outcome id。

## 数据质量判断

任何模型诊断、收益统计和后续阈值校准必须使用同时满足以下条件的样本：

```text
已关联 + 已到期 + success
```

不能用生命周期总数代替成熟样本数，也不能把 missing、pending、not_due 或 unavailable 当作负收益。

v1.78.2 同时要求：纯汇总、公告、测试、dry-run、失败/阻断/跳过以及不合法交易对等 `ineligible` 候选不得进入 Outcome 分母；尚未到期不是错误；`unavailable` 可以计入到期解决率，但不进入有效 Outcome 成熟率。笼统 `no_outcome_row` 在最新质量报告中必须归零，所有缺口都映射到具体、可操作的原因。
