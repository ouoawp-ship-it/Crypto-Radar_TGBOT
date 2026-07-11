# Model Registry & Approval System

## 目标与边界

v1.81.0 把 Production Model、Calibration、Optimization Candidate、人工 Approval、Performance 和 Health 串成可审计的长期模型生命周期。Registry 使用独立 `data/model_registry.db`；Signal、Outcome、Lifecycle 与 Jobs 数据库保持原 schema。

Registry 只管理模型快照、状态和人工治理记录。它不会修改 `decision_model.py`、Decision 阈值、Lifecycle/Risk 权重、Telegram 规则或生产配置，不执行自动交易，也不会自动应用 Candidate。

## 状态与人工门禁

模型状态为 `draft`、`simulation`、`approved`、`production`、`deprecated` 或 `rejected`。允许的治理流程是：

```text
draft -> simulation -> approved
```

`simulation` 不能直接跳到 `production`。审批默认只到 `approved`；后续必须先由独立人工发布流程部署模型，再通过第二次显式确认和运行时参数 hash 校验，Registry 才能登记 Production 并把旧版本标为 Deprecated。Registry 状态不得领先于真实运行时。

Rollback 也采用相同门禁：先记录 rollback 审批、目标版本和原因；运行时尚未人工回滚到目标 hash 时只返回 `manual_deployment_required`。运行时验证成功后才更新 Registry 的 Production 指针，并保留 previous/current/time/reason 审计记录。历史模型和审批记录永不删除。

## 数据与 Diff

Registry 保存 canonical parameters snapshot、SHA-256 model hash、来源版本、来源 commit、创建/发布时间。Optimization Candidate 从 v1.80 持久化报告导入，保存 Production/Candidate 参数变化、影响范围、历史模拟结果和 readiness；所有 Candidate 初始为 `draft` 或 `simulation`。

v1.80 的四类结果是局部、离线模拟方案，不是完整的可部署运行时快照，因此 Registry 会明确标记为 `deployable=false`。它们可以接受人工研究审批，但不能仅凭局部 diff 激活为 Production。未来候选必须先由独立人工发布流程形成完整、不可变参数快照，并让实际运行时的版本与 hash 同时匹配，才能通过 Production 激活门禁；Registry 本身不会生成或部署该运行时。

Diff 只比较 canonical snapshot，输出 parameter、old、new、影响范围与模拟证据。公开 API 不返回完整参数，完整 Diff 只在登录后的私有接口和后台页面显示。

## Performance 与 Health

Performance 使用已落库、`data_status=success` 的 Outcome 聚合 7d、30d、90d 和 all；pending/unavailable/error 不进入收益成功率分母，不请求 Binance，也不重新计算历史 Outcome。未来多版本严格按 Registry 的生产时间窗口归因；当前模型的历史 bootstrap 若缺少逐条 model version，会在 `metrics_json` 明确标注归因假设。

Health 只产生 `healthy`、`warning`、`degraded` 或 `deprecated` 标签和人工告警。最近表现相对基线下降超过 15% 标为 warning，超过 30% 标为 degraded。Health 不会自动回滚、批准、替换或修改模型。

## CLI

```bash
python main.py model-list --pretty
python main.py model-show --model signal-decision --version signal-decision-v1.1 --pretty
python main.py model-diff --model signal-decision --version <candidate> --pretty
python main.py model-register --bootstrap-production --dry-run --pretty
python main.py model-register --bootstrap-production --pretty
python main.py model-register --model signal-decision --scenario threshold_tuning --pretty
python main.py model-register --model lifecycle-risk --scenario risk_control --pretty
python main.py model-register --model lifecycle-intelligence --scenario lifecycle_quality --pretty
python main.py model-register --model simulation-policy --scenario module_rebalance --pretty
python main.py model-approve --model signal-decision --version <candidate> --dry-run --pretty
python main.py model-reject --model signal-decision --version <candidate> --dry-run --pretty
python main.py model-rollback --model signal-decision --version signal-decision-v1.1 --dry-run --pretty
python main.py model-health --pretty
```

审批、拒绝和回滚的真实写入还需要命令实际提供的人工身份、原因与确认参数；以 `--help` 中的最终参数为准。所有 `--dry-run` 都不得写 Registry。

系统仅用于模型治理、历史研究和风险提示，不构成投资建议，不执行自动交易。
