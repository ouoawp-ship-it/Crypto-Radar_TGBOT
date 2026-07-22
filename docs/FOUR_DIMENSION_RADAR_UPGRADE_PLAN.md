# Paoxx 四维资金行为雷达升级目标

状态：已批准，执行中
执行分支：`codex/four-dimension-radar-upgrade`
基线版本：`v1.90.2`
当前版本：`v1.91.0`
当前进度：M0、M1 已完成；下一阶段为 M2 多源数据层。

## 1. 目标

把现有 Telegram 异常扫描和 Mercu 语义验证工作台升级为 Paoxx 自有的四维资金行为雷达：

1. 衍生品资金行为；
2. 现货主动成交行为；
3. 链上实体资金流；
4. 价格结构与清算位置。

Web 与 Telegram BOT 消费同一份版本化证据快照。页面、推送和历史复盘不得各自计算出互相冲突的结论。

## 2. 产品边界

必须保留：

- 原 Telegram BOT 的启动、资金流、资金摘要、资金费率和公告风险基础模块；
- 去重、冷却、话题路由、真实发送确认和信号事实库；
- Binance、Bybit、OKX 公开行情底座；
- 后台单管理员认证、任务、日志、审计、配置和稳定检查；
- 不读取交易私钥、不下单、不承诺收益。

必须移除：

- 主题切换；
- 个人空间、用户管理和头像菜单；
- 收藏、自选和本地 watchlist；
- AI 智选、AI 问答、AI 提示词及其公开入口；
- Mercu 像素相似度作为发布标准。

内部 `launch_watchlist` 是启动候选池，不属于用户收藏；迁移时改名为 `launch_candidates`，避免概念混淆。

## 3. 数据源职责

| 数据源 | 职责 |
| --- | --- |
| 交易所公开 REST/WebSocket | 最低延迟价格、K 线、成交、盘口和可用清算 |
| CoinGlass | 主衍生品聚合、清算、费率、多空、订单簿和流动性结构 |
| Coinalyze | OI/费率第二来源、预测费率、主动成交与历史序列验证 |
| Arkham | 地址、实体、标签置信度、交易所流向和实体资金事件 |
| Paoxx 结构引擎 | 关键位、2B、123、箱体、背离和多周期结构 |

第三方故障只能降低数据质量或信号等级，不能停止原 BOT 基础模块。

## 4. 目标页面

- `/radar`：全市场信号流、四维状态、共振、清算和数据质量；
- `/derivatives`：OI、费率、预测费率、主动成交、多空、清算、订单簿和双源一致性；
- `/entities`：交易所流入流出、项目方、基金、做市商、鲸鱼和休眠钱包；
- `/structure`：TradingView Lightweight Charts、结构、关键位和事件标记；
- `/signals`：S/A/B/C 信号、证据、规则版本、生命周期和结果；
- `/info`：公告、新闻和授权事件流；
- `/coin/[symbol]`：单币四维证据；
- `/admin`：数据源、密钥、额度、映射、规则、Telegram、任务、日志和审计。

## 5. 目标数据链路

```text
Provider adapters
  -> symbol / unit / timestamp normalization
  -> minute facts + significant events
  -> freshness / coverage / source agreement
  -> four-dimension features
  -> versioned evidence snapshot
  -> Web API + Telegram BOT
```

每个公开指标必须带 `source`、`observed_at`、`age_sec`、`status` 和 `quality`。Arkham 标签必须保留置信度；CoinGlass 与 Coinalyze 原始 OI 不直接比较，只比较统一覆盖范围后的方向和标准化变化率。

## 6. 里程碑

| 里程碑 | 交付物 | 完成门禁 |
| --- | --- | --- |
| M0 基线治理 | 独立分支、版本、依赖安全、产品范围、自有视觉门禁 | 全量测试、构建、审计通过 |
| M1 产品边界 | 删除主题/收藏/AI，黑色设计令牌和新导航 | 关键路由无遗留入口，响应式通过 |
| M2 多源数据 | Provider 协议、CoinGlass、Coinalyze、Arkham、Symbol 映射 | 固定夹具、限流、熔断、降级测试通过 |
| M3 证据模型 | 一致性、拥挤、主动成交、链上、结构与 S/A/B/C | 公式黄金测试和解释字段完整 |
| M4 工作站 | 前台八个页面、Lightweight Charts、新后台 | 1440/1920/390 视觉和交互验收 |
| M5 BOT 增强 | 同一证据层进入原 BOT | 原阈值/去重/冷却无回归 |
| M6 发布 | CI、原子部署、HTTPS、性能、移动端和回滚 | 生产阻断为 0 |

已完成：

- M0：独立分支、依赖锁定、安全审计、自有视觉门禁和工程基线；
- M1：固定黑色主题、Paoxx 导航壳层、删除公开主题/用户/收藏/AI 页面与 API、响应式修复和六张正式视觉基准。

## 7. 工程约束

- 新公开与管理接口进入版本化 API 模块，不继续堆入巨型路由文件；
- 先保留 SQLite WAL，不保存全部逐笔成交，只保存分钟聚合和重大事件；
- 暂不引入 Redis、Celery、Kafka 或 Kubernetes；
- API Key 只在后端保存和使用，浏览器永不读取明文；
- 每个阶段结束执行可再生缓存清理，保留源码、配置、锁文件、测试和正式基准图；
- 每个阶段形成独立提交，通过 GitHub Actions 后才进入下一阶段或生产部署。
