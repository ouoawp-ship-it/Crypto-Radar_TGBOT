# OAR 重复源码与文件清理审计

审计基线：`f9edeb9b2427bfabe3ee617644d695cffabc112e`。扫描范围包括 Python
import/AST、CLI 注册、Bash/systemd 引用、测试、文档、Git 跟踪文件、`.gitignore` 和
数据库迁移。动作值严格使用任务规定的枚举。

## 结论

本轮没有文件满足“无生产 import、无 CLI/systemd/测试/迁移/回滚依赖、无独有能力且有
等价替代”这一整组删除条件，因此安全删除数为 **0**。源码数量不是首要目标；先把新逻辑
依赖收敛到领域接口，后续在兼容入口有明确迁移和回滚证据后再删除。

## 候选清单

| 路径/主题 | 当前引用、测试和生产入口 | 独有能力与风险 | 建议动作 |
|---|---|---|---|
| `label_candidates.py` | CLI `label-candidates`、`address_intelligence.py` Arkham 适配、Arkham 测试 | 旧 exact-evidence Arkham schema；和统一候选 Store 重合，但有已部署审计/回滚格式 | compatibility_wrapper |
| `address_intelligence.py` | `address-intelligence` CLI、Watch 本地队列、菜单、P5E 测试 | 通用 Provider、冲突/角色/人工批准事务；生产权威候选边界 | keep |
| 两套候选 JSON | 各自由上述 Store 读取；均有历史文件兼容 | 强行合并会丢旧审核轨迹或破坏回滚 | needs_manual_review |
| `formatter.py` | `notifier.py`、旧 live/replay pipeline 测试 | 格式化 `OnchainAlert`，不是 Token Report/新 deterministic signal | keep |
| `report_formatter.py` | `TokenReportService`/CLI/Telegram Query、P3/P4 测试 | Token Analysis 中文报告和 explorer links | keep |
| `signal_formatter.py` | P7 formatter fixture/test（本轮新增） | 新确定性 signal card，formatter-only；未来由 notification adapter 使用 | keep |
| `notifier.py` | live runtime、旧 collector CLI、notifier 测试 | 旧 `OnchainAlert` 投递，仍有生产兼容入口 | compatibility_wrapper |
| `report_notifier.py` | token-notify/watch、card lifecycle 测试 | 新卡成功后删旧卡、partial/rich-card 保护 | keep |
| `aggregator.py` | `live_runtime.py`、`runtime.py`、pipeline/rolling 测试和 P3.1 文档 | 旧实时 rolling window；AutomationStore baseline 是另一个持久窗口模型 | refactor |
| `scan_baseline.py` | WatchScanner、P6 测试 | Scan Audit median/MAD 和 nested windows；不能由旧 aggregator 替代 | keep |
| `detector.py`/`scorer.py` | live runtime、runtime replay、pipeline 测试 | 旧 collector alert 检测/评分，和 P2 behavior 语义不同 | compatibility_wrapper |
| `behavior.py` | TokenAnalysis、Watch、P2 测试 | 当前持续行为权威实现 | keep |
| `collectors/evm_http.py` | Token Activity、live collector、RPC 测试 | 唯一生产 HTTP JSON-RPC + Adaptive Range；类名 `BaseHttpCollector` 有历史命名债 | refactor |
| `collectors/evm_ws.py` | live runtime 和 runtime 测试 | WSS head trigger，仅旧 live 模式使用 | keep |
| `collectors/replay.py` | replay CLI、测试 | 确定性离线验收与迁移回归 | keep |
| `db.py`/`migrations.py` | live/replay、metadata/price/notifier、迁移测试 | `onchain_flow.db` schema 1–3；仍是旧 facts/alerts 权威实现 | compatibility_wrapper |
| `automation_store.py` | Registry/Watch/Lease/Baseline/Bridge | 独立 Automation DB schema 6，不重复旧 collector DB | keep |
| `health.py` runtime JSON | live runtime/status/doctor | 可重建状态，不是业务事实；仍有运维入口 | keep |
| `arkham_intelligence.py` | 两套候选流程、显式 CLI、测试 | 可选 Provider，默认零调用 | keep |
| Dune/OLI/Manual Provider | 统一地址情报 CLI/菜单/测试 | 只生成 pending 候选，不能进入 Watch 热路径 | keep |
| `controlled_alert_preview.py` | CLI/watch 定向测试 | 受控预览门禁，不是生产发送捷径 | keep |
| `runtime.py`/`live_runtime.py` | `once/daemon/live/replay` CLI、systemd 安装文档、测试 | 旧 Collector 兼容入口和回滚路径 | deprecate |
| `scripts/install_onchain_flow.sh` | 安装文档/部署测试 | 旧 Collector Unit 安装；仍是回滚和兼容路径 | deprecate |
| `scripts/install_oar_watch.sh`、`run_oar_watch.sh` | systemd Unit/部署 | 当前 Watch 唯一生产启动路径 | keep |
| `scripts/install_oar_query.sh`、`run_oar_query.sh` | Query Unit/部署 | 当前群内查询 Worker 启动路径 | keep |
| `scripts/paopao_config.py` | 菜单、安装/更新、配置测试 | 配置锁/备份/原子写/回滚权威入口 | keep |
| 直接 `.env` 修改脚本 | 搜索未发现新的 OAR 生产脚本绕过 ConfigManager | 不存在可删候选；保持审计规则 | keep |
| 旧阶段文档 P1–P6 | 运维/回滚/测试依据仍引用 | 描述阶段历史，可能与现状有偏差但含迁移语义 | deprecate |
| `OAR_CURRENT_ARCHITECTURE_AUDIT.md` | P7 新权威现状文档 | 替代“从旧阶段文档拼接现状”的做法 | keep |
| fixtures | 全部被对应测试引用；无重复内容完全等价 | 删除会降低回归覆盖 | keep |

## 专项重复结论

1. Transfer 数据模型只有 `models.NormalizedTransfer` 是领域事实权威；RPC 原始 log 和
   fixture dict 是边界输入，不应提升为第二模型。
2. 地址规范化统一使用 `labels.normalize_evm_address`；P7 Repository/chain config 复用它。
3. CEX Flow 统一使用 `classifier.classify_transfer`；P7 的 adapter 只提供接口，不复制规则。
4. 两种 Rolling Window 服务于不同持久边界，现阶段不能互删；后续应让它们共享窗口
   value object，而不是合并数据库。
5. HTTP RPC 只有一个通用实现；`BaseHttpCollector` 名称未来可重命名并保留 alias。
6. TelegramGateway 是唯一 HTTP 发送器；notifier/report_notifier/query 只是不同业务编排。
7. 配置读取统一经 `OnchainSettings`，可变配置写入统一经 ConfigManager。
8. SQLite 与 JSON Store 不是同义复制：DB 存结构化权威状态，JSON 存兼容候选、路由、
   outbox、cache 或状态快照。

## Git 与运行文件审计

- `git ls-files` 未发现生产 SQLite、日志、缓存、私有 `.env`、私有 CSV 或 backup 目录被
  跟踪。
- `.gitignore` 已覆盖 `data/`、私有配置、缓存和运行文件；example/fixture 仍可跟踪。
- 未发现名为 `new_`、`v2_`、`final_`、`temp_`、`backup_` 的新增源码。
- 本轮没有注释掉的大段旧代码，也没有生成文件。

## 后续删除门禁

旧 collector 或 Arkham compatibility path 只能在：生产 CLI 已迁移、历史文件已单向迁移
并核验、systemd/安装/回滚文档不再引用、测试已改测保留实现、至少一个版本的 deprecation
周期完成后删除。删除 PR 必须逐文件给出 `git grep`、import graph、CLI help、systemd、
fixture 和迁移证明。
