# FinalShell 中文运维菜单

`paopao` 是现有服务器命令的兼容入口。在 FinalShell 的交互式终端中运行：

```bash
paopao
# 或
paopao menu
# 可选短命令
pp
```

只有 stdin 和 stdout 都是 TTY 时才进入中文菜单。非交互 SSH 命令、SCP、SFTP、管道或自动化任务只显示帮助并确定性退出，不会打开菜单。安装脚本不会修改 `.bashrc`，也不会在登录时自动启动菜单。

## 首页

首页只读取本地轻量状态：

- Version 与当前 Commit
- `paopao-radar`、`paopao-market-stream`、`paopao-oar-watch`
- Base RPC 是否配置
- DeepSeek 是否配置/启用
- 链上 Topic 是否配置
- Registry、Active Watch、Open Unresolved
- 最近扫描与 Automation DB 状态

打开菜单不会自动运行 `git fetch`、RPC、AI、Telegram、Provider Check 或完整测试。

## 主菜单

1. 总览与健康检查
2. 服务管理
3. 检查更新与版本
4. API、Token 与密钥
5. AI 模型与提示词
6. 链上活动雷达
7. Telegram 设置与测试
8. 数据库、备份与清理
9. 日志与故障诊断
10. 高级运维
11. 五因子资金流雷达候选清单

所有原命令继续兼容，例如：

```bash
paopao status
paopao doctor
paopao readiness
paopao radar-status
paopao stable-check
paopao providers
paopao backup
paopao telegram-test
paopao check-update
paopao update
paopao version
paopao flow-candidates --all
```

`paopao radar-status` 只读取本地运行状态、启动观察历史、资金费率状态和资金流候选状态，不访问网络。它分别显示启动预警、资金摘要、资金费率警报和资金流雷达的最近运行、下次调度、候选数量、最近投递结果及投递门禁。`delivery_block_reason=main_bot_dry_run` 表示雷达仍在计算，但真实 Telegram 投递被安全模式阻止；此状态不是雷达停机。

## 五因子资金流雷达候选轮换

候选池覆盖当前全部符合成交额、排除名单和 Binance USDT 永续合约条件的
市场，不再受旧 `FLOW_CANDIDATE_POOL=60` 截断。每轮仍只深度扫描
`FLOW_SCAN_LIMIT` 个（默认 24），按“累计扫描次数最少、最久未扫描、当前
市场优先级”确定性轮换，避免扩大单轮 K 线、OI 和主动成交请求预算。

菜单“查看全市场完整候选清单”和命令 `paopao flow-candidates --all` 只读取
本地 `flow_candidate_state.json`，不会访问 Binance 或 Telegram。清单包含当前
优先级、下轮顺位、扫描次数和选入原因；文件由每轮资金流雷达原子更新。

每轮 `TG_FLOW_RADAR` 话题卡片也会直接附带本轮深度扫描币种、下一轮优先
队列和全市场候选市值排行。市值只读自 15 分钟内的本地
`market_snapshots.db`，按 `≥ $10B`、`$1B–10B`、`$100M–1B`、`< $100M`
分层并在层内保持全局市值名次；没有可靠市值的候选单独列为“市值待补全”，
不会使用成交额冒充市值，也不会改变候选资格或轮换顺序。该展示不增加
Binance、CoinPaprika、OI、K 线或 Telegram 请求；内容超过单条限制时继续
使用现有安全分段和卡片替换流程。菜单入口仅作为不联网的离线备用查看方式。

## API、Token 与密钥

菜单不会使用 `sed` 修改环境文件，而是调用：

```bash
python scripts/paopao_config.py status
python scripts/paopao_config.py set <白名单字段>
python scripts/paopao_config.py enable OAR_AI_ENABLE
python scripts/paopao_config.py disable OAR_AI_ENABLE
python scripts/paopao_config.py validate
```

Secret 不作为命令行参数。根据当前部署者要求，所有 FinalShell 输入均在当前终端明文显示；非 TTY 仍只从 stdin 读取。输入 API Key、Token 或 RPC 时请关闭共享屏幕和录屏，并确认周围无人查看。管理器使用字段白名单、文件锁、写前备份、原子替换、权限 600 和写后校验；失败时自动恢复原文件，并保留未知字段和注释。

输入可见只影响当前输入行。保存后的配置状态、日志和报告仍保持脱敏；管理器不会重新打印刚输入的实际值。管理器还会离线校验 Bot Token、Chat ID、链上 Topic ID、DeepSeek 模型/Base URL 和 CEX 标签安全相对路径；校验不发起 Telegram、Provider 或 RPC 请求。每个环境文件最多保留 30 个 `.bak.*` 备份，不会删除其他文件。

状态只显示 configured/not_configured 或安全枚举，不打印完整 RPC URL、Token、Chat ID、Topic ID、API Key 或 Authorization。

“API、Token 与密钥”菜单的 Base RPC 高级设置可通过配置管理器原子修改
`ONCHAIN_RPC_MAX_BLOCK_RANGE`。该值只接受 1～10000 的整数；修改会在文件锁内
创建备份、写入后执行离线配置校验，并保持 `.env.onchain` 权限为 600。

## AI 与 Prompt

AI 菜单可以：

- 查看脱敏配置并原子应用 DeepSeek V4 Pro 推荐 Profile
- 设置 API Key、Base URL、Model、Thinking Mode、Reasoning Effort、Max Tokens、Timeout 和 Max Retries
- 查看 Prompt 状态与 Hash
- 显示、编辑、校验、恢复默认 Prompt
- 查看历史并回滚
- 执行 `ai-provider-check` 和合成 `ai-smoke`
- 通过 `ai-request-check` 量化真实 Token Analysis 的 AI 请求规模而不调用 AI
- 查看或只清理 AI 结果 Cache（小时调用预算保留）
- 在 Provider Check 与 AI Smoke 之后显式 enable/disable

核心安全 Prompt 不可由菜单编辑。Operator Prompt 只影响业务分析风格；完整规则见 [OAR_DEEPSEEK_V4_PRO.md](OAR_DEEPSEEK_V4_PRO.md)。

## OAR 管理

OAR 菜单复用现有 CLI：

- Registry list/add/verify/disable
- Watch list/add/remove
- `bridge-once`
- `watch-once`
- Unresolved 本地摘要
- `token-activity`
- `token-report`
- `labels-check`

Registry 验证和 Token 查询仍要求显式 `--allow-network`。验证时必须选择“设为 Primary”或“仅验证为 Secondary”；只有前者传递 `--set-primary`。Symbol 不一致时默认停止，显示链上 Symbol 与 Market Symbol，并且只有输入 `接受Symbol不一致` 后才传递 `--accept-symbol-mismatch`。菜单不会按 Symbol 猜合约，也不会自动验证 Pending Token；禁用 Registry 还要求输入 `禁用Registry`。

## 主 BOT 运行模式

“服务管理 → 主 BOT 运行模式”提供：

1. 安全 Dry-run；
2. Real 真实发送；
3. 查看当前脱敏状态；
4. 重启主 BOT；
5. 停止主 BOT。

Dry-run Profile 会在同一次文件锁和原子写入内设置：

```dotenv
MAIN_BOT_DELIVERY_MODE=dry_run
MAIN_BOT_REAL_SEND=false
MAIN_BOT_REAL_SEND_ACK=
```

它不会自动重启服务。重启后 systemd 包装器严格执行 `main.py loop`，
不带 `--send` 或 `--confirm-real-send`，因此 Telegram HTTP 保持为 0。

Real Profile 必须先输入 `启用真实主BOT提醒`，且 Bot Token 和 Chat ID
均已配置；保存后仍不会自动启动或重启。Real 模式重启时还必须输入
`重启真实主BOT`。包装器只有在 Mode、真实发送开关和固定 ACK 全部一致
时才执行 `main.py live --send --confirm-real-send`，随后仍由 `live` 的
现有 readiness 和双发送门禁决定是否运行。

未知模式或不完整 Real Gate 会返回固定安全错误并以退出码 2 停止；
systemd 不会因此反复重启。SIGINT 退出码 130 视为正常停止，真实运行异常
仍可由 `Restart=on-failure` 恢复。

主 BOT Dry-run 日志只显示 `topic_configured` 和
`reply_target_configured` 布尔值，不显示 Topic ID、Reply Message ID、
Chat ID、Bot Token 或 Telegram API URL。本地私有历史的既有 Schema
保持不变。

### Watch 通知模式

“受控自动预警门禁”默认关闭。启用时，链上原有规则、成熟历史异常和受支持的市场来源必须同时满足，才会构建报告并进入当前 Delivery Mode；它不会自动开启 AI、Dry-run 或 Real。启用需输入完整确认短语 `启用受控自动预警`，保存后需要显式重启 OAR Watch。

链上活动雷达菜单可原子切换：

- Observe：只执行链上查询和分析，不创建通知或 AI 客户端；
- Telegram Dry-run：输入 `启用链上DryRun` 后启用规则报告 dry-run；
- Real：输入 `启用真实链上提醒`，且 Bot、Chat、Topic 和现有真实发送门禁完整时才保存；
- 自动 AI：输入 `启用自动AI分析` 只设置 Watch 偏好，不会打开全局 `OAR_AI_ENABLE`。

切回 Observe 会关闭真实发送、清空固定确认值并关闭 Watch 自动 AI。模式修改后仍需由操作者显式重启 OAR Watch 服务。

## Telegram

普通菜单只提供脱敏配置、链上 Topic 设置、规则报告 dry-run 和 readiness。Dry-run 不带 `--send` 或 `--confirm-real-send`。

主雷达和自动链上提醒仍是 outbound-only。可选的
`paopao-oar-query.service` 为群内显式查询提供唯一的 `getUpdates` Worker；
默认关闭，不设置 webhook，也不改变主 BOT 或 OAR Watch。主雷达、链上活动
雷达和查询 Worker 共用 `.env.oi` 中唯一的 `TG_BOT_TOKEN` 与 `TG_CHAT_ID`；
`.env.onchain` 保存链上专用 Topic 和查询 Worker 的独立安全开关。

进入“Telegram 设置与测试 → 自动识别群并创建/修复链上话题”后，程序会：

1. 使用主 BOT 已配置的群执行 `getMe`、`getChat` 和 `getChatMember`；
2. 对已经保存的链上 Topic 使用 `sendChatAction` 做无持久消息验证；
3. Topic 有效时直接复用；
4. Topic 缺失、已关闭或失效，且机器人具有管理话题权限时，调用一次
   `createForumTopic` 创建“链上活动雷达”；
5. 通过配置管理器原子保存 Topic ID，但只显示 configured 状态。

该操作不调用 `getUpdates`，不创建或发送 Telegram 消息，也不会切换
Dry-run/Real。Telegram Bot API 不能仅凭 Token 枚举机器人加入的所有群，
因此这里的“自动识别群”是复用主 BOT 已审核的共享群配置，而不是猜测群。
旧的 `telegram-topic-link` CLI 仅作为兼容恢复工具保留，不再是正常菜单流程。

话题创建不会自动发送说明。需要立即生成并置顶说明时，使用“Telegram
设置与测试 → 发送并置顶链上话题说明”，输入完整确认短语后，菜单才会
执行一次：

```bash
python onchain_main.py telegram-topic intro \
  --allow-network --send --confirm-real-send
```

该命令只发送既有的链上话题说明并置顶，不发送链上报告，不切换主 BOT
或长期 OAR Watch 的运行模式；重复执行会复用当前版本的已置顶说明。

真实 Telegram 测试只在“高级运维”中出现，并要求输入完整短语：

```text
发送真实测试
```

代码仍保留现有真实发送双门禁；菜单不是绕过通道。

### 群内 @Bot 链上异动查询

进入“Telegram 设置与测试 → 群内 @Bot 链上异动查询”，输入完整确认短语
`启用群内链上查询` 后，菜单才会原子保存开关、安装并启动独立查询服务。
服务只接受共享群的“链上活动雷达”话题中的显式命令：

```text
@Bot用户名 查询 CBDOGE 15m
@Bot用户名 查询 0x完整Base合约 1h
/oar@Bot用户名 CBDOGE 4h
```

支持 `15m`、`1h`、`4h`。Symbol 只解析本地已验证且唯一 Primary 的
Registry 记录；不存在或存在歧义时拒绝猜合约。完整合约只执行 Base
只读 Token Activity 与规则分析，不调用 AI，不创建 Watch，不执行交易。
查询按用户冷却并受全局小时预算约束，结果回复到原查询消息。Worker 只保存
Telegram offset、脱敏用户 Hash 和有界时间戳，不保存入站正文或用户 ID。

查询服务启动时会拒绝已配置 webhook 或第二个 `getUpdates` Worker，避免
抢占更新。服务默认关闭；停用会同时停止 Worker、关闭配置开关并清空确认值。

## 更新、日志与备份

- 更新检查：`scripts/update_server.sh --check`
- 安全更新：确认 `执行安全更新` 后运行 `scripts/update_server.sh --yes`
- 日志：按主 BOT、Market Stream、OAR 或 error 过滤
- 诊断：组合现有 `doctor` 和脱敏配置状态
- 备份：复用 `database-backup` 的一致性备份与恢复验证
- 清理：复用现有 bounded cleanup

菜单不猜测数据库恢复目标。真实恢复必须先核对备份清单，并输入完整短语：

```text
恢复数据库
```

## 高风险确认

以下操作不能只用 `y/N`：

- 停止主 BOT：`停止主BOT`
- 重启主服务：`重启主服务`
- 启用主 BOT Real：`启用真实主BOT提醒`
- 重启 Real 主 BOT：`重启真实主BOT`
- 安全更新：`执行安全更新`
- 真实 Telegram 测试：`发送真实测试`
- 清理 AI 结果 Cache：`清理AI缓存`
- 恢复 Prompt：`恢复提示词`
- 配置回滚：`回滚配置`
- 数据库恢复：`恢复数据库`
- 禁用 Registry：`禁用Registry`
- Watch Dry-run：`启用链上DryRun`
- Watch Real：`启用真实链上提醒`
- Watch 自动 AI：`启用自动AI分析`

短语不完全一致时操作取消。

所有确认短语都使用普通可见输入。Operator Prompt 的多行编辑通过
`$EDITOR`（默认 `vi`）完成，正文在编辑器中正常显示；单行配置也会在
FinalShell 输入行原样显示。菜单不会使用星号替代字符，也不会关闭终端
Echo。输入敏感值前应关闭共享屏幕和录屏。

## Arkham 辅助 CEX 标签候选

“链上活动雷达 → CEX 标签候选”提供 Arkham 配置状态、Provider Check、
候选发现、Pending 查看、批准、拒绝和 Labels Check。Arkham 只产生待人工
审核的 Base CEX 地址标签候选，不参与长期 Watch、Transfer 事实、行为分析
或 Telegram 触发。

Arkham API Key 在 FinalShell 输入行明文可见，但只经 stdin 进入配置管理器；
保存后不会回显。候选发现必须手工提供已审核 Base Token 合约并显式联网。
批准操作要求完整输入 `批准CEX标签`，Pending 候选在批准前不会进入私有
生产 CSV。更完整的证据门槛、请求上限和回滚方式见
[`OAR_P5D_ARKHAM_LABEL_REVIEW.md`](OAR_P5D_ARKHAM_LABEL_REVIEW.md)。

## 地址情报中心

“链上活动雷达 → 地址情报中心”统一管理本地已批准标签、Dune、OLI、
BaseScan 人工来源、可选 Arkham 和本地行为角色候选。Dune 与 Arkham Key
均可为空；未配置时显示 `optional_disabled`，不影响 Watch、DeepSeek 或
Telegram。

Watch 热路径只生成本地未知地址队列，不会请求任何外部标签 Provider。
显式候选发现才允许使用 `--allow-network`。Dune CSV、OLI Parquet 和
BaseScan CSV 导入都只进入 Pending；批准必须输入 `批准地址标签`。冲突、
到期或与本地 approved 身份锚点不一致的候选会 fail closed。Dune 自动同步
默认最多 6 次请求、1 秒轮询间隔和 30 秒执行超时；BaseScan Public Tag
不会默认识别为 CEX。
过期或只有行为推断的候选不能直接成为生产 CEX 身份。详见
[`OAR_P5E_ADDRESS_INTELLIGENCE.md`](OAR_P5E_ADDRESS_INTELLIGENCE.md)。

## 安装

`scripts/install_shortcuts.sh` 是安装流程使用的幂等入口；它不访问网络、不启动菜单或服务。`scripts/install_server.sh` 会确保：

- `/usr/local/bin/paopao`
- 可选 `/usr/local/bin/pp -> /usr/local/bin/paopao`

可以用 `INSTALL_PP_SHORTCUT=0` 跳过 `pp`。安装只验证脚本可执行，不会启动交互菜单。
