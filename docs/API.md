# Web API

## 公开接口

公开接口无需登录，只返回脱敏后的信号字段。

### `GET /public-api/signals`

参数：`limit`、`cursor`、`module`、`symbol`、`status`、`q`、`window_sec`。

### `GET /public-api/signals/stats`

参数：`window_sec`，最大 30 天。

### `GET /public-api/signals/detail?id=<signal_id>`

返回单条信号的公开摘要和同币种相关信号。

## 后台接口

`/api/*` 除登录状态和登录/退出外均需要后台认证；写操作还必须携带会话 CSRF Token。

主要读取接口：

- `/api/summary`
- `/api/version`
- `/api/server-status`
- `/api/config`
- `/api/signals`
- `/api/signals/stats`
- `/api/signals/detail`
- `/api/jobs`
- `/api/jobs/stats`
- `/api/logs`
- `/api/audit`
- `/api/ops-snapshot`
- `/api/price-alerts`
- `/api/ai-prompts`

主要写入接口：

- `/api/config`
- `/api/jobs`
- `/api/jobs/cancel`
- `/api/jobs/rerun`
- `/api/jobs/cleanup`
- `/api/service`
- `/api/action`
- `/api/problem-state`
- `/api/price-alerts`
- `/api/ai-prompts`

任务中心只接受 `stable-check`、`doctor`、`readiness`、`cleanup`、`update-check` 和 `api-self-test`。
