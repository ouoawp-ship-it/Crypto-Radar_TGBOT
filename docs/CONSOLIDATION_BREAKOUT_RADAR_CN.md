# 盘整突破雷达 V1

这是 `Crypto-Radar_TGBOT` 内的原生 Binance K 线扫描模块，不依赖
TradingView Webhook，也不会启动新的 Web 服务。它复用项目现有的 Telegram
双重发送门禁、专属话题路由、去重、限流、outbox 和信号账本。

## 它会找什么

默认扫描 Binance USDⓈ-M 24 小时成交额靠前的 24 个 USDT 永续合约，使用
4H、日线和周线已闭合 K 线。每个周期同时检查三种箱体：

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

箱体形成和自然过期只更新本地状态，不发送 Telegram。相同币种、相同周期、
相同收线若多个期限一起触发，只推送优先级最高的一条：假突破 > 放量突破 >
普通突破 > 回踩 > 扫盘；同级优先长期，其次中期、短期。

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
```

主进程会热加载该开关。真实 `live` 模式下，只要新雷达已开启但专属话题未正确
配置，readiness 就会阻止真实运行；开关关闭时不会影响现有雷达。

## 配置

```dotenv
CONSOLIDATION_BREAKOUT_ENABLE=false
CONSOLIDATION_BREAKOUT_INTERVAL_SEC=900
CONSOLIDATION_BREAKOUT_CLOSE_DELAY_SEC=90
CONSOLIDATION_BREAKOUT_SCAN_LIMIT=24
CONSOLIDATION_BREAKOUT_MIN_QUOTE_VOLUME=5000000
CONSOLIDATION_BREAKOUT_TIMEFRAMES=4h,1d,1w
CONSOLIDATION_BREAKOUT_STRONG_VOLUME_RATIO=1.20
CONSOLIDATION_BREAKOUT_REQUIRE_STRONG_VOLUME=false
CONSOLIDATION_BREAKOUT_MAX_SIGNALS_PER_SCAN=8
CONSOLIDATION_BREAKOUT_STATE_FILE=consolidation_breakout_state.json
```

- 扫描上限最大建议保持在 40 以内。默认 24 个标的 × 3 个周期 = 72 次 K 线
  请求，低于默认每轮 120 次预算。
- `REQUIRE_STRONG_VOLUME=true` 时，普通突破仍会改变内部结构状态，但只有达到
  量能门槛的突破会推送；假突破、回踩和扫盘不受此开关影响。
- 收线延迟用于避开交易所边界附近尚未最终确认的数据；算法还会再次按
  `close_time` 过滤未闭合 K 线。
- 每轮推送上限只限制 Telegram 事件，超出部分不会被标记为已处理，会在后续
  轮次重试。

临时缩小扫描范围：

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

状态文件为 `data/consolidation_breakout_state.json`。只有无待发送事件的普通状态
更新，或已经真实发送成功/由 Telegram 历史确认重复的事件，才会提交。发送失败、
话题阻断、全局限流和 Dry-run 都不会消费对应事件。

如需立即停用自动调度，只修改：

```dotenv
CONSOLIDATION_BREAKOUT_ENABLE=false
```

不需要删除状态文件，也不会影响其他雷达、公共市场快照或实时行情服务。重新启用
后会继续使用最近一次已提交的冻结箱体状态。

本模块只提供结构预警，不承诺突破延续，也不构成投资建议。
