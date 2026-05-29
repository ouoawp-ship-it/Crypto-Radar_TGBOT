# 泡泡抓币 Crypto Radar

轻量级加密市场观察雷达。默认 dry-run，不包含 Web/UI、admin 查询、自动交易。

## 功能和推送周期

- Binance 公告机会/风险监听：跟随主扫描，只推当天 CST 的可行动公告；识别 Alpha、上新、HODLer、Launchpool、Airdrop、下架、停止交易等，并按币种区分有无 Binance USDT 合约。
- 资金雷达汇总：默认 6 小时一次、每天最多 4 次，推送负费率榜、综合榜、埋伏榜、动量池、新币池、值得关注和数据质量。
- 启动雷达提醒：默认 3 分钟扫描一次，推送币种、阶段、分数、价格/OI/成交量变化和触发原因；同一币种后续更高阶段会回复上一条启动消息，形成连续追踪链。
- 五因子资金流雷达：默认每 1 小时收线后延迟 5 分钟推送一次，按上一完整窗口内的价格、OI、现货 CVD、合约 CVD、资金费率过滤资金流信号。
- 结构突破雷达：v1.8 新增，独立识别盘整箱体上沿/下沿、ATR/BB 压缩、临近突破、收线确认、假突破，并可生成 K线状态图。
- OI/价格背离扫描：跟随资金雷达，跟踪建仓背离、多头共振、极端背离、持续/增强/消失状态。
- 自动清理：默认 1 小时检查一次，只清理可再生成的缓存、临时文件、坏 JSON 备份、过期日志、过长历史、过期结构图和根目录临时报告。
- CoinGlass 增强源：可选启用，用于后续接入多交易所 OI、爆仓、资金费率和合约市场动态。

## 服务器一键部署

服务器能访问这个 GitHub 私有仓库后，直接运行:

```bash
git clone https://github.com/ouoawp-ship-it/paopao-crypto-radar.git
cd paopao-crypto-radar
bash scripts/install_server.sh
```

第一次运行会自动创建 `.env.oi`。如果没有填写 Telegram 配置，会直接在终端提示输入 `TG_BOT_TOKEN` 和 `TG_CHAT_ID`；token 输入会显示出来，方便确认粘贴成功。空回车或格式不对会反复提示，不会继续启动服务。随后会提示 `COINGLASS_API_KEY` 和可选 `COINALYZE_API_KEY`；CoinGlass 直接回车就是纯 Binance 数据版本，Coinalyze 直接回车就是不启用历史清算辅助。

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
- 创建并启动 `paopao-radar` systemd 服务
- 定时自动清理临时文件、坏 JSON 备份、过期日志和过长历史

## 查看运行

```bash
sudo systemctl status paopao-radar
journalctl -u paopao-radar -f
python main.py runtime-status
python main.py about
python main.py cleanup --force-cleanup
```

## CoinGlass 可选增强

不要把 API key 写进代码或提交到 GitHub。只写入服务器 `.env.oi`：

```bash
COINGLASS_ENABLE=true
COINGLASS_API_KEY=你的key
COINGLASS_BASE_URL=https://open-api-v4.coinglass.com
COINGLASS_REQUEST_BUDGET=60
```

验证 key 和接口连通：

```bash
python main.py coinglass-test
```

## 闭合窗口参数

涉及 OI、CVD、K 线涨跌的雷达会按“上一完整收线窗口”计算，避免刚整点时抓到未收完的数据：

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

## 多源清算/盘口增强 v1.9.6

v1.9 增加 CoinGlass 清算热力图和盘口流动性外部确认；v1.9.1 增加免费源降级；v1.9.2 增加结构雷达独立服务和 Binance 公告抓取增强；v1.9.3 修复“代码已是最新版”时仍要刷新 systemd 服务的问题；v1.9.4 增加手动清理快捷命令和服务器自动清理 timer；v1.9.5 将结构雷达外部确认推送改为完整中文说明，并补充 Binance 盘口降级未命中的原因；v1.9.6 修复资金摘要每日限额按 UTC 统计导致 CST 凌晨/早晨被误拦截的问题，并将主雷达和结构雷达运行状态拆分保存。它只增强结构雷达，不替代原有结构算法；CoinGlass 高级接口默认关闭，避免升级后立即消耗 API 额度。

开启方式：
```bash
COINGLASS_ENABLE=true
COINGLASS_API_KEY=你的Key
COINGLASS_LIQUIDITY_ENABLE=true
```

本地测试：
```bash
python main.py coinglass-liquidity-test
python main.py structure-radar --mode pre --with-coinglass --save-charts
```

增强字段包括上方/下方清算区、距离清算池百分比、清算磁吸方向、上方卖墙、下方买墙、流动性缺口和分数修正。分数修正默认限制在 `-15 ~ +15`，CoinGlass 不可用或无权限时结构雷达会自动降级为 Binance 公共数据版。

```bash
COINGLASS_LIQUIDITY_ENABLE=false
COINGLASS_LIQUIDITY_TIMEOUT_SEC=8
COINGLASS_LIQUIDITY_MAX_SYMBOLS=30
COINGLASS_LIQUIDITY_SCORE_MAX_DELTA=15
COINGLASS_LIQUIDITY_MIN_DISTANCE_PCT=0.5
COINGLASS_LIQUIDITY_MAX_DISTANCE_PCT=8.0
COINGLASS_LIQUIDITY_CACHE_SEC=300
LIQUIDITY_FALLBACK_ENABLE=true
BINANCE_ORDERBOOK_LIQUIDITY_ENABLE=true
BINANCE_ORDERBOOK_DEPTH_LIMIT=100
COINALYZE_ENABLE=false
COINALYZE_API_KEY=
```

流动性增强采用多源优先级：CoinGlass 清算热力图/盘口热力图优先；如果接口返回 `Upgrade plan`、无权限、空数据或超时，则自动降级到 Binance 免费合约盘口深度快照。可选配置 Coinalyze 免费 API Key 后，清算侧会补充 Coinalyze 历史清算量作为方向辅助；它不等同于 CoinGlass 预测清算池，推送里会标明数据源。

推送里的外部确认状态会使用中文解释：清算磁吸说明上方/下方清算池哪边更近或更强；盘口流动性说明当前是否识别到明显买墙/卖墙；流动性缺口说明订单簿哪一侧阻力或支撑更薄。Binance 免费盘口降级只读取当前深度快照，不是历史盘口热力图；如果订单挂单分散、距离不在配置范围内，或没有明显集中墙，就会显示“暂无有效买墙/卖墙”。

## v1.9.4 服务、公告和清理增强

更新脚本会安装/刷新两个 systemd 服务和一个清理 timer，即使当前代码已经是最新版，也会继续补装服务、刷新快捷命令并重启已安装服务：

```bash
paopao-radar      # 主服务：资金摘要、启动雷达、公告、资金流等
paopao-structure  # 结构雷达独立循环：55 分预警，整点后 5 分确认
paopao-cleanup.timer # 每小时自动清理运行垃圾
```

常用结构服务命令：

```bash
paopao structure-status
paopao structure-logs
paopao structure-restart
paopao cleanup
```

Binance 公告抓取默认每个分类分页读取，单页数量从 20 提高到 50，并新增活动关键词识别。专门测试公告抓取和分类：

```bash
python main.py announcements-test
paopao announcements
```

相关配置：

```bash
ANNOUNCEMENT_PAGE_SIZE=50
```

## 山寨币启动雷达 Web 看板 v1.10.1

新增轻量 Web API 和 `/launch-radar` 看板，用于把 OI/价格背离、Funding、主动买卖、OI/市值、刷量风险等字段产品化展示。前端只读取后端 API，不包含扫描逻辑，也不会暴露 CoinGlass key、Telegram token 或 Chat ID。

本地启动 Mock 数据看板：

```bash
python main.py web --web-host 127.0.0.1 --web-port 18090 --web-mode mock
```

访问：

```text
http://localhost:18090/launch-radar
http://localhost:18090/api/launch-radar?mode=mock
```

使用真实数据模式：

```bash
python main.py web --web-host 127.0.0.1 --web-port 18090 --web-mode real
```

新增接口：

```text
GET /api/launch-radar
GET /api/oi-divergence
GET /api/wash-risk
GET /api/symbol/{symbol}
```

Web 状态文件写入 `data/launch_radar_latest.json`、`data/oi_divergence_latest.json`、`data/wash_risk_latest.json`、`data/signal_history.json`。真实扫描失败时 API 会返回上一次成功结果并标记 `stale=true`；没有数据时看板显示“暂无数据”，不会白屏。

### 部署到 paoxx.com/launch-radar

v1.10.1 新增部署模板，生产默认只监听本机 `127.0.0.1:18090`，再由 Nginx 把 `paoxx.com/launch-radar` 和 `paoxx.com/api/` 反向代理到本地服务。

在服务器项目目录执行：

```bash
cd ~/paopao-crypto-radar
bash deploy/install_launch_radar_web.sh
```

脚本会检查项目目录和 `.venv/bin/python`，执行编译检查，安装并启动 `paopao-launch-radar.service`。它不会自动覆盖你的 Nginx 主站配置。

本地健康检查：

```bash
curl http://127.0.0.1:18090/api/health
curl http://127.0.0.1:18090/api/launch-radar
```

Nginx 模板在：

```text
deploy/nginx-paoxx-launch-radar.conf
```

把该模板 include 到 `paoxx.com` 的 HTTPS `server` 配置里，然后执行：

```bash
sudo nginx -t
sudo systemctl reload nginx
```

## 一键更新

```bash
bash scripts/update_server.sh
```

更新脚本每次运行后会自动执行一次安全清理：同步 `.env.oi`、清理 pycache/临时文件/过期日志/过期结构图/根目录临时报告，再重启服务。脚本还会安装 `paopao-cleanup.timer`，每小时自动执行一次 `python main.py cleanup --force-cleanup`。清理不会删除 `.env.oi`、`data/*.json` 状态文件、README、`docs/INSTALL_CN.md` 或源码。

## 安全规则

真实 Telegram 推送必须同时带:

```bash
--send --confirm-real-send
```

`.env.oi` 和 `data/` 状态文件不应提交到 GitHub。

更详细的安装、更新、配置和排错说明见 [docs/INSTALL_CN.md](docs/INSTALL_CN.md)。

## 中文安装目录

第一次安装、重新安装、配置项说明和常见排错见 [docs/INSTALL_CN.md](docs/INSTALL_CN.md)。

修改 bot token、群 ID、CoinGlass key、Coinalyze key 或 Telegram 话题配置:

```bash
bash scripts/install_server.sh config
```

服务器安装后会写入快捷命令:

```bash
paopao          # 打开中文操作菜单
paopao config   # 修改配置
paopao logs     # 查看实时日志
paopao version  # 查看当前版本号
paopao cleanup   # 立即清理运行垃圾
paopao announcements # 测试 Binance 公告抓取和分类
paopao structure-status # 查看结构雷达独立服务状态
paopao check-update # 检查当前版本/GitHub版本
paopao update   # 有更新时确认后更新项目
```

`paopao update` 会在拉取新代码后安全同步 `.env.oi`：新增的普通配置项会自动补上，明确列入迁移白名单的默认参数会自动升级；`TG_BOT_TOKEN`、`TG_CHAT_ID`、`COINGLASS_API_KEY`、`COINALYZE_API_KEY` 和各类话题 ID 不会被覆盖。

项目版本号写在 `VERSION` 文件里，当前为 `v1.10.1`，后续功能更新按 `v1.10.2`、`v2.0` 递增；`paopao update` 会同时显示版本号和 git 提交号。
