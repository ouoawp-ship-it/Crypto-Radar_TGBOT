# 山寨合约异动雷达 P1 运维说明

P1 只提供一次性候选池扫描，内部标识为 `altcoin_contract_anomaly`。它不会加入
主 BOT 定时循环，不会创建 systemd 服务，不修改 Binance WebSocket 订阅，也不会
发送 Telegram 消息。Telegram 能力仅限生成可检查的分页预览文本。

## 准备配置

复制配置示例后，只在本机真实配置 `config/.env.oi` 中填写 CMC Key：

```dotenv
ALTCOIN_CONTRACT_ANOMALY_CMC_API_KEY=
```

不要把 Key 放在命令参数、日志、截图或 Git 中。`doctor` 和配置诊断只报告 Key
是否已配置，不显示其内容。

`ALTCOIN_CONTRACT_ANOMALY_ENABLE=false` 是总开关和安全默认值。执行在线一次性扫描前
必须显式改为 `true`；关闭时在线命令返回配置错误码 `2`，且不会发出 Binance 或 CMC
请求。`--cache-only` 只读复核不受开关影响，也不会访问网络。

主要默认配置如下：

- CMC 连接/读取超时：5 秒 / 15 秒；有限重试 2 次；指数退避基数 0.5 秒；每批最多 100 个 CMC ID；成功批次和重试均默认至少间隔 2 秒。
- CMC 缓存 TTL：300 秒；市值最大可接受年龄：900 秒。
- 潜在逼空：市值不高于 3000 万美元、Binance OI/市值不低于 0.20、费率严格小于可配置阈值（默认 0）。
- 高杠杆候选：Binance OI/市值不低于 0.50。
- Binance OI 与资金费率最大年龄：均为 600 秒，覆盖 5 分钟 OI 桶的正常到达延迟。
- OI 并发：8；单轮请求预算：600。
- Telegram 预览单页上限：3800 字符。

OI 原始字段来自 Binance `sumOpenInterest`，快照单位记为
`contract_base_asset_quantity`；美元值优先直接使用 Binance
`sumOpenInterestValue`。只有缺少美元值且原始单位明确时才使用标记价换算，
`1000PEPE` 一类合约不会再次乘除 1000。

快照同时保留兼容字段 `oi_value_usd` / `oi_market_cap_ratio` 和明确口径字段
`binance_oi_usd` / `binance_oi_market_cap_ratio`；P1 两组值必须一致。全市场字段
`global_oi_usd`、`global_oi_market_cap_ratio`、`global_oi_source` 在 P1 固定为 `null`，
不得写入 CoinGlass 或其他聚合口径。

业务层的比例统一使用小数：`0.20` 表示 20%，`0.50` 表示 50%。只有展示层会
转换为百分比。预览中的“潜在狗庄候选”只是高合约杠杆标签，不是对操纵主体
或操纵事实的认定。

## 一次性命令

默认输出中文概览并原子更新独立候选池快照：

```bash
python main.py altcoin-anomaly
```

机器可读 JSON：

```bash
python main.py altcoin-anomaly --json
```

只使用本地缓存、不发出网络请求：

```bash
python main.py altcoin-anomaly --cache-only
```

同时生成 Telegram 分页预览，但不调用 Telegram API：

```bash
python main.py altcoin-anomaly --preview-telegram
```

另存一份机器可读结果：

```bash
python main.py altcoin-anomaly --output data/altcoin-anomaly-export.json
```

`--json` 与 `--preview-telegram` 可以组合；分页预览会作为结构化结果的一部分，
不会走现有 Telegram 网关，也不会检查、创建或修改话题。

## 退出码

- `0`：成功生成本轮结果；候选数量可以为 0。
- `1`：内部处理或持久化失败。
- `2`：配置错误，例如在线扫描未配置 CMC Key。
- `3`：网络数据与允许年龄内的缓存都不可用，无法生成可信快照。
- `130`：人工中断。

出现 `2` 或 `3` 时不得把旧数据伪装为本轮成功结果。先检查中文错误摘要，再检查
CMC Key 是否配置、缓存年龄以及 Binance/CMC 公共接口状态；不要把响应 Header、
完整响应体或 Key 粘贴到工单中。

## 人工 CMC-ID 覆盖表

覆盖表默认位于 `config/altcoin_contract_anomaly_overrides.json`，只允许保存公开
标识和人工审计说明。文件结构为：

```json
{
  "schema_version": 1,
  "overrides": [
    {
      "binance_symbol": "1000PEPEUSDT",
      "cmc_id": 24478,
      "normalized_asset": "PEPE",
      "token_address": "",
      "note": "人工核对记录"
    }
  ]
}
```

新增覆盖前必须在 CMC 官方页面核对数字 ID。`token_address` 仅在已确认时填写；
空值表示没有地址证据，猜测地址不得记录为地址匹配。重复合约、无效 ID、错误
Schema 或基础资产不一致会被拒绝。仅 Symbol 相同、按排名选择或模糊字符串匹配
只能作为诊断，不得进入正式候选池。

正式映射证据只有三类：`verified_override`（人工确认）、
`contract_address_match`（地址唯一一致）和 `existing_verified_anchor`（Binance
公开 `cmcUniqueId` 经 CMC active map 与规范化资产复核）。
`unique_symbol_diagnostic` 仅用于覆盖诊断；同名多项目记为 `ambiguous`，没有证据
记为 `unmapped`。每条记录都会保存 CMC ID、名称、Symbol、slug、地址、证据和
核验时间，便于复查。

CMC map 每页最多 5000 条并有限分页，只有完整目录才写入缓存；quotes 只按数字
CMC ID 请求，每批最多 100 个。缓存记录生成时间、上游数据时间和过期时间，
正常请求也执行主动限流；429 会遵守 `Retry-After`，过长等待会失败关闭并尝试允许年龄内的完整缓存。
401、403、额度耗尽、协议或身份不一致错误不会被旧缓存掩盖。

## 状态文件与降级

默认运行文件均在 `data/` 下，并与其他雷达隔离：

- `altcoin_contract_anomaly_cmc_cache.json`：CMC map/quotes 缓存及数据时间。
- `altcoin_contract_anomaly_candidate_pool.json`：本轮完整快照、差异、实际规则参数及指纹、成员/标签去重哈希与完整候选内容哈希。

写入采用项目现有原子文件能力；写失败时保留上一份完整文件。损坏缓存不会使主
BOT 崩溃。CMC 临时失败时，只能使用仍在最大可接受年龄内的可信缓存；过期市值、
Symbol-only 结果和 CoinPaprika 排名结果都不能进入正式候选。

`--cache-only` 用于离线复核。若本地缺少所需缓存或缓存已经过期，命令返回 `3`，
不会退回不可信匹配。
离线读取会重新核验每个候选所需字段的新鲜度，并要求快照中的规则参数与当前配置完全一致；
阈值改变后必须重新执行在线扫描，旧标签不会被当作当前规则结果展示。

## 生产边界与回滚

P1 没有触碰以下生产边界：

- `paopao-radar` 与 `paopao-market-stream` 的启动命令；
- Binance WebSocket 连接和订阅；
- CoinGlass 退役配置；
- Telegram 人工建话题、双发送门和缺路由失败关闭逻辑；
- 现有 funding alert、资金流和脉冲雷达状态。

停止 P1 只需不再执行 `altcoin-anomaly`；保持
`ALTCOIN_CONTRACT_ANOMALY_ENABLE=false`。独立缓存和候选快照可先备份留作审计，
无需清理其他模块的数据。代码回滚应撤销 P1 提交，不修改现有生产数据库。

## 验证

专项测试全部使用假凭据和模拟数据，不访问外部网络：

```bash
python -m unittest tests.config_tests.test_altcoin_contract_anomaly_config
python -m unittest tests.runtime_tests.test_altcoin_anomaly_cli
```

发布审查前仍须执行仓库根 `AGENTS.md` 规定的完整编译、全量测试和
`git diff --check`。
