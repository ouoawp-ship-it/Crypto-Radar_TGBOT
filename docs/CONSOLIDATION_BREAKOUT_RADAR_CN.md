# 盘整突破雷达

这是 `Crypto-Radar_TGBOT` 内的原生 Binance K 线扫描模块，不依赖
TradingView Webhook，也不会启动新的 Web 服务。它复用项目现有的 Telegram
双重发送门禁、专属话题路由、去重、限流、outbox 和信号账本。

## 它会找什么

默认候选池覆盖 Binance USDⓈ-M 全部活跃 USDT 永续合约，不按 24 小时成交额
淘汰长尾标的。雷达按合约代码稳定轮转，每批默认扫描 40 个标的；轮转游标持久化，
进程重启后会从上一批继续。现行产品保留旧多周期、自适应日线和 1H 临界预警三条
彼此隔离的识别路径。

### 旧多周期路径保持不变

原有 4H、1D、1W 路径仍在每个周期检查固定的短期 24 根、中期 72 根和长期
240 根冻结箱体。旧状态文件、事件 ID、三推规则、轮转顺序和已配置周期均不迁移、
不清空；没有显式启用自适应日线产品时，升级不会改变旧雷达的 Telegram 流量。

### 自适应 1D 产品

新的日线产品使用独立开关、独立状态文件和 `daily_adaptive.v1` 检测配置。它不把
所有盘整强制切成固定 240 天，而是分别尝试：

- 短期：20 / 30 / 40 / 50 根日 K；
- 中期：60 / 90 / 120 / 150 根日 K；
- 长期：180 / 240 / 300 / 360 / 420 / 500 根日 K。

每一档从最长锚点向较短锚点评估，并选择最长的合格结构。默认读取 620 根日 K，
因此最长 500 日箱体仍有足够的稳定性比较和 ATR 上下文；横盘几十天和数百天的
标的都能进入同一套全市场识别流程。

自适应箱体必须同时满足：

- 上下沿使用 5% 极端影线裁剪，减少单根异常针改变整段边界；
- 至少 90% 的完整 K 线和 95% 的收盘留在裁剪后边界内；
- 上下沿各至少两个分离的触碰簇，连续贴边只算一次；
- 箱宽同时通过该期限独立的 ATR 和百分比上限；
- 收盘路径效率足够低，排除单边趋势伪装成横盘；
- 多个确认窗口内上下沿漂移不超过 0.35 ATR；
- 结构通过后冻结上下沿，之后不会因事件 K 线的新高、新低移动边界。

### 结构周期与触发周期

自适应日线产品明确区分三个步骤：

1. 在 1D K 线闭合后识别并冻结日线箱体；
2. 用后续已闭合 4H K 线监控冻结的日线上下沿，作为更早的边界预警；
3. 只有已闭合 1D K 线自身满足越界和缓冲条件，才构成日线级确认。

因此“结构周期 1D｜触发周期 4H”表示日线箱体的盘中预警，不等于已经取得日线
收盘确认；“结构周期 1D｜触发周期 1D”才是日线收盘事件。两者使用独立事件 ID
和状态，不会把 4H 预警自动改名为 1D 确认。

### 1H 箱体临界预警

这是独立的提前观察产品，不是“预计一定突破”。它在已闭合 1H K 线上识别并冻结
短期 24 根、中期 72 根、长期 240 根箱体；同一标的多个期限同时合格时只监控最长
一个。触发只读取已闭合 15m 和 1H K 线，触发 K 线不参与冻结边界计算。

为了不把旧 4H/1D/1W 全市场轮转从每批 40 个压缩成 24 个，本产品使用独立预算和
状态，分为两层：

- 慢速发现层按合约代码轮转全市场，每批默认刷新 20 个标的的 1H 冻结箱体；
- 快速监控层先用全市场最新价格筛出距任一有效边界不超过 1.0 个冻结 1H ATR 的
  活跃箱体，再为最多 20 个近边界标的读取已闭合 15m K 线；如果 15m 请求失败或
  无法聚合出两根完整 1H，会在预留请求预算内直取已闭合 1H 数据继续兜底。

上沿和下沿判断完全对称，并且收盘必须仍在箱体内部：

- 15m 临界区：距边界同时不超过 `0.20 ATR_1H` 和 `0.35%`；最近 4 根 15m 至少
  有 2 段继续靠近且累计推进不低于 `0.10 ATR_1H`，或者最近 2 根都紧贴在
  `0.10 ATR_1H` 内；
- 1H 兜底：距边界同时不超过 `0.30 ATR_1H` 和 `0.35%`；相较上一根 1H 至少
  靠近 `0.05 ATR_1H`，或者已经紧贴在 `0.10 ATR_1H` 内。若已有更新的闭合 15m，
  不会退回上一根较旧 1H 发预警；
- 同一 `标的 + 冻结箱体 + 上/下沿` 的一次接近过程只预警一次。15m 已成功推送后，
  1H 不再重复；只有未出现或未成功消费 15m 预警时，1H 才作为兜底；
- 价格连续 2 根已闭合 1H 退回该边界至少 `0.60 ATR_1H` 后重新武装。新冻结箱体
  使用新箱体 ID，可独立开始下一轮生命周期。

收盘已经越界时不再叫“临近”；影线越界但收回箱体属于扫流动性。正式突破、跌破、
扫盘、回踩、假突破/假跌破和三推等原结构事件始终优先，同一标的同一收线不会再
补发低优先级临界预警。成交量只展示原始量比，不作为临近硬门槛，也不使用评分、
胜率、目标位或止损位。

快速监控每次判断前都会先用新出现的闭合 1H K 线推进该箱体状态。若慢速发现批次
尚未轮到这个标的，但期间已经发生突破、扫盘、假突破或箱体失效，旧箱体不会继续
产生临近预警；同一批闭合数据也不会在下一轮被重新解释为临近。若旧状态已经早于
当前 264 根 1H 历史窗口，发现层不会跨越未知空档，而会丢弃旧轨迹并只用当前完整
窗口重新识别；无法重建时保持失活。

“4H 以上共振”仅指同方向冻结箱体边界重合：上沿预警只比较 4H/1D/1W 上沿，
下沿预警只比较对应下沿；边界差必须同时不超过
`0.35 × min(ATR_1H, ATR_高周期)` 和 `0.50%`。每个高周期只保留最长合格箱体，
消息按 `1W → 1D → 4H` 列出命中项；没有命中时明确写“无（仅1H结构）”。共振只是
可复核的结构事实，不代表更高胜率或必然突破。

## 每日全市场 1D 地图

北京时间 08:00 只是 Binance UTC 日 K 的收线参考点，不是固定推送时刻，也不是
在 08:00 直接把当时缓存的部分标的发出去。雷达为同一个目标日 K 冻结预期合约
全集，按稳定轮转持续累计每个标的的成功、失败和有效结构；只有完成预期全市场
覆盖后，才为该目标日 K 生成一次地图。

按约 520 个活跃合约、每批 40 个、每 5 分钟一批估算，一次完整覆盖约需 70 分钟，
实际生成时间取决于轮转位置、行情响应和失败重试。默认失败重试轮数为 2，最长
等待为 3 小时；达到重试或等待边界时可以生成明确标注“覆盖不完整”的降级地图，
不会伪装成全市场完成。

Telegram 正文默认只展示排序后的前 20 个重点结构，但完整结构、实际覆盖数、失败
数和生成原因都保留在最近 7 份有界日报快照。当天没有任何结构时也会如实报告零
结果。地图在发送前先持久化；发送失败从 5 分钟开始指数退避，最长退避 6 小时。
待投递队列只保留最新日报，较旧日报转入快照供审计，不会在服务恢复后集中补推
已经过时的日报；相同目标日 K 也不会制造第二份日报。

## 推送事件

- `breakout_up` / `breakout_down`：收盘越过冻结边界和 0.10 ATR 缓冲。
- `strong_breakout_up` / `strong_breakout_down`：突破同时达到相对量能门槛。
- `fake_breakout`：向上突破后 3 根内深度收回上沿内侧。
- `fake_breakdown`：向下跌破后 3 根内深度收回下沿上方。
- `retest_up` / `retest_down`：突破后 12 根内回测旧边界并再次收在突破方向。
- `upper_sweep` / `lower_sweep`：影线越界，但收盘仍在箱体内。
- `three_push_top_forming` / `three_push_bottom_forming`：三推顶/底价格结构与
  三个独立 MACD 枢轴已经由右侧闭合 K 线确认，但尚未越过颈线。
- `three_push_top_confirmed` / `three_push_bottom_confirmed`：形成后 12 根内，
  收盘越过冻结颈线及 0.05 ATR 缓冲。

自适应日线的箱体事件名称相同，但卡片会明确写出“结构周期 1D”和实际触发周期。
4H 触发的突破、假突破、回踩或扫盘都是对冻结日线边界的盘中状态；相同名称在
1D 收线触发时才代表日线级别的闭合结果。

箱体形成和自然过期只更新本地状态，不发送 Telegram。相同币种、相同周期、
相同收线若多个期限一起触发，只推送优先级最高的一条：假突破 > 放量突破 >
普通突破 > 回踩 > 扫盘；同级优先长期，其次中期、短期。三推是每个币种和周期
唯一的一条独立结构，不复制到三个箱体期限；同一根 K 线可以同时保留一条箱体
事件和一条三推事件，两者发送状态分别提交。

## Telegram K 线图

每条实际投递的箱体或三推信号都会把原文字作为 Telegram 图片说明，并附带
价格 K 线、成交量和 MACD 图：

- 旧多周期箱体继续使用事件自身周期，标出冻结上沿、下沿以及事件 K 线。
- 自适应 1D 事件最多保留 620 根日 K，可完整显示最长 500 日箱体。
- 由 4H 收线触发的日线边界事件使用 `1D STRUCT / 4H TRIGGER` 语义：底图仍是
  1D 冻结结构，同时在图上单独标出 4H 事件价格与时间，不用 264 根 4H K 线
  冒充数百日结构。
- 1H 临界预警使用 `1H STRUCT / 15m TRIGGER` 或 `1H STRUCT / 1H TRIGGER`：
  底图保留完整冻结箱体，并单独标出触发时间、收盘价和临近方向。
- 三推图在价格区标出 P1/P2/P3，在 MACD 区分别标出三个独立枢轴，并同时显示
  冻结颈线和失效位。价格与 MACD 标记使用各自的实际枢轴时间，不会把价格
  高低点所在 K 线冒充为 MACD 枢轴。

行情只读取已闭合 K 线。跨周期事件若当前缓存尚无完整日线图表上下文，会在同一
受控扫描中取得所需的日线历史；请求、图表渲染、PNG 或 Telegram caption 校验
失败时，该事件直接降级为原文字推送。纯文字真实发送成功后仍按原逻辑提交事件
状态。图表不参与箱体、三推、质量标签或排序计算，也不改变专属话题路由、去重、
Telegram 双门禁及默认关闭/影子运行的生产门禁。

## 三推背离规则

三推默认由独立开关关闭。rule v2 在所有已配置周期上使用标准
`MACD = EMA(close, 12) - EMA(close, 26)`，并只读取已闭合 K 线：

- 价格与 MACD 分别寻找独立枢轴，左右各需 2 根 K 线。每个价格枢轴必须匹配
  前后 2 根范围内一个不同且时间顺序一致的 MACD 枢轴；不再直接读取价格高低点
  所在 K 线的 MACD 数值。
- 三推顶要求最近三个连续价格高点逐次抬高，对应的三个独立 MACD 峰值逐次
  降低且位于零轴上方；三推底完全对称。第三推和 MACD 枢轴均完成右侧确认后
  才能形成，避免重绘和人为挑选更好看的旧枢轴。
- 第一推到第三推最多相隔 96 根；两段回撤/反弹均需至少 0.5 ATR，且每次价格
  推进至少 0.10 ATR，过滤粘连枢轴和一个 tick 的伪结构。
- 两段 MACD 推进都必须存在实质弱化：每一段相对第一推 MACD 绝对值至少弱化
  5%。未形成三个独立 MACD 峰/谷，或任一段未达门槛，都不构成 rule v2 三推。
- 第三推完成后若顶部出现更高高点、底部出现更低低点，旧第三推立即作废并等待
  新枢轴确认；已经形成但尚未确认的结构同样适用，不能继续沿用过期失效位。
- 顶部颈线冻结为第二、三推之间最低点，底部颈线冻结为其间最高点。卡片仍用
  第三推高点加 0.10 ATR 或低点减 0.10 ATR 显示风险参考位，但确认前的新极值
  作废规则优先；12 根内未确认也静默过期。

通过以上硬规则后，三推质量标签只解释结构，不伪装成历史胜率：

- 量能确认：三推成交量逐次减弱。
- 箱体位置：第三推距离冻结箱体对应边缘不超过 0.5 ATR。
- 两项都满足为“强”，只满足一项为“一般”；形成与确认事件均可推送。
- 两项都不满足为“弱”，只更新内部状态和扫描诊断，不写入 Telegram 待推送事件。

箱体和三推卡片都取消未经回测校准的 `/100` 评分。自适应日线箱体使用“强 / 标准 /
观察”标签，并直接列出完整 K 线覆盖、收盘覆盖、上下沿触碰、路径效率和箱宽等
适用依据；三推列出价格推进、MACD 三枢轴、两段 MACD 弱化、量能确认、箱体位置
和颈线状态。原始价格、MACD、成交量和边界数值继续保留，方便人工复核。

标签只描述当前结构通过了哪些规则，不代表历史成功率、胜率或下一步涨跌概率；
在积累足够真实样本并完成统计校准之前，不把启发式权重重新包装成分数。

“强”或“一般”的三推形成与确认各自只推一次。发送失败、Dry-run 或被本轮
条数上限截断时不会消费状态，下次扫描会用相同事件 ID 重放。若第三推右侧确认
完成时收盘已经越过颈线，直接发送“确认”，不会连续制造两条消息。

## 第一次启用

先创建独立话题。此操作会真实调用 Telegram，所以必须显式提供两重确认：

```bash
.venv/bin/python main.py telegram-topic-setup \
  --topic-template TG_CONSOLIDATION_BREAKOUT \
  --send --confirm-real-send
```

命令会创建或复用“盘整突破雷达”话题，并发布、置顶本雷达说明。普通扫描和
普通推送永远不会自动创建话题。新模板使用严格路由：话题 ID 缺失、非数字、
0 或负数都会在任何 Telegram HTTP 请求前阻断，绝不会退回群主界面。

已有话题升级到包含 1H 临界预警的说明时，只刷新本版说明而不新建话题：

```bash
.venv/bin/python main.py telegram-topic-refresh \
  --topic-template TG_CONSOLIDATION_BREAKOUT \
  --send --confirm-real-send
```

也可以人工创建话题后填写数字 ID：

```dotenv
TG_CONSOLIDATION_BREAKOUT_TOPIC_ID=123
```

先保持开关关闭，执行一次安全演练：

```bash
.venv/bin/python main.py consolidation-breakout
```

单次命令允许在总开关关闭时手工演练。没有恰好在最新已闭合 K 线触发的事件时，
只输出扫描诊断，不制造测试信号。Dry-run 不会消费待推送事件；切到真实发送后，
同一事件仍可投递。

确认话题和演练结果后，在 `config/.env.oi` 开启自动调度：

```dotenv
CONSOLIDATION_BREAKOUT_ENABLE=true
CONSOLIDATION_BREAKOUT_THREE_PUSH_ENABLE=true
```

第一个开关启用整个雷达，第二个开关只启用三推背离。主进程会热加载这两个开关。
真实 `live` 模式下，只要新雷达已开启但专属话题未正确
配置，readiness 就会阻止真实运行；开关关闭时不会影响现有雷达。

## 配置

```dotenv
CONSOLIDATION_BREAKOUT_ENABLE=false
CONSOLIDATION_BREAKOUT_INTERVAL_SEC=300
CONSOLIDATION_BREAKOUT_CLOSE_DELAY_SEC=90
CONSOLIDATION_BREAKOUT_SCAN_LIMIT=40
CONSOLIDATION_BREAKOUT_MIN_QUOTE_VOLUME=0
CONSOLIDATION_BREAKOUT_TIMEFRAMES=4h,1d,1w
CONSOLIDATION_BREAKOUT_STRONG_VOLUME_RATIO=1.20
CONSOLIDATION_BREAKOUT_REQUIRE_STRONG_VOLUME=false
CONSOLIDATION_BREAKOUT_THREE_PUSH_ENABLE=false
CONSOLIDATION_BREAKOUT_MAX_SIGNALS_PER_SCAN=8
CONSOLIDATION_BREAKOUT_STATE_FILE=consolidation_breakout_state.json

# 独立1H箱体临界预警；升级时默认关闭并保持影子模式。
CONSOLIDATION_HOURLY_PROXIMITY_ENABLE=false
CONSOLIDATION_HOURLY_PROXIMITY_SHADOW_MODE=true
CONSOLIDATION_HOURLY_PROXIMITY_DISCOVERY_LIMIT=20
CONSOLIDATION_HOURLY_PROXIMITY_MONITOR_LIMIT=20
CONSOLIDATION_HOURLY_PROXIMITY_KLINE_BUDGET=60
CONSOLIDATION_HOURLY_PROXIMITY_MAX_SIGNALS_PER_SCAN=4
CONSOLIDATION_HOURLY_PROXIMITY_STATE_FILE=consolidation_hourly_proximity_state.json

# 自适应日线产品独立安全门禁；升级时默认不运行且保持影子模式。
CONSOLIDATION_DAILY_PRODUCT_ENABLE=false
CONSOLIDATION_DAILY_SHADOW_MODE=true
CONSOLIDATION_DAILY_DIGEST_ENABLE=false
CONSOLIDATION_DAILY_BOUNDARY_EVENTS_ENABLE=false
CONSOLIDATION_DAILY_HISTORY_BARS=620
CONSOLIDATION_DAILY_DIGEST_MAX_ITEMS=20
CONSOLIDATION_DAILY_RETRY_ROUNDS=2
CONSOLIDATION_DAILY_MAX_WAIT_SEC=10800
CONSOLIDATION_DAILY_STATE_FILE=consolidation_daily_product_state.json
CONSOLIDATION_DAILY_DIGEST_STATE_FILE=consolidation_daily_digest_state.json
```

四个日线布尔开关可以通过现有配置管理器安全启停，并会由主进程读取最新文件值：

```bash
.venv/bin/python scripts/paopao_config.py enable CONSOLIDATION_DAILY_PRODUCT_ENABLE
.venv/bin/python scripts/paopao_config.py enable CONSOLIDATION_DAILY_SHADOW_MODE
.venv/bin/python scripts/paopao_config.py disable CONSOLIDATION_DAILY_DIGEST_ENABLE
.venv/bin/python scripts/paopao_config.py disable CONSOLIDATION_DAILY_BOUNDARY_EVENTS_ENABLE
```

1H 临界预警的两个布尔开关也可通过配置管理器安全启停。建议先只观察影子诊断：
自动调度仍由父开关 `CONSOLIDATION_BREAKOUT_ENABLE=true` 驱动；子开关不会单独启动
第二个常驻服务。

```bash
.venv/bin/python scripts/paopao_config.py enable CONSOLIDATION_HOURLY_PROXIMITY_ENABLE
.venv/bin/python scripts/paopao_config.py enable CONSOLIDATION_HOURLY_PROXIMITY_SHADOW_MODE
```

影子模式会更新独立发现、监控和 `shadow_seen` 状态，但不会调用 Telegram；真实
`live_sent` 状态不会被影子观察消费。确认全市场发现游标、活跃监控数量、临近样本、
收线延迟和高周期共振标注正确后，才关闭影子模式：

```bash
.venv/bin/python scripts/paopao_config.py disable CONSOLIDATION_HOURLY_PROXIMITY_SHADOW_MODE
```

建议按以下顺序上线：

1. 先部署安全默认值，不开启任何日线产品开关；确认旧 4H/1D/1W 状态和推送不变。
2. 开启 `PRODUCT_ENABLE`，保持 `SHADOW_MODE=true`，日报和 4H 边界事件继续关闭。
   此时只积累自适应日线状态和诊断，不新增 Telegram 流量；旧日线发送路径仍工作。
3. 至少观察一个完整目标日 K，确认预期、已扫描、成功和失败覆盖数，以及各期限
   结构样本。不要只在北京时间 08:00 查看一次进程输出就判定日报缺失。
4. 保持影子模式，开启 `DIGEST_ENABLE` 和 `BOUNDARY_EVENTS_ENABLE`，核对日报快照、
   4H 监控状态和图表上下文；影子模式下仍不会调用 Telegram 网关发送新产品消息。
5. 验收完成后最后关闭 `SHADOW_MODE`。从这一步起，日报和 4H 边界事件才允许经过
   既有真实发送门禁；启用 4H 边界发送后，自适应日线发送方会接管对应日线箱体
   事件，避免和旧日线路径重复推送。

```bash
.venv/bin/python scripts/paopao_config.py enable CONSOLIDATION_DAILY_DIGEST_ENABLE
.venv/bin/python scripts/paopao_config.py enable CONSOLIDATION_DAILY_BOUNDARY_EVENTS_ENABLE
.venv/bin/python scripts/paopao_config.py disable CONSOLIDATION_DAILY_SHADOW_MODE
```

上述命令均经过字段白名单、布尔校验、文件锁、修改前备份、原子写入、权限检查和
写后校验。生产真实发送仍额外要求 `--send --confirm-real-send`，配置开关本身不能
绕过双门禁。

历史长度、日报条数、重试轮数、最长等待时间、1H 临界预警的批次/预算参数以及
三个新产品状态文件路径刻意不进入运行时配置管理器白名单，避免日常操作误改算法
口径或状态边界。如需调整这些值，必须在发布维护窗口人工审查 `config/.env.oi`、
执行配置校验并重启相关服务。各状态文件彼此独立，也不复用原有
`consolidation_breakout_state.json`。

- `SCAN_LIMIT` 是每批数量，不是全市场候选池上限。默认 40 个标的 × 3 个周期
  = 120 次 K 线请求，恰好处于默认每轮预算内；即使命令行填得更大，实际批量
  也会按 K 线预算和周期数自动收紧。
- `MIN_QUOTE_VOLUME=0` 表示不以 24 小时成交额过滤活跃合约。若人工设置为正数，
  才会恢复成交额下限过滤。
- 升级脚本只会把已知旧默认组合 `900秒 / 24个 / 5000000` 分别迁移为
  `300秒 / 40个 / 0`；任何自定义值都会原样保留。迁移后才符合默认全市场约
  70 分钟覆盖口径，避免旧配置在 3 小时日报等待窗口内扫不完整。
- 一次完整覆盖耗时约为 `向上取整(活跃合约数 / 每批数量) × 调度间隔`。例如约
  520 个合约、每批 40 个、每 5 分钟一批，大约 70 分钟扫完整个市场。
- 若保留了自定义的更小批次或更长调度间隔，必须先按上式核算完整轮转时间，并在
  维护窗口相应调整日报最长等待；否则日报会如实降级为“覆盖不完整”。
- 候选池按代码排序，不受成交额排名变化影响；每轮扫到末尾后，下一轮才从头开始。
- `REQUIRE_STRONG_VOLUME=true` 时，普通突破仍会改变内部结构状态，但只有达到
  量能门槛的突破会推送；假突破、回踩和扫盘不受此开关影响。
- `THREE_PUSH_ENABLE=true` 单独启用三推背离；总雷达关闭时该子功能也不会运行。
  新安装和升级的安全默认均为 `false`。
- 收线延迟用于避开交易所边界附近尚未最终确认的数据；算法还会再次按
  `close_time` 过滤未闭合 K 线。
- 每轮推送上限只限制 Telegram 事件，超出部分不会被标记为已处理，会在后续
  轮次重试。

临时缩小单批扫描数量：

```bash
.venv/bin/python main.py consolidation-breakout \
  --consolidation-scan-limit 5
```

真实单次推送仍必须使用：

```bash
.venv/bin/python main.py consolidation-breakout \
  --send --confirm-real-send
```

## 状态和排障

```bash
.venv/bin/python main.py status
.venv/bin/python main.py radar-status
.venv/bin/python main.py readiness
.venv/bin/python main.py runtime-status
```

四个状态边界彼此独立：

- `data/consolidation_breakout_state.json`：旧 4H/1D/1W 箱体、三推和全市场轮转；
- `data/consolidation_hourly_proximity_state.json`：1H 冻结箱体、发现游标、近边界
  监控、影子观察、真实发送及重新武装状态；
- `data/consolidation_daily_product_state.json`：自适应 1D 箱体、观察快照和 4H 监控；
- `data/consolidation_daily_digest_state.json`：目标日 K 覆盖、最近 7 份完整快照和
  最新一份待投递日报。

轮转游标与信号状态分开提交：当前批次尝试完成后会推进游标，避免单个异常标的或
发送故障卡住整个市场；只有无待发送事件的普通状态，或已经真实发送成功/由
Telegram 历史确认重复的事件状态，才会完成对应事件迁移。发送失败、话题阻断、
全局限流和 Dry-run 都不会消费事件。

4H 边界事件在发送前会先保存冻结的 `pending_event` 和其下一状态。即使下一轮日线
箱体改变或暂时消失，未成功投递的旧事件仍按原事件 ID 重放；进入影子模式时暂停
发送但继续保留它，只有真实发送成功或精确去重确认后才清除。日报也先保存再发送，
失败时保留最新待投递项并按持久化退避重试，不因进程重启丢失，也不会累计多天旧
日报后突然连续补发。

三推状态继续使用旧文件中独立的 `symbol|timeframe|three_push` 键。升级不会清空
已有短/中/长期箱体、三推或轮转游标；自适应日线文件也不覆盖旧文件。

需要立即停止新产品 Telegram 流量时，先恢复影子模式，再关闭日报和边界事件：

```bash
.venv/bin/python scripts/paopao_config.py enable CONSOLIDATION_DAILY_SHADOW_MODE
.venv/bin/python scripts/paopao_config.py disable CONSOLIDATION_DAILY_BOUNDARY_EVENTS_ENABLE
.venv/bin/python scripts/paopao_config.py disable CONSOLIDATION_DAILY_DIGEST_ENABLE
```

需要立即停止 1H 临界预警流量时，先恢复影子模式；如需连识别也停用，再关闭产品：

```bash
.venv/bin/python scripts/paopao_config.py enable CONSOLIDATION_HOURLY_PROXIMITY_SHADOW_MODE
.venv/bin/python scripts/paopao_config.py disable CONSOLIDATION_HOURLY_PROXIMITY_ENABLE
```

如需连自适应识别也停用，再关闭 `CONSOLIDATION_DAILY_PRODUCT_ENABLE`。只回滚三推
则关闭 `CONSOLIDATION_BREAKOUT_THREE_PUSH_ENABLE`；如需停用整个盘整雷达，才关闭
`CONSOLIDATION_BREAKOUT_ENABLE`。这些回滚都不需要删除任何状态文件，也不会影响
其他雷达、公共市场快照或实时行情服务。重新启用后会从最近已提交状态继续。

完整代码回滚仍应使用部署脚本生成的精确备份。Telegram 话题说明属于外部消息，
不会随服务器文件自动回滚。

本模块只提供结构预警，不承诺突破延续，也不构成投资建议。
