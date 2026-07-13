# 泡泡抓币 Crypto Radar

泡泡抓币是一套面向 Telegram 的加密市场雷达与推送系统。当前版本聚焦实时监控、风险提示、信号留档和稳定运维。

## 核心功能

- 资金雷达：结合价格、成交量、持仓量与资金费率筛选市场异动。
- 启动雷达：按 15m/1h 完整窗口识别预热、临界和启动阶段。
- 资金流雷达：汇总 CVD、OI、费率和量价变化。
- 资金费率警报：监控极端或快速变化的资金费率。
- 结构雷达：识别突破、跌破、假突破风险和流动性结构。
- Binance 公告监听：跟踪上新、下架、Launchpool、HODLer、空投等公告。
- Telegram 推送：支持话题路由、冷却、去重、限流和推送历史。
- AI 助手：独立 Bot，提供状态查询、价格提醒和可选 AI 问答。
- Web 前台：公开总览与信号雷达。
- Web 后台：服务、配置、任务、日志、审计、价格提醒和提示词管理。

## Web 路由

- `/`：公开总览
- `/radar`：公开信号雷达
- `/admin`：需登录的运维控制台
- `/public-api/signals`：公开信号列表
- `/public-api/signals/stats`：公开信号统计
- `/public-api/signals/detail?id=...`：公开信号详情

公开接口只返回脱敏后的结构化信号。配置、日志、任务、服务控制和审计接口均需后台认证。
线上信号接口为 `https://paoxx.com/public-api/signals`。

## 常用命令

```bash
python main.py status
python main.py doctor
python main.py readiness
python main.py stable-check
python main.py once
python main.py live --send --confirm-real-send
python main.py structure-radar
python main.py structure-loop --send --confirm-real-send
python main.py ai-assistant
python main.py web
```

真实 Telegram 推送必须同时传入 `--send --confirm-real-send`，并通过 readiness 门禁。

## 本地运行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.oi.example .env.oi
python main.py doctor
```

公开前台：

```bash
cd frontend
npm ci
npm run dev
```

## 服务器更新

```bash
cd ~/paopao-crypto-radar
paopao update --yes || bash scripts/update_server.sh --yes
bash scripts/check_https_deploy.sh --with-stable-check
```

部署结构为 Nginx 对外提供 80/443，Next.js 仅监听 `127.0.0.1:3000`，Python 后端监听 `0.0.0.0:8080` 供 Nginx 反代。云安全组应关闭公网 3000/8080。

详细安装说明见 [docs/INSTALL_CN.md](docs/INSTALL_CN.md)，公开与后台 API 见 [docs/API.md](docs/API.md)。

## 安全边界

- 不读取交易所私钥，不执行下单。
- 不提供自动交易。
- 所有推送均为市场监控和风险提示，不构成投资建议。
- `.env.oi`、数据库、日志和运行历史不得提交到 Git。
