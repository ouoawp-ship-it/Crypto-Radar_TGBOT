# 泡泡抓币 Crypto Radar

轻量级加密市场观察雷达。默认 dry-run，不包含 Web/UI、admin 查询、自动交易。

## 功能和推送周期

- Binance 公告机会/风险监听：跟随主扫描，只推当天 CST 的可行动公告；识别 Alpha、上新、HODLer、Launchpool、Airdrop、下架、停止交易等，并按币种区分有无 Binance USDT 合约。
- 资金雷达汇总：默认 6 小时一次、每天最多 4 次，推送负费率榜、综合榜、埋伏榜、动量池、新币池、值得关注和数据质量。
- 启动雷达提醒：默认 3 分钟扫描一次，推送币种、阶段、分数、价格/OI/成交量变化和触发原因；同一币种后续更高阶段会回复上一条启动消息，形成连续追踪链。
- 五因子资金流雷达：默认每 1 小时收线后延迟 5 分钟推送一次，按上一完整窗口内的价格、OI、现货 CVD、合约 CVD、资金费率过滤资金流信号。
- OI/价格背离扫描：跟随资金雷达，跟踪建仓背离、多头共振、极端背离、持续/增强/消失状态。
- 自动清理：默认 1 小时检查一次，只清理可再生成的缓存、临时文件、坏 JSON 备份、过期日志和过长历史。
- CoinGlass 增强源：可选启用，用于后续接入多交易所 OI、爆仓、资金费率和合约市场动态。

## 服务器一键部署

服务器能访问这个 GitHub 私有仓库后，直接运行:

```bash
git clone https://github.com/ouoawp-ship-it/paopao-crypto-radar.git
cd paopao-crypto-radar
bash scripts/install_server.sh
```

第一次运行会自动创建 `.env.oi`。如果没有填写 Telegram 配置，会直接在终端提示输入 `TG_BOT_TOKEN` 和 `TG_CHAT_ID`；token 输入会显示出来，方便确认粘贴成功。空回车或格式不对会反复提示，不会继续启动服务。随后会提示 `COINGLASS_API_KEY`，直接回车就是纯 Binance 数据版本，填写 key 就启用 Binance + CoinGlass 双源版本；如果 CoinGlass 测试失败，安装脚本会自动退回纯 Binance。

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
```

## 一键更新

```bash
bash scripts/update_server.sh
```

## 安全规则

真实 Telegram 推送必须同时带:

```bash
--send --confirm-real-send
```

`.env.oi` 和 `data/` 状态文件不应提交到 GitHub。

更详细说明见 [docs/SERVER_DEPLOY.md](docs/SERVER_DEPLOY.md)。
项目结构说明见 [docs/PROJECT_STRUCTURE.md](docs/PROJECT_STRUCTURE.md)。

## 中文安装目录

第一次安装、重新安装、配置项说明和常见排错见 [docs/INSTALL_CN.md](docs/INSTALL_CN.md)。

修改 bot token、群 ID、CoinGlass key 或 Telegram 话题配置:

```bash
bash scripts/install_server.sh config
```

服务器安装后会写入快捷命令:

```bash
paopao          # 打开中文操作菜单
paopao config   # 修改配置
paopao logs     # 查看实时日志
paopao version  # 查看当前版本号
paopao check-update # 检查当前版本/GitHub版本
paopao update   # 有更新时确认后更新项目
```

`paopao update` 会在拉取新代码后安全同步 `.env.oi`：新增的普通配置项会自动补上，明确列入迁移白名单的默认参数会自动升级；`TG_BOT_TOKEN`、`TG_CHAT_ID`、`COINGLASS_API_KEY` 和各类话题 ID 不会被覆盖。

项目版本号写在 `VERSION` 文件里，当前为 `v1.7`，后续功能更新按 `v1.8`、`v1.9` 递增；`paopao update` 会同时显示版本号和 git 提交号。
