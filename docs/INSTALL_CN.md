# 泡泡抓币服务器安装与更新

## 首次安装

```bash
git clone https://github.com/ouoawp-ship-it/Crypto-Radar_TGBOT.git ~/paopao-crypto-radar
cd ~/paopao-crypto-radar
bash scripts/install_server.sh
```

安装脚本会创建 Python 虚拟环境、安装前后端依赖、配置 systemd 服务并准备 Nginx 反代。

## 基础配置

```bash
cd ~/paopao-crypto-radar
cp .env.oi.example .env.oi
nano .env.oi
```

至少配置：

```dotenv
TG_BOT_TOKEN=你的雷达BotToken
TG_CHAT_ID=你的群或频道ID
WEB_AUTH_MODE=password
WEB_ADMIN_USERNAME=admin
PAOXX_COCKPIT_V2_MODE=enabled
```

设置后台密码：

```bash
.venv/bin/python main.py admin-password set
```

如启用独立 AI 助手，再配置 `AI_ASSISTANT_ENABLE=true`、`AI_BOT_TOKEN` 和允许的用户/群 ID。

实时市场流默认同时启用 Binance、Bybit 和 OKX 公共通道，不需要交易所 API Key。若服务器所在区域无法访问某个官方公共通道，可在 `.env.oi` 设置 `REALTIME_BYBIT_ENABLE=false` 或 `REALTIME_OKX_ENABLE=false`；健康接口会按实际启用集合验收，不能在已启用但无数据时伪装为健康。

## 服务

- `paopao-radar`：主雷达和 Telegram 推送
- `paopao-market-stream`：Binance、Bybit、OKX 实时成交与可用强平采集
- `paopao-web`：Python 后台与 API
- `paopao-frontend`：Next.js 公开前台
- `paopao-ai`：AI 助手和价格提醒

查看服务：

```bash
sudo systemctl status paopao-radar paopao-market-stream paopao-web paopao-frontend paopao-ai
```

## 更新

```bash
cd ~/paopao-crypto-radar
paopao update --yes || bash scripts/update_server.sh --yes
```

更新脚本会拉取 `main`、同步新增环境变量、构建 Next.js、重启服务、更新 Nginx 配置并运行稳定版自检。

更新后验收：

```bash
bash scripts/check_https_deploy.sh --with-stable-check
```

需要额外验证证书自动续期时：

```bash
bash scripts/check_https_deploy.sh --with-stable-check --with-certbot-dry-run
```

## 访问结构

- `https://paoxx.com/`：公开总览
- `https://paoxx.com/radar`：信号雷达
- `https://paoxx.com/funds`：资金中心
- `https://paoxx.com/info`：信息中心
- `https://paoxx.com/agents`：AI 决策
- `https://paoxx.com/admin`：后台控制台
- `127.0.0.1:3000`：Next.js 本机入口
- `127.0.0.1:8080` 或 Nginx 可达的后端入口

Nginx 对外监听 80/443。云安全组应关闭公网 3000/8080，只开放 SSH、HTTP 和 HTTPS。

后台“配置中心”用于填写、替换或清空 Telegram Token、AI API Key 和 CoinGlass API Key。密钥字段只显示掩码，接口不会向浏览器回传明文；保存前自动备份 `.env.oi`，Linux 上密钥文件和备份会限制为当前用户读写。CoinGlass Key 保存后先点击“测试 CoinGlass”，验证套餐和有效期，再开启 `COINGLASS_ENABLE`。配置变更只允许预先登记的字段，不提供任意环境变量编辑器。

## V2 灰度与紧急回滚

驾驶舱开关支持 `enabled`、`preview`、`disabled`。`preview` 用于带预览标识的观察期；`disabled` 会隐藏 V2 资金、信息、Agent 页面，并让雷达退回旧信号列表。Telegram Bot、AI 助手、后台和旧信号 API 不受影响。

紧急回滚：

```bash
cd ~/paopao-crypto-radar
sed -i 's/^PAOXX_COCKPIT_V2_MODE=.*/PAOXX_COCKPIT_V2_MODE=disabled/' .env.oi
bash scripts/update_server.sh --yes
bash scripts/check_https_deploy.sh --with-stable-check
```

必须通过更新脚本重建前台；只重启服务会造成浏览器编译配置与后端开关不一致。回滚后至少验证 `/public-api/signals`、公开雷达旧列表、Telegram 实际推送和后台登录。恢复时把开关改回 `preview` 或 `enabled`，再次运行更新与验收脚本。

`/public-api/stream` 是 SSE 长连接。Nginx 配置必须保留该路径的 `proxy_buffering off`、`proxy_cache off` 和足够的读取超时；部署验收脚本会检查握手。

## 常用诊断

```bash
.venv/bin/python main.py status
.venv/bin/python main.py doctor
.venv/bin/python main.py readiness
.venv/bin/python main.py stable-check
journalctl -u paopao-radar -n 200 --no-pager
journalctl -u paopao-market-stream -n 200 --no-pager
journalctl -u paopao-web -n 200 --no-pager
```

稳定版自检返回 `attention` 时，先在后台查看日志、审计和失败任务，确认是否影响真实 Telegram 推送。

## 数据与安全

`.env.oi`、`data/*.db`、日志、图表、运行状态和推送历史都是服务器运行数据，不应提交到 Git。系统不需要交易所私钥，不下单，也不执行自动交易。

V2 运行数据默认位于 `data/market_snapshots.db`、`data/news_events.db` 和 `data/agent_insights.db`。市场快照与资讯事件按配置执行数量和天数双重保留；升级会自动补齐兼容字段并去重，不应手工覆盖生产数据库。
