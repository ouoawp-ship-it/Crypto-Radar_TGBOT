# 泡泡抓币中文安装目录

更新时间: 2026-05-25

这份说明用于第一次安装、重新安装、更新和排错。配置文件 `.env.oi` 不会提交到 GitHub，里面只放服务器自己的 token、群 ID 和 API key。

## 1. 第一次安装

在服务器执行:

```bash
cd ~
git clone https://github.com/ouoawp-ship-it/paopao-crypto-radar.git
cd paopao-crypto-radar
bash scripts/install_server.sh
```

安装脚本会进入中文向导，按顺序完成:

1. 显示安装目录和配置文件位置。
2. 安装 `git`、`python3`、`python3-venv`、`python3-pip`。
3. 创建 `.env.oi`。
4. 输入 Telegram bot token 和群 ID。
5. 默认启用 Telegram 话题自动分类，不手动填写话题 ID。
6. 可选输入 Coinalyze API key。
7. 创建 `.venv` 并安装 Python 依赖。
8. 运行编译检查和单元测试。
9. 生成 dry-run 启动观察历史。
10. 运行 readiness。
11. 安装并启动 systemd 服务。

## 2. 输入项说明

`TG_BOT_TOKEN`
: BotFather 给你的机器人 token，格式类似 `123456:ABC...`。

`TG_CHAT_ID`
: Telegram 群 ID，通常类似 `-1001234567890`，也可以是频道用户名 `@channel_username`。

`TG_TOPIC_ID` 以及其他 `TG_..._TOPIC_ID`
: 这是 Telegram 话题的数字 `message_thread_id`。默认不需要填，机器人有权限时会自动创建和记录话题。

`COINALYZE_API_KEY`
: Coinalyze API key。只在脚本提示 `COINALYZE_API_KEY 可选` 时填写。直接回车就是不启用历史清算辅助；填写后会作为结构雷达外部确认的历史清算辅助数据源。

## 3. 修改 token、群 ID、Coinalyze key

安装完成后，如果填错了 bot token、群 ID 或 Coinalyze key，不需要重新安装项目，推荐直接打开 Web 控制台的“配置”页修改:

```text
http://服务器IP:8080/
```

Web 控制台支持运行健康度、最近错误、日志搜索筛选、推送样例预览、GitHub 更新检查、分类配置页、真实模块开关、保存前预览改动、保存后中文结果提示、最近 `.env.oi` 备份一键恢复/删除，以及结构复盘参数建议一键应用。保存成功后会自动应用新配置；主服务和结构雷达会自动重启，改了 Web 端口或 Web 令牌时 Web 控制台会短暂重启，稍后刷新页面即可。

服务器命令行仍保留应急配置向导:

```bash
cd ~/paopao-crypto-radar
bash scripts/install_server.sh config
```

应急配置向导会提供这些功能:

```text
1. 修改 TG_BOT_TOKEN
2. 修改 TG_CHAT_ID / 群 ID
3. 修改 COINALYZE_API_KEY
4. 修改 Telegram 话题配置
5. Telegram / Coinalyze 全部重新填写
6. 清理旧 Telegram 话题路由
0. 保存并退出
```

如果修改了 `TG_CHAT_ID`，脚本会自动删除 `data/tg_topic_routes.json`。这是必要的，因为旧群的话题 ID 不能继续用于新群。服务重启后，bot 会按新群重新自动创建话题。

如果修改了 `TG_BOT_TOKEN`，建议确认新 bot 已经加入目标群，并且具备发送消息、管理话题、置顶消息权限。

`COINALYZE_API_KEY` 是可选清算历史辅助，直接回车表示关闭 Coinalyze；结构雷达仍会使用 Binance 免费盘口深度做外部确认。

如果走服务器命令行的应急配置向导，修改完成后向导会提示是否立即重启服务。也可以手动重启:

```bash
sudo systemctl restart paopao-radar
```

## 4. 快捷操作命令

安装脚本会自动写入 `/usr/local/bin/paopao`。以后在服务器任意目录输入:

```bash
paopao
```

会打开中文数字菜单。服务器日常只需要记住这一个入口命令。

菜单会显示:

```text
1. 查看 Web 地址和令牌
2. 查看 Web 控制台服务状态
3. 查看 Web 控制台实时日志
4. 重启 Web 控制台服务
5. 检查 GitHub 是否有更新
6. 更新项目代码
7. 查看当前版本
0. 退出
```

菜单顶部会详细说明 Web 地址、访问令牌、项目版本，以及哪些功能应该去 Web 页面操作。配置修改、服务启停、日志查看、测试消息、readiness、doctor、cleanup、结构复盘等控制功能已经移到 Web 控制台。

Web 控制台会作为 `paopao-web.service` 安装并启动，浏览器直接打开:

```text
http://服务器IP:8080/
```

页面会要求输入 `WEB_ADMIN_TOKEN`。更新脚本会自动生成令牌，查看令牌:

```bash
paopao
```

进入菜单后选择 `1. 查看 Web 地址和令牌`。

相关配置项:

```bash
WEB_HOST=0.0.0.0
WEB_PORT=8080
WEB_ADMIN_TOKEN=
```

如果 `WEB_ADMIN_TOKEN` 为空，程序会拒绝启动公网监听；安装/更新脚本会自动补齐。

如果是从旧版本更新上来，想只安装快捷命令:

```bash
cd ~/paopao-crypto-radar
bash scripts/install_server.sh shortcut
```

### AI 助手 Bot 和价格提醒

v1.13.0 新增 `paopao-ai.service`。它使用独立的 `AI_BOT_TOKEN`，和群里推送雷达信号的 `TG_BOT_TOKEN` 分开：

```text
TG_BOT_TOKEN = 群话题推送雷达信号
AI_BOT_TOKEN = 私聊 AI 助手、手动价格提醒、个人提醒
```

推荐在 Web 控制台的「配置 -> AI 助手」里填写：

```bash
AI_ASSISTANT_ENABLE=true
AI_BOT_TOKEN=
AI_ADMIN_USER_IDS=你的Telegram用户ID
AI_PRICE_ALERTS_ENABLE=true
AI_ALERT_CHECK_INTERVAL_SEC=10
```

默认建议只用私聊。如果开启群内调用，需要同时配置：

```bash
AI_ALLOW_GROUP_CHAT=true
AI_ALLOWED_CHAT_IDS=-1001234567890,-1009876543210
```

`AI_ALLOWED_CHAT_IDS` 支持多个群/频道 ID，用英文逗号分隔，也可以填 `@channel_username`。群里即使开通了白名单，也不会读取一句话就回复，只有别人 `@机器人用户名` 或回复机器人消息时才会处理。

价格提醒不需要 AI API Key。打开 AI 助手 Bot 私聊，点击「设置价格提醒」，可选择目标价提醒、价格急涨急跌、持仓量变化、资金费率变化；支持 Binance、Bybit、OKX、Bitget、Gate 的现货或 USDT 合约价格源，并可选择提醒一次、重复提醒或持续每5分钟提醒。v1.25.0 起，Web 后台的价格提醒页支持按状态、类型和关键词筛选提醒，日志中心会显示筛选摘要和第一条错误。v1.26.0 起，Web 接口统一返回 `_meta` 元信息，页面操作结果会显示 HTTP 状态、接口路径、服务端时间和浏览器耗时，检查测试页也提供 Web API 自诊断入口。v1.27.0 起，Web 后台新增审计记录页，配置保存、备份恢复/删除、检查测试、服务启停、价格提醒管理和 AI 提示词操作都会记录操作摘要，不保存 Token、API Key 或提示词正文。v1.28.0 起，Web 后台新增诊断报告页，可一键复制安全运维快照，汇总服务状态、最近错误、失败审计和日志错误片段。v1.29.0 起，配置页保存前会显示影响预检，包括影响模块、自动重启服务、敏感/危险配置提醒和回滚说明。

```text
BTC 现在多少钱
查 BTC
GWEI 怎么看
SOL 可以做多吗
我的提醒有哪些
暂停提醒 12
恢复提醒 12
删除提醒 12
分析这段：粘贴雷达信号或市场数据
直接粘贴启动雷达/结构雷达/资金流数据，机器人会自动按分析处理
```

私聊发送 `/start` 会打开中文按钮首页。v1.19.0 起，首页里的「设置价格提醒」会按固定步骤执行：选择监控类型 -> 输入币种 -> 识别可用现货/合约 -> 手动选择交易所 -> 按类型选择目标价或窗口/阈值/方向 -> 选择触发方式 -> 确认添加。只有点击「确认添加提醒」才会真正创建提醒。v1.21.3 起，AI Bot 只保留 `/start`，其它斜杠入口全部取消。查价格直接发送 `BTC`，看行情直接发送 `BTC 怎么看`，粘贴雷达/市场数据会自动进入专业分析，提醒管理点击首页「我的提醒」。v1.21.4 起，「我的提醒」显示的是当前列表序号，不再暴露数据库真实 ID。

自然语言不再创建价格提醒。你说“BTC 跌破 58000 提醒我”时，机器人会提示去点击「设置价格提醒」走手动选择流程；只转发雷达信号会自动走数据分析，不会乱建个人提醒。

如果要启用真正 AI 问答，再配置：

```bash
AI_PROVIDER_ENABLE=true
AI_API_KEY=
AI_BASE_URL=https://api.deepseek.com
AI_MODEL=deepseek-v4-pro
AI_REQUEST_TIMEOUT_SEC=90
AI_PROMPTS_FILE=ai_prompts.json
SIGNAL_EVENTS_FILE=signal_events.json
SIGNAL_EVENTS_LIMIT=5000
SIGNAL_EVENTS_RETENTION_DAYS=60
```

`AI_MODEL` 只填写模型名本身，比如 `deepseek-v4-pro`，不要填写成 `AI_MODEL=deepseek-v4-pro`。使用 `deepseek-v4-pro` 或 `deepseek-v4-flash` 时，请求会自动按 DeepSeek v4 接口带上思考模式参数；如果接口返回 400，Web 和 AI Bot 会显示服务端返回的具体错误正文。`deepseek-v4-pro` 思考模式响应较慢，超时时可在 Web 后台把 `AI_REQUEST_TIMEOUT_SEC` 调到 120-180，或者临时改用 `deepseek-v4-flash`。

v1.16.0 起，AI Bot 支持自然语言查询币种雷达档案：例如“查 BTC”“GWEI 怎么看”“SOL 可以做多吗”。它会读取 `data/signal_events.json`、推送历史、启动雷达历史、结构复盘和资金费率状态，再结合当前 Binance 行情、OI、成交量、市值、流动性、结构和多交易所资金费率，输出偏多/偏空/观望/高风险观望。v1.19.0 起，AI Bot 首次打开或发送 `/start` 会显示按钮首页，价格提醒走多类型手动监控流程；v1.20.0 起，价格提醒扫描不再阻塞用户聊天和按钮处理，五大交易所价格源识别会并发执行并短时间缓存。v1.21.1 起，首页不再显示 AI 对话按钮，直接发消息即可自动分流到泡泡 AI 助手或专业分析师模式。v1.21.2 起，慢任务临时提示会在最终回复成功后自动删除。v1.21.3 起，只保留 `/start`，其它 Bot 功能全部去命令化。v1.21.4 起，提醒编号用当前列表序号，交易所/交易对字段按 Telegram HTML 优化展示。v1.21.5 起，按钮回调先静默 ACK，不再对每次按钮点击弹出“处理中...”。v1.22.0 起，AI Bot 热路径复用 Settings 与精确报价短缓存，并通过 `ai-assistant: slow_callback` / `slow_message` 日志定位慢请求。v1.22.1 起，统一意图分类器会先判断分析/市场数据，再判断查价，避免“当前价格”等字段误触发查价。v1.23.0 起，Web 后台菜单改为总览、AI 助手、价格提醒、雷达服务、配置中心、日志中心、检查测试、更新备份、功能说明。v1.24.0 起，总览和日志中心可 15 秒自动刷新，最近错误可一键跳转对应日志。v1.25.0 起，价格提醒页支持状态/类型/关键词筛选，日志中心会显示筛选摘要和第一条错误。v1.26.0 起，Web API 自带元信息和耗时显示，检查测试页可以一键做 Web API 自诊断。v1.27.0 起，Web 审计记录页可以查看后台关键操作流水，并按成功/失败和关键词筛选。v1.28.0 起，Web 诊断报告页可以复制安全运维快照，方便排查 bug。v1.29.0 起，Web 配置保存前会预检影响模块和自动重启服务。`SIGNAL_EVENTS_*` 控制结构化信号索引的文件名、保留数量和保留天数，通常保持默认即可。

Web 控制台的「AI 助手」页提供「编辑 AI 提示词」入口，可以编辑泡泡 AI 助手提示词和专业分析师提示词。泡泡 AI 助手用于日常问答、生活问题、状态解释和提醒说明，默认语气更轻松；专业分析师用于 `分析这段：...`、`帮我分析...` 以及自动识别出的雷达/市场数据。提示词默认保存在 `data/ai_prompts.json`，保存后会自动重启 `paopao-ai`。

没配置 `AI_BOT_TOKEN` 时，`paopao-ai.service` 会保持等待状态，不影响主雷达推送。

## 5. 版本号规则

项目根目录有一个 `VERSION` 文件，用来记录用户可读的版本号。当前为 `v1.29.0`，后续功能更新按 `v1.29.1`、`v2.0` 这种方式递增。

中文菜单里的“检查 GitHub 是否有更新”和“更新项目代码”会同时显示:

- 当前版本号
- GitHub 最新版本号
- 当前 git 提交号
- GitHub 最新 git 提交号

例如:

```text
当前版本 : v1 (d5a72c3)  Add interactive update check shortcut
GitHub版本: v1.5 (xxxxxxx)  Add xxx feature
```

以后如果只是小修复，也会保留 git 提交号作为精确定位；如果是功能变化，会同步升级 `VERSION`。

## 6. 更新时 `.env.oi` 的安全同步

中文菜单里的 `6. 更新项目代码` 会自动运行 `.env.oi` 安全同步:

- 会补充 `.env.oi.example` 里新增的普通配置项。
- 会自动升级明确写进迁移白名单的默认参数，例如资金摘要频率这类项目级默认值。
- 不会覆盖 `TG_BOT_TOKEN`、`TG_CHAT_ID`、`COINALYZE_API_KEY`、`TG_TOPIC_ID`、各类话题 ID。
- 如果你自己把某个参数改成了自定义值，脚本会尽量保留，不会用新默认值强行覆盖。

所以后续我优化配置参数后，你通常直接执行:

```bash
paopao
```

然后选择 `6. 更新项目代码`，即可完成代码更新、依赖检查、测试、`.env.oi` 安全同步和服务重启。

## 7. Telegram 话题推荐设置

推荐默认配置:

```bash
TELEGRAM_USE_TOPIC=true
TG_AUTO_CREATE_TOPICS=true
TG_TOPIC_INTRO_ENABLE=true
TG_TOPIC_INTRO_PIN=true
```

bot 需要在群里具备这些权限:

- 发送消息
- 管理话题
- 置顶消息

每个话题第一次真实推送前，项目会先发送一条中文说明消息，并尝试置顶。说明消息会解释这个话题推什么、怎么看信号。

启动预警话题里，同一币种如果先出现预警、后续又出现更高阶段信号，新消息会自动回复上一条该币启动消息，方便在 Telegram 里按一条回复链追踪。

## 8. 重新安装

如果你想完全重新安装，并备份旧目录:

```bash
cd ~
pkill -f "main.py daemon" || true

if [ -d paopao-crypto-radar ]; then
  mv paopao-crypto-radar "paopao-crypto-radar-old-$(date +%Y%m%d-%H%M%S)"
fi

git clone https://github.com/ouoawp-ship-it/paopao-crypto-radar.git
cd paopao-crypto-radar
bash scripts/install_server.sh
```

如果你想保留旧 `.env.oi`，先备份:

```bash
cp ~/paopao-crypto-radar/.env.oi /tmp/paopao.env.oi.backup
```

新项目 clone 完之后再恢复:

```bash
cp /tmp/paopao.env.oi.backup ~/paopao-crypto-radar/.env.oi
cd ~/paopao-crypto-radar
bash scripts/install_server.sh
```

## 9. 更新现有项目

```bash
cd ~/paopao-crypto-radar
bash scripts/update_server.sh
```

也可以用中文菜单:

```bash
paopao
```

进入菜单后选择 `5. 检查 GitHub 是否有更新` 或 `6. 更新项目代码`。

更新脚本会执行:

- `git fetch` 检查 GitHub 最新版本
- 显示当前版本和 GitHub 版本
- 有更新时询问是否更新
- `git pull --ff-only`
- 安全同步 `.env.oi`，保留 token、群 ID、key 和话题 ID
- 安装/刷新依赖
- 编译检查
- 单元测试
- 自动清理 pycache、临时文件、过期日志、过期结构图和根目录临时报告
- 安装/刷新 `paopao-radar.service` 主服务、`paopao-structure.service` 结构雷达独立服务、`paopao-web.service` Web 控制台服务和 `paopao-ai.service` AI 助手服务
- 即使当前代码已经是最新版，也会刷新快捷命令、补装 `paopao-structure.service`、`paopao-web.service`、`paopao-ai.service` 和 `paopao-cleanup.timer`，并重启已安装服务

结构雷达独立服务由 `paopao-structure.service` 管理，专门运行 `structure-loop`，用于每小时 55 分提前临界扫描和整点后 5 分收线确认。服务状态、日志和重启操作统一在 Web 控制台完成。

自动清理由 `paopao-cleanup.timer` 管理，默认每小时执行一次 `python main.py cleanup --force-cleanup`。手动立即清理可以在 Web 控制台执行。

查看自动清理 timer:

```bash
systemctl list-timers paopao-cleanup.timer
journalctl -u paopao-cleanup.service -n 80 --no-pager
```

## 10. 常用检查命令

```bash
cd ~/paopao-crypto-radar
. .venv/bin/activate

python main.py status
python main.py readiness
python main.py telegram-test --send --confirm-real-send
python main.py announcements-test
python main.py funding-alert
python main.py runtime-status
```

查看服务:

```bash
sudo systemctl status paopao-radar
journalctl -u paopao-radar -f
```

## 11. 手动启动方式

如果你不想用 systemd，也可以手动后台运行:

```bash
cd ~/paopao-crypto-radar
. .venv/bin/activate
pkill -f "main.py daemon" || true
nohup .venv/bin/python -u main.py daemon --send --confirm-real-send > data/runtime.log 2>&1 &
tail -f data/runtime.log
```

## 12. 闭合窗口与推送时间

涉及 OI、CVD、K 线涨跌的雷达默认不在刚整点立刻读取数据，而是等待数据源收线完成后再统计上一完整窗口。资金流雷达使用 Binance 免费公开数据，CVD 由 K 线主动买入成交额估算:

```bash
RADAR_SUMMARY_MIN_INTERVAL_SEC=21600   # 资金摘要 6 小时窗口
RADAR_SUMMARY_CLOSE_DELAY_SEC=300      # 收线后延迟 5 分钟
FLOW_INTERVAL_SEC=3600                 # 资金流 1 小时窗口
FLOW_CLOSE_DELAY_SEC=300               # 收线后延迟 5 分钟
FUNDING_ALERT_ENABLE=true              # 启用独立资金费率警报话题
FUNDING_ALERT_INTERVAL_SEC=180         # 资金费率警报默认 3 分钟扫描一次
FUNDING_ALERT_SCAN_LIMIT=120           # 按 Binance 成交额扫描前 N 个 USDT 合约
FUNDING_ALERT_EXCHANGES=BINANCE,OKX,BYBIT,BITGET,GATE
FUNDING_ALERT_EXTREME_NEGATIVE_PCT=-0.5 # 极负费率阈值
FUNDING_ALERT_SUPER_NEGATIVE_PCT=-1.0  # 超极负费率阈值
FUNDING_ALERT_EXTREME_POSITIVE_PCT=0.5 # 极正费率阈值
FUNDING_ALERT_MIN_EXCHANGE_COUNT=2     # 多交易所共振最少交易所数量
FUNDING_ALERT_DIVERGENCE_PCT=0.75      # 交易所之间费率偏离阈值
FUNDING_ALERT_REPLY_CHAIN_ENABLE=true  # 同币后续资金费率警报回复上一条
FUNDING_ALERT_DECAY_QUIET_SCANS=2      # 连续安静几轮后提示热度衰减
FUNDING_ALERT_END_QUIET_SCANS=5        # 连续安静几轮后标记观察结束
LAUNCH_CLOSE_DELAY_SEC=60              # 启动雷达 15m 收线后延迟 1 分钟
STRUCTURE_PRE_SCAN_MINUTE=55           # 结构突破雷达每小时提前临界扫描
STRUCTURE_CONFIRM_DELAY_SEC=300        # 结构突破雷达收线后延迟 5 分钟确认
STRUCTURE_MIN_SCORE=65                 # 结构雷达最低推送分，复盘提示假突破偏高时可提高
STRUCTURE_SEND_CHART_TOP_N=3           # 每轮最多给前 N 个结构信号发送 K 线图，信号太多时可降低
STRUCTURE_DELETE_CHART_AFTER_SEND=true # 真实图片推送成功后立即删除本地 PNG
STRUCTURE_CHART_RETENTION_HOURS=12     # dry-run/失败图片最多保留 12 小时
STRUCTURE_MAX_CHART_FILES=200          # 超过 200 张时只保留最新图片
STRUCTURE_REPLY_CHAIN_ENABLE=true      # 同币结构信号回复上一条结构消息
STRUCTURE_REVIEW_ENABLE=true           # 启用结构信号复盘统计
STRUCTURE_REVIEW_LOOKBACK_HOURS=24     # 默认复盘过去 24 小时信号
STRUCTURE_REVIEW_FORWARD_HOURS=4       # 最多跟踪信号后 4 小时
STRUCTURE_REVIEW_MIN_AGE_MINUTES=15    # 信号至少等待 15 分钟后复盘
STRUCTURE_REVIEW_MAX_REPORT_INTERVAL_SEC=3600 # 复盘报告真实推送最小间隔
LIQUIDITY_FALLBACK_ENABLE=true         # 启用结构雷达免费流动性辅助
LIQUIDITY_SCORE_MAX_DELTA=15           # 分数修正上限，避免压倒结构原始评分
LIQUIDITY_MIN_DISTANCE_PCT=0.5         # 买墙/卖墙距离现价至少 0.5%
LIQUIDITY_MAX_DISTANCE_PCT=8.0         # 买墙/卖墙距离现价最多 8%
BINANCE_ORDERBOOK_LIQUIDITY_ENABLE=true # 使用 Binance 免费盘口深度估算买墙/卖墙
BINANCE_ORDERBOOK_DEPTH_LIMIT=100      # 每个币读取的 Binance 盘口档位
COINALYZE_ENABLE=false                 # 可选：开启 Coinalyze 免费 Key 的清算历史辅助
COINALYZE_API_KEY=                     # 可选：Coinalyze 免费 API Key
ANNOUNCEMENT_PAGE_SIZE=50              # Binance 公告单页数量，公告测试会分页抓取多个分类
```

结构外部确认默认使用 Binance 免费合约深度快照估算上方卖墙/下方买墙；清算侧可选使用 Coinalyze 历史清算量做方向辅助，但它不是预测清算池，推送里会标明数据源。

结构雷达推送中的外部确认状态会显示完整中文：清算磁吸说明清算池方向，盘口流动性说明买墙/卖墙是否明显，流动性缺口说明哪一侧阻力或支撑更薄。Binance 免费盘口快照不是盘口热力图，只能看当前订单簿；如果挂单不集中、距离超出配置范围，或深度档位内没有明显墙，就会显示“暂无有效买墙/卖墙”。

如果修改这些参数，推荐使用 Web 控制台的“配置”页；保存成功后会自动应用新配置，不需要再手动重启。更新项目时脚本会保留 token、群 ID、Coinalyze key 和话题 ID。

结构突破雷达 v1.8 的单次 dry-run：

```bash
python main.py structure-radar --mode pre --save-charts
python main.py structure-radar --mode confirm --save-charts
python main.py structure-review
python main.py announcements-test
python main.py funding-alert
```

独立循环：

```bash
python main.py structure-loop
```

## 13. 排错

如果提示 `TG_BOT_TOKEN 缺失或格式无效`:

```bash
nano .env.oi
```

检查:

```bash
TG_BOT_TOKEN=你的bot_token
TG_CHAT_ID=你的群ID
```

如果把非数字内容错填到了 `TG_TOPIC_ID`，重新运行安装脚本即可。新脚本会检测到非数字话题 ID，并自动清空。

如果 Telegram 话题无法置顶，通常是 bot 缺少置顶消息或管理话题权限。推送本身不会因此停止。
