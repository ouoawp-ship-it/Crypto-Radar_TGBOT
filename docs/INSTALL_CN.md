# BOT-only 服务器安装与更新

## 首次安装

项目要求 Python 3.12，服务器不再需要 Node.js、Next.js、Playwright 或 Nginx。

```bash
cp config/.env.oi.example config/.env.oi
nano config/.env.oi
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

主 BOT 默认是 Dry-run。旧配置文件没有这三个字段时，启动包装器也会
安全按 `dry_run` 处理，不会自动进入真实发送。

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
paopao backup
paopao telegram-test
```

`paopao backup` 会立即创建一次备份；也可用 `systemctl status paopao-backup.timer` 和 `journalctl -u paopao-backup` 检查自动备份。

`telegram-test` 默认 dry-run。真实测试需手动执行：

```bash
.venv/bin/python main.py telegram-test --send --confirm-real-send
```

普通推送不会自动创建话题或刷新置顶说明。需要创建/修复时，请使用中文菜单
“Telegram 设置与测试 → 手工创建/修复话题并置顶说明”，或明确运行：

```bash
.venv/bin/python main.py telegram-topic-setup \
  --topic-template TG_RADAR_SUMMARY \
  --send --confirm-real-send
```

只刷新已经存在的脉冲雷达说明时使用 `telegram-topic-refresh`。它按说明版本和
正文去重，不会创建新话题：

```bash
.venv/bin/python main.py telegram-topic-refresh \
  --topic-template TG_LAUNCH_ALERT \
  --send --confirm-real-send
```

缺少某个核心雷达的专属话题时，真实推送会安全阻断，不会发到群主界面。

盘整突破雷达升级后默认关闭。启用前先显式创建它的独立话题：

```bash
.venv/bin/python main.py telegram-topic-setup \
  --topic-template TG_CONSOLIDATION_BREAKOUT \
  --send --confirm-real-send
```

随后在 `config/.env.oi` 设置 `CONSOLIDATION_BREAKOUT_ENABLE=true`。完整扫描周期、
240 日箱体和假突破规则见
[盘整突破雷达说明](CONSOLIDATION_BREAKOUT_RADAR_CN.md)。

## 更新

```bash
bash scripts/update_server.sh --check
bash scripts/update_server.sh --yes --refresh-pulse-topic-intro
```

上述入口继续用于 `main` 的普通 fast-forward 维护。正式生产发布只允许使用已
通过 Tag CI 的 annotated Tag，并使用独立入口：

```bash
bash scripts/deploy_tag.sh --check-tag v2.3.0
bash scripts/deploy_tag.sh --tag v2.3.0 --yes --refresh-pulse-topic-intro
```

`--refresh-pulse-topic-intro` 是显式真实 Telegram 操作；省略时更新脚本不会
刷新置顶说明。执行时脚本内部仍同时提供两重真实发送确认。

该入口会在停服后备份私有配置、运行状态、数据库和 systemd 定义；部署失败
自动回滚。它不会打印或用示例值覆盖服务器密钥。完整门禁和人工回滚命令见
[山寨合约异动雷达生产运行与发布手册](ALTCOIN_CONTRACT_ANOMALY_FINAL_CN.md)。

更新脚本只接受 fast-forward，遇到已跟踪文件本地修改或 Git 分叉会停止。更新通过 Python 编译和完整单元测试后，先同步安全默认配置并安装包装器 Unit，才会重启 BOT 服务；缺少新字段时重启进入 Dry-run。

从旧目录升级时，脚本会先把根目录的 `.env.oi` 做哈希备份并复制到
`config/.env.oi`。在新 systemd Unit 安装并成功重启前，旧文件仍会保留，避免
升级中途失败导致服务丢失配置；全部完成后才删除旧副本。新旧文件内容不同会
以 `env_path_conflict` 停止，绝不覆盖任意一份。回滚到旧版本时，应从
`backups/config-migration-*` 中恢复 `legacy.env.oi` 到根目录并保持权限 600。

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
