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
```

如需启用 P1 衍生品多源校验，再填写：

```dotenv
COINGLASS_ENABLE=true
COINGLASS_API_KEY=...
COINALYZE_ENABLE=true
COINALYZE_API_KEY=...
```

只启用其中一套不会阻止服务启动，但健康检查会报告降级；启用某数据源却未填写对应 Key 会被健康检查判定为配置失败。

安装脚本会创建 `.venv`、安装锁定依赖、执行编译和单元测试，并安装以下 systemd 单元：

- `paopao-radar`：主 BOT 信号推送服务。
- `paopao-market-stream`：实时行情上下文服务。
- `paopao-cleanup.timer`：运行数据定时清理。
- `paopao-health.timer`：定时运行稳定性与数据新鲜度检查。
- `paopao-backup.timer`：每天在线备份活动 SQLite 数据库并执行恢复验证。

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

更新脚本只接受 fast-forward，遇到已跟踪文件本地修改或 Git 分叉会停止。更新通过 Python 编译和完整单元测试后，才会重启 BOT 服务。

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
