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
6. 可选输入 CoinGlass API key。
7. 可选输入 Coinalyze API key。
8. 创建 `.venv` 并安装 Python 依赖。
9. 运行编译检查和单元测试。
10. 生成 dry-run 启动观察历史。
11. 运行 readiness。
12. 安装并启动 systemd 服务。

## 2. 输入项说明

`TG_BOT_TOKEN`
: BotFather 给你的机器人 token，格式类似 `123456:ABC...`。

`TG_CHAT_ID`
: Telegram 群 ID，通常类似 `-1001234567890`，也可以是频道用户名 `@channel_username`。

`TG_TOPIC_ID` 以及其他 `TG_..._TOPIC_ID`
: 这是 Telegram 话题的数字 `message_thread_id`。默认不需要填，机器人有权限时会自动创建和记录话题。

`COINGLASS_API_KEY`
: CoinGlass API key。只在脚本提示 `COINGLASS_API_KEY 可选` 时填写。直接回车就是纯 Binance 数据版本。

`COINALYZE_API_KEY`
: Coinalyze API key。只在脚本提示 `COINALYZE_API_KEY 可选` 时填写。直接回车就是不启用历史清算辅助；填写后会作为 CoinGlass 高级清算热力图不可用时的免费降级数据源。

## 3. 修改 token、群 ID、CoinGlass key、Coinalyze key

安装完成后，如果填错了 bot token、群 ID、CoinGlass key 或 Coinalyze key，不需要重新安装项目，直接运行配置修改向导:

```bash
cd ~/paopao-crypto-radar
bash scripts/install_server.sh config
```

安装完成后也可以直接使用快捷命令:

```bash
paopao config
```

菜单会提供这些功能:

```text
1. 修改 TG_BOT_TOKEN
2. 修改 TG_CHAT_ID / 群 ID
3. 修改 COINGLASS_API_KEY
4. 修改 COINALYZE_API_KEY
5. 修改 Telegram 话题配置
6. Telegram / CoinGlass / Coinalyze 全部重新填写
7. 清理旧 Telegram 话题路由
0. 保存并退出
```

如果修改了 `TG_CHAT_ID`，脚本会自动删除 `data/tg_topic_routes.json`。这是必要的，因为旧群的话题 ID 不能继续用于新群。服务重启后，bot 会按新群重新自动创建话题。

如果修改了 `TG_BOT_TOKEN`，建议确认新 bot 已经加入目标群，并且具备发送消息、管理话题、置顶消息权限。

如果修改了 `COINGLASS_API_KEY`，直接回车表示关闭 CoinGlass，切回纯 Binance 数据版本；粘贴新 key 表示启用双源版本。`COINALYZE_API_KEY` 是可选免费清算历史辅助，直接回车表示关闭 Coinalyze。

修改完成后，向导会提示是否立即重启 systemd 服务。也可以手动重启:

```bash
sudo systemctl restart paopao-radar
```

## 4. 快捷操作命令

安装脚本会自动写入 `/usr/local/bin/paopao`。以后在服务器任意目录输入:

```bash
paopao
```

就会弹出中文操作菜单。

常用快捷指令:

```bash
paopao              # 打开中文操作菜单
paopao config       # 修改 token / 群 ID / CoinGlass key / Coinalyze key / 话题配置
paopao logs         # 查看实时日志
paopao status       # 查看服务状态和 runtime-status
paopao version      # 查看当前项目版本号
paopao restart      # 重启服务
paopao start        # 启动服务
paopao stop         # 停止服务
paopao check-update # 只检查当前版本和 GitHub 版本
paopao update       # 检查 GitHub 版本，有更新时确认后更新
paopao update --yes # 有更新时自动确认更新
paopao test         # 发送 Telegram 测试消息
paopao coinglass    # 测试 CoinGlass API
paopao liquidity    # 测试 CoinGlass 清算/盘口增强接口
paopao announcements # 测试 Binance 公告抓取和分类
paopao cleanup      # 立即清理运行垃圾
paopao structure-review # 生成结构信号复盘报告
paopao structure-status # 查看结构雷达独立服务状态
paopao structure-logs   # 查看结构雷达独立服务日志
paopao structure-restart # 重启结构雷达独立服务
paopao readiness    # 检查真实推送准备度
paopao doctor       # 查看环境诊断
paopao help         # 查看帮助
```

如果是从旧版本更新上来，想只安装快捷命令:

```bash
cd ~/paopao-crypto-radar
bash scripts/install_server.sh shortcut
```

## 5. 版本号规则

项目根目录有一个 `VERSION` 文件，用来记录用户可读的版本号。当前为 `v1.9.4`，后续功能更新按 `v1.9.5`、`v2.0` 这种方式递增。

`paopao check-update` 和 `paopao update` 会同时显示:

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

`paopao update` 会自动运行 `.env.oi` 安全同步:

- 会补充 `.env.oi.example` 里新增的普通配置项。
- 会自动升级明确写进迁移白名单的默认参数，例如资金摘要频率这类项目级默认值。
- 不会覆盖 `TG_BOT_TOKEN`、`TG_CHAT_ID`、`COINGLASS_API_KEY`、`COINALYZE_API_KEY`、`TG_TOPIC_ID`、各类话题 ID。
- 如果你自己把某个参数改成了自定义值，脚本会尽量保留，不会用新默认值强行覆盖。

所以后续我优化配置参数后，你通常直接执行:

```bash
paopao update
```

即可完成代码更新、依赖检查、测试、`.env.oi` 安全同步和服务重启。

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

也可以用快捷命令:

```bash
paopao check-update   # 只检查，不更新
paopao update         # 先显示当前版本/GitHub版本，有更新再询问
paopao update --yes   # 有更新时自动确认
```

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
- 安装/刷新 `paopao-radar.service` 主服务和 `paopao-structure.service` 结构雷达独立服务
- 即使当前代码已经是最新版，也会刷新快捷命令、补装 `paopao-structure.service` 和 `paopao-cleanup.timer`，并重启已安装服务

结构雷达独立服务由 `paopao-structure.service` 管理，专门运行 `structure-loop`，用于每小时 55 分提前临界扫描和整点后 5 分收线确认。常用命令:

```bash
paopao structure-status
paopao structure-logs
paopao structure-restart
```

自动清理由 `paopao-cleanup.timer` 管理，默认每小时执行一次 `python main.py cleanup --force-cleanup`。手动立即清理:

```bash
paopao cleanup
```

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
python main.py coinglass-test
python main.py announcements-test
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

涉及 OI、CVD、K 线涨跌的雷达默认不在刚整点立刻读取数据，而是等待数据源收线完成后再统计上一完整窗口:

```bash
RADAR_SUMMARY_MIN_INTERVAL_SEC=21600   # 资金摘要 6 小时窗口
RADAR_SUMMARY_CLOSE_DELAY_SEC=300      # 收线后延迟 5 分钟
FLOW_INTERVAL_SEC=3600                 # 资金流 1 小时窗口
FLOW_CLOSE_DELAY_SEC=300               # 收线后延迟 5 分钟
LAUNCH_CLOSE_DELAY_SEC=60              # 启动雷达 15m 收线后延迟 1 分钟
STRUCTURE_PRE_SCAN_MINUTE=55           # 结构突破雷达每小时提前临界扫描
STRUCTURE_CONFIRM_DELAY_SEC=300        # 结构突破雷达收线后延迟 5 分钟确认
STRUCTURE_DELETE_CHART_AFTER_SEND=true # 真实图片推送成功后立即删除本地 PNG
STRUCTURE_CHART_RETENTION_HOURS=12     # dry-run/失败图片最多保留 12 小时
STRUCTURE_MAX_CHART_FILES=200          # 超过 200 张时只保留最新图片
STRUCTURE_REPLY_CHAIN_ENABLE=true      # 同币结构信号回复上一条结构消息
STRUCTURE_REVIEW_ENABLE=true           # 启用结构信号复盘统计
STRUCTURE_REVIEW_LOOKBACK_HOURS=24     # 默认复盘过去 24 小时信号
STRUCTURE_REVIEW_FORWARD_HOURS=4       # 最多跟踪信号后 4 小时
STRUCTURE_REVIEW_MIN_AGE_MINUTES=15    # 信号至少等待 15 分钟后复盘
STRUCTURE_REVIEW_MAX_REPORT_INTERVAL_SEC=3600 # 复盘报告真实推送最小间隔
COINGLASS_LIQUIDITY_ENABLE=false       # 默认关闭 CoinGlass 清算/盘口增强
COINGLASS_LIQUIDITY_MAX_SYMBOLS=30     # 每轮最多增强前 30 个结构候选
COINGLASS_LIQUIDITY_SCORE_MAX_DELTA=15 # 分数修正上限，避免压倒结构原始评分
COINGLASS_LIQUIDITY_CACHE_SEC=300      # 增强数据至少缓存 300 秒
LIQUIDITY_FALLBACK_ENABLE=true         # CoinGlass 不可用时启用免费降级源
BINANCE_ORDERBOOK_LIQUIDITY_ENABLE=true # 使用 Binance 免费盘口深度估算买墙/卖墙
BINANCE_ORDERBOOK_DEPTH_LIMIT=100      # 每个币读取的 Binance 盘口档位
COINALYZE_ENABLE=false                 # 可选：开启 Coinalyze 免费 Key 的清算历史辅助
COINALYZE_API_KEY=                     # 可选：Coinalyze 免费 API Key
ANNOUNCEMENT_PAGE_SIZE=50              # Binance 公告单页数量，公告测试会分页抓取多个分类
```

多源优先级：CoinGlass 可用时优先使用 CoinGlass；如果清算热力图或盘口热力图返回 `Upgrade plan`、无权限、空数据或超时，则自动降级。盘口侧默认使用 Binance 免费合约深度快照估算上方卖墙/下方买墙；清算侧可选使用 Coinalyze 历史清算量做方向辅助，但它不是预测清算池，推送里会标明数据源。

如果修改这些参数，可以用 `paopao config` 打开配置向导；更新项目时脚本会保留 token、群 ID、CoinGlass key、Coinalyze key 和话题 ID。

结构突破雷达 v1.8 的单次 dry-run：

```bash
python main.py structure-radar --mode pre --save-charts
python main.py structure-radar --mode confirm --save-charts
python main.py structure-review
python main.py coinglass-liquidity-test
python main.py announcements-test
python main.py structure-radar --mode pre --with-coinglass --save-charts
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

如果把 CoinGlass key 错填到了 `TG_TOPIC_ID`，重新运行安装脚本即可。新脚本会检测到非数字话题 ID，并自动清空。

如果 Telegram 话题无法置顶，通常是 bot 缺少置顶消息或管理话题权限。推送本身不会因此停止。
