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

## 链上与市场共振诊断

Watch 会把已存在的动态来源与本轮链上规则、历史基线组合成只读 `market_convergence`：

- `no_market_context`：没有已验证的主 BOT sent 来源；
- `market_context_only`：只有市场来源，链上规则门禁未满足；
- `onchain_market_cooccurrence`：市场来源与链上规则候选同时出现；
- `historical_anomaly_cooccurrence`：市场来源与单窗口历史异常同时出现；
- `multi_window_anomaly_cooccurrence`：市场来源、链上规则候选和多窗口历史异常同时出现。

共振分是确定性共现规则分，不是概率。由于现有主信号尚未提供统一的结构化方向字段，`direction_alignment=not_evaluated`；系统不得从摘要文字猜测多空方向。第一阶段 `notification_gate_changed=false`，不会因为共振诊断自动发送。

## 受控自动预警预演

Watch 同时输出只读 `controlled_alert_preview`。它要求扫描完整、现有链上规则门禁已通过、历史基线成熟且本轮异常、并且存在受支持的市场来源，才会返回 `would_alert=true`。多窗口同时异常时预演等级为 `high`，否则为 `medium`。

未满足条件时，`block_reasons` 只使用固定错误码，例如 `historical_baseline_not_ready`、`historical_anomaly_not_observed` 或 `market_context_not_present`。该字段始终标记 `dry_run_only=true`、`notification_gate_changed=false`、`telegram_calls=0`，不会创建 Telegram 客户端、不会修改现有通知门禁，也不会产生持久消息。

## 谨慎 AI 输入门禁

AI 仍只在显式请求且 `OAR_AI_ENABLE=true` 时调用。查询或分析不完整、行为证据不足、CEX 标签覆盖不足，或关联市场信号尚无统一结构化方向时，报告会标记 `restricted_input=true` 并给出固定 `restriction_reasons`。此时 AI 输出契约只接受 `neutral/uncertain + low`；规则摘要不受 AI 成败影响。Prompt、Context、凭据和 Provider 原始错误均不进入这些诊断字段。

## 多链 EVM 能力门禁

`python onchain_main.py chain-readiness` 只读解析版本化链配置并输出脱敏能力真值表。它不连接 RPC、不创建数据库，也不调用 AI 或 Telegram。只有已经实现运行适配器、显式启用、RPC 已配置且 URL 结构合法的链，才会显示 `token_activity_supported=true` 和 `watch_supported=true`。

Token Activity 已使用通用 EVM 查询适配器：链注册表必须提供唯一 Chain ID、slug、确认深度、回看范围、RPC 环境变量名和 Explorer 模板；对应专用 RPC 配置合法后，显式手工查询才会显示 `token_activity_supported=true`。RPC 值和 Host 不进入能力报告。

长期 Registry / Watch 运行适配器当前仍只有 Base。把其他 EVM 链写入配置文件不会自动进入长期监控；显式启用时会返回 `watch_adapter_not_implemented` 和 `activation_blocked=true`。新增 Watch 链仍须分别完成标签命名空间、Registry 验证、重组策略、预算、恢复点和灰度验收，不能复用 Base 结论冒充已支持。
