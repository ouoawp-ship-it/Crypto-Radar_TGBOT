# 实时异常情报方法

## 数据边界

实时异常情报只使用泡泡雷达自行采集并持久化的 Binance、Bybit 与 OKX 线性 USDT 永续合约公开 WebSocket 数值事实。每个分钟桶包含主动买入额、主动卖出额、CVD、成交次数、OHLC 和可公开获得的多空强平额。未完成分钟不参与计算；缺失窗口不补零，不用 REST 指标冒充实时指标。

Bybit 使用官方 `publicTrade.{symbol}` 与 `allLiquidation.{symbol}`；OKX 使用公开 `trades`，并以公开 instruments 元数据中的 `ctVal/ctValCcy` 把合约张数换算为名义金额。OKX 的 `liquidation-warning` 是需要认证的账户私有风险提示，不是全市场强平事实，因此不接入。多交易所合流时成交/CVD 相加，窗口覆盖按唯一时间桶计算；价格固定优先使用 Binance，缺失时才选择另一个单一交易所，禁止把不同交易所的开收盘拼接成涨跌幅。

官方协议：

- Bybit Trade：<https://bybit-exchange.github.io/docs/v5/websocket/public/trade>
- Bybit All Liquidation：<https://bybit-exchange.github.io/docs/v5/websocket/public/all-liquidation>
- OKX V5 WebSocket Trades：<https://www.okx.com/docs-v5/en/#order-book-trading-market-data-ws-trades-channel>

## Surge 加速

Surge 比较相邻两个封闭 5 分钟窗口：

- CVD 占成交额比例的变化，表示主动资金方向加速度；
- 当前与上一窗口成交额的变化，表示活动速度；
- 当前窗口价格变化，作为方向确认；
- 空头与多头强平差，作为辅助证据。

规则分只用于同口径排序。达到分数、资金加速度和当前资金方向三重门槛后才触发，不能解释为未来收益概率。

## 短周期潜伏

短周期潜伏寻找“资金先行、价格尚未充分移动”的候选：5m 与 15m CVD 占比必须同向且超过最低幅度，15m 价格保持压缩，同时当前不能已经进入 Surge。该规则是泡泡雷达自有版本，不依赖或复制第三方 AI 结论。

## 方向共振与排名

5m、15m、1h 每个窗口先独立判断 CVD 方向，并要求价格没有明显反向；至少两个可用窗口同向才确认共振。排名分为：

- 自身排名：当前 5m CVD 强度在该币近 24 小时非重叠 5m 样本中的经验分位；
- 市场强度：同一封闭时点的 Surge 规则分横截面排名；
- 绝对规模：同一封闭时点的合约成交额排名。

所有排名至少需要两个同口径样本。

## 生命周期

生命周期使用 NEW、增强、持续、降温、重启、失效和未触发状态。判断依据来自当前是否触发、过去一小时最近触发时间及规则分变化，响应中始终返回可读 `basis`，不把生命周期等同于价格结果。

## 离线结果统计

离线统计在历史封闭 5m 时点重放同一 Surge 规则，再读取该时点之后 5m、15m、1h 的封闭分钟价格。方向收益为多头原始收益、空头取反后的收益。样本少于 30 时状态为 `insufficient`；统计不含手续费、滑点、资金费率和成交约束，因此不构成交易建议。
