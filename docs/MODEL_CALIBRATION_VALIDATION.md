# Model Calibration Validation Engine

## 范围

v1.79.0 用 `calibration-v1` 验证现有 `signal-decision-v1.1`、Lifecycle Intelligence 与风险规则的历史表现。输入只来自 `signals.db`、`outcomes.db`、`lifecycle.db`、`lifecycle_outcome_links`、`lifecycle_intelligence` 和 `lifecycle_replays`。

系统不会请求外部历史行情，不会重新计算或修改历史 Signal/Outcome，不会自动修改 Decision Model 阈值、Lifecycle 权重或风险规则，也不会接入自动交易或改变 Telegram 主推送。

## 统计口径

- Decision：分别统计 `observe`、`wait_pullback`、`probe`、`avoid_chase` 和 `risk_alert` 的总样本、成熟 success 样本、正收益率、平均/中位收益、平均最大涨幅、平均最大回撤、回撤率、期望分和置信度准确率。
- Signal Level：按首次信号 `15m / 1h / 4h / 24h / unknown` 统计升级率、最终成功率、收益、回撤、生命周期长度和失败率。
- Upgrade Path：按生命周期真实升级路径统计成功率、收益、时长、最大涨幅、回撤和风险警报率。
- Lifecycle Intelligence：按 `0-20 / 20-40 / 40-60 / 60-80 / 80-100` 分桶，验证高分是否对应更高成功率、更好收益或更低回撤。
- Factors：验证 OI 与价格四象限、Spot CVD、Futures CVD、Volume、Funding 以及资金同步组合。
- Risk Alert：统计风险事件之后 1h、4h、24h 的表现、最大回撤、避免损失比例和提前量，评价其是否提前识别风险。

只有 `data_status=success` 的 Outcome 参与成熟收益统计。`not_due` 和 `pending` 不进入成熟分母，`unavailable` 单独统计且不视为失败或亏损。小样本返回 `insufficient_samples`，不显示虚假的 0% 结论。

## 缓存与持久化

`calibration_reports` 保存报告摘要和人工建议，`calibration_metrics` 保存各维度指标。完整报告原子写入：

```text
docs/generated/model_calibration_latest.json
docs/generated/model_calibration_latest.md
```

公开 API 只读取最近一次持久化报告或短缓存，不会在 HTTP 请求中执行完整聚合。报告生成使用批量读取和单事务写入，不影响 Bot 主循环。

## CLI

```bash
python main.py calibration-report --dry-run --pretty
python main.py calibration-report --symbol BTCUSDT --pretty
python main.py calibration-decision --pretty
python main.py calibration-lifecycle --pretty
python main.py calibration-factors --pretty
python main.py calibration-readiness --pretty
```

`--dry-run` 只计算内存结果，不创建表、不写报告；`--symbol` 只验证指定单币；`--limit` 限制读取规模。报告中的 recommendation 仅是人工复核建议，不是自动调参指令。

## 安全边界

公开输出经过字段投影和递归脱敏，不包含 token、Cookie、Authorization、Telegram 标识、数据库/服务器路径、原始 payload 或异常堆栈。

本系统仅用于模型研究、信号整理和风险提示，不构成投资建议，不执行自动交易。
