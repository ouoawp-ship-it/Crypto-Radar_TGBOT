# Web API

## 公开接口

公开接口无需登录，只返回脱敏后的信号和市场上下文字段。市场字段统一携带 `value`、`unit`、`source`、`observed_at`、`age_sec`、`status` 与 `quality`，禁止把缺失值伪装成 `0`。

### `GET /public-api/signals`

参数：`limit`、`cursor`、`module`、`symbol`、`status`、`q`、`window_sec`。

### `GET /public-api/signals/stats`

参数：`window_sec`，最大 30 天。

### `GET /public-api/signals/detail?id=<signal_ref>`

返回单条信号的公开摘要和同币种相关信号。新链接使用不可枚举的稳定 `public_ref`（形如 `sig_xxx`）；兼容旧数字 ID。

### `GET /public-api/signals/context?id=<signal_ref>`

返回面向前台详情抽屉的聚合上下文：

- 脱敏信号摘要；
- 市场快照与关键证据；
- 自身历史极端度、市场相对强度和同口径绝对规模排名；
- 15m/30m/1h/4h/1d 跨模块共振；
- NEW、增强、持续、降温、重启和失效生命周期；
- 同币种最近信号；
- 信号和币种深链。

市场数据源失败时仍返回信号事实，并通过 `market_error` 和字段级状态说明降级，不把上游错误扩散为整页失败。

### `GET /public-api/market/snapshot?symbol=<symbol>`

返回币种轻量市场快照，币种可传 `BTC` 或 `BTCUSDT`。当前包含：

- 价格与 15m/1h/4h/24h 变化；
- 24h 成交额与相对量能；
- OI 绝对值与 15m/1h/4h 变化；
- Binance 和多交易所资金费率；
- 市值、流动性分层与箱体结构。

接口使用 30 秒服务端单航班缓存，避免同一币种的并发请求重复访问上游。

### `GET /public-api/radar/intelligence?window_sec=86400&limit=5`

返回雷达情报层和四类机会榜：启动候选、跨模块共振、极端费率、结构与公告风险。

- 自身排名：同币同模块近 30 天规则分数百分位；
- 市场强度：同模块窗口内每币最新规则分数横截面排名；
- 绝对规模：只有信号含同口径成交额、OI 或市值事实时才计算；
- 样本少于 2 个时返回 `available=false`，不生成伪排名；
- 共振表示同币跨模块同时出现，不代表多空方向一致。

### `GET /public-api/coin/context?symbol=<symbol>`

返回轻量单币验证上下文：市场快照、最近 30 条信号、排名/共振/生命周期，以及雷达、AI 和提醒动作。该接口不包含回测、模型或交易执行能力。

### `GET /public-api/market/watchlist?symbols=BTC,ETH`

一次读取 1–12 个自选币种的服务端聚合快照。每个币独立返回成功或降级状态，单一上游失败不会使整批失败。

### `GET /public-api/health`

返回公开 API 的安全聚合健康信息：信号库状态、进程内缓存命中统计、请求状态码、各路由 P95、限流计数与匿名前端错误计数；不返回 IP、Token、Cookie 或错误正文。

### `POST /public-api/telemetry`

只接受固定事件名并仅做内存计数：`frontend_api_error`、`frontend_render_error`、`frontend_unhandled_error`、`frontend_route_loaded`。不接收页面输入、错误文本或用户标识。

### 限流与安全响应

- 普通公开接口默认每来源每分钟 180 次；市场聚合/单币/上下文接口默认 30 次；
- 仅对 `PUBLIC_API_TRUSTED_PROXY_IPS` 中的直接代理信任 `X-Forwarded-For`；
- 429 返回 `Retry-After`、`X-RateLimit-Limit`、`X-RateLimit-Remaining` 和 `X-RateLimit-Reset`；
- JSON 响应增加 `nosniff`、拒绝 iframe、严格来源策略与浏览器权限限制。

### 数据状态

- `fresh`：在目标新鲜度内且核心字段完整；
- `stale`：数据年龄超过目标，展示旧缓存语义；
- `degraded`：只有部分核心字段可用；
- `unavailable`：该字段或快照不可用。

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
