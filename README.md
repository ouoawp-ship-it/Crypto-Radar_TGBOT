# 泡泡抓币 Crypto Radar

轻量级加密市场观察雷达。默认 dry-run，包含一个本地 Web 控制台用于查看状态、日志和修改关键配置，不包含自动交易。

## 功能和推送周期

- Binance 公告机会/风险监听：跟随主扫描，只推当天 CST 的可行动公告；识别 Alpha、上新、HODLer、Launchpool、Airdrop、下架、停止交易等，并按币种区分有无 Binance USDT 合约。
- 资金雷达汇总：默认 6 小时一次、每天最多 4 次，推送负费率榜、综合榜、埋伏榜、动量池、新币池、值得关注和数据质量。
- 启动雷达提醒：默认 3 分钟扫描一次，推送币种、阶段、分数、价格/OI/成交量变化和触发原因；同一币种后续更高阶段会回复上一条启动消息，形成连续追踪链。
- 五因子资金流雷达：默认每 1 小时收线后延迟 5 分钟推送一次，使用 Binance 免费公开数据，按上一完整窗口内的价格、OI、现货 CVD、合约 CVD、资金费率过滤资金流信号。
- 结构突破雷达：v1.8 新增，独立识别盘整箱体上沿/下沿、ATR/BB 压缩、临近突破、收线确认、假突破，并可生成 K线状态图。
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
- 创建并启动 `paopao-radar`、`paopao-structure`、`paopao-web` systemd 服务
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

页面会要求输入 `WEB_ADMIN_TOKEN`。更新脚本会自动生成令牌，可用 `paopao web-token` 查看。

常用命令:

```bash
paopao web-status
paopao web-logs
paopao web-restart
paopao web-token
```

前台调试启动仍然可用:

```bash
paopao web --host 127.0.0.1 --port 8080
```

配置项:

```bash
WEB_HOST=0.0.0.0
WEB_PORT=8080
WEB_ADMIN_TOKEN=
```

控制台功能包括：服务状态、实时日志、runtime-status、readiness、Telegram 测试消息、doctor、Binance 公告测试、结构信号复盘、cleanup、主服务/结构雷达重启，以及 `.env.oi` 关键配置编辑。保存配置前会自动备份 `.env.oi`。Web 内置“功能说明”页，会说明每个页面的用途、版本号、提交号和安全规则。

如果 `WEB_ADMIN_TOKEN` 为空，程序会拒绝监听公网地址；安装/更新脚本会自动补齐。

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
paopao-cleanup.timer # 每小时自动清理运行垃圾
```

常用 Web 控制台命令：

```bash
paopao web-status
paopao web-logs
paopao web-restart
paopao web-token
```

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

更新脚本每次运行后会自动执行一次安全清理：同步 `.env.oi`、清理 pycache/临时文件/过期日志/过期结构图/根目录临时报告，再重启服务。脚本还会安装/刷新 `paopao-structure.service`、`paopao-web.service` 和 `paopao-cleanup.timer`。清理不会删除 `.env.oi`、`data/*.json` 状态文件、README、`docs/INSTALL_CN.md` 或源码。

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
paopao          # 显示 Web 控制台地址和访问令牌
paopao web-token # 查看访问令牌
paopao web-status # 查看 Web 服务状态
paopao web-logs # 查看 Web 服务日志
paopao web-restart # 重启 Web 服务
paopao version  # 查看当前版本号
paopao check-update # 检查当前版本/GitHub版本
paopao update   # 有更新时确认后更新项目
```

`paopao update` 会在拉取新代码后安全同步 `.env.oi`：新增的普通配置项会自动补上，明确列入迁移白名单的默认参数会自动升级；`TG_BOT_TOKEN`、`TG_CHAT_ID`、`COINALYZE_API_KEY` 和各类话题 ID 不会被覆盖。

项目版本号写在 `VERSION` 文件里，当前为 `v1.11.2`，后续功能更新按 `v1.11.3`、`v2.0` 递增；`paopao update` 会同时显示版本号和 git 提交号。
