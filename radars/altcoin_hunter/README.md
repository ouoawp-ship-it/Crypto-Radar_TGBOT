# Altcoin Hunter P1A：离线数据底座

完整测试、故障注入、回放摘要和容量实测见 [离线验收记录](VALIDATION.md)。

P1A 提供独立策略域的数据合同、离线币池、分钟聚合、滚动窗口、描述性动态基线、SQLite 存储和可重复回放。它不连接交易所，不调用 Telegram，不执行交易，不启动服务。已有 `paopao-radar`、`paopao-market-stream` 的入口、配置及数据库不接入这个模块。

异动特征不等于开仓建议；主动成交买卖差不是资金净流入；OI 增加不能直接解释为新增多头。本阶段不输出这类方向结论。

## 运行边界与配置

配置由 `AltcoinHunterConfig.from_mapping()` 显式传入，不自动读取生产 `.env`。导入模块及构造 Writer/Reader 不打开数据库、建目录或加载运行时配置。

功能开关默认全部关闭：`enable=False`、`live_data_enable=False`、`send_enable=False`、`raw_capture_enable=False`。P1A 会拒绝将后三项设置为真；`enable` 也不会自动注册到现有主服务。

其余默认值：

- `db_file=None`：没有默认运行时数据库。必须显式指定绝对路径，拒绝父目录跳转、符号链接和已有生产数据库名称。
- `bucket_sec=60`：P1A 只允许一分钟桶。
- `allowed_lateness_ms=2000`：离线水位宽限候选值，不是交易所延迟承诺。
- `retention_1m_days=3`：尚未执行自动清理的候选保留值。
- `config_version=1`：状态输出包含配置版本与配置哈希。

同名配置可使用 `ALTCOIN_HUNTER_` 前缀传给配置解析器，例如 `ALTCOIN_HUNTER_SEND_ENABLE=false`；这不表示旧 `Settings` 或旧 `.env` 已接入这些字段。

仅在临时目录验证。下面的 PowerShell 先创建新的绝对临时目录；命令本身不会被文档自动执行：

```powershell
$p1aDirectory = Join-Path ([System.IO.Path]::GetTempPath()) ("altcoin-hunter-p1a-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $p1aDirectory | Out-Null
$p1aDatabase = Join-Path $p1aDirectory "hunter.db"
python -B -m runtime.altcoin_hunter migrate --db $p1aDatabase
python -B -m runtime.altcoin_hunter replay --fixture tests/altcoin_hunter_tests/fixtures/replay_normal.json --db $p1aDatabase
python -B -m runtime.altcoin_hunter status --db $p1aDatabase
```

`replay` 使用 `--fixture <本地fixture路径> --db <上述绝对临时数据库路径>`。它只接受已显式迁移且业务表为空的数据库。已有回放结果要在另一新临时数据库用原 fixture 从头重放；P1A 没有增量恢复或 `resume` CLI，不应在旧库上继续跑一遍。

现有可重复 recipe 为 `tests/altcoin_hunter_tests/fixtures/replay_normal.json`（正常序列）和 `replay_epoch.json`（连接 epoch 边界）。使用第二个 fixture 时也应创建另一新临时数据库，不复用上一步已有结果的库。

`status` 不执行迁移、修复、建库、创建 `.lock` 文件；缺库返回缺失状态。它只支持下文所述关闭后的离线数据库。

Fixture 必须显式推进虚拟时钟关闭最后一分钟。若 EOF 仍有未提交桶，输出 `status=incomplete`、`uncommitted_input=true`，CLI 退出 2；不会靠系统当前时间补完输入。

## 文件职责

```text
radars/altcoin_hunter/
  configuration.py       独立配置与显式数据库路径校验
  identity.py            交易所合约身份、倍率和显式资产映射
  models.py              版本化事件信封与六类 typed payload
  universe.py            正交币池属性、完整刷新、变更历史
  aggregation.py         有界事件聚合、迟到去重、待提交批次
  quality.py             每分钟质量计数、延迟、队列与检查点指标
  windows.py             只消费已提交分钟桶的六个窗口
  baselines.py           因果 median/MAD、可选 EWMA、冷启动与快照
  storage.py             显式 migration、单 writer、事务与幂等凭据
  read_model.py          关闭后数据库的零写入查询
  migrations/
    __init__.py          显式读取迁移资源
    001_foundation.sql   七张基础表与索引
runtime/altcoin_hunter.py 独立离线 CLI
tests/altcoin_hunter_tests/
                         合同、聚合、窗口、基线、持久化、回放及容量验证
```

独立 CLI 不修改 `main.py` 或现有命令分发。测试结果和容量实测由 PR 验收记录提供。

## 身份、币池与数据合同

合约身份使用 `(exchange, market, instrument_id)`；数据时间序列还包含 `source`。`symbol` 是展示与搜索字段，不能代替主键。保留交易所原始 `exchange_symbol`，跨交易所映射使用显式 `canonical_asset_id`；未知映射保留为空，不能依靠相似名称猜测。单位、合约倍率与报价币种保存在身份/负载中。

币池属性相互独立：

- `eligibility_status`：`ELIGIBLE / INELIGIBLE / BLACKLISTED`。
- `listing_stage`：`NEW_LISTING / MATURE / DELISTING / UNKNOWN`。
- `activity_tier`：`NORMAL / HOT / HUNTER / EXTREME`。
- `sampling_priority`：`BASE / ELEVATED / CRITICAL`。

因此新币可以同时处于高活跃和不可交易状态。记录还包含 `reason_codes`、`effective_at_ms`、整数 `metadata_version`、`data_quality`。只有语义变化进入历史，刷新时间变化不重复制造变更。失败、部分或空目录不能删除上一份有效目录；未出现在一次响应中不等于已经下架，退市需显式记录。

Universe 内存只缓存最近 4096 条变更，超出时增加 `history_truncated`；每次刷新仍返回该次全部变化。数据库历史独立持久化，不随内存缓存淘汰。

统一事件信封包含：`schema_version`、`source`、`exchange`、`market`、`instrument_id`、`canonical_asset_id`、`symbol`、`exchange_symbol`、`event_type`、`event_time_ms`、`receive_time_ms`、`receive_monotonic_ns`、`source_event_id`、`sequence_start`、`sequence_end`、`connection_epoch`、`quality_flags` 和 typed `payload`。

六类事件分别为：

- `trade`：价格、数量、`buyer_is_maker`、数量单位、合约倍率、报价币种。
- `mark_price`：标记价格与可选指数价格。
- `funding`：资金费率、周期小时数、下次结算时间。
- `open_interest`：OI、base/contracts/quote 单位、可选报价名义金额与倍率。
- `book_ticker`：bid/ask 价格与可选数量。
- `liquidation`：价格、数量、买卖方向、数量单位与倍率。

价格、数量及名义金额以有限十进制字符串传输，用 `Decimal` 做金额推导，分析结果才转换为受检查的 float。布尔值不能冒充数字；NaN/Infinity、无法安全表示的极值、秒误作毫秒和不匹配 payload 类型应在边界拒绝。缺失指标用 `null` 和缺失原因表达，不能用零补齐。

`missing_reason` 只解释核心字段缺失：MarkPrice 的 `mark_price`、Funding 的 `funding_rate`、OI 的 `open_interest` 为空时必须有原因，有值时原因必须为空。有值但质量较差用事件 `quality_flags`；可选 index price、OI quote notional 等附属值为空不改变这个合同。BookTicker 的 bid/ask 双边存在时原因为空，缺任一边时原因必填，另一边可以保留有效值；可选 bid/ask quantity 为空不表示盘口价格缺失。Liquidation 的 price/quantity/side 缺任一字段必须给原因，三者完整时不得再给缺失原因。上述组合均经过序列化往返和非法组合测试。

绝对时间使用 Unix 毫秒；接收单调时间使用纳秒，仅用于同一次进程生命周期内的耗时。序列号与来源事件 ID 保留各自语义，不跨交易所比较。事件去重键包括来源、交易所、市场、合约、事件类型、来源事件 ID，不因重连 epoch 改变而把重复成交重新计入。

## 分钟聚合、窗口与质量

聚合按事件时间对齐 `[start_ms, end_ms)` 的闭合一分钟桶，接收时间只用于延迟/健康度。桶保存 OHLC、基础数量、主动买卖报价金额、成交量、Delta、成交数、首尾事件及序列、事件摘要、连接 epoch、覆盖与缺失标记。

`buyer_is_maker=False` 表示主动买入；True 表示主动卖出。`delta_quote = buy_quote - sell_quote`，`quote_volume = buy_quote + sell_quote`。窗口 Delta 可以作为 CVD 的窗口增量，但不是全市场永久累计 CVD，更不是资金净流入。

缺一分钟就保留缺口，不合成零成交桶。迟到宽限、水位、去重容量和事件缓存均有明确边界；已经提交的桶不可静默改写。一个桶涉及多个连接 epoch 时不能标为完整；单笔成交不构成整分钟持续在线的证据。覆盖必须由显式连接健康区间支持。

桶内 `late_count` 统计封桶前已接收、但接收时间越过该分钟结束的有效迟到事件；超过水位的拒绝事件只进入接收分钟的健康汇总，不回写历史桶。本地容量不足丢弃事件时，已有桶标为 `local_data_loss/incomplete`，不能仅因剩余序列连续就声称完整。

滚动窗口固定为 `1m / 3m / 5m / 15m / 30m / 1h`。窗口缓存受币种容量及最多 120 分钟保留限制。输出字段为：

- `observed_minutes` / `expected_minutes`：实际存在的桶数 / 窗口预期分钟数。
- `observed_minute_ratio = observed_minutes / expected_minutes`：桶存在比例。
- `observed_coverage_ms = sum(bucket.coverage_ms)`，`expected_coverage_ms = expected_minutes * 60000`。
- `time_coverage_ratio = observed_coverage_ms / expected_coverage_ms`：真实时间覆盖率。
- `complete_minutes` / `incomplete_minutes`：已观察桶中的完整 / 不完整桶数；缺桶单独见 `missing_minutes`。

已删除窗口的歧义字段 `coverage_ratio`。分钟桶自身的 coverage_ratio 仍是 coverage_ms/60000；基线的 coverage_ratio 仍表示有效历史采样覆盖，三个概念不可混用。5 桶各覆盖 10 秒时 observed_minute_ratio=1、time_coverage_ratio=1/6、complete=false，全部分析指标为空。混 epoch 即使时间覆盖为 1 也不能完整。任何 incomplete window 都以 unavailable 进入基线观察，不能成为有效值。

健康度采用 active/prepared 双缓冲：prepare 将本代计数、四个 gauge maxima、epoch 与 status_changes 一并冻结，新观测进入新的 active 行；重试返回同一不可变快照，acknowledge 只接受该代对象。空代也有独立 token，不会让过期确认消费下一代。各缓冲最多 8192 行，总上限两倍；overflow 明确计数。同分钟不同代的相同内容拥有不同 batch ID，同一待提交代重试保持原 ID。

source `*` 分钟汇总保留正常计数及延迟、队列深度、检查点滞后。instrument 行只在异常计数、实际状态变化、明显延迟时持久化；正常 accepted/non_trade/connection/health 观察不单独制造每币每分钟诊断行。第一次 complete 仅初始化状态，后续 incomplete 和恢复 complete 都保留；状态身份缓存有界，耗尽时产生 status_memory_overflow。正常 trade_count 已由市场桶承担。

明显延迟的离线监控默认策略为 event latency >= 2000ms 或 processing latency >= 500ms；可在 QualityTracker 构造时独立调整，尚非生产 SLO。duplicate、late、gap、incomplete、epoch change、local data loss、queue overflow、writer failure、状态变化等证据不能被正常行过滤。queue/checkpoint gauge 本身在 source 汇总保留，超容量错误另有异常计数。

五类非 Trade 输入仍不计算市场聚合，但其核心字段缺失、事件质量标记和未来时间会留下 instrument/source 异常计数。状态原因保留事件类型、missing_reason 与 quality_flags；按事件类型独立比较恢复，避免健康 Funding 覆盖 OI 缺失。原因超过 2048 字符时对超额 flags 显式计入 quality_flags_omitted/health_reason_truncated；缺失原因原文保留。所有类型共享有界状态缓存。

Health 的 source/exchange/market/instrument_id 必须是显式原生 str，长度 1..128，无首尾空白、控制和不可见格式字符；不允许隐式 str()。调用方必须显式填写 instrument_id="*" 才表示源级汇总，缺失身份直接拒绝。每行保留最多 32 个 connection epochs，超过时增加 `connection_epoch_overflow_observations`：表示当前缓冲未保留 epoch 的观测次数，不声称是不同 epoch 数。跨批合并优先保留已有值，对不能保留的输入 epoch 另计 `connection_epoch_merge_overflow_values`（每代遗漏的成员数）；这个数量不是事件数，不能混加到前一指标。

已提交健康分钟的 counters 按代增量相加，gauges 取各代最大值，status_changes 去重合并。其累计最大值可以仍是 900，但下一待提交代只能包含新观测的 20；二者语义不同。桶、健康、检查点和批次凭据一起提交，同一代重试不会再次累计。

## 动态基线的含义

`BaselineKey` 按 source、exchange、market、instrument、feature、window 隔离历史。每个窗口显式指定 `BaselinePolicy`，不强制所有指标使用同一组最小样本、跨度和采样步长。

对时刻 `t` 的当前值，先只使用 `t` 之前的保留样本计算：

```text
center = median(history)
MAD = median(abs(history - center))
raw_z = (current - center) / max(1.4826 * MAD, metric_floor)
robust_z = clamp(raw_z, -clip_z, +clip_z)
```

`clip_z` 不超过 6；MAD 为零时使用每指标明确的正数 floor。当前值不能先进入自身基线。输出原值、样本数、预期样本数、覆盖率、有效历史跨度、median/MAD、截断前后 z、截断标记、可选 EWMA、就绪状态、原因码、版本和配置哈希。

只有同时满足样本数、时间跨度、覆盖和当前值可用性条件才 ready。缺失观察保留为空；样本缓存有上限。`sampling_stride` 与采样间隔共同规定采样网格，重叠窗口的观察值彼此相关，不能宣称为独立统计样本。

默认策略是离线工程示例，尚未校准到真实市场。robust z 不是概率、胜率、Heat/Bias/Tradeability 分数或交易建议。调整 policy 需要明确的新版本运行及回放对比，不能静默覆盖同一时间的不同基线快照。

## 七张表与持久化边界

P1A Schema v1 仅包含下列七张表；市场小数等完整字段也保留在 `record_json` 中：

1. `schema_migrations`：`version` 主键、SQL `checksum`、调用者提供的 `applied_at_ms`。
2. `instruments`：复合主键 `(exchange,market,instrument_id)`；symbol、canonical ID、四个正交属性、effective time、整数 metadata version、内容摘要、完整 JSON；symbol 索引。
3. `universe_history`：`change_id` 主键、合约身份、effective time、前后内容摘要、完整 JSON；合约加生效时间索引。
4. `market_buckets_1m`：复合主键 `(source,exchange,market,instrument_id,start_ms)`；symbol、end time、epoch、quality、摘要、桶 JSON；时间索引及 symbol+时间索引。
5. `baseline_state`：复合主键 `(source,exchange,market,instrument_id,feature,window_sec,baseline_version)`；updated time、完整 policy/样本/逻辑时钟快照。拒绝时间回退和同时间不同内容。
6. `ingest_checkpoints`：`checkpoint_key` 主键，区分 source 与 batch 类型；来源身份、committed watermark、batch ID、摘要、JSON；类型索引。batch 类型保存持久化提交凭据，source 类型保存检查点。
7. `health_rollups_1m`：复合主键 `(source,exchange,market,instrument_id,minute_ms)`；摘要、健康 JSON；分钟时间索引。

迁移只能通过显式 `migrate` 调用执行。SQL 校验和或历史版本不一致、未知更高版本、已有非 Hunter Schema 均拒绝，不自动修复或降级。读路径不创建 Schema，Writer.open 也不自动迁移。

本次 hardening 保持七表、索引及 Schema v1 SQL/checksum 不变；减少的是冗余健康行的生成，新增 epoch 证据仍放在健康 JSON 内，不创建 v2。旧 P1A 临时库可离线读取，但其健康/覆盖结果不满足本次新语义，不能作为本轮验收或继续写入回放的输入。请在新临时空库重放；不自动删除旧临时库，也不触碰旧策略数据库。

Writer 必须显式打开、限定所属线程、持有现有 DB 文件上的非阻塞排他锁，使用 WAL、有限 busy timeout 和短事务。数据库构造器不建父目录。锁竞争、写入失败不推进已提交检查点。

数据交接顺序为：

```text
事件 -> 有界内存聚合 -> prepare(PendingBatch)
                         |
                         v
            commit_batch：桶 + 对应 source checkpoint
                          + 健康增量 + batch 提交凭据
                         |
                         v
                    CommittedBatch
                         |
               acknowledge(batch_id)
                         |
               已提交桶 -> 窗口 -> 派生基线
```

失败时未确认批次保留，事务回滚后可用同一 batch ID 重试。发生“数据库已提交但调用者未获成功结果”时，持久化 batch 凭据避免重复计数。相同 batch ID 配不同内容会被拒绝；检查点必须与本批各来源的最新桶结束时间/epoch 对应。已有测试分别在实际 commit 前、commit 后注入异常，并检查原子性及重试结果，最终测试数量由 PR 验收记录提供。

batch ID 由内容和确定性的进程内批代序号共同计算，避免同分钟两份完全相同的合法健康增量被误认作重试。该序号只在生成新批次时递增，失败重试不变；它不构成跨进程增量恢复协议。不同进程从相同 fixture 和空库开始仍确定性一致。

**默认回放流程的基线是已提交桶的派生状态，不与当前桶提交构成一个原子事务。** 存储层可以接受显式附带的 baseline states，但不能据此声称整个回放流程已经具备生产级崩溃恢复。P1A 恢复路径是在新临时空库从原 fixture 重建全部派生状态，不提供在半完成库上增量续跑的承诺。

`HunterReadModel` 仅用于关闭、检查点已刷回主文件的离线库：拒绝任何 `-wal`、`-shm`、`-journal` 文件，连空 sidecar 也拒绝。它在已有 DB 文件上持有共享 advisory lock，使用 `mode=ro&immutable=1` 和 `query_only`，并检查读取前后的文件属性。Writer.close 尝试 TRUNCATE checkpoint 后关闭连接。

这套锁只协调本模块的 Writer/Reader。外部 SQLite 程序不会自动遵守该 advisory lock，调用者必须先停止外部写入；文件属性复核不是对不合作 writer 的完整并发保证。本模式不能用于未来实时 Web 查询，也绝不能让 `immutable` 忽略活跃 WAL。**不要直接删除生产中的 `-wal`/`-shm` 来满足只读条件。**

## 容量、保留与未实施项

当前没有自动数据库清理入口。`retention_1m_days=3` 仅是后续保留策略候选，不表示已经自动淘汰三天以前的数据。源检查点、batch 凭据、健康与历史的生命周期也需要后续独立设计；P1A 的有限回放不能替代长期运行的磁盘增长验证。

即使只按一分钟聚合，600 个币、每天 1440 分钟、3 天也有：

```text
600 × 1440 × 3 = 2,592,000 个市场桶
```

该数字还没包括索引、JSON、健康行、batch 凭据、WAL 和备份，不能直接换算成已验证的服务器磁盘或内存要求。容量脚本示例：

```powershell
python -B -m tests.altcoin_hunter_tests.capacity --instruments 600 --minutes 20 --pattern normal
```

容量实测、环境、峰值内存、写入吞吐及最终测试数由 PR 验收记录提供。短时合成数据结果不代表真实全市场吞吐、交易所可用性或六小时稳定性。

容量工具还提供关闭后数据库的只读页归属审计：逐表行数、数据页、索引页、record_json UTF-8 平均字节和健康证据分层。Windows SQLite 缺少 dbstat 时按 [SQLite 官方文件格式](https://www.sqlite.org/fileformat2.html#b_tree_pages)解析 B-tree/overflow 页；可用时与 dbstat 交叉验证，所有已分配页、freelist 和保留页必须与主文件大小守恒。该审计在计时及内存采样结束后运行，不是生产数据库读取入口。

尚未实施：真实 REST/WS adapter、全市场实时币池调度、交易所订阅与重连编排、持续网络限流、生产健康守护、原始成交长期存储、自动保留清理、实时 Web API/页面、Telegram Topic 接入、五类策略信号、三评分、八态策略状态机、Outcome Tracker、自动部署与生产容量承诺。六类事件的合同存在，不等于六类真实采集器已经完成。

## 后续阶段与回滚

P1B 再评审受控真实数据 adapter：先只读公共行情、有限 timeout/retry、共享预算、显式订阅容量、断线与 epoch 验证、数据缺失降级；仍需保持真实 Telegram/交易关闭。启用任何真实采集前先确认服务器资源和现有服务负载，重新设计实时只读查询及完整的重启恢复协议。

P1C 才在确认容量、保留、恢复和故障注入要求后进行至少六小时受控 soak；短时离线容量测试不替代该门槛。

本次代码默认不会影响旧服务，且没有部署、重启或生产 Migration。停止离线命令即可停止新域活动，保留临时库和 fixture 供核查。若将来需要移除本次文件或回退提交，应先检查工作区与 PR，并通过单独评审处理；本文不提供会自动覆盖他人修改的 Git 命令，也不要求操作旧数据库或服务。
