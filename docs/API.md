# API

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
