# Altcoin Hunter Production Candidate v1.0 执行计划

计划版本：1。范围及权重冻结于2026-09-10；状态证据见 [PROJECT_STATUS.md](PROJECT_STATUS.md)。本计划只授权既定边界内的开发、验证及阶段合并，不能取代以下人工停止门禁。

## 不可变边界

- 从PR #173开始；每个后续阶段使用独立分支、独立Draft PR、独立验收。前一阶段验收和Merge Commit未完成，不实施下一阶段。
- 不修改旧策略来适配Hunter，不削弱、删除或永久跳过旧测试；不写旧signals.db、market_snapshots.db、realtime_features.db或生产运行时文件。
- 所有新功能默认关闭或dry-run；不得自动交易、调用下单或账户接口。Telegram保留明确的真实发送门禁，候选验收只使用dry-run。
- 自动允许：Binance官方公开只读REST/WS、临时目录/数据库/独立输出、分支/commit/push/Draft PR；CI成功且无阻断后可转Ready并Merge Commit。不得squash/rebase merge，不删除阶段分支。
- 第一次真实Smoke固定BTCUSDT、ETHUSDT、SOLUSDT、BNBUSDT、XRPUSDT，3分钟；全部验收通过后，允许最多20币、10分钟。固定币若不满足有效USDⓈ-M目录条件则停止，不自动替换。
- 必须立即停止并等待人工确认：生产部署；systemd安装/启动/重启；生产Migration；修改生产.env；真实Telegram；任何交易/账户接口；Smoke超过20币或10分钟；主分支漂移/外部提交/他人工作；测试失败、协议不兼容、限流或数据完整性异常。停止后保存已完成证据，不自动修复该阻断或继续后续阶段。
- P1C可先完成离线长时回放及本机受控验证。超过上述时长的真实soak仍必须先获得人工确认；离线虚拟时间不能冒充真实长期运行。

## 固定完成度口径

总体百分比是工程交付进度，不是生产可用率、市场效果或胜率。保留之前已报告的40%=P1A25%+P1B-I15%。此前剩余60%未逐阶段拆分，本计划首次固定为下列权重，今后不改变权重、分母或通过标准来提高数字。

- P1A离线底座：25%。已合并PR #172，代码/回放/临时SQLite验收通过。
- P1B-I离线协议与连接合同：15%。PR #173代码验收已通过；最终合并门禁为当前任务。最终合并不再次增加已有40%。
- P1B-II公共Transport与有界Smoke：15%。阶段全部门禁通过才记分。
- P1C恢复/保留/实时读取/服务化与长时验证：10%。阶段全部门禁通过才记分。
- P2五类异动及三评分：12%。阶段全部门禁通过才记分。
- P3八态/事件簇/Outcome：8%。阶段全部门禁通过才记分。
- P4Telegram Topic与消息生命周期：5%。仅dry-run验收，真实发送不是本阶段自动门禁。
- P5Web总览/榜单/Coin Detail/时间线：5%。仅本地/隔离验收，生产发布关闭。
- P6多源/旧雷达只读联动/校准与候选验收：5%。效果指标如实报告，无盈利承诺。

总权重100%。未通过阶段不按代码行数或测试数量给部分分；状态单列not_started/in_progress/blocked/passed/merged。每阶段结束更新状态文件的能力、未完成项、风险、PR/commit/CI和下一入口。

## 每阶段共同门禁

1. 确认仓库、当前分支/HEAD、远端main与阶段分支、工作区/暂存/未跟踪/ignored、Git操作、worktree占用、运行进程、AGENTS和PR评论/review/thread。记录预期SHA；只认可本任务已验证的合并导致的main推进。
2. 从精确已验收main SHA新建阶段分支，禁止从含额外提交的本地分支隐式继承。
3. 完成该阶段最小闭环；代码、文档、专项测试和失败证据同PR。源数据、配置、采样口径、版本及局限可追溯。
4. 在无生产.env/DB的临时副本运行：`python -m compileall -q radars shared runtime config tests scripts main.py`；`python -m unittest discover -s tests -t . -p "test_*.py"`；Hunter专项；实际适用CLI；`git diff --check`。阶段真实网络测试另用明确白名单和预算，不解除全量单元测试隔离。
5. 对最终提交检查CI成功、差异范围、风险、旧策略/文件hash及外部变化。Draft转Ready后如果有新CI，等待其成功；Merge Commit必须匹配预期head，并核对两个parents及远端main，不操作其他worktree。
6. 更新本计划任务状态和PROJECT_STATUS证据。下一阶段从该merge SHA开始。若是合并后才能得到的证据，先保存在独立执行日志，再由下一阶段首个文档提交固化；不能为了在已合并PR写回merge SHA而重写历史。

## 分阶段任务与验收

### 当前：PR #173 最终门禁

- 分支：codex/altcoin-hunter-p1b-public-data-adapters；起始head444724461d9e27f843117cd36e47dced0b153daa；base e7622becdec46c179d97820f0769790b9a49e3af。
- [x] 初始只读安全检查、精确head旧CI、工作区与受保护文件核验。
- [x] 独立最终代码审查，无阻断问题；范围、连接/准入/身份/协议边界均只读复核。
- [ ] 提交长期计划及状态文件，等待新head CI。
- [ ] Ready后复核并Merge Commit；验证parents/main与工作区。

### P1B-II：公共Transport与两级Smoke

- 计划分支：codex/altcoin-hunter-p1b-ii-public-transport。
- 依赖：#173已合并、main无外部漂移。
- 范围：显式有限公共REST/WS Transport；复用协议、分片、ACK、epoch、REST关联及健康门禁；独立公共预算，不接旧协调DB；默认不连接；不注册主Bot。
- Smoke前：重核Binance官方当前USDⓈ-M文档与ACK模式，显式选择INTEGER或STRING，不自动无限回退；ExchangeInfo响应记录status/Content-Length/body bytes/symbol count/解析接受拒绝；固定目录与事件单位校验。
- 验收：全部离线测试/CI通过；5币3分钟通过后才运行最多20币10分钟；订阅全部确认，无未知路由/UM混入/限流/未解释数据丢失；全部请求有限deadline/retry/预算；记录延迟、覆盖、拒绝、epoch、控制消息、bytes、内存、输出digest。停止后连接/线程/队列无残留，生产文件hash不变。
- 无法证明完整性的区间必须incomplete。协议不兼容、429/418或完整性异常立即停止等待人工，不能把降级结果宣称Smoke通过。
- 复杂度：高；风险：真实协议差异、共享IP预算、地域网络、体积与时钟。

### P1C：恢复、保留、读取及服务候选

- 计划分支：codex/altcoin-hunter-p1c-runtime-recovery。
- 依赖：P1B-II合并及两级Smoke验收通过。
- 范围：独立Hunter数据库的提交/恢复协议、滚动保留、备份恢复、WAL安全实时只读、进程退出/健康、默认关闭的独立服务定义；不安装/启动服务、不动旧数据库。
- 验收：提交前/后崩溃回放幂等，checkpoint不超前、pending不丢；保留清理不破坏恢复凭据；并发读写/locked/磁盘失败可解释；离线至少72小时虚拟输入、600/1000币容量及内存/磁盘曲线；恢复结果与连续回放一致。真实长期soak需人工批准超时额度，未获批准则阶段标blocked，不冒充已完成。
- 复杂度：高；风险：SQLite锁、WAL读取、恢复边界、保留/备份、有限缓存和磁盘。

### P2：五类信号与三个独立评分

- 计划分支：codex/altcoin-hunter-p2-signals-scores。
- 依赖：P1C通过并合并。
- 范围：Price/Volume/OI/CVD异常或背离/Funding Extreme；Heat0..100、Bias-100..100、Tradeability0..100。使用独立动态基线、因果窗口、版本化权重及解释；不引入ML/AI方向预测。
- 验收：缺失、冷启动、截断、异常值与多窗口测试；incomplete输入不出有效分；缺depth时明确不可用，不能把BBO当深度；Tradeability考虑已走幅度、距离可用结构参考、spread/流动性/滑点可用性、funding/OI拥挤、新币及过期；同输入同版本确定性。交易方向与OI/主动成交语义不混淆。
- 复杂度：高；风险：刷量/假突破、评分解释、数据稀疏、未经市场校准。

### P3：状态、事件簇和Outcome

- 计划分支：codex/altcoin-hunter-p3-lifecycle-outcomes。
- 依赖：P2通过并合并。
- 范围：IDLE/WATCH/BUILDING/CONFIRMED/ACCELERATING/EXHAUSTION/INVALID/COOLDOWN，升级/降级/超时/冷却；稳定event cluster；5m/15m/30m/1h/4h/12h/24h Outcome。
- 验收：状态边界与恢复幂等；return/MFE/MAE/direction correctness/latency/drawdown、缺失/未结算状态；同cluster不当独立样本，失败信号不删除，未结算不污染胜率；只使用触发后可获得数据。
- 复杂度：高；风险：重复统计、幸存者偏差、状态震荡、未来数据泄漏。

### P4：独立Telegram Topic的Dry-run交付

- 计划分支：codex/altcoin-hunter-p4-telegram-dry-run。
- 依赖：P3通过并合并。
- 范围：独立Topic配置、L1..L4、首次/升级/更新/失效、event ID/去重键、频控、编辑/删除与失败回滚、发送凭据；默认关闭。
- 验收：Fake Telegram完整生命周期及重试幂等；格式/转义/链接预览/日志脱敏；真实发送必须双门禁，未配置Topic不能发送；所有测试真实Telegram调用0。
- 不为验收请求真实发送。生产Topic/.env配置和真实消息等待单独人工确认。
- 复杂度：中高；风险：消息爆炸、发送后本地失败、重试重复、敏感配置。

### P5：Web独立读取与页面

- 计划分支：codex/altcoin-hunter-p5-web-console。
- 依赖：P4通过并合并；先定位仓库现有Web框架/API及访问控制，以真实代码决定最小接入。
- 范围：总览、热度榜、确认机会、衰竭风险、Coin Detail、时间线、三评分和Outcome；中文字段及明确数据时效/缺失/错误降级。
- 验收：只读API、分页/筛选/索引/访问控制、延迟数据与异常路径、前端交互与截图、数据库写入0；不新建未授权外部托管、不部署。
- 复杂度：高；风险：实时DB读锁、查询放大、访问控制、缺失展示误导。

### P6：多源、联动、校准与生产候选

- 计划分支：codex/altcoin-hunter-p6-production-candidate。
- 依赖：P5通过并合并。
- 范围：多源身份/质量/聚合合同及至少第二来源离线adapter；现有OI/结构/链上雷达只读联动；版本化回放校准、失败样本/分层统计、全链路候选验收与人工上线/回滚手册。
- 公开网络自动授权仅Binance；其他交易所或聚合服务的真实连接若需要，应另行确认，不能假称已验证多源live。
- 验收：真实与离线证据分开；所有阶段合并main、测试/CI成功、有界真实Smoke、独立DB恢复、信号评分/八态/Outcome/Telegram dry-run/Web闭环通过；风险/资源待确认项明确。生产部署、服务启停、生产Migration/.env、真实Telegram保持关闭。
- 如果范围要求的真实长期/第二源验证仍被人工门禁阻挡，不能标记Production Candidate完成。
- 复杂度：高；风险：symbol映射、来源冲突、共振重复、校准过拟合、候选与生产边界。
