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
```

设置后台密码：

```bash
.venv/bin/python main.py admin-password set
```

如启用独立 AI 助手，再配置 `AI_ASSISTANT_ENABLE=true`、`AI_BOT_TOKEN` 和允许的用户/群 ID。

## 服务

- `paopao-radar`：主雷达和 Telegram 推送
- `paopao-structure`：结构雷达
- `paopao-web`：Python 后台与 API
- `paopao-frontend`：Next.js 公开前台
- `paopao-ai`：AI 助手和价格提醒

查看服务：

```bash
sudo systemctl status paopao-radar paopao-structure paopao-web paopao-frontend paopao-ai
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
- `https://paoxx.com/admin`：后台控制台
- `127.0.0.1:3000`：Next.js 本机入口
- `127.0.0.1:8080` 或 Nginx 可达的后端入口

Nginx 对外监听 80/443。云安全组应关闭公网 3000/8080，只开放 SSH、HTTP 和 HTTPS。

## 常用诊断

```bash
.venv/bin/python main.py status
.venv/bin/python main.py doctor
.venv/bin/python main.py readiness
.venv/bin/python main.py stable-check
journalctl -u paopao-radar -n 200 --no-pager
journalctl -u paopao-web -n 200 --no-pager
```

稳定版自检返回 `attention` 时，先在后台查看日志、审计和失败任务，确认是否影响真实 Telegram 推送。

## 数据与安全

`.env.oi`、`data/*.db`、日志、图表、运行状态和推送历史都是服务器运行数据，不应提交到 Git。系统不需要交易所私钥，不下单，也不执行自动交易。
