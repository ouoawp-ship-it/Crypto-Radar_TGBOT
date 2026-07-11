# Lifecycle Replay

## 预计算回放

`lifecycle-replay-v1` 将生命周期事件按 UTC 时间和事件 id 稳定排序，并生成从 1 开始连续编号的 replay frames。每个 frame 只保存回放所需的投影字段和裁剪后的摘要；API 请求读取预计算表，不现场重算完整生命周期。

Replay summary 记录 duration、首信号/最高周期、升级路径、事件/确认/风险/冷却数量、到达 1h/4h/24h 的时间、最大涨幅、最大回撤、最终收益、最终状态、Outcome 状态和结果标签。

## Outcome 关联

关联严格按以下优先级执行：

1. `first_signal_id`
2. `latest_signal_id`
3. lifecycle event 的 `signal_id`
4. symbol 与生命周期起止时间窗口

最后一级必须同时满足 symbol 和时间窗口，禁止退化为 symbol-only。Outcome 数据只读，不修改 `outcomes.db` schema。

## 结果标签

结果标签包括 strong_success、success、partial_success、neutral、failed、risk_avoided 和 insufficient_data。评价综合最终涨跌、最大涨幅/回撤、最高周期、持续时间及风险/冷却/假突破事件；这些标签用于复盘与模型诊断，不产生交易指令。

## CLI

```bash
python main.py lifecycle-replay --symbol BTCUSDT [--dry-run] [--pretty]
python main.py lifecycle-replay --lifecycle-id 12 [--force-rebuild]
python main.py lifecycle-replay-backfill --limit 500 [--dry-run]
```

批量回填使用单事务，单币失败隔离；源事件未变化时跳过。系统仅用于信号整理、研究和风险提示，不构成投资建议，不执行自动交易。
