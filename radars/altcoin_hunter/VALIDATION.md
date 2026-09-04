# Altcoin Hunter 验收记录

前半部分保留 PR #172 的 P1A hardening 历史记录。本轮 P1B-I 的范围、测试和容量见文末独立记录，不将历史结果当作新代码验收。

日期：2026-09-04。仓库：`C:\Users\多多\Desktop\Crypto-Radar_TGBOT`。分支：`codex/altcoin-hunter-p1a-foundation`。本轮起始 HEAD：`11616ef042908a97de23fbca6c8fd1172e4e4bc9`；main 精确基线仍为 `5fbec29ec2c854504e8f1cf561855fba8acb349c`。继续原 [Draft PR #172](https://github.com/ouoawp-ship-it/Crypto-Radar_TGBOT/pull/172)，不转 Ready、不合并、不部署。

## 安全与范围

开始时本地、远端分支与 PR head 一致，工作区/暂存区干净；PR OPEN/Draft、MERGEABLE，原 CI 通过；无 merge/rebase/cherry-pick/revert/bisect，当前分支未被其他 worktree 占用，未发现使用本仓库的 Python/Bot 进程或其他活动任务。其他 worktree 未操作。读取根 AGENTS.md 后仅修改 PR 原先新增的 Hunter 文件及专项测试、README/本记录。

80 个忽略的旧配置、数据库、锁及运行时文件以 SHA-256 检查前后不变。原策略、shared、主调度、生产配置、部署与依赖无 diff。导入和运行隔离测试阻止旧运行时与网络客户端导入；没有真实/测试 Telegram、公开行情请求、WebSocket、生产数据库打开、服务或部署动作。

完整测试与专项分别使用 git archive 加本次允许文件覆盖生成的独立临时副本，不复制原仓库忽略文件。子进程网络 guard 禁止外部连接/DNS，禁止写原仓库；Windows 标准库 asyncio 的内部 loopback socketpair 仅用于本机唤醒。旧测试 urllib3 的本机 IPv6 bind 探测仍被拒绝并由旧代码处理；Hunter 专项、容量与 CLI 的外部网络尝试均为 0。没有修改旧测试、永久 skip 或降低门禁。

## 修复与可复现证据

- `QualityTracker` 原来 ack 只减 counters 和 status prefix，旧 gauge maxima 留在同一活动行。先复现 900/800/100/5000 污染下一代 20/30/2/100，再改为 active/prepared 双缓冲；新值更大也测试。`test_quality.py` 覆盖 source/instrument、freeze 失败、stale empty ACK、相同内容代、状态恢复与容量上限。
- prepared snapshot 不可变，ACK 必须匹配对象 token；只清冻结代，新观测保留。`test_storage.py::test_health_frozen_retry_then_post_prepare_delta_is_committed_once` 在真实临时 SQLite 注入失败并重试，计数为 2，下一代 gauge 为 20/30/2/100，持久化整个分钟累计最大值正确保留 900/800/100/5000。
- 同分钟两份相同健康增量不能共享内容哈希 ID；`aggregation.py` 加确定性本地 batch generation，retry 不递增。专项真实落库验证两代 accepted=2，重试不重复。这不是跨进程恢复协议，仍只支持新空库重放。
- `windows.py` 删除窗口 coverage_ratio；明确 observed_minutes、expected_minutes、observed_minute_ratio、observed_coverage_ms、expected_coverage_ms、time_coverage_ratio、complete_minutes、incomplete_minutes。complete/incomplete 计数只统计存在的桶，missing_minutes 单列。
- 5 完整分钟：两个 ratio 都为 1；5 个 10 秒桶：存在率 1、时间覆盖率 1/6，全部分析字段 null；缺 1 完整分钟：两率 0.8；3 完整加 10/20 秒：存在率 1、时间率 0.7。混 epoch 即使时间率 1 也不完整。`test_replay.py::test_partial_time_coverage_never_enters_valid_baseline_history` 验证 8 个部分桶、0 ready、有效样本数 0、6 份快照只保留 null 观察。
- Health 身份四字段只接受原生 str，长度 1..128、无首尾空白、ASCII/Unicode Cc/Cf 控制或格式字符；None/bool/int/float/str 子类/空白/超长均拒绝。Storage 在 JSON detachment 前使用同一校验；source 汇总必须显式 instrument_id="*"。
- MarkPrice/Funding/OI 核心值为 null 当且仅当 missing_reason 非空。Book 任一边缺失必须有原因；双边存在原因必须空，可选数量不参与此判定。Liquidation price/quantity/side 八种有无组合均验证。质量较差放事件 quality_flags。六类 typed payload 完整 roundtrip，合法零值不等于缺失。
- 非 Trade 缺失、flags、future 也持久化异常证据，恢复按 event_type 隔离；Unicode 256 字符 missing_reason 保留原文；超额 flags 有明确截断计数，不静默消失。
- `test_capacity.py` 增加真实 100,000 桶回归；源 accepted=200,000、connection observations=100,000，正常 instrument health=0，checkpoint=1100。duplicate 场景断言异常计数在 instrument/source 两级都保留。

## 健康存储收口

Schema v1 七表和索引 SQL/checksum 未变化，无 v2。问题可以通过减少重复事实的生成解决，不需要删除任何异常样本或改变市场桶：正常 accepted/non_trade/connection/health 计数只留 source 分钟汇总；币种级仅留异常、状态变化和显著延迟。第一次 complete 不制造状态变化，之后异常和恢复均保留。正常 trade_count 已在 market_buckets_1m。

默认 event latency >=2000ms 或 processing latency >=500ms 保留币种证据，构造参数可调，未宣称生产校准。duplicate/late/gap/incomplete/epoch/local loss/queue overflow/writer failure/明显延迟/status changes 不被正常行过滤；队列深度和 checkpoint lag 始终保存在 source 汇总。每缓冲 8192 行、双缓冲最多 16384；状态 cache 共 8192 identities（含类型维度），满时明确计数。

最多保留 32 个 epoch 值。tracker 的 connection_epoch_overflow_observations 是遗漏值对应的观测次数；跨代合并另用 connection_epoch_merge_overflow_values 计每代遗漏成员数，不能把两种单位混加。异常拥挤时状态/flags 细节仍受有界截断并明确计数。

旧 P1A 临时库 Schema 可只读查询，但旧健康和覆盖结果不满足本轮合同，也不支持继续回放。验收必须新建临时空库重放；本轮不自动删除旧临时库，不触碰任何旧策略数据库。

## 测试结果

- compileall：exit 0，1.948s。
- 完整 unittest discover：collected=1214，运行结果 1198 passed、0 failed、16 原有 Windows 平台条件 skipped；343.672s。真正执行非 skip 项 1198。
- Hunter 专项：collected/executed=172，passed=172、failed=0、skipped=0；189.535s。
- 本轮新增 43 个 test methods；subTest 组合不虚增该计数。原专项 129 项、整个旧系统 1042 项；未移除旧覆盖。质量/聚合、配置合同、存储、窗口、基线、Universe、回放、容量、隔离均被发现执行。
- 两份隔离副本并行运行，因此以上耗时不是独占机器跑分。100k 体积专测单独运行；全量与专项各再次执行 100k 回归。
- 完整命令：`python -m compileall -q radars shared runtime config tests scripts main.py`；`python -m unittest discover -s tests -t . -p "test_*.py"`；`python -m unittest discover -s tests/altcoin_hunter_tests -t . -p "test_*.py"`；`git diff --check`。
- Linux / Python 3.12 结果以原 PR 当前 HEAD 的 GitHub Tests check 为准；本文件不把上一个提交的绿色 CI 当成新提交已通过。最终完成报告和 PR 描述记录新 run 链接及结果。

## 100,000 桶逐表审计与前后对比

正常 fixture：1000 instruments ×100 minutes、seed417、每币每分钟2笔；200,000事件、100,000完整桶、0拒绝、100个提交批次。修改前代码从精确起始 HEAD 独立 git archive，修改后为本次实现；都使用全新临时库。100k 专测关闭基线计算（baseline_state 为0行），保留提交后窗口；未开启 tracemalloc，不报告未测量的 Python 峰值或伪装完整基线负载。旧 P1A 的独立六窗口基线内存案例保留在前一提交验收记录，不能冒充本轮复测。

计时覆盖目录、事件、聚合、事务、窗口与关闭，不含初始 migrate 和事后页审计。WAL 在每个 writer transaction commit 后采样，包括基线事务；它是该调用边界采样峰值，不声称纳秒级观测所有瞬态。Windows SQLite 无 dbstat，使用只读 B-tree/interior/overflow 页归属；Linux 有 dbstat 时做逐对象交叉核验。512字节页、UTF-8均值、freelist、共享页拒绝、尾部垃圾、sidecar拒绝与文件SHA/mtime/文件列表不变均有测试。主库已关闭、checkpoint完成；不操作 live SQLite。

以下每条为“前 → 后”，bytes 为十进制字节。数据页含该表 B-tree 与 overflow，索引单列，平均 JSON 取 UTF-8 BLOB 长度，不把字符数当字节：

- `market_buckets_1m`：rows 100,000 → 100,000；data pages 136,876,032 → 136,876,032 bytes；indexes 11,104,256 → 11,104,256 bytes；avg record_json 888.942520 → 888.942520 bytes。
- `health_rollups_1m`：rows 100,199 → 199；data pages 51,384,320 → 98,304 bytes；indexes 8,183,808 → 20,480 bytes；avg record_json 337.775008 → 319.070352 bytes。
- `baseline_state`：rows 0 → 0；data pages 4,096 → 4,096 bytes；indexes 4,096 → 4,096 bytes；avg record_json N/A → N/A bytes。
- `ingest_checkpoints`：rows 1,100 → 1,100；data pages 667,648 → 667,648 bytes；indexes 131,072 → 131,072 bytes；avg record_json 244.072727 → 243.800000 bytes。
- `instruments`：rows 1,000 → 1,000；data pages 688,128 → 688,128 bytes；indexes 73,728 → 73,728 bytes；avg record_json 486.670000 → 486.670000 bytes。
- `universe_history`：rows 1,000 → 1,000；data pages 688,128 → 688,128 bytes；indexes 143,360 → 143,360 bytes；avg record_json 486.670000 → 486.670000 bytes。
- `schema_migrations`：rows 1 → 1；data pages 4,096 → 4,096 bytes；indexes 0 → 0 bytes；avg record_json N/A → N/A bytes。
- `sqlite_schema`：rows 19 → 19；data pages 12,288 → 12,288 bytes；indexes 0 → 0 bytes；avg record_json N/A → N/A bytes。

全部索引逐项：

- `idx_checkpoints_kind`（ingest_checkpoints）：20,480 → 20,480 bytes。
- `idx_health_rollups_time`（health_rollups_1m）：1,691,648 → 4,096 bytes。
- `idx_instruments_symbol`（instruments）：24,576 → 24,576 bytes。
- `idx_market_buckets_symbol_time`（market_buckets_1m）：2,940,928 → 2,940,928 bytes。
- `idx_market_buckets_time`（market_buckets_1m）：1,687,552 → 1,687,552 bytes。
- `idx_universe_history_instrument_time`（universe_history）：57,344 → 57,344 bytes。
- `sqlite_autoindex_baseline_state_1`（baseline_state）：4,096 → 4,096 bytes。
- `sqlite_autoindex_health_rollups_1m_1`（health_rollups_1m）：6,492,160 → 16,384 bytes。
- `sqlite_autoindex_ingest_checkpoints_1`（ingest_checkpoints）：110,592 → 110,592 bytes。
- `sqlite_autoindex_instruments_1`（instruments）：49,152 → 49,152 bytes。
- `sqlite_autoindex_market_buckets_1m_1`（market_buckets_1m）：6,475,776 → 6,475,776 bytes。
- `sqlite_autoindex_universe_history_1`（universe_history）：86,016 → 86,016 bytes。

汇总与投影：

- 总业务/元数据行数（七表，含schema_migrations；不把sqlite_schema目录行算业务行）：203,300 → 103,300。
- 总索引bytes：19,640,320 → 11,476,992。
- DB bytes / 每100,000桶bytes（实测）：209,965,056 → 150,515,712。
- WAL采样峰值bytes：14,984,472 → 9,768,552。
- 每1,000,000桶bytes（线性估算）：2,099,650,560 → 1,505,157,120。
- 600×1440×3=2,592,000桶bytes（线性估算）：5,442,294,252 → 3,901,367,255。
- 处理耗时：136.621492s → 118.490279s；events/sec：1463.899 → 1687.902。这是单次本机样本，不是吞吐提升保证。
- DB 减少 28.3139%；WAL减少 34.8088%；health行减少 99.8014%；总行数减少 49.1884%。

页守恒：前 51261×4096=209,965,056，后36747×4096=150,515,712；freelist均0。源级健康行均为199：100个市场source分钟 +99个已关闭runtime分钟。币种健康从100000→0；source accepted 200000全部保留，并新增100000次显式connection coverage observations。市场桶总JSON字节88,894,252及数据页/索引完全相同，bucket digest前后均为 `e99dba8988701314c2ac1af38a5759d497c83ce07d5d6b5e39749a2ee49cf6ba`，证实压缩没有删除市场样本。

这只证明健康正常流不再按 instrument×minute 长期写重复诊断。duplicate/late/gap/epoch/丢失/写失败场景仍可产生币种行；其保留语义由专项故障测试验证，不拿正常样本的0行声称异常流也为0。SQLite locked 与 commit失败均使用真实临时库，checkpoint不前进、pending不丢失、重试不重复。

线性投影含当前固定目录及batch等开销，没有实测百万/三天库；基线快照、异常比例、JSON实际分布、备份与WAL余量均需另计。`retention_1m_days=3`仍为未实现自动清理的候选值，不能宣布生产保留已定。

## 剩余边界与后续

本轮离线hardening验收不等于生产就绪。尚未实施真实WS/REST、公开行情smoke、6h live soak、实际服务器资源验证、增量恢复、自动保留/备份、live DB只读并发、五类策略、三评分、策略八态、Outcome、Telegram/Web/链上。配置默认关闭及拒绝真实send/live保持原样。

P1B另立任务审查只读公开数据adapter的消息映射、有限timeout/retry、订阅预算、限流、断线epoch及缺失质量；真实联网必须另行明确范围与门禁。P1C才讨论受控长期soak。本文不启动P1B。

回滚本次离线使用只需停止离线命令；临时数据保留供核查。若要回退代码，在重新安全检查后用单独提交反向回退本次hardening commit，不重写已发布历史、不回退或迁移旧策略数据库、不删除活跃WAL。

## P1B-I：Binance USDⓈ-M 离线协议与调度底座

2026-09-04 UTC，从精确合并基线 `e7622becdec46c179d97820f0769790b9a49e3af` 创建 `codex/altcoin-hunter-p1b-public-data-adapters`。创建前本地仍在 P1A 分支 `6b42fab76b411a39bb224d01fc0fb55f45d7db45`；已验证远端 main、PR #172 MERGED 及 merge parents `5fbec29ec2c854504e8f1cf561855fba8acb349c`、`6b42fab76b411a39bb224d01fc0fb55f45d7db45`。目标分支原先不存在，工作区/暂存区/未跟踪为空，无 Git 操作或使用该工作树的 Bot/Python 进程；其他 worktree 未操作。

只增加 adapters、subscription_plan、connection、rest_budget、rest_scheduler、ingestion，以及本域新测试/40 个静态虚构 Fixture；既有修改仅 runtime/altcoin_hunter.py 的三个离线子命令和 README/本文。configuration/models/identity/universe/aggregation/windows/baselines/quality/storage/read_model 及 migration 均与基线 Git blob 相同。Schema v1 七表不变，旧策略、shared、主调度、依赖、部署和生产配置无 diff。

80 个原有 ignored 配置、DB、锁和运行时文件在实施前后按 SHA-256 比较；无变化。所有新 CLI 无 DB 参数或 storage 实例，新导入和 CLI 子进程测试禁止网络客户端、socket/DNS、SQLite、线程启动与文件写入。Windows 路径读取额外拒绝 UNC、映射网络盘、reparse 组件。全部验证在不含生产 .env/ignored 运行时文件的临时副本运行，原仓库受独立审计钩子写入保护。

### 关键合同证据

- `test_binance_exchange_info.py`：目录整批有效才发布、失败保留 last-good、同名不合并、filters/precision/时间、explicit identity 与元数据版本。
- `test_binance_protocol.py`：Raw/Combined、UM/CM、坏元素隔离、全部六类 typed event、q/nq、nullable Funding interval、BBO 去重、清算 snapshot、精确 OI 倍率与 ID、wrapper 大小/深度共享限制。
- `test_binance_subscriptions.py`、`test_binance_connection.py`：600/1000/1500 规划、路由/容量、增删稳定分配、严格 ACK、有限重连、回收、8 controls/s、旧 epoch、实际调用 P1A note_connection 验证覆盖。覆盖缓存 301 条保留 256、淘汰 45，诊断计数完整，不从摘要伪造证据。
- `test_binance_rest.py`：端点权重、防伪造预算、funding 独立次数、Retry-After/418/429/超时/5xx、取消/stale/请求 ID 碰撞、队列有界、3:1 公平、80 高频上限、预算故障 fail closed 及显式恢复。
- `test_binance_ingestion.py`：ACK/route/epoch/liveness/未来时间拒绝、有界去重、全市场观察分母、promoted 优先级升级、筛选后的 metadata 对齐、诊断脱敏和有界快照。
- `test_binance_cli.py`、`test_binance_isolation.py`：实际主入口、确定性输出、元数据不伪造成事件、默认无在线子命令、零运行时副作用。另在临时副本实际执行三条 `python -B -m runtime.altcoin_hunter ...` 命令，均退出 0。

OI 保留 `1200.500 contracts` 原值并在 adapter metadata 中精确换算 `×1000=1200500.000 base`；base 直接保留，quote 缺价格返回 null/reason，极值溢出/下溢拒绝。source_event_id 包含 symbol、毫秒时间和 SHA-256。不把 OI 增加或主动成交差解释为资金方向。

未修改、删除或弱化任何既有测试，未新增永久 skip。本轮没有真实 DNS/HTTP/WS 行情连接、Telegram、Web/链上、生产数据库访问、Migration、服务、部署、合并或 P1B-II。官方文档和 GitHub 的审计操作与运行时离线网络计数分开记录。

### P1B-I 最终本地回归

环境：Windows、Python 3.14.7。最终源码在独立 `release-full` / `release-special` 临时副本验证；此前对已被修正代码的运行已作废，不计入本表。

| 检查 | collected / runner count | 实际执行 / passed | failed | skipped | elapsed |
| --- | ---: | ---: | ---: | ---: | ---: |
| compileall 指定全目录 | 不适用 | 成功 | 0 | 0 | 1.945s |
| 全量 unittest | 1353 | 1337 / 1337 | 0 | 16 | 348.550s |
| Hunter 完整专项 | 311 | 311 / 311 | 0 | 0 | 211.985s |

相比基线新增 139 项测试；311=原 Hunter 172+新增139，1353=原全量1214+新增139。全量的16个 skip 来自未修改的既有 Windows 平台条件，不是新增永久跳过。Linux 由本 PR 的原有 Tests workflow（Ubuntu/Python3.12）验证，最终状态以 PR 最终 HEAD check 为准，不用 Windows 结果冒充 Linux 结果。

执行命令：

```text
python -m compileall -q radars shared runtime config tests scripts main.py
python -m unittest discover -s tests -t . -p "test_*.py"
python -m unittest discover -s tests/altcoin_hunter_tests -t . -p "test_*.py"
git diff --check
```

新域完整专项、三条实际 CLI、最终容量进程：外部网络连接尝试0、DNS0、HTTP/WS0、真实Telegram0、生产文件写入0。全量旧测试加载 urllib3 时有6次本地 IPv6 `socket.bind` 能力探测，被隔离钩子阻止；这是旧库本地探测，不是外部行情连接，不能把这6次也写成“所有 socket API 调用0”。Windows 标准库 asyncio socketpair 的本机唤醒通道由隔离器单独识别；未放行任意客户端连接。全量输出中的 Telegram 故障日志来自旧测试 mock，不表示真实发送。

三条实际 CLI 均退出0，状态ok、offline_dry_run、network_calls=0、dns_calls=0、real_send=false：

- validate-binance-fixture：0.212s，digest `b7894ff2bef91d66aaa0b0f3eeb37966fea55c63556d8139c609c6f62b69a60f`。
- plan-binance-subscriptions：0.221s，digest `322044fa239d4da8780811e2d04678ee394bcedb3c27149ce3be16130afec5b4`。
- simulate-binance-connection：0.248s，digest `3186fa01e78bbae1015019ffb14d527d1ae42510cf5af2a3d08bb9efcc73a639`。所选 MARKET shard ACTIVE/epoch1；无 pending ACK/control；REST 演示请求取消后 queue/inflight 均0。

### P1B-I 离线容量

全量回归结束后单独执行 `python -B -m tests.altcoin_hunter_tests.binance_capacity`，总进程耗时5.091s。以下均启用 tracemalloc，是 Python allocation 峰值，不是进程 RSS、服务器内存承诺或真实网络吞吐。

| eligible / promoted | MARKET streams | PUBLIC streams | 漏订阅 | plan ms | 增删两次 diff 合计 ms | Python peak bytes |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| 600 / 30 | 601（1连接） | 31（1连接） | 0 | 48.069 | 17.694 | 1,720,673 |
| 1000 / 50 | 800+201（2连接） | 51（1连接） | 0 | 83.898 | 37.041 | 2,834,538 |
| 1500 / 75 | 800+701（2连接） | 76（1连接） | 0 | 135.770 | 47.664 | 4,385,065 |

三个规模增加/删除10%时，存续 Stream 迁移数均0；全部连接≤800。Mark 全局流和 BBO 全局流均计入槽位；liquidation默认未启用，不能把它记为已订阅。规划摘要分别为 `0645c1ca6099a54f9f165c9d4b1e7bc4f2179b7bfe145cf06c64399513bbce70`、`3a1c9a089ca804636c46e52370de1426aee4e7242f5d3c7aaaaa5669e3244da7`、`f0f94b9964b7148abafa0f9b70ce9d8e842bc6158b181c55f68c330d23a003d9`。

单帧2000项全市场 Mark 数组；每个有效元素产生 Mark+Funding 两个事件：

| 坏元素比例 | typed events | rejects | seconds | events/s | rejects/s | Python peak bytes | 拒绝详情 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1% | 3960 | 20 | 0.909190 | 4355.53 | 22.00 | 6,262,539 | 20 |
| 10% | 3600 | 200 | 0.945179 | 3808.80 | 211.60 | 5,494,919 | 64 |
| 50% | 2000 | 1000 | 0.512093 | 3905.54 | 1952.77 | 2,971,247 | 64 |

细节被截断时总拒绝计数不丢失；跨帧 diagnostics capacity=128。帧内坏币不丢弃正常兄弟元素。

100轮重连：101次连接尝试、100次 reconnect、101个epoch、303批ACK成功。ACK丢失使用499币+1全局流=10批，精确丢1批（10%），9批确认后因缺ACK超时进入BACKOFF；pending ACK峰值8，不错误进入ACTIVE。模拟限定所选分片，不冒称同时运行全部市场连接。

OI模型：1500币，100个申请高频、80个获高频、20个overflow明确标degraded。使用同一虚拟时刻的假200响应得到初始1500条，60s时排80个请求；尚未响应时 freshness 覆盖1400/1500（93.333%）。这不是测得每秒1500次HTTP采样；结束取消80个待办，queue/inflight均0。

最终容量确定性摘要：`963cd0ff217f27a7391b9f5217ebd0dd82fcc69c1a32f75f4f1edd4743b2b5d3`。摘要排除耗时/内存测量；CLI、parser和connection专项验证相同输入/seed/版本得到相同结果。

### P1B-I 剩余边界

尚无真实 Transport、共享生产 Coordinator、公开行情 Smoke、长期运行/恢复/保留策略或服务器容量验证。官方 catalog 与订阅说明的 ACK ID 类型存在差异，3s Mark 的官方 wire 为无后缀形式；P1B-II 必须重新核对。BBO不代表深度，清算是快照，未知canonical/倍率不得猜测，默认阈值/预算未经市场或生产校准。

本轮只提交并创建 Draft PR，不转Ready、不合并、不部署。后续 P1B-II 须另行授权、重验基线、明确共享预算和退出条件；本记录不启动任何真实连接。
