# 泡泡抓币 v1.8 结构突破雷达升级报告

## 1. 本次新增功能

- 新增独立的结构突破雷达，不把结构逻辑塞进 `paopao_radar/radar.py`。
- 支持盘整箱体上沿/下沿识别、箱体宽度、箱体内位置、上下沿触碰次数。
- 支持 ATR 百分比、ATR 压缩、Bollinger Band 宽度、BB 压缩、成交量倍数。
- 支持 Binance K线内主动买入比例 `taker_buy_ratio`。
- 支持 Binance OI 历史计算 `oi_change_pct_1h` 和 `oi_change_pct_4h`，OI 缺失时不报错。
- 支持提前临界信号：`PRE_BREAKOUT_NEAR`、`PRE_BREAKDOWN_NEAR`、`SQUEEZE_WATCH`。
- 支持收线确认信号：`BREAKOUT_CONFIRMED`、`BREAKDOWN_CONFIRMED`、`FAKE_BREAKOUT`、`FAKE_BREAKDOWN`、`BACK_INSIDE_BOX`。
- 支持生成 K线状态图 PNG，默认保存到 `data/charts/`。
- 支持 Telegram 图片推送 `TelegramGateway.send_photo()`，仍遵守 dry-run、`--send --confirm-real-send`、话题路由、去重限流。
- 新增结构雷达专属 Telegram 话题说明，真实推送前会尝试发送并置顶。

## 2. 修改文件清单

新增文件：

- `paopao_radar/structure_radar.py`：结构雷达算法、信号对象、状态文件、调度时间计算、推送文本。
- `paopao_radar/charts.py`：K线状态图 PNG 生成。
- `tests/test_structure_radar.py`：结构雷达算法、CLI、调度测试。
- `tests/test_charts.py`：图表 PNG 生成测试。
- `UPGRADE_STRUCTURE_RADAR_V1_8.md`：本升级报告。

修改文件：

- `paopao_radar/config.py`：新增结构雷达配置项、路径、脱敏状态输出、结构话题 ID。
- `paopao_radar/cli.py`：新增 `structure-radar` 和 `structure-loop` 命令、结构雷达 dry-run 报告和图片推送编排。
- `paopao_radar/telegram.py`：新增结构话题路由、结构话题说明、`send_photo()`。
- `.env.oi.example`：新增结构雷达配置模板。
- `requirements.txt`：新增 `matplotlib>=3.8.0`。
- `.gitignore`：忽略 `data/charts/`，避免把运行生成的图表提交到 GitHub。
- `README.md`、`docs/INSTALL_CN.md`：新增 v1.8 结构雷达说明和运行示例。
- `scripts/install_server.sh`：手动话题配置中加入 `STRUCTURE_TOPIC_ID`。
- `scripts/paopao_menu.sh`：新增 `paopao structure` 和 `paopao structure-loop` 快捷命令。
- `scripts/sync_env.py`：保护 `STRUCTURE_TOPIC_ID`，更新时不覆盖用户已填的话题 ID。
- `tests/test_telegram.py`：新增图片 dry-run 推送测试。
- `VERSION`：升级为 `v1.8`。

## 3. 新增配置项

```env
STRUCTURE_RADAR_ENABLE=true
STRUCTURE_TOPIC_ID=
STRUCTURE_INTERVAL=15m
STRUCTURE_HIGHER_INTERVAL=1h
STRUCTURE_BOX_LOOKBACK=36
STRUCTURE_TOP_SYMBOLS=80
STRUCTURE_NEAR_EDGE_PCT=1.5
STRUCTURE_MIN_SCORE=65
STRUCTURE_SEND_CHART_TOP_N=3
STRUCTURE_SAVE_CHARTS=true
STRUCTURE_PRE_SCAN_MINUTE=55
STRUCTURE_CONFIRM_DELAY_SEC=300
STRUCTURE_COOLDOWN_SEC=3600
STRUCTURE_STATE_FILE=structure_state.json
STRUCTURE_HISTORY_FILE=structure_history.json
STRUCTURE_CHART_DIR=charts
```

## 4. 结构雷达算法说明

- K线数据来自 `BinanceDataSource.klines()`，默认周期 `15m`。
- OI 数据来自 `BinanceDataSource.open_interest_hist()`。
- Funding 数据来自 `BinanceDataSource.premium_index()`。
- 时间窗口使用 `time_windows.closed_window()`，不绕过已有闭合窗口逻辑。
- 箱体基于最近 `STRUCTURE_BOX_LOOKBACK` 根已闭合 K线计算：
  - `box_high` 为区间最高价。
  - `box_low` 为区间最低价。
  - `box_mid` 为中点。
  - `box_width_pct` 为箱体宽度占中点价格比例。
  - `position_in_box` 为当前价格在箱体内的位置百分比。
  - `touch_high_count` / `touch_low_count` 用容差判断接近上下沿次数。
- 箱体过宽会降低结构分，避免趋势行情被误判为盘整箱体。

## 5. 提前临界信号说明

提前临界信号用于每小时 55 分附近提醒，不作为最终确认。

- `PRE_BREAKOUT_NEAR`：价格接近箱体上沿，位于箱体上半区，ATR/BB 至少一个压缩，并且成交量或 OI 有活动，主动买入不弱。
- `PRE_BREAKDOWN_NEAR`：价格接近箱体下沿，位于箱体下半区，ATR/BB 至少一个压缩，并且成交量或 OI 有活动，主动卖出不弱。
- `SQUEEZE_WATCH`：价格仍在箱体内，但压缩和活动度达到观察条件。

## 6. 收线确认信号说明

收线确认信号用于整点后延迟 5 分钟，使用完整闭合 K线。

- `BREAKOUT_CONFIRMED`：完整 K线收盘价站上箱体上沿，量能不弱，主动买入不弱。
- `BREAKDOWN_CONFIRMED`：完整 K线收盘价跌破箱体下沿，量能不弱，主动卖出不弱。
- `FAKE_BREAKOUT`：之前出现上沿临界或突破，当前完整 K线收回箱体内。
- `FAKE_BREAKDOWN`：之前出现下沿临界或跌破，当前完整 K线收回箱体内。
- `BACK_INSIDE_BOX`：之前突破或跌破确认后，后续重新回到箱体内。

## 7. K线图说明

K线图文件默认保存到：

```text
data/charts/
```

文件名示例：

```text
structure_PLAYUSDT_15m_20260526_0055.png
```

图片内容包含：

- 简化 OHLC K线。
- 箱体上沿和下沿。
- 当前价格线。
- 成交量柱。
- 标题：币种、周期、信号类型、等级、评分。
- 图中文字：距离上沿/下沿、量能倍数、1h OI 变化。

## 8. Telegram 图片推送说明

- 文本推送仍使用 `TelegramGateway.send()`。
- 图片推送使用新增的 `TelegramGateway.send_photo()`。
- 默认 dry-run，不真实发送。
- 真实发送必须同时提供 `--send --confirm-real-send`。
- 支持 `message_thread_id` 话题路由。
- 支持结构雷达专属话题 `STRUCTURE_TOPIC_ID`。
- 单轮最多发送 `STRUCTURE_SEND_CHART_TOP_N` 张图。
- 图片发送失败不会影响文本推送和主流程。

## 9. 状态文件说明

新增状态文件：

```text
data/structure_state.json
data/structure_history.json
```

用途：

- `structure_state.json`：记录每个币最近一次结构信号、箱体上下沿、最近价格、最近分数、真实推送冷却。
- `structure_history.json`：记录每轮结构扫描结果，用于排查和后续优化。

## 10. 测试结果

已执行：

```bash
python -m unittest discover -s tests
```

结果：

```text
Ran 77 tests
OK
```

覆盖点包括：

- 箱体上沿/下沿识别。
- 接近上沿触发 `PRE_BREAKOUT_NEAR`。
- 接近下沿触发 `PRE_BREAKDOWN_NEAR`。
- ATR 计算。
- BB 宽度计算。
- 成交量倍数。
- OI 缺失不报错。
- S/A/B/C 评分等级。
- 图表 PNG 生成。
- Telegram 图片 dry-run 不真实发送。
- `structure-radar` CLI 不破坏旧命令。
- `structure-loop` 55 分预警和整点后确认时间计算。

## 11. 本地 dry-run 方法

```bash
python main.py structure-radar --mode pre --top-symbols 80 --min-score 65 --save-charts
python main.py structure-radar --mode confirm --top-symbols 80 --min-score 65 --save-charts
```

查看本地报告：

```bash
type data\structure_report.txt
```

查看图片：

```text
data/charts/
```

## 12. 真实推送开启方法

先确认 `.env.oi` 已配置：

```env
TG_BOT_TOKEN=已配置
TG_CHAT_ID=已配置
TELEGRAM_USE_TOPIC=true
STRUCTURE_TOPIC_ID=可选
```

单次真实推送：

```bash
python main.py structure-radar --mode pre --send --confirm-real-send
python main.py structure-radar --mode confirm --send --confirm-real-send
```

独立循环真实推送：

```bash
python main.py structure-loop --send --confirm-real-send
```

## 13. 当前限制

- 本期只做 Binance 公共数据版。
- 未接 CoinGlass 清算热力图。
- 未接 CoinGlass 盘口流动性热力图。
- 未做自动交易。
- 图表是轻量 K线状态图，不是完整交易终端图表。
- 主动买卖方向只使用 Binance K线内 taker buy 字段，不等同于完整订单流。
- 高周期方向只做轻量趋势辅助分，不作为独立交易结论。

## 14. 下一版建议

- 接入 CoinGlass 清算热力图：用于判断突破方向上方/下方是否存在密集清算带。
- 接入 CoinGlass 盘口流动性热力图：用于判断箱体边缘是否有流动性墙或扫单目标。
- 加入突破后的回踩确认状态。
- 加入同币结构信号 Telegram 回复链，类似启动雷达。
- 增加结构信号回测和误报统计。
- 增加更细的箱体质量评分，例如上下沿聚类、趋势斜率过滤、波动率分位数。
