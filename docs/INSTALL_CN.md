# BOT-only 服务器安装与更新

## 首次安装

项目要求 Python 3.12，服务器不再需要 Node.js、Next.js、Playwright 或 Nginx。

```bash
cp .env.oi.example .env.oi
nano .env.oi
bash scripts/install_server.sh
```

至少填写：

```dotenv
TG_BOT_TOKEN=123456:...
TG_CHAT_ID=-1001234567890
MAIN_BOT_DELIVERY_MODE=dry_run
MAIN_BOT_REAL_SEND=false
MAIN_BOT_REAL_SEND_ACK=
```

主 BOT 默认是 Dry-run。旧 `.env.oi` 没有这三个字段时，启动包装器也会
安全按 `dry_run` 处理，不会自动进入真实发送。

如需使用 CoinGlass/Coinalyze 只读诊断，再填写（方向信号不需要）：

```dotenv
COINGLASS_ENABLE=true
COINGLASS_API_KEY=...
COINALYZE_ENABLE=true
COINALYZE_API_KEY=...
```

只启用其中一套不会阻止服务启动，但健康检查会报告降级；启用某数据源却未填写对应 Key 会被健康检查判定为配置失败。

安装脚本会创建 `.venv`、安装锁定依赖、执行编译和单元测试，并安装以下 systemd 单元：

- `paopao-radar`：主 BOT Dry-run/Real 安全运行服务。
- `paopao-market-stream`：实时行情上下文服务。
- `paopao-cleanup.timer`：运行数据定时清理。
- `paopao-health.timer`：定时运行稳定性与数据新鲜度检查。
- `paopao-backup.timer`：每天在线备份活动 SQLite 数据库并执行恢复验证。

`paopao-radar.service` 通过 `scripts/run_main_bot.sh` 启动：

- `dry_run` 严格执行 `main.py loop`，不带任何发送参数；
- `real` 只有在 Mode、真实发送开关、固定中文 ACK、Bot Token 和 Chat ID
  全部通过包装器门禁后，才执行
  `main.py live --send --confirm-real-send`；
- `live` 内部原有 readiness 与双发送门禁保持不变；
- 配置或安全门禁失败以退出码 2 停止，systemd 不会重启风暴；
- SIGINT 的退出码 130 视为正常停止，其他异常仍按 `Restart=on-failure`
  恢复。

可通过配置管理器原子切换：

```bash
.venv/bin/python scripts/paopao_config.py main-bot-delivery dry-run
# Real 仅在完成风险确认和 Telegram 配置后使用：
.venv/bin/python scripts/paopao_config.py main-bot-delivery real
```

Profile 只保存配置，不会自动启动或重启服务。新 Unit 可单独幂等安装，
默认也不会启动主 BOT：

```bash
sudo bash scripts/install_main_bot_service.sh
```

## 日常维护

```bash
paopao status
paopao logs
paopao restart
paopao doctor
paopao readiness
paopao stable-check
paopao providers
paopao backup
paopao telegram-test
```

`paopao providers` 是只读验收，不发送 Telegram 消息，也不会在输出中泄露 API Key。`paopao backup` 会立即创建一次备份；也可用 `systemctl status paopao-backup.timer` 和 `journalctl -u paopao-backup` 检查自动备份。

`telegram-test` 默认 dry-run。真实测试需手动执行：

```bash
.venv/bin/python main.py telegram-test --send --confirm-real-send
```

## 更新

```bash
bash scripts/update_server.sh --check
bash scripts/update_server.sh --yes
```

更新脚本只接受 fast-forward，遇到已跟踪文件本地修改或 Git 分叉会停止。更新通过 Python 编译和完整单元测试后，先同步安全默认配置并安装包装器 Unit，才会重启 BOT 服务；缺少新字段时重启进入 Dry-run。

从旧 Web 版本升级时，脚本会停用并删除 `paopao-frontend`、`paopao-web`、`paopao-ai` 三个旧 systemd 单元，并只删除本项目原先创建的 `/etc/nginx/conf.d/00-paoxx-frontend.conf`。其他 Nginx 配置不会被触碰。

## 排障

```bash
systemctl status paopao-radar paopao-market-stream paopao-health.timer paopao-backup.timer
journalctl -u paopao-radar -n 200 --no-pager
journalctl -u paopao-market-stream -n 200 --no-pager
journalctl -u paopao-backup -n 100 --no-pager
.venv/bin/python main.py doctor
.venv/bin/python main.py runtime-status
```
