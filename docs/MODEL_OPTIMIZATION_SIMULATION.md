# Model Optimization Simulation Engine

## 安全边界

v1.80.0 只在历史数据上模拟候选参数，不发布或应用候选模型。生产模型来源为 `signal-decision-v1.1`，并始终标记 `immutable=true`。

模拟不会：

- 修改 `decision_model.py`、Lifecycle/Risk 权重或生产配置；
- 请求 Binance 或其他外部历史行情；
- 重新生成、覆盖或删除 Signal、Outcome、Lifecycle、Replay、Calibration 数据；
- 改变 Telegram 推送或引入自动交易；
- 自动应用任何 recommendation。

## 内置场景

- Decision Threshold：模拟提高 probe/高质量候选阈值，比较样本保留率、成功率、收益和回撤。
- Risk Alert：模拟 OI 背离、Funding 和 CVD 背离权重组合，比较风险命中率、误报率、避免回撤和提前量。
- Lifecycle Intelligence：模拟提高 Spot CVD、降低 Futures CVD 权重，验证高质量启动准确率。
- Signal Module：对 structure、flow、launch、funding、structure_review 做候选权重重评分，比较模块贡献和历史置信度。

候选参数经过白名单、类型和范围校验；任意未知参数、越界值或非有限数都会被拒绝。候选模型只是描述和模拟结果，不是生产可加载模型。

## 指标与可信度

每个场景统一返回样本数、成熟样本、成功率、平均/中位收益、平均最大涨幅、平均回撤、回撤率、expectancy、risk-adjusted score 与 confidence，并提供 production/candidate/delta。

confidence 同时考虑样本量、效果差异、收益方差和回撤变化。成熟样本少于 50 时强制标为 `low_confidence`，不得生成强建议。Readiness 还要求：24h 成熟样本至少 100、72h 至少 50、候选改善至少 5%、回撤没有明显增加、样本不由单一币种主导且 confidence 至少 0.7。

这些门槛只判断“是否值得人工继续评估”，不表示候选模型已获批准。

## 回放口径

Decision Threshold 和 Signal Module 场景是对已经落库的 `decision_confidence` 做离线 gate / multiplier，不是重新执行原始信号决策树。Risk Alert 和 Lifecycle Intelligence 场景只使用 Outcome 所链接 `lifecycle_event_id` 的事件时点 OI、CVD、Funding 与评分；绝不回退到生命周期最新快照。生产基线与候选方案使用同一 exact-as-of cohort，并单独报告 cohort 覆盖率。事件时点特征不足时会降低置信度并阻断 readiness，不能把低样本误解为模型失效。

全局报告只聚合同一个 `source_signature` 下的完整四场景结果，避免混合不同数据快照。仅运行单一场景后，该场景报告可以较新，而全局报告仍保留最后一组完整同口径结果；`generated_at` 用于判断报告新鲜度。

## 数据与持久化

输入只来自现有 `signals.db`、`outcomes.db`、`lifecycle.db` 和 Calibration 聚合数据。输出写入独立表：

```text
optimization_scenarios
optimization_runs
optimization_metrics
```

聚合报告原子写入 Git 忽略的：

```text
docs/generated/model_optimization_latest.json
docs/generated/model_optimization_latest.md
```

Public API 只读取最新缓存。完整报告默认使用全部历史成熟样本；只有显式传入 `--limit` 才截断。

## CLI

```bash
python main.py optimization-scenarios --pretty
python main.py optimization-run --scenario threshold_tuning --dry-run --pretty
python main.py optimization-run --dry-run --pretty
python main.py optimization-run --pretty
python main.py optimization-report --pretty
python main.py optimization-readiness --pretty
```

`--symbol` 和显式 `--limit` 运行不覆盖全局报告。任何 recommendation 都包含 `auto_apply=false`，只能进入人工审核流程。

本系统仅用于历史模拟、模型研究和风险提示，不构成投资建议，不执行自动交易。
