# Lifecycle Outcome Data Quality & Coverage

## 目标与边界

v1.78.2 为每个 Lifecycle Outcome 候选建立可持久化的资格、到期、处理、重试和终止状态。目标不是强行把所有历史缺口变成 Outcome，而是让每个缺口有明确原因，并持续补算 eligible、已到期且可以计算的项目。

本功能不修改 Outcome 计算语义、Decision Model 阈值、Lifecycle Intelligence 权重或 Telegram 主推送，不接交易所下单 API，也不执行自动交易。

## 候选资格

候选按 `lifecycle + signal/event + horizon` 生成稳定 `candidate_key`，重复刷新不会创建重复行。eligible 候选必须能确定 signal、具有合法单币 Binance USDT symbol 与 UTC 信号时间、horizon 合法，并且不是测试、dry-run、failed、blocked、skipped、纯汇总或公告。

不合资格原因包括：

```text
aggregate_summary_signal  announcement_signal  test_signal
dry_run_signal            failed_signal        blocked_signal
skipped_signal            missing_symbol       invalid_symbol
unsupported_quote_asset   non_binance_symbol   missing_signal_id
missing_signal_time       invalid_signal_time  unsupported_module
unsupported_signal_type   duplicate_candidate  lifecycle_event_without_signal
```

合资格但没有 Outcome 的原因进一步区分等待到期、尚未进入批次、正在处理、历史 K 线不可用、交易对下架、现货/合约交易对不可用、限流、超时、网络错误、响应无效、旧数据歧义、signal 不存在、重试耗尽和真实错误。最新质量报告不再输出笼统 `no_outcome_row`。

## 状态机与恢复

```text
eligible + 未到期 -> not_due
到期             -> ready -> queued -> processing
找到已有 Outcome -> linked
计算成功         -> success
暂时性错误       -> retry_wait
确定不可获得     -> terminal_unavailable
本身不合资格     -> terminal_ineligible
重试耗尽         -> terminal_error
```

`processing` 超过 `LIFECYCLE_OUTCOME_PROCESSING_STALE_SEC` 会恢复为 ready。可重试错误按有上限的指数退避设置 `next_retry_at`，达到最大次数后才进入 terminal_error。success、terminal_ineligible、terminal_unavailable、未到 retry 时间和未到期候选都会跳过。

## 五种指标

1. 生命周期关联覆盖率：至少关联一条 Outcome 的 lifecycle / lifecycle 总数。
2. 候选信号关联覆盖率：已关联 eligible 候选 / eligible 候选总数。
3. 到期候选解决率：`success + terminal_unavailable + terminal_error` / 已到期 eligible 候选。
4. 有效 Outcome 成熟率：`success` / 已到期 eligible 候选。
5. 生命周期成熟率：至少一个 success horizon 的 lifecycle / 至少一个已到期 eligible horizon 的 lifecycle。

`not_due` 不进入到期分母；`ineligible` 不进入 Outcome 分母；`unavailable` 可以表示已解决，但不等于成功、零收益或亏损。

## 增量任务

```bash
python main.py lifecycle-outcome-refresh-candidates --dry-run --pretty
python main.py lifecycle-outcome-classify-gaps --dry-run --pretty
python main.py lifecycle-outcome-incremental --limit 50 --dry-run --pretty
python main.py lifecycle-outcome-quality --pretty
python main.py lifecycle-calibration-readiness --pretty
```

`refresh-candidates` 与 `classify-gaps` 不请求行情。`incremental` 只处理 eligible、已到期、尚未解决且符合重试时间的候选，批量读取 Signal/Outcome、复用同 symbol 行情窗口和 decision 缓存，并在单事务写入 candidates、links 和 coverage。任务不进入 Telegram 主线程。

调度任务按候选刷新 15 分钟、增量补算 1 小时、质量报告 6 小时、reconcile 每天的口径运行；均使用任务锁防止重复提交。生产首次运行应先 dry-run，再执行 50 条，确认无异常后扩大到 200 条，不得无上限重算全部历史。

## 分析与报告

质量 API 和报告按 module、首信号级别、signal type、horizon 及 24h/7d/30d/全历史统计候选数、资格、关联、成功、不可用、错误、覆盖率、成熟率和主要原因。运行报告写入：

```text
docs/generated/lifecycle_outcome_quality_latest.json
docs/generated/lifecycle_outcome_quality_latest.md
```

报告经过字段投影与脱敏，不包含 token、Telegram 标识、数据库/服务器路径、异常堆栈、完整信号正文或内部任务 payload。

系统仅用于信号整理、研究和风险提示，不构成投资建议，不执行自动交易。
