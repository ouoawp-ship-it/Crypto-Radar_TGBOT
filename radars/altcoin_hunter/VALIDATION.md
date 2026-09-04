# P1A 离线验收记录

日期：2026-09-04。基线：`5fbec29ec2c854504e8f1cf561855fba8acb349c`。平台：Windows / Python 3.14.7。测试和容量数据库全部由临时目录承载。本文不是生产容量承诺。

## 范围与安全证据

- 仅新增独立 Hunter 域、CLI、配置例子和新测试；旧调度、订阅、Telegram 与数据库模块无改动。
- 模块导入不读取运行时配置、不建立客户端/数据库/线程/文件锁；动态导入保护与静态依赖检查均通过。
- 80 个已有配置、数据库、锁及状态文件的 SHA-256 前后相同；未读取或输出凭证值。
- 隔离副本不包含原仓库忽略的 `.env`、运行时目录及数据库；测试进程拦截外部网络连接和原仓库写入。
- 旧测试依赖 urllib3 的 6 次 IPv6 本地 bind 探测被拦截；新域专项、CLI 及容量测试没有网络尝试。Windows asyncio 的标准库内部 loopback socketpair 仅用于本机唤醒。
- 初轮基线保护脚本误拦截 asyncio socketpair 导致测试错误；修正该测试环境限制后，未修改旧代码或断言即通过。

## 测试

- 原基线：1042 项，1026 通过、16 原有平台条件跳过、0 失败，141.249 秒。
- 集成初验：1171 项，1155 通过、16 原有平台条件跳过、0 失败，176.810 秒。
- 最终代码复核：1171 项，1155 通过、16 原有平台条件跳过、0 失败，144.757 秒；最终 compileall 退出 0，1.495 秒。
- Hunter 专项：collected/executed 129，passed 129、failed 0、skipped 0，58.703 秒；外部网络尝试 0。
- `compileall`、完整 `unittest discover`、专项发现和 `git diff --check` 均通过。最终隔离副本的 Python/SQL/配置文件与提交内容一致。
- 原有跳过来自 POSIX bash、权限、symlink、服务脚本等平台条件；没有新增 skip 或修改旧测试。

覆盖分组：配置与六事件合同 22；身份/Universe 16；聚合 19；窗口 8；基线 12；健康 7；存储 16；回放/CLI 24；容量 3；隔离 2。测试分别覆盖用户列出的配置非法值、NaN/Inf/下溢、秒毫秒、未来值、去重、乱序/迟到、epoch/缺口/空窗、六窗口、无前视基线、MAD=0、冷启动、迁移/只读、真实锁和提交失败重试、容量以及旧系统隔离。

关键故障断言包括 `prepare()` 保留同一待提交批次、事务失败时桶/健康/checkpoint 全部回滚、提交后异常按持久化 batch ID 重试不重复，以及窗口拒绝未提交批次。基线快照另事务失败明确降级，不声称已具备增量崩溃恢复。

## CLI 与确定性回放

三个实际进程入口 `python -m runtime.altcoin_hunter migrate/replay/status` 在新临时库退出 0。正常 fixture 为 2 币种、8 分钟、seed 42：32 输入、16 完整桶、0 拒绝、36 基线状态、288 次评估，其中 30 次 ready。长窗口仍保持冷启动，不用未来数据补齐。

- Bucket digest：`86bb4425d969ba48dd374168518b1185099744d10effe78ac3a4160f17215c34`。
- Baseline digest：`fed07e9c99eb4ae86196f11b73a6cf6326b1e7b6c8b06063c468e262663223b1`。
- 不同物理数据库路径产生相同完整回放结果；配置算法哈希排除输出路径。计时/内存测量不进入确定性输出。

## 容量方法

工具：`python -m tests.altcoin_hunter_tests.capacity`。固定 seed 417，默认每币每分钟 2 笔，突发场景中一轮放大 10 倍。全部为虚构数据。
计时覆盖目录写入、事件处理、聚合、事务、窗口、可选基线及最终关闭；不包括首次 migration。`tracemalloc` 峰值包括 Python 分配，不等于 RSS，也不包括 SQLite 全部 native cache；它会显著降低速度。短测运行期间有其他验证工作，数值不能作为独占服务器跑分。
`--no-baselines` 对应容量 helper 的默认空 baseline_windows：仍聚合、写库并维护提交后的窗口缓存，但不执行基线评估。完整基线案例单独列出。

以下数据库总大小包含全部七表和索引。行数顺序为 instruments / universe_history / market_buckets_1m / baseline_state / ingest_checkpoints / health_rollups_1m，另有一行 schema_migrations。

### 600-normal

- 600 币种 × 20 分钟，24000 输入，12000 桶；63.020 秒，380.833 events/s。
- Python 分配峰值：38299638 bytes（36.53 MiB，tracemalloc）。
- DB：26292224 bytes；WAL 采样峰值：6715632 bytes（after_each_batch_commit）。
- 行数：600 / 600 / 12000 / 0 / 620 / 12039。
- 基线窗口：关闭；提交失败/SQLite locked 注入：本轮未注入。

### 1000-duplicates

- 1000 币种 × 20 分钟，45720 输入，20000 桶；114.260 秒，400.141 events/s。
- Python 分配峰值：60910864 bytes（58.09 MiB，tracemalloc）。
- DB：43786240 bytes；WAL 采样峰值：7922792 bytes（after_each_batch_commit）。
- 行数：1000 / 1000 / 20000 / 0 / 1020 / 20039。
- 基线窗口：关闭；提交失败/SQLite locked 注入：均验证通过。

### 1000-burst

- 1000 币种 × 3 分钟，24000 输入，3000 桶；30.426 秒，788.787 events/s。
- Python 分配峰值：30519429 bytes（29.11 MiB，tracemalloc）。
- DB：8589312 bytes；WAL 采样峰值：6031712 bytes（after_each_batch_commit）。
- 行数：1000 / 1000 / 3000 / 0 / 1003 / 3005。
- 基线窗口：关闭；提交失败/SQLite locked 注入：本轮未注入。

### 1000-six-baselines

- 1000 币种 × 6 分钟，12000 输入，6000 桶；94.522 秒，126.955 events/s。
- Python 分配峰值：106301543 bytes（101.38 MiB，tracemalloc）。
- DB：30572544 bytes；WAL 采样峰值：6554952 bytes（after_each_batch_commit）。
- 行数：1000 / 1000 / 6000 / 18000 / 1006 / 6011。
- 基线窗口：1, 3, 5, 15, 30, 60 分钟；提交失败/SQLite locked 注入：本轮未注入。

### 1000-six-baselines-untraced

- 1000 币种 × 6 分钟，12000 输入，6000 桶；18.091 秒，663.309 events/s。
- Python 分配峰值：未在此轮测量。
- DB：30572544 bytes；WAL 采样峰值：19429952 bytes（after_each_writer_transaction_commit）。
- 行数：1000 / 1000 / 6000 / 18000 / 1006 / 6011。
- 基线窗口：1, 3, 5, 15, 30, 60 分钟；提交失败/SQLite locked 注入：本轮未注入。

### 1000-100k-buckets

- 1000 币种 × 100 分钟，200000 输入，100000 桶；134.208 秒，1490.223 events/s。
- Python 分配峰值：未在此轮测量。
- DB：209965056 bytes；WAL 采样峰值：14984472 bytes（after_each_writer_transaction_commit）。
- 行数：1000 / 1000 / 100000 / 0 / 1100 / 100199。
- 基线窗口：关闭；提交失败/SQLite locked 注入：本轮未注入。

完整六基线案例的两次回放结果完全一致。初轮 WAL 只在桶批次后取样，因此遗漏最终基线事务；已将测量工具改为每次 Writer 事务提交后取样。该案例的最终 WAL 峰值应采用复测的 **19,429,952 bytes**，不能使用旧的 6,554,952 bytes。其他不含基线快照的早期案例明确保留原采样方法。

## 体积与保留限制

1000 币种 × 100 分钟实测 **100,000 个桶**：全库 **209,965,056 bytes（200.24 MiB）**。按相同结构线性换算：

- 每 100,000 桶：209,965,056 bytes，实际测量。
- 每 1,000,000 桶：约 2,099,650,560 bytes，估算。
- 600 × 1440 × 3 = **2,592,000 桶**：约 5,442,294,252 bytes（5.07 GiB），估算；未包含生产备份余量、完整长历史基线快照和未知市场数据分布。

短时含基线的 6000 桶案例带有 18,000 条固定 series 快照，不能按桶数把其整个 DB 大小线性放大作为长期模型。数据库保留、batch 凭据清理、健康生命周期和备份尚未实施。3 天只是配置候选，必须在后续代表性负载和服务器实测后确认。

## 尚未通过的生产门槛

真实 WS/REST、公开行情 smoke、服务器资源、长期 RSS/队列、自动保留、实时只读访问、增量重启恢复、市场阈值校准和 6h live soak 均未执行。未接 Web/Telegram/链上/交易，也未创建或部署服务。P1A 完成只代表离线数据底座验收通过。
