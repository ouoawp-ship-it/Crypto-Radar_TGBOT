# 泡泡抓币 Crypto Radar

泡泡抓币是一套面向 Telegram 用户的异常机会雷达与信号验证工作台。系统聚焦实时监控、风险提示、可解释信号、主动提醒和稳定运维，不执行交易。

## 核心功能

- 资金雷达：结合价格、成交量、持仓量与资金费率筛选市场异动。
- 启动雷达：按 15m/1h 完整窗口识别预热、临界和启动阶段。
- 资金流雷达：汇总 CVD、OI、费率和量价变化。
- 资金费率警报：监控极端或快速变化的资金费率。
- Binance 公告监听：跟踪上新、下架、Launchpool、HODLer、空投等公告。
- Telegram 推送：支持话题路由、冷却、去重、限流、推送历史与精确信号深链。
- AI 助手：独立 Bot，承接 Web 币种分析深链、目标价/涨跌/OI/费率提醒和可选 AI 问答。
- 信号情报：自身历史极端度、市场相对强度、同口径绝对规模、跨模块共振和生命周期。
- Web 前台：公开总览、四类机会榜、证据抽屉、轻量单币上下文和浏览器本地自选。
- Web 后台：服务、配置、任务、日志、审计、价格提醒和提示词管理。

## Web 路由

- `/`：公开总览
- `/radar`：公开信号雷达
- `/coin/<symbol>`：轻量单币验证上下文
- `/watchlist`：当前浏览器的本地自选
- `/admin`：需登录的运维控制台
- `/public-api/signals`：公开信号列表
- `/public-api/signals/stats`：公开信号统计
- `/public-api/signals/context?id=...`：证据、排名、共振和生命周期
- `/public-api/radar/intelligence`：四类机会榜与情报层
- `/public-api/coin/context?symbol=...`：单币聚合上下文
- `/public-api/market/watchlist?symbols=...`：批量自选快照
- `/public-api/health`：脱敏健康、P95、缓存和限流计数

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

工程门禁：

```bash
python -m unittest discover -s tests -p "test_*.py"
python -m compileall -q paopao_radar tests scripts
cd frontend
npm run typecheck
npm run build
npm run e2e
npm audit --audit-level=high
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
- 不恢复回测、模型注册、校准和研究生命周期等研究型 Web 平台。
- 所有推送均为市场监控和风险提示，不构成投资建议。
- `.env.oi`、数据库、日志和运行历史不得提交到 Git。
