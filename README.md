# 泡泡抓币 Crypto Radar

轻量级加密市场观察雷达。默认 dry-run，包含一个本地 Web 控制台用于查看状态、日志和修改关键配置，不包含自动交易。

## 功能和推送周期

- Binance 公告机会/风险监听：跟随主扫描，只推当天 CST 的可行动公告；识别 Alpha、上新、HODLer、Launchpool、Airdrop、下架、停止交易等，并按币种区分有无 Binance USDT 合约。
- 资金雷达汇总：默认 6 小时一次、每天最多 4 次，推送负费率榜、综合榜、埋伏榜、动量池、新币池、值得关注和数据质量。
- 启动雷达提醒：默认 3 分钟扫描一次，推送币种、阶段、分数、市值分档、流动性分档、价格/OI/成交量变化、资金费率/结算周期和触发原因；推送前会拉 Binance、OKX、Bybit、Bitget、Gate 五家公开资金费率，显示每家实时费率、当前周期和下次结算时间；资金费率极负会标注当前周期，例如 `-2.000%/1H`，结算周期从 8H→4H 或 4H→1H 会在信号里提示；同一币种后续更高阶段会回复上一条启动消息。
- 资金费率警报：v1.15 新增独立话题，默认每 3 分钟扫描 Binance 成交额前 120 个 USDT 合约，使用 Binance、OKX、Bybit、Bitget、Gate 免费公开数据，专门提示极负/极正费率、多交易所共振、结算周期缩短和交易所费率偏离；v1.15.3 起升级为跟踪型信号，首次发现会标注，同币后续信号会回复上一条，阶段会按首次异动、拥挤加剧、高危活跃、风险释放、热度衰减跟踪，并补充市值、24h 成交额、等宽交易所费率表和偏离解释。
- 五因子资金流雷达：默认每 1 小时收线后延迟 5 分钟推送一次，使用 Binance 免费公开数据，按上一完整窗口内的价格、OI、现货 CVD、合约 CVD、资金费率过滤资金流信号。
- 结构突破雷达：v1.8 新增，独立识别盘整箱体上沿/下沿、ATR/BB 压缩、临近突破、收线确认、假突破，并可生成 K线状态图。
- Web 控制台会说明每个外部接口在本项目里的用途，并用平台真实站点图标区分 Telegram、Binance、CoinPaprika、Coinalyze、CoinMarketCap：Telegram 必填，Binance/CoinPaprika 无需 Key，Coinalyze 仅作结构雷达历史清算辅助，CoinMarketCap 当前只是预留未接入；配置页按 Telegram、AI、雷达参数、资金费率、模块开关、外部接口、Web 控制台和备份恢复分类显示，并完整显示当前 Token / Key / Web 令牌。
- AI 币种档案：v1.16.0 新增 AI Bot 自然语言查币，v1.18.0 升级价格提醒为纯手动选择流程，v1.20.0 升级为异步队列/worker 架构。发送“查 BTC”“GWEI 怎么看”“SOL 可以做多吗”时，机器人会读取历史雷达信号、当前价格/OI/成交量/资金费率/市值/流动性和结构状态，先给本地多空证据结论，AI Key 开启后再生成增强研判；设置价格提醒时会手动选择现货/合约和 Binance、Bybit、OKX、Bitget、Gate 价格源。
- Web 配置页会同时识别 `.env.oi` 里的手动话题 ID 和 `data/tg_topic_routes.json` 里的自动创建话题 ID；自动话题会标注“自动话题”，避免误以为没有配置。
- OI/价格背离扫描：跟随资金雷达，跟踪建仓背离、多头共振、极端背离、持续/增强/消失状态。
- 自动清理：默认 1 小时检查一次，只清理可再生成的缓存、临时文件、坏 JSON 备份、过期日志、过长历史、过期结构图和根目录临时报告。

## 服务器一键部署

服务器能访问这个 GitHub 私有仓库后，直接运行:

```bash
git clone https://github.com/ouoawp-ship-it/paopao-crypto-radar.git
cd paopao-crypto-radar
bash scripts/install_server.sh
```

第一次运行会自动创建 `.env.oi`。如果没有填写 Telegram 配置，会直接在终端提示输入 `TG_BOT_TOKEN` 和 `TG_CHAT_ID`；token 输入会显示出来，方便确认粘贴成功。空回车或格式不对会反复提示，不会继续启动服务。随后会提示可选 `COINALYZE_API_KEY`；直接回车就是不启用历史清算辅助。

Telegram 群开启话题后，可以把不同推送分到不同话题，避免消息交叉：

```bash
TELEGRAM_USE_TOPIC=true
TG_RADAR_SUMMARY_TOPIC_ID=资金摘要话题ID
TG_LAUNCH_ALERT_TOPIC_ID=启动预警话题ID
TG_ANNOUNCEMENT_ALERT_TOPIC_ID=公告风险话题ID
TG_TEST_TOPIC_ID=测试消息话题ID
TG_FUNDING_ALERT_TOPIC_ID=资金费率警报话题ID
TG_AUTO_CREATE_TOPICS=true
```

没有配置专属话题的消息会先读取 `data/tg_topic_routes.json` 里已自动创建过的话题 ID；仍没有时，如果 `TG_AUTO_CREATE_TOPICS=true` 且 bot 有管理话题权限，会自动创建并记录话题。`TG_TOPIC_ID` 可作为默认兜底话题；所有话题都不可用时，消息发到群默认主聊天。

每个推送话题第一次真实发送前，会自动发一条“本话题功能说明/信号阅读方式/扫描发送频率”，并尝试置顶；如果后续版本的说明内容变化，会尽量删除旧说明并重新发送、置顶最新版。置顶和删除需要 bot 具备置顶消息、删除消息或管理话题权限。可用 `TG_TOPIC_INTRO_ENABLE=false` 或 `TG_TOPIC_INTRO_PIN=false` 关闭。

```bash
bash scripts/install_server.sh
```

脚本会自动:

- 安装系统依赖
- 创建 `.venv`
- 安装 Python 依赖
- 编译检查
- 跑单元测试
- 生成 dry-run 启动观察历史
- 通过 readiness 检查
- 创建并启动 `paopao-radar`、`paopao-structure`、`paopao-web`、`paopao-ai` systemd 服务
- 定时自动清理临时文件、坏 JSON 备份、过期日志和过长历史

## 查看运行

```bash
sudo systemctl status paopao-radar
journalctl -u paopao-radar -f
python main.py runtime-status
python main.py about
python main.py cleanup --force-cleanup
```

## Web 控制台

Web 控制台默认作为 `paopao-web.service` 安装，监听 `0.0.0.0:8080`，浏览器直接访问:

```text
http://服务器IP:8080/
```

页面会要求输入 `WEB_ADMIN_TOKEN`。更新脚本会自动生成令牌，可在服务器输入 `paopao`，选择“查看 Web 地址和令牌”。

服务器快捷入口只需要记住一个命令:

```bash
paopao
```

进入中文菜单后，用数字选择查看地址/令牌、Web 服务状态、Web 实时日志、重启 Web 服务、检查更新、更新项目和查看版本。配置修改、主服务/结构雷达控制、测试消息、readiness、doctor、cleanup、结构复盘等日常动作在 Web 页面里完成。

前台调试启动仍然保留在脚本里，但正常使用不需要记任何 Web 子命令。

配置项:

```bash
WEB_HOST=0.0.0.0
WEB_PORT=8080
WEB_ADMIN_TOKEN=
```

控制台功能包括：服务状态、运行健康度、最近错误、实时日志、日志搜索筛选、runtime-status、readiness、Telegram 测试消息、doctor、Binance 公告测试、资金费率警报扫描、结构信号复盘、cleanup、主服务/结构雷达重启、推送样例预览、GitHub 更新检查，以及 `.env.oi` 关键配置编辑。配置页按功能分类进入，支持保存前预览改动、保存后中文结果提示、最近 `.env.oi` 备份一键恢复/删除、真实模块开关，以及结构复盘参数建议一键应用。结构复盘推送里建议调整的 `STRUCTURE_MIN_SCORE` 和 `STRUCTURE_SEND_CHART_TOP_N` 可以在 Web 的“配置 -> 雷达参数”里直接修改。保存配置前会自动备份 `.env.oi`，保存成功后会自动应用新配置；主服务和结构雷达会自动重启，Web 端口或令牌变更会让 Web 控制台短暂重启。Web 内置“功能说明”页，会说明每个页面的用途、版本号、提交号和安全规则。

如果 `WEB_ADMIN_TOKEN` 为空，程序会拒绝监听公网地址；安装/更新脚本会自动补齐。

## AI 助手 Bot 和价格提醒

v1.13.0 新增独立 AI 助手服务 `paopao-ai.service`。它和群里的雷达推送 Bot 分开：

```text
TG_BOT_TOKEN  = 群话题推送雷达信号
AI_BOT_TOKEN  = 私聊 AI 助手、手动价格提醒、个人提醒
```

推荐用 BotFather 单独创建一个新的 Telegram Bot，填到 Web 控制台的「配置 -> AI 助手」里：

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

价格提醒不需要 AI API Key。v1.19.0 起，价格提醒升级为多类型监控：打开 AI 助手 Bot 私聊，点击「设置价格提醒」，可选择目标价提醒、价格急涨急跌、持仓量变化、资金费率变化；支持 Binance、Bybit、OKX、Bitget、Gate 的现货或 USDT 合约价格源，并可选择提醒一次、重复提醒或持续每5分钟提醒。v1.20.0 起，AI Bot 使用异步发送队列、后台价格提醒扫描和更新处理 worker；查询价格、输入币种识别交易所、AI 分析等慢任务会先回复“已收到/正在处理”，结果完成后再单独发送。v1.20.12 起，多交易所价格查询正文强制使用 Telegram HTML：价格表整块等宽显示，合约和现货共用列宽，表头简化为交易所/交易对/价格，并按 Binance、Bybit、OKX、Bitget、Gate 固定顺序展示；CoinGlass K线入口放在表格下方的文字链接里，不使用按钮，也不让链接破坏表格排版。v1.21.1 起，首页不再显示 AI 对话按钮，只保留设置价格提醒、我的提醒、查询价格和使用说明；日常/生活问题、交易/行情问题仍可直接发消息，系统会自动分流到泡泡 AI 助手或专业分析师提示词。Binance/Bybit 合约若普通交易对不存在，会自动尝试 `1000`、`10000`、`1000000` 前缀合约，并把这类合约报价折算成单币价格显示，交易对仍保留交易所原始名称。v1.21.2 起，慢任务的“已收到/正在处理”临时提示会在最终回复发送成功后自动撤回，聊天里只保留真正的结果消息。v1.21.3 起，AI Bot 去命令化，只保留 `/start` 打开首页；查价格、看行情、分析数据直接发消息，提醒创建和管理全部走按钮。v1.21.4 起，提醒编号改为当前列表序号，删除后自动重排；提醒里的交易所名称加粗并跳转 CoinGlass K线，交易对使用等宽格式方便复制。v1.21.5 起，按钮点击会先静默确认 Telegram 回调，不再弹出“处理中...”提示，也避免配置/数据库加载拖住按钮加载圈。v1.22.0 起，AI Bot 进入极速响应目标模式：入口只做轻判断和分发，Settings 使用热缓存，交易所精确报价使用短 TTL 缓存，慢按钮/慢消息会写入耗时日志。

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

如果需要真正的 AI 问答，再开启兼容 OpenAI 格式的模型接口：

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

Web 控制台新增「AI 助手」页面，可以查看 `paopao-ai` 服务状态、提醒统计、新增 Web 提醒、暂停/恢复/删除提醒。Web 创建提醒需要填写接收提醒的 Telegram 用户 ID，或者先配置 `AI_DEFAULT_CHAT_ID`；从 Telegram 私聊创建提醒会自动识别当前私聊。`SIGNAL_EVENTS_*` 控制 AI 币种档案读取的结构化信号索引，通常保持默认即可。

Web 控制台新增「AI 提示词」页面，可以编辑泡泡 AI 助手提示词和专业分析师提示词。泡泡 AI 助手用于日常问答、生活问题、状态解释和提醒说明，默认语气更轻松；专业分析师用于 `分析这段：...`、`帮我分析...` 以及自动识别出的雷达/市场数据。提示词默认保存在 `data/ai_prompts.json`，保存后会自动重启 `paopao-ai`。

## 闭合窗口参数

涉及 OI、CVD、K 线涨跌的雷达会按“上一完整收线窗口”计算，避免刚整点时抓到未收完的数据。资金流雷达的 CVD 来自 Binance K 线主动买入成交额估算：

```bash
RADAR_SUMMARY_MIN_INTERVAL_SEC=21600
RADAR_SUMMARY_CLOSE_DELAY_SEC=300
FLOW_INTERVAL_SEC=3600
FLOW_CLOSE_DELAY_SEC=300
LAUNCH_CLOSE_DELAY_SEC=60
STRUCTURE_PRE_SCAN_MINUTE=55
STRUCTURE_CONFIRM_DELAY_SEC=300
```

## 结构突破雷达 v1.8

单次 dry-run：

```bash
python main.py structure-radar --mode pre --top-symbols 80 --min-score 65 --save-charts
python main.py structure-radar --mode confirm --top-symbols 80 --min-score 65 --save-charts
```

独立循环：

```bash
python main.py structure-loop
```

真实推送仍必须显式确认：

```bash
python main.py structure-radar --mode pre --send --confirm-real-send
```

默认提前临界扫描在每小时 55 分附近运行，收线确认在整点后延迟 5 分钟运行。图片保存到 `data/charts/`，结构雷达状态保存到 `data/structure_state.json` 和 `data/structure_history.json`。
真实 Telegram 图片发送成功后默认会立即删除本地 PNG；dry-run 和发送失败的图片会暂时保留，并由 cleanup 按保留时间和数量上限清理：

```bash
STRUCTURE_DELETE_CHART_AFTER_SEND=true
STRUCTURE_CHART_RETENTION_HOURS=12
STRUCTURE_MAX_CHART_FILES=200
```

## 结构信号复盘 v1.8.3

结构雷达会把本轮信号写入 `data/structure_review.json`，后续通过 K 线渐进复盘 15m、1h、4h 后价格变化、有效突破、假突破、MFE/MAE，并生成聚合统计。

```bash
python main.py structure-review
python main.py structure-review --lookback-hours 24
python main.py structure-review --send --confirm-real-send
```

复盘报告保存到 `data/structure_review_report.txt`，聚合统计保存到 `data/structure_stats.json`。结构雷达同币种后续信号默认会回复上一条该币结构消息，形成 Telegram 追踪链。

```bash
STRUCTURE_REPLY_CHAIN_ENABLE=true
STRUCTURE_REVIEW_ENABLE=true
STRUCTURE_REVIEW_LOOKBACK_HOURS=24
STRUCTURE_REVIEW_FORWARD_HOURS=4
STRUCTURE_REVIEW_MIN_AGE_MINUTES=15
STRUCTURE_REVIEW_MAX_REPORT_INTERVAL_SEC=3600
```

## 结构雷达外部确认

结构雷达外部确认使用 Binance 免费合约盘口深度，可选叠加 Coinalyze 历史清算量。它只增强结构雷达，不替代原有结构算法。

本地测试：
```bash
python main.py structure-radar --mode pre --save-charts
```

增强字段包括上方卖墙、下方买墙、流动性缺口、清算历史方向辅助和分数修正。分数修正默认限制在 `-15 ~ +15`。

```bash
LIQUIDITY_FALLBACK_ENABLE=true
LIQUIDITY_SCORE_MAX_DELTA=15
LIQUIDITY_MIN_DISTANCE_PCT=0.5
LIQUIDITY_MAX_DISTANCE_PCT=8.0
BINANCE_ORDERBOOK_LIQUIDITY_ENABLE=true
BINANCE_ORDERBOOK_DEPTH_LIMIT=100
COINALYZE_ENABLE=false
COINALYZE_API_KEY=
```

流动性增强默认读取 Binance 免费合约盘口深度快照。可选配置 Coinalyze 免费 API Key 后，清算侧会补充 Coinalyze 历史清算量作为方向辅助；它不是预测清算池，推送里会标明数据源。

推送里的外部确认状态会使用中文解释：清算磁吸说明上方/下方清算池哪边更近或更强；盘口流动性说明当前是否识别到明显买墙/卖墙；流动性缺口说明订单簿哪一侧阻力或支撑更薄。Binance 免费盘口降级只读取当前深度快照，不是历史盘口热力图；如果订单挂单分散、距离不在配置范围内，或没有明显集中墙，就会显示“暂无有效买墙/卖墙”。

## v1.9.4 服务、公告和清理增强

更新脚本会安装/刷新两个 systemd 服务和一个清理 timer，即使当前代码已经是最新版，也会继续补装服务、刷新快捷命令并重启已安装服务：

```bash
paopao-radar      # 主服务：资金摘要、启动雷达、公告、资金流等
paopao-structure  # 结构雷达独立循环：55 分预警，整点后 5 分确认
paopao-web        # Web 控制台：状态、日志、配置和维护操作
paopao-ai         # AI 助手 Bot：私聊问答、手动价格提醒、个人提醒
paopao-cleanup.timer # 每小时自动清理运行垃圾
```

服务器快捷入口：

```bash
paopao
```

进入中文菜单后按数字查看 Web 地址/令牌、Web 服务状态、Web 实时日志、重启 Web 服务、检查更新、更新项目和查看版本。

Binance 公告抓取默认每个分类分页读取，单页数量从 20 提高到 50，并新增活动关键词识别。专门测试公告抓取和分类：

```bash
python main.py announcements-test
```

相关配置：

```bash
ANNOUNCEMENT_PAGE_SIZE=50
```

## 一键更新

```bash
bash scripts/update_server.sh
```

更新脚本每次运行后会自动执行一次安全清理：同步 `.env.oi`、清理 pycache/临时文件/过期日志/过期结构图/根目录临时报告，再重启服务。脚本还会安装/刷新 `paopao-structure.service`、`paopao-web.service`、`paopao-ai.service` 和 `paopao-cleanup.timer`。清理不会删除 `.env.oi`、`data/*.json` 状态文件、README、`docs/INSTALL_CN.md` 或源码。

## 安全规则

真实 Telegram 推送必须同时带:

```bash
--send --confirm-real-send
```

`.env.oi` 和 `data/` 状态文件不应提交到 GitHub。

更详细的安装、更新、配置和排错说明见 [docs/INSTALL_CN.md](docs/INSTALL_CN.md)。

## 中文安装目录

第一次安装、重新安装、配置项说明和常见排错见 [docs/INSTALL_CN.md](docs/INSTALL_CN.md)。

修改 bot token、群 ID、Coinalyze key 或 Telegram 话题配置，推荐在 Web 控制台的“配置”页完成。服务器命令行保留应急配置向导:

```bash
bash scripts/install_server.sh config
```

服务器安装后会写入快捷命令:

```bash
paopao
```

输入后会打开中文数字菜单。菜单里会详细说明 Web 地址、访问令牌、项目版本，以及每个编号的用途；日常使用不需要记其它长命令。

中文菜单里的“更新项目代码”会在拉取新代码后安全同步 `.env.oi`：新增的普通配置项会自动补上，明确列入迁移白名单的默认参数会自动升级；`TG_BOT_TOKEN`、`TG_CHAT_ID`、`COINALYZE_API_KEY` 和各类话题 ID 不会被覆盖。

项目版本号写在 `VERSION` 文件里，当前为 `v1.22.0`，后续功能更新按 `v1.22.1`、`v2.0` 递增；中文菜单检查/更新时会同时显示版本号和 git 提交号。
