# BOT-only 架构边界

## 保留模块

| 模块 | 作用 |
| --- | --- |
| `data_sources.py` / `funding_sources.py` | 交易所 REST 数据、缓存、限流与降级 |
| `derivatives_quality.py` | CoinGlass/Coinalyze 可选只读诊断，不参与方向信号值或门控 |
| `realtime_market.py` / `realtime_intelligence.py` | Binance 成交、清算、CVD 与实时异动；其他交易所采集默认关闭 |
| `radar.py` | 资金摘要、启动预警和公告分类 |
| `flow_radar.py` | 多因子资金流信号 |
| `funding_alert.py` | Binance 极端资金费率；可显式配置其他原生交易所用于独立观察 |
| `market_cockpit.py` | BOT 需要的市场快照与窗口比较；不再对外提供网页 API |
| `bot_market_context.py` | 给 Telegram 推送补充 Binance 实时/闭合窗口市场证据，不读取新闻或社交情报 |
| `signal_store.py` / `symbol_dossier.py` | 信号事实、生命周期和币种上下文 |
| `signal_effectiveness.py` | 已发送信号的方向语义、四窗口结果回填与只读效果统计 |
| `database_backup.py` | 活动 SQLite 在线备份、完整性检查、恢复验证与保留期清理 |
| `telegram.py` | 推送、话题路由、去重、冷却、限流与重试 |
| `cli.py` | 运维命令、readiness 与安全发送门禁 |

## 已移除边界

- Next.js 前端、Playwright 和视觉基准。
- Python Web/API/SSE 服务与管理后台。
- 用户、登录、收藏、主题与浏览器遥测。
- 独立 AI 助手和 AI 价格提醒服务。
- Web 任务队列、Web 鉴权与 Web-only 聚合接口。
- Web/Frontend/AI systemd 服务和网站发布流程。

`market_cockpit.py` 名称暂时保留，因为它是 Telegram 市场上下文的持久化计算层；改名只会制造无价值的大范围改动。

## 生产进程

```text
paopao-market-stream
    └─ 写入 realtime_features.db

paopao-radar
    ├─ 扫描 REST / 公告 / 资金费率
    ├─ 用 Binance 原生窗口完整性确认价格、OI、成交与费率
    ├─ 读取实时与历史上下文
    ├─ 生成、去重并记录信号
    └─ 推送 Telegram
```

## P1 数据质量边界

```text
Binance Spot + Binance USDⓈ-M Futures 原生口径
                         ↓
窗口对齐 → 完整性检查 → allow / block
                         ↓
资金流、启动预警、资金费率警报
```

- 方向信号只使用 Binance 原生数据，不允许外部聚合源改写 OI、费率或评分。
- 价格、OI、现货主动成交、合约主动成交和费率必须覆盖声明的完整窗口；缺项直接阻止该条资金流信号。
- `数据质量分` 作为历史兼容字段保留，但 Telegram 改为展示“Binance 原生、完整窗口、覆盖项数”，不再展示难解释的跨源一致性分。
- Binance 数据缺失或超过额度时保留诊断并阻止依赖缺失字段的信号，不把缺失值当作 0。
- 资金费率警报默认只读取 Binance；如人工开启其他交易所，消息会逐所列明原生来源，且不会改写启动或资金流信号。
- Binance 合约观察池会先与现货市场目录核对；不存在的现货交易对不再发送必然失败的 K 线请求，也不消耗现货 K 线预算。
- `provider-check` 使用隔离的只读客户端验证 CoinGlass/Coinalyze，明确报告 Key、套餐权限、空数据或可用状态，且不输出密钥；诊断结果不会影响生产方向信号。
- Arkham 属于链上实体事件层，后续独立开发，本层不包含 Arkham 依赖。

## P1.2 数据运维闭环

```text
signals / market_snapshots / realtime_features / news_events
        ↓ SQLite 在线备份
临时备份集完整性检查
        ↓ 原子发布
只读打开 + 恢复到内存 + 再次完整性检查
        ↓
manifest.json → health/stable-check 新鲜度监控
```

- 每日备份只覆盖 BOT 当前活动数据库，不把缓存、日志或已退役 Web 数据混入灾备范围。
- 备份集使用时间戳目录和原子发布；未完成目录不会被健康检查当成有效备份。
- 自动清理仅匹配备份根目录直属的标准时间戳目录，不跟随符号链接，不宽泛删除未知文件。
- 默认保留 7 天本机备份；异机或对象存储复制仍是后续独立的灾备增强项。
- 信号效果样本默认保留 365 天、最多 20,000 条，为 P2.1 人工校准保留足够跨行情周期的数据。

## P2 信号有效性闭环

```text
结构化且真实发送的信号
        ↓
仅接受明确方向语义与非 block 数据质量门
        ↓
使用已持久化 Binance 行情匹配入场价
        ↓
15m / 1h / 4h / 24h 到期价格
        ↓
原始收益、方向收益、命中状态
        ↓
按模块、分类、评分区间和质量等级复盘
```

- `launch` 只按做多启动假设追踪；`flow` 和 `funding` 仅追踪有明确方向含义的分类。
- 合约拉盘、挤空结束、费率分歧等仅提示风险但没有稳定方向语义的事件不会被硬塞进命中率。
- 缺失入场价、缺失到期价和未到期记录分别标记，不把缺失数据计算成失败。
- 只有同一结果窗口内质量门可信且成熟样本不少于 50 条时，才进入 P2.1 人工校准评审。
- P2.0 不自动修改阈值、权重或生产模型。

## 启动预警消息生命周期

```text
观察 watching → 预警 primed → 确认 breakout → 启动 launched
                                              ↓
                                      降温 cooling（默认 30 分钟）
                                              ↓
                                        失效 failed
                                              ↓
                         删除该币本轮 Telegram 消息，保留 SQLite 信号样本
```

- 单次分数回落或短时掉出候选池只进入降温期，不立即删除。
- 降温期内信号恢复时继续原周期；确认失效并完成清理后，再次出现视为新周期。
- 首版仅清理 `TG_LAUNCH_ALERT` 的单币消息。资金流、资金摘要等多币聚合消息不能按单币安全拆除，因此明确保留。
- Telegram 删除成功、失败和超出安全删除窗口的消息都有审计；失败会在后续扫描重试，超过窗口后停止无效重试。
- 消息删除不修改 `signals.status='sent'`，避免破坏 P2 信号有效性样本、发送限额统计和历史复盘。
