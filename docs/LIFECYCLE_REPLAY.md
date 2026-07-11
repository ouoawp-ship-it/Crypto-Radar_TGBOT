# Lifecycle Replay

## v1.78.1 Outcome coverage 口径

Replay summary 从 `lifecycle_outcome_links` / `lifecycle_outcome_coverage` 读取稳定的 primary Outcome、已成熟/待到期/不可用 horizon、关联覆盖率与成熟度，不再在每个 API 请求中按 symbol 动态寻找 Outcome。回放帧不会复制完整 Outcome 大字段。

`final_return_pct`、`max_price_gain_pct`、`max_drawdown_pct` 与 `result_label` 只使用成熟的 `success` Outcome；实时快照价格表现单独记录为 `observed_*` 字段。pending、not_due 或 unavailable 时不会用观测价格伪造最终 Outcome 结果。

Primary 优先使用 `first_signal_id`；首信号无可用 Outcome 时才使用最早的有效 lifecycle event signal id。一个生命周期仍可保留多个事件 Outcome link，但每个生命周期最多一个 primary。关联覆盖率与数据成熟度分别展示：尚未到期不是失败，pending 不是失败，unavailable 不等于亏损。

## 预计算回放

`lifecycle-replay-v1` 将生命周期事件按 UTC 时间和事件 id 稳定排序，并生成从 1 开始连续编号的 replay frames。每个 frame 只保存回放所需的投影字段和裁剪后的摘要；API 请求读取预计算表，不现场重算完整生命周期。

Replay summary 记录 duration、首信号/最高周期、升级路径、事件/确认/风险/冷却数量、到达 1h/4h/24h 的时间、最大涨幅、最大回撤、最终收益、最终状态、Outcome 状态和结果标签。

## Outcome 关联

兼容关联严格按以下优先级执行：

1. `first_signal_id`
2. lifecycle event 的 `signal_id`
3. `latest_signal_id`
4. 规范化 symbol + 信号时间容差 + module/template

最后一级只用于缺少 signal id 的旧数据，必须候选唯一；多候选标记 `ambiguous_match`，禁止退化为 symbol-only。Outcome 计算语义和 `outcomes.db` schema 保持不变。

## 结果标签

结果标签包括 strong_success、success、partial_success、neutral、failed、risk_avoided 和 insufficient_data。评价综合最终涨跌、最大涨幅/回撤、最高周期、持续时间及风险/冷却/假突破事件；这些标签用于复盘与模型诊断，不产生交易指令。

## CLI

```bash
python main.py lifecycle-replay --symbol BTCUSDT [--dry-run] [--pretty]
python main.py lifecycle-replay --lifecycle-id 12 [--force-rebuild]
python main.py lifecycle-replay-backfill --limit 500 [--dry-run]
```

批量回填使用单事务，单币失败隔离；源事件未变化时跳过。系统仅用于信号整理、研究和风险提示，不构成投资建议，不执行自动交易。
