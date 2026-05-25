# 泡泡抓币 Crypto Radar

轻量级加密市场观察雷达。默认 dry-run，不包含 Web/UI、admin 查询、自动交易。

## 服务器一键部署

服务器能访问这个 GitHub 私有仓库后，直接运行:

```bash
git clone https://github.com/ouoawp-ship-it/paopao-crypto-radar.git
cd paopao-crypto-radar
bash scripts/install_server.sh
```

第一次运行会自动创建 `.env.oi`。填好 Telegram 配置后，再跑同一条安装命令:

```bash
nano .env.oi
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

## 查看运行

```bash
sudo systemctl status paopao-radar
journalctl -u paopao-radar -f
python main.py runtime-status
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

更详细说明见 [SERVER_DEPLOY.md](SERVER_DEPLOY.md)。
