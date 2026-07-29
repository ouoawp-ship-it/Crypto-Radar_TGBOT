# OAR-P3 AI 分析报告与 Telegram 推送

OAR-P3 在 OAR-P1 的 Base ERC-20 查询事实和 OAR-P2 的规则分析之上增加报告层。它不改变 `token-activity`、`token-analysis`、Base Live Collector、链上数据库 Schema 或主 BOT。

## 命令边界

纯报告命令：

```bash
python onchain_main.py token-report \
  --chain base \
  --contract 0x... \
  --window 24h \
  --allow-network \
  --pretty
```

该命令生成确定性中文规则摘要和 AI Context；只有同时使用 `--with-ai` 且 `OAR_AI_ENABLE=true` 时才创建 AI 客户端。它永远不创建 Telegram 网关，不写 Telegram history/outbox，也不写信号库。

通知命令：

```bash
python onchain_main.py token-notify \
  --chain base \
  --contract 0x... \
  --window 24h \
  --allow-network
```

无 `--send` 时仅通过现有 `TelegramGateway` 记录链上独立 dry-run 审计。真实发送必须同时满足：

1. `ONCHAIN_REAL_SEND=true`
2. `--send`
3. `--confirm-real-send`
4. Telegram Bot、Chat 和 `TG_ONCHAIN_FLOW_TOPIC_ID` 配置完整

`token-report` 和 `token-notify` 复用 OAR-P1 的 `--max-events`、`--max-rpc-requests`、`--top`、`--with-price`、`--min-usd`、`--pretty` 与安全无覆盖 `--output-file`。

## AI Context

`schema_version=1`，只包含以下白名单事实：

- Token、chain ID、合约、查询窗口和完整性
- Transfer 汇总和最多 20 条代表性 Transfer
- CEX 流入、提出与净流向
- Primary Behavior、Behavior Candidates
- 最多 10 个 Wallet Group，每组最多 20 个钱包
- 支持证据、反证和数据限制

输入按稳定顺序去重，`context_hash` 使用 canonical JSON 的 SHA-256。全部 Transfer、RPC URL、API Key、Telegram Token、私有标签路径和 Provider 原始异常体不会进入 Context。Token 名称、Symbol、地址和标签均被视为不可信数据，不能覆盖 System Instruction。

## 规则摘要兜底

规则摘要不依赖 AI。AI 关闭、超时、限流、额度不足、非法 JSON 或缓存失败时，报告仍保留：

- 查询范围与完整性
- Transfer 和钱包数量
- 流入交易所、从交易所提出和净流向
- Behavior 类型、规则分数、支持证据和反证
- Wallet Group 候选
- 代表性交易链接与数据限制

报告明确声明：

- 入所不等于已经卖出
- 提币不等于已经买入或必然上涨
- 钱包关联分数不是概率
- 高分不等于确认同一主力
- Partial 输入不能形成高确定性判断

## 可选 AI

安全默认配置：

```dotenv
OAR_AI_ENABLE=false
OAR_AI_BASE_URL=
OAR_AI_API_KEY=
OAR_AI_MODEL=
OAR_AI_TIMEOUT_SEC=20
OAR_AI_MAX_RETRIES=1
OAR_AI_MAX_CALLS_PER_HOUR=10
OAR_AI_CACHE_TTL_SEC=3600
OAR_AI_MAX_CONTEXT_CHARS=30000
OAR_AI_MAX_OUTPUT_CHARS=8000
OAR_AI_CACHE_FILE=oar_ai_cache.json
```

适配器使用 OpenAI-compatible `chat/completions`，有限超时和重试，拒绝 HTTP Redirect，并对 401/403、429、5xx、timeout 和连接失败分类。API Key 只进入 Authorization Header，不进入日志、JSON、异常或缓存。

AI 必须返回严格 JSON，字段为 `bias`、`confidence`、主要/备选假设、可能动作、观察信号、失效条件和风险提示。未知字段、Markdown 代码块、价格目标、杠杆建议、自动交易指令、确定性钱包身份，以及“入所已经卖出/提币已经买入”等表述会被拒绝。

当查询不完整，或分析为 `partial_input`、`partial_analysis`、`insufficient_evidence` 时，AI 只能输出 `neutral|uncertain` 和 `low` confidence。

有效 AI 结果可缓存于 `data/onchain/oar_ai_cache.json`。缓存只保存 context hash、model、验证后结果和过期时间，不保存 Prompt、Key 或 Header。小时调用预算独立于 RPC 预算。

## 独立 Telegram 话题

用户可见话题名称为“链上活动雷达”，内部 Template ID 继续使用 `TG_ONCHAIN_FLOW_ALERT`。实现复用：

- `TelegramGateway`
- `TG_ONCHAIN_FLOW_TOPIC_ID`
- 链上独立 push history、outbox 和 topic routes
- cooldown、小时限额、delivery ID 和多 chunk 发送
- topic intro 和双发送门禁

没有第二个 Bot，也没有直接调用 Telegram HTTP 的新发送器。

## 最新状态卡片

卡片身份：

```text
card_key = oar:{chain_id}:{contract}:{window}
dedup_key = {card_key}:{content_hash 前 16 位}
```

生命周期：

1. 相同内容由现有 cooldown 去重。
2. 新内容先完整发送。
3. 发送成功后只删除同 `card_key` 的旧消息。
4. 新消息失败或部分发送时回滚新消息的已发送部分，并保留旧卡片。
5. 旧消息删除失败会记录失败 ID；旧记录保持活跃，后续同卡片更新会重试。
6. 不删除其他 Token、其他窗口或 Topic Intro。

默认 `OAR_REPLACE_COMPLETE_CARD_WITH_PARTIAL=false`。已有完整卡片时，新的 Partial 结果不会进行真实替换。

## SignalEvent 摘要

`token-notify` 通过 `TelegramGateway.signal_records` 写入独立的 `data/onchain/onchain_signals.db`，记录：

- `module=onchain`
- chain、contract、symbol
- behavior 类型/标签/规则分数
- analysis 状态和完整性
- context/content hash
- AI 状态
- `source=manual_token_notify`

不会写主 BOT 的 `data/signals.db`，也没有新增数据库 Migration。

## 明确不做

OAR-P3 不实现自动 Watchlist、Token Registry、其他话题联动、定时扫描、长期状态机、AI 自动交易、真实钱包身份推断、Arkham 或新的基础设施。上述跨模块候选池属于后续 OAR-P4。
