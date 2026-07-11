# API

## Model Registry API

v1.81.0 新增公开只读模型治理接口：

```text
GET /public-api/models/current
GET /public-api/models/history
GET /public-api/models/performance
GET /public-api/models/health
```

公开接口只返回模型名称、版本、状态、健康度和聚合性能摘要，不返回完整 `parameters_json`、审批人内部标识、数据库路径、任务 payload 或异常堆栈。查询只读取 Registry 缓存，不重算 Outcome、不请求外部行情。

后台接口受登录鉴权保护：

```text
GET  /api/models/list
GET  /api/models/detail
GET  /api/models/diff
POST /api/models/register
POST /api/models/approve
POST /api/models/reject
POST /api/models/rollback
```

写接口通过 Jobs 系统执行、防重复并记录审计。审批默认止于 `approved`；登记 Production 或完成 Rollback 必须显式二次确认且通过运行时 hash 校验。API 本身不会改写生产模型或配置。

## Model Optimization Simulation API

v1.80.0 新增只读的候选模型模拟接口：

```text
GET /public-api/optimization/summary
GET /public-api/optimization/scenarios
GET /public-api/optimization/report
GET /public-api/optimization/readiness
```

公开接口只返回最近一次持久化的 `optimization-v1` 报告，展示 immutable production model、候选场景、production/candidate/delta、置信度和人工建议。公开请求不会执行模拟、请求外部行情、重算 Outcome 或修改模型。

后台接口受登录鉴权保护：

```text
POST /api/optimization/run
GET  /api/optimization/report
POST /api/optimization/rebuild
```

执行接口通过 Jobs 系统防重复提交并返回 `job_id`。`rebuild` 只强制重建模拟结果和聚合报告，不会写入 `decision_model.py`、Lifecycle/Risk 权重或生产配置。所有建议固定 `auto_apply=false`。

## Model Calibration Validation API

v1.79.0 新增预计算、只读的模型验证接口：

```text
GET /public-api/calibration/summary
GET /public-api/calibration/decision
GET /public-api/calibration/lifecycle
GET /public-api/calibration/factors
GET /public-api/calibration/risk
GET /public-api/calibration/readiness
```

`summary` 返回 `calibration-v1` 报告版本、被验证的 `signal-decision-v1.1` 模型版本、总样本、成熟样本、数据质量和人工建议。其他接口只投影对应统计，不返回原始信号正文、内部路径、数据库路径、异常堆栈或任务 payload。API 不请求外部行情，不现场重算历史 Outcome，也不修改模型。

后台接口继续受登录鉴权保护：

```text
POST /api/calibration/run
GET  /api/calibration/report
POST /api/calibration/rebuild
```

执行接口通过 Jobs 系统防重复提交并返回 `job_id`；`rebuild` 仅强制重建验证报告，不重算或改写历史 Outcome。所有建议仅供人工复核，不构成投资建议，不执行自动交易。

## Lifecycle Outcome data quality API

v1.78.2 新增预计算、只读、短缓存的数据质量接口：

```text
GET /public-api/lifecycle/outcomes/quality/summary
GET /public-api/lifecycle/outcomes/quality/reasons
GET /public-api/lifecycle/outcomes/quality/modules
GET /public-api/lifecycle/outcomes/quality/levels
GET /public-api/lifecycle/outcomes/quality/horizons
GET /public-api/lifecycle/outcomes/quality/timeline
GET /public-api/lifecycle/calibration-readiness
```

quality 接口接受可选 `symbol`、`lifecycle_id`、`horizon`、`module` 和 `time_range=24h|7d|30d|all`。`summary` 分开返回：

- `lifecycle_link_coverage_ratio`：已关联生命周期 / 生命周期总数。
- `candidate_link_coverage_ratio`：已关联 eligible 候选 / eligible 候选。
- `due_resolution_ratio`：success + 终止 unavailable/error / 已到期 eligible 候选。
- `usable_outcome_maturity_ratio`：success / 已到期 eligible 候选。
- `lifecycle_maturity_ratio`：至少一个 success 的生命周期 / 至少一个已到期 eligible horizon 的生命周期。

现有 `GET /public-api/lifecycle/outcomes/summary` 保留全部旧字段，并兼容增加上述字段。`not_due` 不进入到期分母，`ineligible` 不进入 Outcome 分母，`unavailable` 不计作成功或亏损。API 只读取候选表和聚合结果，不触发外部行情请求或 Outcome 回填。

后台只读接口继续受登录鉴权：

```text
GET  /api/lifecycle/outcomes/quality/summary
GET  /api/lifecycle/outcomes/quality/reasons
GET  /api/lifecycle/calibration-readiness
POST /api/lifecycle/outcomes/run-refresh-candidates
POST /api/lifecycle/outcomes/run-classify-gaps
POST /api/lifecycle/outcomes/run-incremental
POST /api/lifecycle/outcomes/run-quality-report
```

POST 执行端点通过 Jobs 系统防重复提交并立即返回 `job_id`，不会在 HTTP 线程运行长任务。校准准入响应包含 `ready`、`label`、`passed`、`blocked`、`warnings`、`current` 和 `required`；它只判断数据质量是否达标，不自动修改模型。

公开响应不包含 candidate/outcome 内部 ID、完整 signal 正文、数据库/服务器路径、异常堆栈、任务 payload、Telegram 标识、token、Cookie 或 Authorization。

## Lifecycle Outcome coverage API

公开接口只读取已经计算并写入 `lifecycle.db` 的关联与覆盖率结果，不会在 API 请求中触发 Outcome 补算：

```text
GET /public-api/lifecycle/outcomes/summary
GET /public-api/lifecycle/outcomes/coverage?limit=50
GET /public-api/lifecycle/outcomes/list?limit=50
GET /public-api/lifecycle/outcomes/detail?symbol=BTCUSDT
GET /public-api/lifecycle/outcomes/reasons
GET /public-api/lifecycle/outcomes/maturity
```

`summary` 分开返回 `link_coverage_ratio` 与 `maturity_ratio`，并按 1h / 4h / 24h / 72h 统计 `success`、`pending`、`not_due`、`unavailable`、`error`。列表使用字段投影，不返回完整 signal excerpt、metrics JSON 或 replay frames；详情不公开内部 `outcome_id`。

后台只读接口沿用既有登录鉴权；执行接口通过 jobs system 防重复提交并返回 `job_id`：

```text
GET  /api/lifecycle/outcomes/summary
GET  /api/lifecycle/outcomes/coverage
GET  /api/lifecycle/outcomes/detail?symbol=BTCUSDT
GET  /api/lifecycle/outcomes/reasons
POST /api/lifecycle/outcomes/run-link
POST /api/lifecycle/outcomes/run-backfill
POST /api/lifecycle/outcomes/run-reconcile
```

公开响应不会包含 `outcome_id`、`chat_id`、`topic_id`、`message_id`、`dedup_key`、原始 payload/text、token、Cookie、Authorization、数据库/服务器路径、任务 payload 或异常堆栈。尚未到期不是失败，pending 不进入成熟样本，unavailable 不等于亏损；只有 success Outcome 参与成熟收益统计。

## Lifecycle intelligence / replay public API

以下接口公开、只读，只读取预计算或缓存结果：

```text
GET /public-api/lifecycle/intelligence/summary
GET /public-api/lifecycle/intelligence/list?limit=50
GET /public-api/lifecycle/intelligence/detail?symbol=BTCUSDT
GET /public-api/lifecycle/replay?symbol=BTCUSDT
GET /public-api/lifecycle/replay/frames?symbol=BTCUSDT&limit=100
GET /public-api/lifecycle/analytics/first-level
GET /public-api/lifecycle/analytics/upgrade-path
GET /public-api/lifecycle/analytics/module
GET /public-api/lifecycle/analytics/capital-confirmation
GET /public-api/lifecycle/similar?symbol=BTCUSDT&limit=10
```

列表不返回完整 replay frames、长 excerpt 或完整 metrics JSON；frames 使用 `limit/offset`。所有公开响应统一为 `{"ok": true, "data": ...}`，并递归删除 Telegram 标识、dedup key、原始 payload/text、配置、job payload、异常堆栈、鉴权信息与服务器/数据库路径。

后台提供对应只读 `/api/lifecycle/*` 端点；`run-intelligence`、`run-replay`、`run-analytics` 和 `rebuild-replay` 仍受既有登录/CSRF 保护，通过 jobs system 防重复提交并返回 job id。

历史相似样本仅用于研究，不代表未来结果。系统不执行自动交易。

## Lifecycle public API

以下接口公开、只读、短缓存并经过字段投影与递归脱敏：

```text
GET /public-api/lifecycle/summary
GET /public-api/lifecycle/list?limit=50&state=&level=&risk=&symbol=
GET /public-api/lifecycle/detail?symbol=BTCUSDT
GET /public-api/lifecycle/events?symbol=BTCUSDT&limit=100
GET /public-api/lifecycle/metrics?symbol=BTCUSDT&limit=100
```

成功响应保持 `{"ok": true, "data": ...}` contract。公开响应不会包含 `chat_id`、`topic_id`、`message_id`、`dedup_key`、原始 `payload_json`、`text_html`、配置、token、Cookie、Authorization、数据库路径或服务器路径。

## Lifecycle private API

以下接口沿用现有后台登录和 CSRF 保护；未登录请求返回 401：

```text
GET  /api/lifecycle/summary
GET  /api/lifecycle/list
GET  /api/lifecycle/detail?symbol=BTCUSDT
GET  /api/lifecycle/events?symbol=BTCUSDT
POST /api/lifecycle/run-scan
POST /api/lifecycle/run-backfill
```

私有 API 不改变 `/api` 既有鉴权模式。运行接口只触发生命周期整理，不会调用交易所下单 API，也不会执行自动交易。

仅用于信号整理和风险提示，不构成投资建议，不执行自动交易。
