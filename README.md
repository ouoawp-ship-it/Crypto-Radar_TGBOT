# 泡泡抓币 Crypto Radar

轻量级加密市场观察雷达，保留命令行运行方式，默认 dry-run，不包含 Web/UI、admin 查询、自动交易。

## 快速开始

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.oi.example .env.oi
python main.py status
python main.py readiness
```

Linux 服务器使用:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.oi.example .env.oi
python main.py status
python main.py readiness
```

## 常用命令

```bash
python main.py observe --duration-minutes 360 --launch-interval 180 --launch-scan-limit 40 --records 200 --top 12
python main.py telegram-test --send --confirm-real-send
python main.py live --send --confirm-real-send
python main.py runtime-status
```

## 安全规则

真实 Telegram 推送必须同时带:

```bash
--send --confirm-real-send
```

`.env.oi` 和 `data/` 状态文件不应提交到 GitHub。

服务器部署步骤见 [SERVER_DEPLOY.md](SERVER_DEPLOY.md)。
