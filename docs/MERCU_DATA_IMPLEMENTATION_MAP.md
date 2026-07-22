# Mercu 数据语义到 Paoxx 自有实现映射

更新时间：2026-07-22

## 工程边界

Mercu 的接口只用于黑盒比对字段、刷新节奏、排序结果和计算语义。Paoxx 生产环境不得调用 Mercu 私有接口，也不得保存或转发 Mercu 的登录令牌。Paoxx 使用交易所/授权供应商数据，自行完成采集、封闭窗口聚合、持久化、排名、异常识别和 Web/BOT 输出。

## 已确认的参考接口与 Paoxx 对应关系

| Mercu 黑盒参考 | 已确认语义 | Paoxx 自有接口 | 当前状态 |
|---|---|---|---|
| `/radar/anomaly-v4` | 异动流、自身历史排名、全场强度、全场量级、状态生命周期 | `/public-api/workstation/radar/anomalies` | 已有，继续校准标签和阈值 |
| `/radar/momentum?window=` | 价格、OI、合约主动净流、现货主动净流；量级榜与强度榜 | `/public-api/workstation/radar/momentum`、`momentum-windows` | 本轮修正固定量级分和五窗口上榜共振 |
| `/radar/surge` | 短周期加速度榜 | `/public-api/workstation/radar/surge` | 已有 |
| `/radar/rank` | 24h 总榜与埋伏池 | `/public-api/workstation/radar/rank` | 已有，继续校准入池阈值 |
| `/fund/capital-flow` | 现货/合约资产资金表 | `/public-api/workstation/funds/overview` | 字段已覆盖 |
| `/fund/sectors` | 板块净流和气泡分布 | `/public-api/workstation/funds/overview` | 数据已覆盖，视觉/碰撞布局另行校准 |
| 单币资金时序 | 现货/合约主动净流、OI、费率等时序 | `/public-api/workstation/funds/series`、`open-interest` | 已有 |
| `/news/feed`、`/info/plaza` | 新闻、公告、KOL、广场活跃度 | `/public-api/workstation/info/feed`、`dashboard`、`briefs` | 公告/RSS 已有；KOL/广场授权覆盖待增强 |

## Radar 已落地计算口径

量级分 `score` 与异常强度 `strength_percentile` 是两个独立概念：

- 价格量级分：`min(1, abs(change_pct) / 10)`。
- OI 量级分：`min(1, abs(oi_delta_usd) / 50_000_000)`。
- 合约主动净流量级分：`min(1, abs(perp_cvd_usd) / 20_000_000)`。
- 现货主动净流量级分：`min(1, abs(spot_cvd_usd) / 20_000_000)`。
- `strength_percentile` 优先使用同币、同窗口的历史异常分位，历史不足时才回退当前横截面分位。
- 五窗口蓝格只表示同币在相应窗口进入了“同指标、同榜型、同方向”榜单，不表示泛化多空共振。
- 右侧合流只统计 OI、合约主动净流、现货主动净流。`board_count`/`N` 是出现维度数；多数方向写入 `side`；方向冲突时保留记录并标记 `divergent=true`。

主动净流统一定义为窗口内主动买入成交额减主动卖出成交额（CVD）。它不代表交易所充值减提现，也不能把 OI 增加直接解释为多头流入。

## 数据源与 API Key 清单

### 当前运行必须项

| 配置 | 是否需要新增 | 用途 |
|---|---:|---|
| Binance 公共 REST/WebSocket | 否 | 价格、K 线、主动买卖、OI、费率等公开市场数据 |
| Bybit 公共 REST/WebSocket | 否 | 多交易所成交、公开强平、OI/费率交叉验证 |
| OKX 公共 REST/WebSocket | 否 | 多交易所成交、OI/费率交叉验证 |
| `TG_BOT_TOKEN` | 项目原有 | Telegram BOT 推送；不是 Mercu 复刻新增项 |

### 建议优先补充

1. `COINGLASS_API_KEY`：最高优先级、可选。用于聚合多交易所 OI、CVD/NetFlow、强平、多空比等，最能缩小与聚合型目标数据的差距。适配器接入前需要同时确认订阅等级、每分钟限额和可用历史深度。官方要求通过 `CG-API-KEY` 请求头鉴权。
2. `COINGECKO_API_KEY`：次优先级、可选。用于更稳定的市值、分类、流通量和币种元数据。Demo/Pro 的地址与请求头不同，接入前必须确认 Key 类型。

### 后续功能按需补充

| 配置 | 什么时候需要 | 备注 |
|---|---|---|
| `X_BEARER_TOKEN` | 做授权的 X/KOL 信息流时 | 需要对应 X API 访问计划，不是 Radar/Funds 的前置条件 |
| `TELEGRAM_API_ID`、`TELEGRAM_API_HASH` | 经授权读取公开频道历史/消息时 | 与 `TG_BOT_TOKEN` 不同；必须遵守 Telegram 条款和频道授权边界 |
| `AI_API_KEY`、`AI_BASE_URL` | 启用 Paoxx 自有 AI 摘要/智选时 | 项目已有配置；不复制 Mercu AI 输出 |
| 新闻供应商 Key（待选型） | RSS/官方公告覆盖不足时 | 先确定合法来源、许可、额度后再定义变量，不提前绑定供应商 |

## 提供密钥的安全方式

不要把真实 Key 写入源码、测试夹具、Git 提交或截图。请把值写入服务器/本机未跟踪的 `.env.oi` 或部署平台 Secret；只需告知 Key 类型、订阅等级、额度和是否已配置成功。适配器代码只读取环境变量，并在日志与公开 API 中强制脱敏。

## 下一批实施顺序

1. 接入 CoinGlass 可选适配器（有 Key 时启用，无 Key 时继续使用交易所自采并明确标记来源）。
2. 用同一固定时间夹具校准多交易所 OI、CVD/NetFlow 和强平排名。
3. 增强 Info 的授权新闻/KOL 覆盖和可解释情绪统计。
4. 三页数据验收后，把同一证据层输入 BOT；BOT 只消费 Paoxx 内部标准字段，不直接依赖任何页面或 Mercu 接口。
