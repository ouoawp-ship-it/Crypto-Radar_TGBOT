# Web API

## 公开接口

公开接口无需登录，只返回脱敏后的信号和市场上下文字段。市场字段统一携带 `value`、`unit`、`source`、`observed_at`、`age_sec`、`status` 与 `quality`，禁止把缺失值伪装成 `0`。

### `GET /public-api/signals`

参数：`limit`、`cursor`、`module`、`symbol`、`status`、`q`、`window_sec`。

列表只返回一个标准 `data` 信封：`data.items`、`data.count`、`data.next_cursor` 与 `data.filters`。不再在顶层重复返回同一份 `items`，卡片字段采用长度受限白名单投影。

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

### `GET /public-api/market/overview?window_sec=3600`

返回全市场广度、成交额、现货/合约主动资金估算及字段覆盖率。所有聚合值都由同一版市场快照契约生成，并携带 `schema_version`、`generated_at`、`data_status`、`warnings` 与 `coverage`。

### `GET /public-api/radar/boards?window_sec=3600&limit=8`

返回价格、OI、合约主动资金、现货主动资金与资金费率榜单。榜单只在对应字段覆盖足够时给出结果；不可用维度返回明确状态，不用其他指标代替。

### `GET /public-api/funds/sectors?window_sec=3600&market_type=spot`

返回板块轮动、净流入/流出、覆盖率和板块领先资产。主动资金为交易所 K 线主动买卖成交差估算，不等同于链上资金流。

### `GET /public-api/funds/assets`

返回可筛选、排序的资产资金表，支持时间窗口、市场类型、板块、方向和关键词。每条记录保留来源、单位、时间和数据状态，可进入单币证据页复核。

### `GET /public-api/radar/intelligence?window_sec=86400&limit=5&refs=<signal_ref,...>`

返回雷达情报层和四类机会榜：启动候选、跨模块共振、极端费率、结构与公告风险。

- `refs` 可选，最多 40 个稳定 `public_ref` 或旧数字 ID；雷达页用当前 40 条信号精确请求对应情报；
- 未传 `refs` 时最多返回最新 40 条情报；`projection` 明确返回请求数、命中数和上限；
- 列表只保留卡片需要的排名、生命周期与共振字段，完整方法、来源和依据继续由单条 context 接口返回；
- 服务端只计算当前窗口信号；单币和单信号 context 使用稳定引用定向计算，但仍读取完整 30 天历史作为排名与生命周期依据；
- 单次未压缩 JSON 发布预算不超过 256 KiB，Nginx 对 JSON/文本启用 gzip。

- 自身排名：同币同模块近 30 天规则分数百分位；
- 市场强度：同模块窗口内每币最新规则分数横截面排名；
- 绝对规模：只有信号含同口径成交额、OI 或市值事实时才计算；
- 样本少于 2 个时返回 `available=false`，不生成伪排名；
- 共振表示同币跨模块同时出现，不代表多空方向一致。

### `GET /public-api/coin/context?symbol=<symbol>`

返回轻量单币验证上下文：市场快照、最近 30 条信号、排名/共振/生命周期，以及雷达、AI 和提醒动作。该接口不包含回测、模型或交易执行能力。

### `GET /public-api/market/watchlist?symbols=BTC,ETH`

一次读取 1–12 个自选币种的服务端聚合快照。每个币独立返回成功或降级状态，单一上游失败不会使整批失败。

### `GET /public-api/info/feed`

返回官方公告事件、聚类、币种关联、来源链接和授权状态。当前仅保存与展示官方链接允许范围内的结构化事实，不复制第三方受限正文。高重要度事件的规则解读强制区分“事实”和“推断”。

### `GET /public-api/agents/overview?window_sec=14400`

返回全局、BTC/ETH、异常候选和消息 Agent 的结构化结论。方向性结论只有在核心证据 `ready` 时才生成；降级或证据不足时必须返回 `insufficient_data`，并提供证据引用、反证、过期时间、规则版本和免责声明。

### `GET /public-api/stream`

SSE 实时事件流。支持 `Last-Event-ID` 和查询参数续传，发送 `status`、`signal` 与心跳事件；连接时长有上限，客户端断线后自动重连，并始终保留 `/public-api/signals` 轮询兜底。Nginx 必须关闭该路径的代理缓冲和缓存。

### `GET /public-api/health`

返回公开 API 的安全聚合健康信息：信号库状态、进程内缓存命中统计、请求状态码、各路由 P95、限流计数与匿名前端错误计数；不返回 IP、Token、Cookie 或错误正文。

### `POST /public-api/telemetry`

只接受固定事件名并仅做内存计数：`frontend_api_error`、`frontend_render_error`、`frontend_unhandled_error`、`frontend_route_loaded`。不接收页面输入、错误文本或用户标识。

### 限流与安全响应

- 普通公开接口默认每来源每分钟 180 次；市场聚合/单币/上下文接口默认 30 次；
- 仅对 `PUBLIC_API_TRUSTED_PROXY_IPS` 中的直接代理信任 `X-Forwarded-For`；
- 429 返回 `Retry-After`、`X-RateLimit-Limit`、`X-RateLimit-Remaining` 和 `X-RateLimit-Reset`；
- JSON 与公开前台响应增加 `nosniff`、拒绝 iframe、严格来源策略、浏览器权限限制与 HSTS。

### V2 灰度开关

`PAOXX_COCKPIT_V2_MODE` 支持 `enabled`、`preview`、`disabled`。`disabled` 仅关闭 V2 市场驾驶舱接口与页面，旧 `/public-api/signals`、Telegram Bot、AI 助手和后台运维保持可用。修改后必须重建 Next.js 前台并重启 Web/前台服务，不能只重启 Python Web。

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
