# API

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
