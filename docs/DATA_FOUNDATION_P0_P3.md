# 泡泡雷达数据工程 P0–P3 目标与运行手册

## 工程目标

把系统从“Telegram 文本可推送、页面偶尔有数据”升级为可审计、可迁移、可持续积累的市场数据产品。完成标准不是页面出现数字，而是：事实有来源和观测时间，缺失值不伪造为 0，历史迁移可回滚，状态可解释，自动化测试与生产验收可重复执行。

## 优先级与验收标准

### P0：信号数据正确性

- 符号提取仅扫描用户可见文本，不扫描 HTML/Markdown 链接目标，阻断 `%3A` 生成 `3A*USDT` 伪币种。
- Telegram 只是交付渠道；引擎产出的币种、评分、阶段和指标以结构化记录写入 `signals.db`。
- 文本反解析仅作为旧消息兼容路径，记录为 `text_fallback/degraded`。
- `signal-repair` 默认只审计；`--apply` 使用 SQLite 在线备份后才删除伪币种、恢复可识别评分。
- 更新脚本先审计再迁移，迁移失败会阻止继续更新。

### P1：30 天市场事实层

- 行情快照默认每 5 分钟采集、保留 30 天，覆盖成交额排序前 500 个 USDT 永续资产。
- OI 每轮轮转补齐 80 个资产，按周期轮转覆盖完整资产池。
- 现货与合约主动买卖差使用封闭 15 分钟 K 线计算，前 40 个资产独立采集，不依赖 Telegram 推送频率。
- 所有事实保存 `source`、`observed_at`、`window_sec`、`coverage` 和 `data_status`。
- 15m、30m、1h、4h、1d 榜单从同一事实层生成；历史不足时明确使用 ticker fallback 或返回空值。

### P2：数据源治理

- `/public-api/data/sources` 公开数据源、指标用途、保留策略、内容策略和降级规则，不包含密钥。
- 数值事实与文章内容分开治理；公告只保存必要元数据、短摘要和原文链接，不复制受限全文。
- 新增数据源或扩大用途前必须重新审核服务条款；接口失败时保留最后一次已验证事实并标记 stale，不能伪造 0。

### P3：就绪度、SLA 与前端状态

- 状态统一为 `empty`、`warming_up`、`partial`、`ready`、`stale`。
- `/public-api/health` 和市场接口返回新鲜度预算、历史跨度、30 天预热进度、预计完成时间和指标覆盖率。
- 前端分别展示等待、预热、部分可用、就绪和过期；数据覆盖面板展示真实预热进度。
- stable-check 检查伪币种污染和市场事实层状态；预热是正常状态，过期或空库是观察项，污染是阻断项。

## 数据链路

```text
官方公开 API
  ├─ Binance Futures: price / volume / funding / OI / futures CVD estimate
  ├─ Binance Spot: spot CVD estimate
  ├─ CoinPaprika: market cap enrichment
  └─ Bybit / OKX / Bitget / Gate: funding confirmation
          │
          ▼
MarketSnapshotStore (SQLite, 30 days)
          │
          ├─ window comparison and percentile boards
          ├─ readiness / coverage / freshness SLA
          └─ public API → Next.js dashboard

Radar engines → structured signal facts → SignalEventStore
       │                                      │
       └──────── Telegram delivery ───────────┘
```

## 关键配置

| 配置 | 默认值 | 说明 |
|---|---:|---|
| `MARKET_SNAPSHOT_INTERVAL_SEC` | 300 | 行情事实采集间隔 |
| `MARKET_SNAPSHOT_RETENTION_DAYS` | 30 | SQLite 历史保留天数 |
| `MARKET_SNAPSHOT_LIMIT` | 500 | 行情资产范围 |
| `MARKET_SNAPSHOT_OI_LIMIT` | 80 | 单轮 OI 轮转数量 |
| `MARKET_SNAPSHOT_WORKERS` | 8 | 有界并发数 |
| `MARKET_FLOW_FACT_INTERVAL_SEC` | 900 | 主动资金封闭窗口 |
| `MARKET_FLOW_FACT_LIMIT` | 40 | 主动资金资产范围 |
| `MARKET_READINESS_TARGET_DAYS` | 30 | 完整历史预热目标 |

## 发布与回滚

1. 本地执行全量 Python 测试与 Next.js 生产构建。
2. 服务器更新脚本执行测试后运行 `signal-repair` 审计。
3. 发现可修复旧数据时自动创建 `signals.db.pre-signal-repair-*.bak` 并迁移。
4. 服务重启后检查 `/public-api/health`、`/public-api/data/sources` 和雷达数据覆盖面板。
5. 迁移异常时停止发布；需要数据回滚时先停写服务，再用对应 `.bak` 替换 `signals.db`。

30 天预热不会阻止核心服务运行。预热期间短周期榜单会逐步可用，长周期榜单只有在对应历史跨度满足后才标记就绪。
