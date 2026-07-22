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

### `GET /public-api/market/realtime?symbol=BTC&limit=80&max_age_sec=180`

返回 Binance、Bybit 与 OKX 线性 USDT 永续合约已封闭分钟窗口的实时衍生特征：主动买入/卖出成交额、CVD、分钟 OHLC、多头/空头强平额和事件数量。`symbol` 可省略；每条记录保留 `exchange`，接口只读取持久化特征，不直接连接交易所。

- 聚合成交按主动方方向计算，`CVD = 主动买入额 - 主动卖出额`；
- 强平使用已执行数量和平均成交价，并按强平订单方向映射被清算的持仓方向；
- OKX `sz` 是合约张数，必须先用公开合约 `ctVal/ctValCcy` 换算基础币数量或 USDT 名义额，禁止直接当作币数量；
- OKX 没有公开的全市场真实强平流，因此只贡献成交/CVD，不用私有风险预警伪装强平；
- 只发布完成且超过宽限期的分钟桶，重连重放按事件 ID 和数据库主键去重；
- 无新鲜数据时返回 `data_status=unavailable` 或 `stale`，不生成模拟值。

### `GET /public-api/radar/realtime-intelligence?limit=10&backtest=1`

返回由实时分钟特征计算的自有异常情报：Surge 加速、短周期潜伏、5m/15m/1h 方向共振、自身/市场强度/绝对成交规模排名和异常生命周期。`backtest=1` 时额外返回离线方向结果统计；默认不计算回测以降低冷请求开销。

- Surge 比较相邻两个已封闭 5m 窗口的 CVD 占比、成交额速度、价格变化和强平偏向；
- 短周期潜伏要求 5m 与 15m CVD 同向、价格仍压缩且尚未触发 Surge；
- 方向共振至少需要两个可用窗口同向，并要求价格没有反向否定；
- 回测只使用信号时点之后的封闭分钟价格，分别计算 5m/15m/1h 方向收益；
- 任一回测周期少于 30 个样本时标记 `insufficient`，不把小样本命中率包装成有效结论；
- 历史统计不含手续费、滑点和成交约束，不构成投资建议。

### `GET /public-api/radar/boards?window_sec=3600&limit=8`

返回价格、OI、合约主动资金、现货主动资金与资金费率榜单。实时分钟特征新鲜且窗口覆盖达标时，追加实时合约 CVD、多空强平、Surge 与短周期潜伏榜；实时链路不可用时仍保留原 REST 榜单。榜单只在对应字段覆盖足够时给出结果；不可用维度返回明确状态，不用其他指标代替。

### Radar 工作站分模块接口

- `GET /public-api/workstation/radar/anomalies?limit=30`：独立异动事件流，保留自身历史、全场强度与全场绝对量级三类排名。
- `GET /public-api/workstation/radar/momentum?window=15m|30m|1h|4h|1d&limit=8`：单窗口价格、OI、合约流、现货流的量级榜和强度榜。
- `GET /public-api/workstation/radar/momentum-windows?limit=8`：一次历史扫描返回五个封闭窗口；每个窗口附带服务端计算的资金流/资金力度共振榜。
- `GET /public-api/workstation/radar/surge?limit=5`：按封闭窗口加速度得分降序返回 Surge 榜。
- `GET /public-api/workstation/radar/rank?total_limit=14&ambush_limit=8`：返回 24h 累积异动总榜、埋伏池及同一时点的排名上下文。
- `GET /public-api/workstation/radar/briefs?limit=6`：把已排名异动事实压缩成可供 Info 联动的确定性摘要，不添加第三方 AI 推荐。

这些接口都返回独立的 `schema_version`、时间、状态、覆盖率、警告和 `methodology`。页面可分别轮询并保留上次成功数据；任一模块失败不应阻断其他模块。

Radar 量级与共振口径：

- 榜单行的 `score` 是固定阈值归一化量级分，范围 0–1：价格为 `abs(涨跌幅)/10%`，OI 为 `abs(OI 变化额)/5000 万美元`，现货/合约主动净流为 `abs(CVD 净额)/2000 万美元`，均在 1 截断；`strength_percentile` 仍是独立的历史/横截面异常分位，二者不可混用。
- `window_states` 的每个亮格只表示同一币种在对应封闭窗口内，出现在“同一指标、同一榜单模式（量级或强度）、同一方向”的榜单中；它不是泛化的多空判断，也不因其他指标同向而点亮。
- 右侧 `confluence` 只统计 OI、合约主动净流和现货主动净流三个维度。`board_count`/`N` 是实际出现的维度数，`side` 为多数方向，正负方向同时出现时 `divergent=true`；价格不参与资金合流计数。

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

当 `source_type=plaza` 时，响应额外包含服务端所有的 `plaza_rankings`：

- `provider` 明确标注实际广场数据提供方、来源类型和权利状态；前端不得把公开聚合源伪装成另一家平台的数据。

- `active_4h`：4 小时币种活力榜，返回真实帖子数、本轮/上轮 1 小时帖子数、环比倍数、`NEW` 状态、方向计数和公开互动；仅保留本轮出现过的币种，并按 `NEW → 环比倍数 → 本轮提及数 → 4h 帖数` 排序；
- `total_24h`：24 小时总榜，返回广场多空占比、情绪状态、互动强度、24h 价格变化，以及由合约主动资金方向与强度拆分出的主力合约多/空占比；
- 帖子数只统计已入库公开事件，不使用视觉夹具或估算值补量；
- 情绪占比只使用 `opportunity/risk` 规则标签，中性事件不进入方向分母；
- 互动分数为 `点赞 + 2×转发 + 回复`，行情字段缺失时保持 `null`，页面显示 `—`；
- `methodology` 明确给出帖子、情绪、互动与行情的计算口径。

### `GET /public-api/agents/overview?window_sec=14400`

返回全局、BTC/ETH、异常候选和消息 Agent 的结构化结论。方向性结论只有在核心证据 `ready` 时才生成；降级或证据不足时必须返回 `insufficient_data`，并提供证据引用、反证、过期时间、规则版本和免责声明。

### `GET /public-api/stream`

SSE 实时事件流。支持 `Last-Event-ID` 和查询参数续传，发送 `status`、`signal` 与心跳事件；连接时长有上限，客户端断线后自动重连，并始终保留 `/public-api/signals` 轮询兜底。Nginx 必须关闭该路径的代理缓冲和缓存。

### `GET /public-api/data/sources`

返回数据源治理清单及进程内运行状态。每个已声明来源包含用途、授权边界、保留策略和降级规则，并合并请求成功率、P50/P95、缓存命中率、数据年龄与最近的安全错误类别。未观测来源明确标记为 `unobserved`，错误正文、URL、请求参数和凭据不会进入响应。

### `GET /public-api/health`

返回公开 API 的安全聚合健康信息：信号库状态、历史市场快照状态、Binance/Bybit/OKX 各自的实时分钟特征新鲜度与覆盖币种数、进程内缓存命中统计、请求状态码、各路由 P95、关键聚合路由 800ms P95 预算状态、上游来源成功率/延迟/缓存/数据年龄、限流计数与匿名前端错误计数；不返回 IP、Token、Cookie、URL、请求参数或错误正文。任何已启用实时交易所缺少新鲜封闭分钟桶时，实时状态为 `partial`/`stale`/`empty`，整体状态为 `degraded`。

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
