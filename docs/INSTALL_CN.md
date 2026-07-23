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

安装脚本会创建 `.venv`、安装锁定依赖、执行编译和单元测试，并安装三个 systemd 单元：

- `paopao-radar`：主 BOT 信号推送服务。
- `paopao-market-stream`：实时行情上下文服务。
- `paopao-cleanup.timer`：运行数据定时清理。

## 日常维护

```bash
paopao status
paopao logs
paopao restart
paopao doctor
paopao readiness
paopao stable-check
paopao telegram-test
```

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
systemctl status paopao-radar paopao-market-stream
journalctl -u paopao-radar -n 200 --no-pager
journalctl -u paopao-market-stream -n 200 --no-pager
.venv/bin/python main.py doctor
.venv/bin/python main.py runtime-status
```
