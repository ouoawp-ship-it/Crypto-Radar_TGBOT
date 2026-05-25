# 当前实现状态

更新时间：2026-05-25

## 已完成

1. 新工程骨架

- `main.py`
- `config.py`
- `storage.py`
- `data_sources.py`
- `telegram.py`
- `radar.py`

旧 `crypto_monitor_merged.py` 保留为参考文件，没有继续在上面堆功能。

2. 安全默认行为

- `python main.py` 默认执行 `status`。
- 默认 dry-run，不真实发送 Telegram。
- 真实发送必须同时带 `--send --confirm-real-send`。
- 没有自动 cleanup。

3. 统一基础设施

- 配置读取集中到 `config.py`。
- 敏感值只显示是否配置，不打印真实值。
- JSON 状态写入集中到 `storage.py`，使用原子写。
- Binance 请求集中到 `data_sources.py`。
- 请求层支持 timeout、retry、backoff、cache、预算、熔断。
- Telegram 发送集中到 `telegram.py`。
- Telegram 支持 dry-run、真实发送确认、分片、去重、全局小时限流、摘要日限流。

4. 资金雷达摘要

已实现模块：

- 负费率榜
- 综合榜
- 埋伏池
- 动量池
- 新币池
- 背离雷达
- 值得关注
- 数据质量/请求预算输出
- 背离状态追踪：首次出现、持续第 N 次、信号增强、信号减弱、重新出现、消失失效。

已加入保护：

- `openInterestHist` 请求预算。
- K线请求预算。
- `RADAR_SCAN_LIMIT=0` 表示禁用扫描，不再表示全量扫描。
- 资金雷达候选数量不会超过 OI/K线预算。
- 默认过滤 `XAU/XAG` 这类非加密标的，可通过 `EXCLUDED_BASE_ASSETS` 调整。

5. 启动雷达

已实现：

- 15m / 1h 价格变化。
- 15m / 1h OI 变化。
- 成交量放大倍数。
- 突破近 4h 高点判断。
- 简单分数。
- 状态文件 `data/launch_state.json`。
- 观察文件 `data/launch_watchlist.json`，保存每轮启动候选分数，方便后续调参。
- 历史文件 `data/launch_watch_history.json`，滚动保存最近若干轮启动观察。
- 状态阶段：`idle / watching / primed / breakout / launched / failed`。
- 启动阶段阈值已配置化：`watching / primed / breakout / launched`。
- 同币同阶段冷却。
- 只有真实 Telegram 发送成功后，才记录 `last_pushed`。
- 启动状态会按 TTL 清理，避免长期运行后状态文件无限增长。

6. 公告机会和风险扫描

已实现：

- Binance 公告 API。
- 机会公告识别：Alpha、上新、HODLer、Launchpool、Airdrop 等。
- 风险公告识别：下架、移除、停止交易等。
- 过滤期货合约上线、TradFi、Pre-IPO 等噪音公告。
- 多币公告会汇总 symbol 列表，不拆成大量消息。
- 过滤 `Ghibli (SOL)` 这类链名括号，避免把 `SOL/BSC` 误判为风险币。
- dry-run 不会把公告标记为已发送；只有真实发送成功后才写入 `announcement_state.json`。

7. 命令行启动方式

Windows 一键启动脚本已移除，后续统一通过命令行运行，方便上传 GitHub 后部署到服务器。

常用命令：

```bash
python main.py status
python main.py readiness
python main.py observe --duration-minutes 360 --launch-interval 180 --launch-scan-limit 40 --records 200 --top 12
python main.py telegram-test --send --confirm-real-send
python main.py live --send --confirm-real-send
```

8. 基础测试和忽略规则

- `tests/test_storage.py`
- `tests/test_telegram.py`
- `tests/test_main_commands.py`
- `tests/test_radar_logic.py`
- `tests/test_maintenance.py`
- `tests/test_launch_report.py`
- `.gitignore`

已覆盖：JSON 状态写入/损坏恢复、Telegram dry-run/真实发送确认/摘要日限流、公告多币解析、启动阶段分档、负费率评分、非加密标的过滤、启动历史分析、旧状态迁移。

9. 维护命令

- `python main.py doctor`：检查环境、状态文件、旧状态文件。
- `python main.py readiness`：检查真实推送准备度。
- `python main.py telegram-test`：测试 Telegram，默认 dry-run，真实发送必须 `--send --confirm-real-send`。
- `python main.py live`：通过 readiness 门禁后进入真实推送循环，仍必须 `--send --confirm-real-send`。
- `python main.py watchlist`：查看最近一轮启动候选排名和分数。
- `python main.py launch-history`：查看最近多轮启动观察历史。
- `python main.py launch-report`：汇总启动历史，输出阶段统计和调参建议。
- `python main.py trial`：有限轮数试跑启动雷达，用于观察噪音和调参。
- `python main.py observe`：固定时长 dry-run 观察，强制不真实发送，每轮刷新报告。
- `python main.py migrate-state`：预览旧状态迁移。
- `python main.py migrate-state --apply`：复制旧状态到 `data/`，不删除源文件、不覆盖已有目标。

已执行迁移：

- `bn_signal_history.json` 已复制到 `data/bn_signal_history.json`。
- 根目录旧文件仍保留为备份。

10. 长期运行保护

- `loop/daemon` 中单轮摘要或启动扫描异常不会直接打掉整个进程。
- 启动雷达 loop 分支也使用同样的同阶段冷却。
- 启动雷达阈值、状态 TTL、失败 TTL 都可以通过 `.env.oi` 调整。
- 启动观察历史有滚动上限，默认保留 500 轮。
- `observe` 模式会忽略真实发送参数，避免误触发 Telegram。
- `observe` 每轮都会刷新报告，中断或单轮失败时也尽量保留最新观察结果。
- `telegram-test` 可以在开真实循环前验证 Telegram 配置。
- `live` 和真实发送版 `once/loop/daemon` 都会先经过 readiness 门禁。

## 已验证

1. 编译检查

```bash
python -m py_compile main.py config.py storage.py data_sources.py telegram.py radar.py maintenance.py
```

结果：通过。

2. 状态检查

```bash
python main.py status
```

结果：通过，敏感值已隐藏。

3. 小预算资金雷达 dry-run

```bash
RADAR_SCAN_LIMIT=2
OI_HIST_REQUEST_BUDGET=2
KLINE_REQUEST_BUDGET=2
python main.py once --no-launch --no-announcements
```

结果：通过，不真实发送。

4. 只扫公告 dry-run

```bash
RADAR_SCAN_LIMIT=0
OI_HIST_REQUEST_BUDGET=0
KLINE_REQUEST_BUDGET=0
python main.py once --no-launch
```

结果：通过，只触发公告 dry-run，不触发 OI/K线扫描。

5. 小预算启动雷达 dry-run

```bash
RADAR_SCAN_LIMIT=2
LAUNCH_SCAN_LIMIT=2
OI_HIST_REQUEST_BUDGET=2
KLINE_REQUEST_BUDGET=2
python main.py once --no-announcements
```

结果：通过，不真实发送。

6. 公告多币解析样例

```bash
python -c "from radar import RadarEngine; print(RadarEngine._extract_symbols('Binance Will List Genius Terminal (GENIUS) and OpenGradient (OPG) with Seed Tag Applied'))"
```

结果：通过，能解析 `GENIUS, OPG`；链名括号不会误报 `SOL/BSC`。

7. 单元测试

```bash
python -m unittest discover -s tests -v
```

结果：23 条通过。

8. 真实推送准备度检查

```bash
python main.py readiness
```

结果：通过，当前准备度 `5/5`，下一步建议先跑 Telegram 测试消息。

9. Telegram 测试消息 dry-run

```bash
python main.py telegram-test
```

结果：通过，默认 dry-run，不真实发送。

10. live 门禁检查

```bash
python main.py live
```

结果：通过，未带 `--send --confirm-real-send` 时会阻止真实推送。

11. 启动候选观察表

```bash
python main.py watchlist --top 5
```

结果：通过，能读取 `data/launch_watchlist.json` 并输出候选分数。

12. 启动观察历史

```bash
python main.py launch-history --top 5
```

结果：通过，能读取 `data/launch_watch_history.json` 并输出最近轮次摘要。

13. 启动历史分析

```bash
python main.py launch-report --records 20 --top 8
```

结果：通过，能输出最高分、阶段合计、高频候选和调参建议。

14. 有限启动雷达试跑

```bash
python main.py trial --cycles 1
```

结果：通过，运行后自动退出，不会长期占用终端。

14. 固定时长 dry-run 观察

```bash
python main.py observe --duration-minutes 0 --launch-interval 60 --records 20 --top 5
```

结果：通过，强制 dry-run，每轮刷新并保存 `data/launch_observe_report.txt`。

15. 旧状态迁移预览和应用

```bash
python main.py migrate-state
python main.py migrate-state --apply
```

结果：已复制 `bn_signal_history.json` 到 `data/bn_signal_history.json`，没有删除源文件。

## 下一步建议

1. 长时间小额度试跑

建议先用 dry-run 跑半天，观察：

- 启动雷达是否过于敏感。
- 公告噪音是否还需要继续过滤。
- 背离状态是否能稳定显示持续/增强/消失。

2. 调参后再开真实 Telegram

先确认 dry-run 的信号质量，再使用 `--send --confirm-real-send`。
## 2026-05-25 final handoff additions

- Added `data/runtime_status.json` heartbeat support.
- Added `python main.py runtime-status`.
- `once`, `trial`, `observe`, `loop`, and `live` now write runtime status, recent errors, recent push status, and next scan time where applicable.
- Added `RUNTIME_STATUS_FILE=runtime_status.json` to `.env.oi.example`.
- Added `FINAL_RUNBOOK.txt` as the clean operational handoff note.
- Current test count: 26 passing.
