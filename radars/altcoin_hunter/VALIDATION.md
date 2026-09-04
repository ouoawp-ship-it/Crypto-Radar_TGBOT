# P1A Final Correctness & Storage Hardening 验收记录

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
