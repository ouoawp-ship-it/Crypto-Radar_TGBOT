# OAR P6A 完整扫描历史基线

本阶段为长期 Observe 增加跨扫描历史基线，不改变现有链上事实、行为评分、通知阈值或 Telegram 发送模式。

## 数据边界

只有同一 Token、同一查询窗口并同时满足以下条件的扫描才成为样本：

- Token Activity `complete=true`；
- Behavior Analysis `complete=true`；
- Scan Audit `status=ok`。

Partial、failed、stale 和其他窗口的记录不会污染基线。旧 Schema 中无法证明查询窗口的记录也不会被推断或回填。

## 指标

- Transfer 数；
- Token 总量；
- 独立发送钱包数；
- 独立接收钱包数；
- 确定性行为分；
- 最高钱包关联组分。

每个指标保存当前值、历史中位数、MAD、阈值和是否异常。查询为 1h、4h 或 24h 时，还会分别为其包含的 15m / 1h / 4h / 24h 嵌套窗口建立独立基线；至少两个窗口同时异常时只读字段 `multi_window_anomaly=true`。诊断不保存 Transfer 明细、RPC URL、凭据或外部 Provider 响应。

`multi_window_anomaly` 当前只表示多个时间尺度同时偏离自身历史，不会自动改变行为分、通知阈值或 Telegram 状态。Partial 扫描直接标记 `skipped_incomplete`，不得参与当前异常判断或后续历史样本。

## 冷启动与降级

默认至少需要 8 个完整样本，最多使用最近 64 个样本，MAD 倍数为 3.5。样本不足时状态为 `cold_start` 且 `anomaly=false`。本地基线读取或计算失败时状态为 `local_error`，扫描与 Lease 释放继续进行。

## 配置

```text
OAR_WATCH_BASELINE_MIN_SAMPLES=8
OAR_WATCH_BASELINE_MAX_SAMPLES=64
OAR_WATCH_BASELINE_MAD_MULTIPLIER=3.5
```

范围分别为 4～100、8～100、1～10，且最大样本数不得小于最小样本数。配置继续通过 ConfigManager 原子写入和校验。

## 只读检查

```text
python onchain_main.py watch-baseline --token-key <verified-token-key>
```

该命令不访问 RPC、AI、Telegram、Dune 或 Arkham，也不写数据库。

## 标签覆盖与动态 Watch 安全语义

Token Activity 在缺少可用于方向分类的已审核 CEX 标签时继续保留 Transfer 事实，并将方向保持为 `unclassified`。结构化报告、AI 白名单上下文和中文卡片会明确显示 `insufficient_cex_coverage`；“流入/提出为 0”不再被表达成“确认没有交易所流向”。

动态 Watchlist 已由 Signal Bridge 提供，但只接受主 BOT 中 `status=sent`、`sent=1`、结构化且质量就绪的真实发送信号。主 BOT Dry-run 记录继续被审计为 `ignored_not_sent`，不会创建 Watch 来源、不会触发链上扫描。这一门禁不得为扩大 Watchlist 而放宽。
