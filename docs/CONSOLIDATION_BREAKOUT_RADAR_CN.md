# 盘整突破雷达 V2.4.2

这是 `Crypto-Radar_TGBOT` 内的原生 Binance K 线扫描模块，不依赖
TradingView Webhook，也不会启动新的 Web 服务。它复用项目现有的 Telegram
双重发送门禁、专属话题路由、去重、限流、outbox 和信号账本。

## 它会找什么

默认候选池覆盖 Binance USDⓈ-M 全部活跃 USDT 永续合约，不再按 24 小时
成交额淘汰长尾标的。雷达按合约代码稳定轮转，每批扫描 40 个标的的 4H、
日线和周线已闭合 K 线；轮转游标持久化，进程重启后会从上一批继续。
每个周期同时检查三种箱体：

- 短期：24 根 K 线。
- 中期：72 根 K 线。
- 长期：240 根 K 线，不设箱体总寿命上限。

因此，日线长期箱体可以识别约 240 天的盘整；周线可以观察更大级别结构。
三个期限使用不同的箱宽、ATR 和路径效率阈值，不会拿短线的窄箱标准硬套到
数百天结构上。

箱体必须同时满足：

- 上下沿各至少两个分离的触碰簇，连续贴边只算一次；
- 箱宽同时通过 ATR 和百分比上限；
- 收盘路径效率足够低，排除单边趋势伪装成横盘；
- 多个确认窗口内上下沿漂移不超过 0.35 ATR；
- 突破前冻结上下沿，之后不会因新高、新低移动边界。

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

箱体形成和自然过期只更新本地状态，不发送 Telegram。相同币种、相同周期、
相同收线若多个期限一起触发，只推送优先级最高的一条：假突破 > 放量突破 >
普通突破 > 回踩 > 扫盘；同级优先长期，其次中期、短期。三推是每个币种和周期
唯一的一条独立结构，不复制到三个箱体期限；同一根 K 线可以同时保留一条箱体
事件和一条三推事件，两者发送状态分别提交。

## Telegram K 线图

每条实际投递的箱体或三推信号都会把原文字作为 Telegram 图片说明，
并附带同信号周期的价格 K 线、成交量和 MACD 图：

- 箱体突破、假突破/假跌破、回踩和扫盘图标出冻结上沿、下沿以及事件 K 线。
- 三推图在价格区标出 P1/P2/P3，在 MACD 区分别标出三个独立枢轴，
  并同时显示冻结颈线和失效位。价格与 MACD 标记使用各自的实际枢轴时间，
  不会把价格高低点所在 K 线冒充为 MACD 枢轴。

图表复用雷达为判定信号已经取得、并通过 `close_time` 过滤的闭合 K 线，
不额外发起 Binance K 线请求。图表渲染失败、PNG 或 Telegram caption
校验失败时，该事件直接降级为原文字推送；纯文字真实发送成功后仍按原逻辑
提交事件状态。图表不参与箱体、三推、质量标签或排序计算，也不改变
专属话题路由、去重、Telegram 双门禁及两个默认关闭的生产开关。

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

通过以上硬规则后，质量标签只解释结构，不伪装成历史胜率：

- 量能确认：三推成交量逐次减弱。
- 箱体位置：第三推距离冻结箱体对应边缘不超过 0.5 ATR。
- 两项都满足为“强”，只满足一项为“一般”；形成与确认事件均可推送。
- 两项都不满足为“弱”，只更新内部状态和扫描诊断，不写入 Telegram 待推送事件。

三推卡片取消未经回测校准的 `/100` 评分，改为直接列明价格推进、MACD 三枢轴、
两段 MACD 弱化、量能确认、箱体位置和颈线状态，同时保留原始价格、MACD 与成交量
数值供人工复核。这个变化只作用于三推事件；原有箱体突破、假突破、回踩和扫盘
的评分及筛选逻辑保持不变。

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

已有话题升级到 V2.4.2 时，只刷新本版图表说明而不新建话题：

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
```

- `SCAN_LIMIT` 是每批数量，不是全市场候选池上限。默认 40 个标的 × 3 个周期
  = 120 次 K 线请求，恰好处于默认每轮预算内；即使命令行填得更大，实际批量
  也会按 K 线预算和周期数自动收紧。
- `MIN_QUOTE_VOLUME=0` 表示不以 24 小时成交额过滤活跃合约。若人工设置为正数，
  才会恢复成交额下限过滤。
- 一次完整覆盖耗时约为 `向上取整(活跃合约数 / 每批数量) × 调度间隔`。例如约
  520 个合约、每批 40 个、每 5 分钟一批，大约 70 分钟扫完整个市场。
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

状态文件为 `data/consolidation_breakout_state.json`。轮转游标与信号状态分开提交：
当前批次尝试完成后会推进游标，避免单个异常标的或发送故障卡住整个市场；只有无
待发送事件的普通箱体状态，或已经真实发送成功/由 Telegram 历史确认重复的事件
状态，才会提交。发送失败、话题阻断、全局限流和 Dry-run 都不会消费对应事件；
该标的下一轮再次覆盖时仍可重放。

三推状态继续使用同一 schema 1 文件中的独立 `symbol|timeframe|three_push` 键，
轨迹规则版本升级为 rule v2。升级不会清空已有短/中/长期箱体或轮转游标；旧规则
尚未完成的三推上下文会被安全丢弃，等待新规则重新确认。需要只回滚新功能时设置：

```dotenv
CONSOLIDATION_BREAKOUT_THREE_PUSH_ENABLE=false
```

无需删除状态文件；旧版本会忽略新三推键。完整代码回滚仍应使用部署脚本生成的
精确备份。Telegram 话题说明属于外部消息，不会随服务器文件自动回滚。

如需立即停用自动调度，只修改：

```dotenv
CONSOLIDATION_BREAKOUT_ENABLE=false
```

不需要删除状态文件，也不会影响其他雷达、公共市场快照或实时行情服务。重新启用
后会继续使用最近一次已提交的冻结箱体状态。

本模块只提供结构预警，不承诺突破延续，也不构成投资建议。
