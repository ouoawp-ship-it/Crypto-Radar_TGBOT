# 泡泡抓币中文安装目录

## v1.76.2 运维说明

v1.76.2 修复多交易所资金费率消息的表格对齐。资金费率警报和启动预警中的“多交易所资金费率”会使用统一等宽表格展示，列包含交易所、费率/周期、上次结算、本次周期和下次结算；当周期出现 8H→4H 或 4H→1H 变化时仍保持列对齐。

该补丁只调整 Telegram 展示格式，不改变资金费率采集策略、Telegram 主推送流程、数据库 schema 或自动交易相关逻辑。

## v1.76.1 运维说明

v1.76.1 用于补强资金费率展示。多交易所资金费率表会显示上次结算时间、本次结算周期和下次结算时间；如果交易所结算周期出现 8H→4H 或 4H→1H 变化，消息正文会直接展示该周期变化，方便运维和人工复盘判断资金费率是否进入高频结算状态。

该补丁不改变 Telegram 主推送流程、不改变资金费率采集策略、不改变数据库 schema，也不引入自动交易。

## v1.76.0 运维说明

v1.76.0 新增 Binance-Centric Signal Lifecycle Tracker。生命周期数据写入独立运行库 `data/lifecycle.db`，不迁移也不破坏 `signals.db`、`outcomes.db` 或 `jobs.db`。一个币首次出现有效信号后会自动建档，后续同币种信号会进入同一生命周期，用于观察同级确认、周期升级、短线冷却、风险升高和启动失败。

生命周期核心数据只以 Binance 为主，包括价格、K 线、成交量、OI、合约 taker buy/sell 近似 CVD、现货 aggTrades 近似 CVD 和 funding rate。其他交易所只作为旁路观察，仅看当前价格和资金费率偏离，不参与生命周期评分或状态流转。

常用命令：

```bash
cd /home/ubuntu/paopao-crypto-radar
.venv/bin/python main.py lifecycle-backfill --lookback-hours 168
.venv/bin/python main.py lifecycle-scan --lookback-hours 24 --limit-symbols 80
.venv/bin/python main.py lifecycle-status --symbol BTCUSDT
```

生产部署后可先 dry-run：

```bash
.venv/bin/python main.py lifecycle-backfill --lookback-hours 168 --dry-run
.venv/bin/python main.py lifecycle-scan --lookback-hours 24 --limit-symbols 30 --dry-run
```

公开只读 API：

```text
/public-api/lifecycle/summary
/public-api/lifecycle/list
/public-api/lifecycle/detail?symbol=BTCUSDT
/public-api/lifecycle/events?symbol=BTCUSDT
/public-api/lifecycle/metrics?symbol=BTCUSDT
```

后台私有 API 仍需要登录：

```text
/api/lifecycle/summary
/api/lifecycle/list
/api/lifecycle/detail?symbol=BTCUSDT
/api/lifecycle/events?symbol=BTCUSDT
/api/lifecycle/run-scan
/api/lifecycle/run-backfill
```

公开前台新增 `/lifecycle` 生命周期页面，单币详情页 `/coin/BTCUSDT` 会展示生命周期状态、首次信号、最高周期、Binance 价格/OI/CVD/funding 跟随和事件时间线。生命周期 Telegram 跟随提醒是新增辅助消息，不改变现有 Telegram 主推送流程。该功能仅用于信号整理和风险提示，不构成投资建议，不执行自动交易。

## v1.74.5 运维说明

v1.74.5 加固 active Nginx 清理逻辑。部署脚本现在会扫描 `/etc/nginx/sites-enabled` 和 `/etc/nginx/conf.d` 下所有 active 文件，查找 `server_name paoxx.com` 或 `server_name ... www.paoxx.com`，再用 `readlink -f` 和 keep file 对比，只保留 `/etc/nginx/conf.d/00-paoxx-frontend.conf`。

被禁用项会先备份到 `/etc/nginx/backup-paopao/duplicate-cleanup.<timestamp>/`。旧入口如果是 symlink，只删除 symlink；如果是普通文件，则改名为 `.disabled.<timestamp>`。不会删除 `/etc/nginx/sites-available` 中的历史源文件，不会删除 Let's Encrypt 证书，也不会删除 certbot 需要的 SSL 配置。HTTP 80 会保留：

```nginx
location ^~ /.well-known/acme-challenge/ {
    root /var/www/html;
}
```

部署后检查：

```bash
sudo nginx -t 2>&1
sudo grep -RIn "server_name .*paoxx.com" /etc/nginx/sites-enabled /etc/nginx/conf.d 2>/dev/null || true
sudo nginx -T 2>&1 | grep -nE "configuration file|server_name paoxx.com|listen 80|listen 443"
bash scripts/check_https_deploy.sh --with-stable-check
```

预期只剩 `/etc/nginx/conf.d/00-paoxx-frontend.conf` 声明 `paoxx.com`，且 `check_https_deploy.sh` 阻断为 0。本版本不改 Telegram 主推送流程，不引入自动交易，不改数据库 schema 或后端 API contract。

## v1.74.4 运维说明

v1.74.4 用于清理生产 Nginx 的重复 `paoxx.com` server block。部署后 active 入口应只保留 `/etc/nginx/conf.d/00-paoxx-frontend.conf`；旧的 `/etc/nginx/sites-enabled/default`、`/etc/nginx/sites-enabled/paoxx.com` 和 `/etc/nginx/conf.d` 中其他包含 `server_name paoxx.com` 的入口会被禁用。

禁用前会备份到 `/etc/nginx/backup-paopao/`。如果旧入口是 symlink，只删除 symlink，保留 `sites-available` 原始文件；如果是普通文件，会改名为 `.disabled.<timestamp>`。部署脚本会运行 `nginx -t 2>&1` 和 `nginx -T 2>&1`，如果仍有 `conflicting server name "paoxx.com"` 会停止并提示：

```bash
sudo grep -RIn "server_name .*paoxx.com" /etc/nginx/sites-enabled /etc/nginx/conf.d
```

最终路由必须保持：

```text
/admin       -> http://127.0.0.1:8080
/api/        -> http://127.0.0.1:8080
/public-api/ -> http://127.0.0.1:8080
/_next/      -> http://127.0.0.1:3000
/            -> http://127.0.0.1:3000
```

`scripts/check_https_deploy.sh` 会把重复 `server_name paoxx.com` 的 Nginx warning 判为阻断，同时继续检查 `paopao-frontend`、本机 3000、HTTPS `paoxx-frontend` marker、`nginx -T` 中的 3000/8080 路由、`/admin`、`/public-api` 和私有 `/api` 401。本版本不改 Telegram 主推送流程，不引入自动交易，不改数据库 schema 或后端 API contract。

## v1.74.3 运维说明

v1.74.3 修复 `bash scripts/check_https_deploy.sh --with-stable-check` 的日志误判。类似下面的正常运行日志不会再被当成阻断项：

```text
OK observe_history: 启动观察历史 500 轮
```

部署验收日志扫描会先过滤白名单，例如 `OK observe_history`、`启动观察历史`、稳定版自检、readiness wait、可自动重试网络超时、测试重试队列和单次 ReadTimeout / ConnectTimeout。随后只按明确错误规则判定阻断，例如 `Traceback`、`Exception occurred during processing`、`Unhandled exception`、`RuntimeError`、`sqlite database is locked`、`no such table`、`EADDRINUSE`、`ECONNREFUSED`、`500 Internal Server Error`、明确的 `/api/ 500` / `/public-api/ 500` / `/admin 500`、`ERROR`、`CRITICAL` 等。

如果仍发现阻断日志，验收脚本会输出服务名、匹配规则、判定原因和脱敏后的原始日志行。v1.74.2 的 Next.js / Nginx active route 验收保持不变：`paopao-frontend`、本机 3000、HTTPS `paoxx-frontend` marker、`nginx -T` 中的 3000/8080 路由、`/admin`、`/public-api` 和私有 `/api` 401 仍会继续检查。本版本不改 Telegram 主推送流程，不引入自动交易，不改数据库 schema 或后端 API contract。

## v1.74.2 运维说明

v1.74.2 修复公开前台公网路由未切到 Next.js 的问题。`paopao-frontend.service` 和本机 `127.0.0.1:3000` 正常，不代表公网 `https://paoxx.com/` 一定已经走 Next.js；必须看 Nginx 实际生效配置。

更新脚本现在会写入 `/etc/nginx/conf.d/00-paoxx-frontend.conf`，并禁用旧 default / legacy 站点入口，避免旧 Python 前台继续接管 `/`。生效路由要求：

```text
/admin       -> http://127.0.0.1:8080
/api/        -> http://127.0.0.1:8080
/public-api/ -> http://127.0.0.1:8080
/_next/      -> http://127.0.0.1:3000
/            -> http://127.0.0.1:3000
```

验收时优先执行：

```bash
sudo nginx -T 2>/dev/null | grep -E "proxy_pass http://127.0.0.1:3000|proxy_pass http://127.0.0.1:8080"
curl -s https://paoxx.com/ | grep -E "paoxx-frontend|nextjs-dashboard"
bash scripts/check_https_deploy.sh --with-stable-check
```

`scripts/check_https_deploy.sh` 已改为检查 `nginx -T` 的 active config，而不是只检查仓库里的模板。`/admin`、`/api/`、`/public-api/` 仍由 Python 后端提供；`/` 和 `/_next/` 由 Next.js 提供。本版本不改 Telegram 主推送流程，不引入自动交易。

## v1.74.1 运维说明

v1.74.1 补齐 Next.js 公开前台生产接线。`paopao update --yes` 会构建 `frontend/`、安装或更新 `paopao-frontend.service`、启动并重启该服务、写入 Nginx 反代配置并 reload Nginx，然后再执行 stable-check。

`paopao-frontend.service` 使用服务器上实际的 `npm` 路径，工作目录为 `/home/ubuntu/paopao-crypto-radar/frontend`，只监听 `127.0.0.1:3000`，不会暴露到公网。`/admin`、`/api/`、`/public-api/` 必须优先反代到 Python 后端 `127.0.0.1:8080`，根路径 `/` 最后反代到 Next.js `127.0.0.1:3000`。

部署验收脚本现在会检查：

```text
paopao-frontend.service 是否存在并 active
127.0.0.1:3000 是否监听
https://paoxx.com/ 是否包含 paoxx-frontend / nextjs-dashboard 标识
https://paoxx.com/admin 是否仍返回后台
/public-api 是否 ok=true
/api 未登录是否 401
```

Next.js 页面带有隐藏标识 `paoxx-frontend=nextjs-dashboard`，因此旧 Python fallback 不会再被误判为新前台。日志验收如果发现阻断关键词，会输出服务名和匹配片段，便于定位。

## v1.74.0 运维说明

v1.74.0 新增 `frontend/` Next.js 公开前台。公开前台负责 `https://paoxx.com/` 的中文数据仪表盘，页面包括：首页、信号雷达、决策模型、结果追踪、决策回测、单币详情和公开 API。Python 后端继续负责 Telegram、数据采集、`/admin`、`/api/*`、`/public-api/*`。

生产服务结构：

```text
paopao-frontend: Next.js 公开前台，监听 127.0.0.1:3000
paopao-web: Python 后台和 API，监听 8080
Nginx: 统一 HTTPS 入口
```

Nginx 反代建议：

```text
/             -> http://127.0.0.1:3000
/radar        -> http://127.0.0.1:3000
/decision     -> http://127.0.0.1:3000
/outcomes     -> http://127.0.0.1:3000
/backtest     -> http://127.0.0.1:3000
/coin/...     -> http://127.0.0.1:3000
/admin        -> http://127.0.0.1:8080
/api/         -> http://127.0.0.1:8080
/public-api/  -> http://127.0.0.1:8080
```

更新脚本会自动安装或检查 Node.js 20+，执行 `frontend` 的 `npm install` 和 `npm run build`，并安装/重启 `paopao-frontend` systemd 服务。部署后执行：

```bash
cd /home/ubuntu/paopao-crypto-radar
paopao update --yes || bash scripts/update_server.sh --yes
bash scripts/check_https_deploy.sh --with-stable-check
```

公开前台只访问 `/public-api/*`，不会读取后台 `/api/*`、Cookie、Authorization、后台配置、日志、审计、Telegram 私有字段或任何 token/secret。本版本不改 Telegram 主推送流程，不实现自动交易，不接交易所下单 API，不修改 `signals.db` / `outcomes.db` / `jobs.db` schema。

## v1.73.0 运维说明

v1.73.0 新增“决策回测”看板。它只读取 `data/outcomes.db` 的 `signal_outcomes` 表，统计不同决策在 1h / 4h / 24h / 72h 后的表现，用于评估 Signal Decision Model 是否需要继续校准。

新增公开只读 API：

```text
/public-api/backtest/decision
/public-api/backtest/decision/matrix
/public-api/backtest/decision/detail
```

新增后台私有 API：

```text
/api/backtest/decision
/api/backtest/decision/matrix
/api/backtest/decision/detail
```

后台私有 API 继续需要登录；公开 API 继续脱敏，不返回 payload_json、text_html、dedup_key、Telegram topic/message/reply、jobs、audit、config、logs、Cookie、Authorization 或 token/secret 字段。

统计口径：

```text
success      已计算成功，参与平均收益、回撤、正收益比例等指标。
pending      结果窗口未到期，只参与覆盖率统计，不当作亏损。
unavailable  价格源无法提供该交易对数据，只参与覆盖率统计，不当作亏损。
error        系统异常，需要后台排查，不参与收益统计。
```

看板会输出样本数、覆盖率、平均最终涨跌、平均最大涨幅、平均最大回撤、正收益比例、明显回撤比例、期望评分和样本质量，并生成模型诊断结论，例如可试仓是否有效、风险警报是否有过滤价值、禁止追高是否过度压制强趋势、等待回踩是否符合先回撤后转正特征。

该功能仅用于复盘、统计和模型校准；不会执行自动交易，不接交易所私有下单接口，不设置杠杆，不挂止盈止损，不操作真实资金，也不会改变 Telegram 推送节奏。

## v1.72.2 运维说明

v1.72.2 将 outcome 价格源不可用与系统错误拆开处理。Binance 返回 HTTP 400、invalid symbol、Bad Request、symbol not found 或空 K 线数据时，结果会写入 `data_status=unavailable`、`result_label=数据不足`，不会计入真实系统 `error`。执行 `outcome-scan` 时会自动把旧库中 `HTTP Error 400` 这类历史误分类记录修复为 `unavailable`。

扫描报告会单独输出“数据不足 / 价格源不可用摘要”，按 `symbol horizon` 显示原因，便于确认哪些交易对当前价格源无法覆盖。1000 前缀、非 Binance 现货交易对、部分新币可能需要后续 futures 或多交易所行情源补齐；在补齐前会显示为“数据不足 / 价格源不可用”，不应作为 stable-check 的阻断错误处理。

## v1.72.0 运维说明

v1.72.0 新增 Signal Outcome Tracking。系统会从 `signals.db` / `signal_events` 兼容视图读取 `status=sent`、带有效 `USDT` 交易对和可解析时间的信号，为 1h / 4h / 24h / 72h 四个窗口创建追踪记录，并在窗口到期后读取公开行情 K 线计算后续表现。

结果保存在独立运行库 `data/outcomes.db`，表名为 `signal_outcomes`。记录字段包括 signal_id、symbol、signal_time、horizon、entry_price、future_price、max_high_price、min_low_price、final_return_pct、max_gain_pct、max_drawdown_pct、result_label、data_status，以及 outcome 扫描时的决策快照。`signals.db`、`jobs.db` 和 `outcomes.db` 都是运行数据，不应提交到 Git。

常用命令：

```bash
cd /home/ubuntu/paopao-crypto-radar
.venv/bin/python main.py outcome-scan
.venv/bin/python main.py outcome-scan --backfill-days 7
.venv/bin/python main.py outcome-scan --dry-run
.venv/bin/python main.py outcome-scan --limit 50 --horizon 1h --symbol BTCUSDT
```

新增公开只读 API：

```text
/public-api/outcomes
/public-api/outcomes/stats
/public-api/symbol-outcomes
```

新增后台私有 API：

```text
/api/outcomes
/api/outcomes/stats
/api/symbol-outcomes
POST /api/outcomes/scan
```

`POST /api/outcomes/scan` 需要后台登录会话和 CSRF，会创建 `outcome-scan` 后台任务。公开 API 继续脱敏，不返回 payload_json、text_html、dedup_key、Telegram topic/message/reply、jobs、audit、config、logs、Cookie、Authorization 或 token/secret 字段。

结果追踪只用于复盘、统计和模型校准；它不会执行自动交易，不接交易所私有下单接口，不设置杠杆，不挂止盈止损，不操作真实资金，也不会改变 Telegram 推送节奏。

## v1.71.0 运维说明

v1.71.0 统一决策模型 API 契约。单币接口 `/public-api/decision?symbol=BTCUSDT` 和 `/api/decision?symbol=BTCUSDT` 都返回 `ok + data + _meta`，其中 `data` 包含 `model_version`、`symbol`、`decision`、`scores`、`reasons`、`risks`、`watch_points`、`factor_explanations`、`calibration` 和最近相关信号。为兼容旧前端，顶层旧字段仍保留。

新增统计接口:

```text
/public-api/decisions/stats
/api/decisions/stats
```

公开统计返回决策分布、风险分布、可试仓列表、风险/禁止追高列表和摘要；私有统计会额外返回模型权重、阈值和校准说明。未登录访问 `/api/decisions/stats` 仍应返回 `401 Unauthorized`。

模型校准版本为 `signal-decision-v1.1`。BTC、ETH、SOL、BNB、XRP 等高频币种不会仅因信号数量多就直接判为风险警报；风险警报必须有明确风险因子，例如资金费率拥挤、结算周期缩短、假突破、破位、失败/阻止信号增加等。高密度强信号但无明确风险因子时，模型会优先输出“等待回踩”或“禁止追高”。

公开前台“决策模型”和“币种详情”会展示决策分布、风险分布、模型解释、校准说明和组成因子。该模型仅用于信号整理和风险提示，不构成投资建议，不执行自动交易，也不会改动 Telegram 主推送流程。

## v1.70.3 运维说明

v1.70.3 为后台账号密码登录增加安全加固。默认同一用户名和来源 IP 连续输错 5 次后锁定 10 分钟，失败计数窗口为 900 秒；锁定期间即使密码正确也会返回 429，并在登录页显示“登录失败次数过多，请稍后再试”。

后台认证审计会记录登录成功、登录失败、登录锁定、退出登录、会话过期、会话无效和密码变更。审计文件为 `data/admin_auth_audit.json`，失败/锁定状态文件为 `data/admin_auth_state.json`，默认最多保留最近 500 条审计。审计不会记录明文密码、密码哈希、Cookie、session secret、旧访问令牌、完整 User-Agent 或完整 IP，只保存哈希和事件枚举。

后台登录后会显示当前用户、登录时间、会话到期和剩余时间；当会话剩余时间低于 TTL 的一半时，后台 API 会自动安全续期。写操作继续需要登录会话和 `X-CSRF-Token`，公开 `/public-api/*` 不受影响，仍然无需登录且保持脱敏。

相关配置项:

```bash
WEB_AUTH_MAX_FAILURES=5
WEB_AUTH_LOCKOUT_SEC=600
WEB_AUTH_FAILURE_WINDOW_SEC=900
WEB_AUTH_AUDIT_LIMIT=500
WEB_SESSION_REFRESH_THRESHOLD_RATIO=0.5
```

如果忘记后台密码，可在服务器本地重新设置:

```bash
cd /home/ubuntu/paopao-crypto-radar
.venv/bin/python main.py admin-password set
sudo systemctl restart paopao-web
```

## v1.70.2 运维说明

v1.70.2 调整后台账号密码设置命令：执行 `.venv/bin/python main.py admin-password set` 时，密码输入会明文显示，方便确认输入内容。请先确认当前终端环境安全。

系统仍然不会保存明文密码，只保存 `WEB_ADMIN_PASSWORD_HASH=pbkdf2_sha256$...`。如果需要隐藏输入，可以执行：

```bash
.venv/bin/python main.py admin-password set --hidden
```

## v1.70.1 运维说明

v1.70.1 将后台控制台改为自定义用户名 + 密码登录。后台入口仍是 `https://paoxx.com/admin`；公开前台 `https://paoxx.com/` 和 `/public-api/*` 不受影响，继续只读、脱敏、无需登录。首次设置或重置后台账号密码:

```bash
cd /home/ubuntu/paopao-crypto-radar
.venv/bin/python main.py admin-password set
sudo systemctl restart paopao-web
```

设置密码时终端会明文显示输入内容，便于确认；请确保当前终端环境安全。如需隐藏输入，可使用 `.venv/bin/python main.py admin-password set --hidden`。密码不会明文保存，只保存 `PBKDF2-HMAC-SHA256` 哈希；登录成功后写入签名会话 Cookie，包含 `HttpOnly`、`SameSite=Lax`，HTTPS 反代下会带 `Secure`。默认菜单和更新输出不会显示后台密码、密码哈希、会话密钥或旧访问令牌。

新增配置项包括 `WEB_AUTH_MODE=password`、`WEB_ADMIN_USERNAME`、`WEB_ADMIN_PASSWORD_HASH`、`WEB_SESSION_SECRET`、`WEB_SESSION_TTL_SEC` 和 `WEB_AUTH_COOKIE_NAME`。`WEB_ADMIN_TOKEN` 仅保留给显式设置 `WEB_AUTH_MODE=token` 的旧模式兼容或紧急回滚，不再作为默认登录方式。

## v1.70.0 运维说明

v1.70.0 新增 Signal Decision Model v1。它只读取 `signals.db` / `signal_events` 兼容视图，把现有信号整理成五类决策状态：`观察`、`等待回踩`、`可试仓`、`禁止追高`、`风险警报`。每个结果会说明置信度、风险等级、主要依据、风险提示、下一步观察点、组成因子分数和最近相关信号。

该模型仅用于信号整理和风险提示，不构成投资建议，不执行自动交易，不接交易所下单 API，不会自动下单、挂止盈止损或操作真实资金。本版也不改变 Telegram 推送主流程。

新增接口：私有后台 API `/api/decision`、`/api/decisions` 继续需要后台登录会话；公开只读 API `/public-api/decision`、`/public-api/decisions` 会脱敏输出，不返回 `payload_json`、`text_html`、`dedup_key`、Telegram topic/message/reply、jobs、audit、config、logs 或 token 类字段。公开前台新增“决策模型”区域，信号卡片和币种详情会显示当前决策、置信度、风险等级和观察点。

## v1.69.1 运维说明

公开前台用户界面已统一中文：标题为 `Paoxx 信号雷达`，筛选、信号卡片、详情弹窗、全市场时间线、币种详情、空状态和错误提示都使用中文；Paoxx 仅作为品牌名保留。公开页面仍只读取 `/public-api/*`，不会读取后台 `/api/*` 或显示配置、任务、日志、审计、服务控制、Telegram 私有字段。

生产入口显示已收口：`paopao update`、安装完成提示和中文菜单默认显示 `公开前台: https://paoxx.com/`、`后台控制台: https://paoxx.com/admin`。8080 是 Nginx 反代后端入口，不作为公网访问入口；生产环境应在云安全组关闭公网 8080。默认菜单和更新输出不再明文打印后台访问令牌；如需查看令牌，请在服务器本地使用专门菜单项并确认终端环境安全。

## v1.69.0 运维说明

公开前台 `https://paoxx.com/` 现在更适合日常查看信号：可以按币种、模块、状态、时间窗口和关键词筛选 Signal Card；点击卡片打开公开脱敏详情弹窗；点击币种进入单币详情视角；全市场时间线入口用于按时间顺序查看最近公开信号。

这些页面只读取 `/public-api/*`，不会读取后台 `/api/*`，不会显示任务、日志、审计、配置、服务控制或 Telegram 私有 topic/message/reply 数据。移动端会自动单列显示筛选栏、信号卡片、时间线和详情弹窗。服务器更新脚本的结束提示已改为正式 HTTPS 入口：公开前台 `https://paoxx.com/`，后台 `https://paoxx.com/admin`；8080 只作为本机/Nginx 反代后端入口。

## v1.68.1 运维说明

v1.68.1 修复 `scripts/check_https_deploy.sh` 的 HTTPS 验收误判。后台 `/admin` 页面检查现在使用普通 GET、`curl -L`、临时文件和固定字符串匹配，允许命中 `泡泡雷达控制台`、`brand-title` 或 `/admin` 即通过；失败时会显示 HTTP_CODE、下载字节数和页面前 8 行摘要。

证书路径 `/etc/letsencrypt/live/paoxx.com/fullchain.pem` 和 `privkey.pem` 可能需要 sudo 权限，普通用户无法读取不等于证书缺失。脚本会优先使用 `sudo test -f` 检查；如果路径权限导致失败，会再用 `certbot certificates --cert-name paoxx.com` 或 `certbot renew --dry-run` 的成功结果兜底，避免把权限问题误判为阻断。脚本不会读取或打印私钥内容。

## v1.68.0 运维说明

生产 HTTPS 入口已经固定为：公开前台 `https://paoxx.com/`，后台控制台 `https://paoxx.com/admin`，公开 API `https://paoxx.com/public-api/*`，私有 API `https://paoxx.com/api/*`。私有 API 必须有后台登录后的安全会话 Cookie，未授权访问应返回 `401 Unauthorized`。

服务器更新后建议执行固定验收脚本：

```bash
cd /home/ubuntu/paopao-crypto-radar || exit 1
bash scripts/check_https_deploy.sh
bash scripts/check_https_deploy.sh --with-stable-check
bash scripts/check_https_deploy.sh --with-certbot-dry-run
```

默认脚本不会执行 `certbot renew --dry-run`，只检查 `/etc/letsencrypt/renewal/paoxx.com.conf`、`/etc/letsencrypt/live/paoxx.com/fullchain.pem` 和 `privkey.pem` 是否存在；只有传入 `--with-certbot-dry-run` 才会执行证书续期 dry-run。生产稳定版验收使用 `.venv/bin/python main.py stable-check`，不要依赖全局 `python` 命令。

注意：`curl -I https://paoxx.com` 可能返回 `501 Unsupported method ('HEAD')`，这是 paopao-web 当前不支持 HEAD，不代表 HTTPS 异常。验收页面时请使用普通 GET，例如 `curl -sS https://paoxx.com/` 并检查页面内容包含 `Paoxx 信号雷达`。

8080 在本机监听不等于公网暴露。Nginx 会反代到本机 paopao-web，生产环境应在云安全组关闭公网 8080，只开放 80/443 作为正式入口；脚本发现本机 8080 监听时只提示确认云安全组，不直接判定阻断。

## v1.67.0 运维说明

Web 现在拆成公开前台和后台控制台两个入口：`/` 显示公开信号前台，不需要登录，只读取脱敏后的 `/public-api/*`；`/admin` 显示后台控制台，后台 `/api/*` 需要用户名 + 密码登录后的安全会话 Cookie。公开前台不会显示配置中心、任务中心、日志、审计、服务控制、更新入口、服务器资源或 Git 细节。

如果使用 `paoxx.com` 这类域名部署，建议 DNS A 记录指向服务器 IP，由 Nginx 负责 HTTPS 入口并反代到本机 `127.0.0.1:8080`：`/` 作为公开信号前台，`/admin` 作为后台控制台，`/public-api/*` 作为公开只读 API，`/api/*` 作为后台私有 API。生产环境建议关闭公网 8080，只开放 80/443，启用 HTTPS，并对 `/public-api/*` 增加 Nginx 限流。

示例反代结构如下，按实际证书路径和域名调整：

```nginx
server {
    listen 443 ssl http2;
    server_name paoxx.com;

    ssl_certificate /etc/letsencrypt/live/paoxx.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/paoxx.com/privkey.pem;

    location ^~ /public-api/ {
        limit_req zone=public_api burst=30 nodelay;
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    location ^~ /api/ {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    location ^~ /admin {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    location ^~ /_next/ {
        proxy_pass http://127.0.0.1:3000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

## v1.66.0 运维说明

Web 新增轻量「信号时间线」入口，并增强 Coin Detail 的按日期分组时间线。时间线数据来自 `signals.db` / `signal_events` 兼容视图，可以按币种、模块、状态、关键词和 24h / 7d / 30d 时间窗口查看信号历史；点击 Timeline item 可打开同一套 Signal Detail 面板。
时间线页面不访问外部行情 API，不触发行情扫描，也不会发送 Telegram 消息。如果时间线为空，先确认 `signals.db` 是否已有结构化推送记录，或放宽筛选条件。`signals.db`、`jobs.db`、`data/*.json`、日志、图表和生产 `.env.oi.bak*` 仍然是运行数据，不应提交到 Git。

## v1.65.0 运维说明

Web 新增「Coin Detail / 币种详情」页面，用于按单币种排查信号历史。你可以输入 `BTC` 或 `BTCUSDT`，查看最近 7 天该币种的信号数量、已发送/失败/阻止统计、活跃模块、按日期分组的时间线、最近 Telegram message_ids/topic_ids 和同币种最新信号。

这个页面只读取 `signals.db`，不会访问外部行情 API，也不会触发扫描或推送；适合排查“某个币近期是否多模块集中出现信号”“某条 Telegram 推送对应哪条结构化记录”。`signals.db`、`jobs.db`、`data/*.json`、日志和生产 `.env.oi.bak*` 仍然是运行数据，不应提交到 Git。

## v1.64.0 运维说明

Web「信号推送」页现在可以按币种、模块、状态、关键词、时间窗口和排序条件筛选信号。点击 Signal Card 可以查看该条推送的 Telegram topic/message、dedup_key、payload_json、同币种最近信号和原始摘要，适合日常排查“某条信号为什么发送/跳过/失败”。

Signal Card 和 Dashboard 最新信号都只读取 `signals.db`，不会触发行情扫描，也不会改变 Telegram 推送链路。`signals.db`、`jobs.db`、`data/*.json`、日志和图表仍然是运行数据，不应提交到 Git；生产环境里的 `.env.oi.bak*` 备份文件也不要提交或移动。

## v1.62.1 维护说明

v1.62.1 修正 Web 任务中心的 stable-check 状态口径。`stable-check` 返回码 1 只表示“基本可运行，建议关注”，通常是网络超时等可观察项；Web 会显示为“关注”，不会当作后台任务失败处理。成功的 update-check 等任务也不会把正常 Git fetch 输出当作错误摘要。

更新时间: 2026-05-25

这份说明用于第一次安装、重新安装、更新和排错。配置文件 `.env.oi` 不会提交到 GitHub，里面只放服务器自己的 token、群 ID 和 API key。

## 1. 第一次安装

在服务器执行:

```bash
cd ~
git clone https://github.com/ouoawp-ship-it/paopao-crypto-radar.git
cd paopao-crypto-radar
bash scripts/install_server.sh
```

安装脚本会进入中文向导，按顺序完成:

1. 显示安装目录和配置文件位置。
2. 安装 `git`、`python3`、`python3-venv`、`python3-pip`。
3. 创建 `.env.oi`。
4. 输入 Telegram bot token 和群 ID。
5. 默认启用 Telegram 话题自动分类，不手动填写话题 ID。
6. 可选输入 Coinalyze API key。
7. 创建 `.venv` 并安装 Python 依赖。
8. 运行编译检查和单元测试。
9. 生成 dry-run 启动观察历史。
10. 运行 readiness。
11. 安装并启动 systemd 服务。

## 2. 输入项说明

`TG_BOT_TOKEN`
: BotFather 给你的机器人 token，格式类似 `123456:ABC...`。

`TG_CHAT_ID`
: Telegram 群 ID，通常类似 `-1001234567890`，也可以是频道用户名 `@channel_username`。

`TG_TOPIC_ID` 以及其他 `TG_..._TOPIC_ID`
: 这是 Telegram 话题的数字 `message_thread_id`。默认不需要填，机器人有权限时会自动创建和记录话题。

`COINALYZE_API_KEY`
: Coinalyze API key。只在脚本提示 `COINALYZE_API_KEY 可选` 时填写。直接回车就是不启用历史清算辅助；填写后会作为结构雷达外部确认的历史清算辅助数据源。

## 3. 修改 token、群 ID、Coinalyze key

安装完成后，如果填错了 bot token、群 ID 或 Coinalyze key，不需要重新安装项目，推荐直接打开后台控制台的“配置”页修改:

```text
https://paoxx.com/admin
```

Web 控制台支持运行健康度、最近错误、日志搜索筛选、推送样例预览、GitHub 更新检查、分类配置页、真实模块开关、保存前预览改动、保存后中文结果提示、最近 `.env.oi` 备份一键恢复/删除，以及结构复盘参数建议一键应用。保存成功后会自动应用新配置；主服务和结构雷达会自动重启，改了 Web 端口或 Web 令牌时 Web 控制台会短暂重启，稍后刷新页面即可。v1.30.0 起，Web 接口异常会显示统一错误卡片，提供重试、诊断报告和日志中心入口；AI 助手/价格提醒页支持局部失败展示。v1.30.1 起，诊断报告会忽略正常 JSON 里的空 `errors: []` / `error: ""` 字段，减少误报。v1.30.2 起，`poll_timeout=5s` 这类字段名不会误报；复制报告/复制日志在 HTTP 访问下也会尝试 fallback。v1.30.3 起，Telegram 轮询超时会单独显示为“网络超时”，低频时不再作为日志错误。v1.31.0 起，Web 后台 UI 成品化：核心页面都有统一入口说明、状态标签、空状态和按钮说明，服务控制执行后会刷新当前页。v1.32.0 起，配置中心拆成 Telegram、AI Bot、价格提醒、主雷达参数、资金费率、结构雷达、行情源/外部接口、模块开关、Web 控制台和备份恢复，并给每个配置项标明做什么、影响什么、改完是否自动重启。v1.33.0 起，诊断报告升级为问题中心，会把健康异常、runtime 错误、失败审计、日志错误和网络超时整理成可处理的问题卡片。v1.38.0 起，诊断报告顶部新增“问题中心总览”，会先给出当前健康、需要关注或优先处理的结论。v1.39.0 起，诊断报告会展示“处理清单”，把问题映射到雷达服务、日志中心、审计记录、配置中心和检查测试入口。v1.39.1 起，Web 客户端断开连接导致的 `ConnectionResetError` 不再作为真实错误影响稳定版验收。v1.40.0 起，诊断报告顶部新增“长期运行就绪度”，会用当前稳定版验收、问题中心、验收历史、日志错误和审计记录给出完整稳定候选评分。v1.41.0 起，命令行稳定版验收也会输出同样的长期运行就绪度摘要。v1.42.0 起，验收历史会记录并展示长期运行就绪度分数和候选状态。v1.43.0 起，诊断报告会判断长期运行趋势和是否发生回退。v1.44.0 起，趋势变差或回退会进入问题中心、建议动作和处理清单。v1.45.0 起，处理清单可标记“已确认”或“已解决观察中”，并在复制报告里带上处理状态。v1.46.0 起，已标记问题会自动复查仍存在还是已消失待复查。v1.47.0 起，诊断报告会显示 v1.50.0 收口路线，并把功能冻结、问题复查和长期趋势纳入 stable-check 门禁。v1.48.0 起，诊断报告和 stable-check 会显示服务器部署验收。v1.49.0 起，功能说明和安装说明固定日常检查、更新、排错和回滚流程。v1.50.0 起，v1 主线进入长期维护，只做 bug 修复、策略微调、文档和运维补丁。

v1.34.0 起，AI Bot 和价格提醒稳定性继续收口：按钮回调用短超时静默确认，发送队列会自动重试临时失败，AI/查价/按钮异常会转成中文可读提示，价格提醒只有确认进入发送链路后才会标记触发。

v1.35.0 是早期稳定版自检基线。诊断报告新增“稳定版自检”，会按版本信息、后台服务、健康门禁、问题中心、日志稳定性、后台审计和关键配置判断当前部署是否适合长期运行。最终完整稳定版以 v1.50.0 为准。

v1.36.0 起，更新脚本会在安装、服务重启后自动执行稳定版验收。也可以手动运行 `python main.py stable-check` 查看同样的验收摘要。

v1.37.0 起，稳定版验收会自动保存最近一次完整快照和精简历史。Web 后台“诊断报告”里可以直接查看最近一次验收状态和历史记录。

v1.38.0 起，Web 后台“诊断报告”会把稳定版验收、健康检查、日志错误、网络超时、失败审计和问题列表聚合成“问题中心总览”，先告诉你是否需要动手，再给出下一步建议。

v1.39.0 起，Web 后台“诊断报告”会继续给出“处理清单”。处理清单不是普通说明文字，而是按问题类型生成的点击入口；Web“检查测试”页也新增“执行稳定版验收”，可直接保存验收历史。

v1.39.1 起，浏览器刷新、网络中断或公网探测导致的 Web `ConnectionResetError` 会被归类为可忽略事件；真正的 Web 程序异常仍会进入问题中心。

v1.40.0 起，如果你想判断当前服务器是不是已经可以当作“完整稳定版”长期运行，优先看 Web 后台“诊断报告”的“长期运行就绪度”。它会显示评分、完整稳定候选状态、阻断项、警告项和下一版本目标；显示“准稳定候选”时建议继续观察并再执行一次稳定版验收。

v1.41.0 起，服务器执行 `python main.py stable-check` 或 `paopao update --yes` 时，命令行也会直接打印“长期运行就绪度”。大白话就是：不打开网页，也能看到当前是完整稳定版候选、准稳定候选，还是需要先处理问题。

v1.42.0 起，稳定版验收历史会多保存长期运行就绪度。以后你看 Web 后台“诊断报告 -> 验收历史”，能直接看到每次更新后分数是变好了还是变差了。

v1.43.0 起，Web 后台“诊断报告”和 `stable-check` 会直接告诉你趋势是变好、持平、变差，还是从候选回退到需要处理。这样更新后不用自己对比两条历史分数。

v1.44.0 起，如果趋势变差或发生回退，Web 后台会把它当成需要处理的运维问题显示在问题中心里，并给出“查看趋势详情”的处理入口。

v1.45.0 起，Web 后台“诊断报告”的处理清单可以记录处理状态。每个问题都有稳定编号，可标记“已确认”或“已解决观察中”；记录保存在 `data/problem_state.json`，只用于排查复查，不影响雷达扫描和真实推送。

v1.46.0 起，Web 后台会自动复查这些处理状态。已解决的问题如果仍然出现在当前处理清单里，会显示“仍然存在”；如果当前已经消失，会显示“已消失待复查”。这只是运维提示，不会自动删除记录。

v1.47.0 起，项目进入 v1.50.0 完整稳定版收口路线。Web 后台“诊断报告”会显示 v1.47.0 功能冻结和稳定性收口、v1.48.0 服务器部署验收闭环、v1.49.0 文档说明和运维流程最终整理、v1.50.0 完整稳定版发布。收口期不新增大模块，只处理现有功能修复、稳定性、验收和文档。

v1.48.0 起，Web 后台“诊断报告”新增“服务器部署验收”。它会检查代码版本、后台服务、Web 入口、Telegram/AI 配置、stable-check、日志、审计和部署脚本。服务器执行 `paopao update --yes` 后，命令行 stable-check 也会显示这份摘要。

v1.49.0 起，Web 后台“功能说明”和这份安装说明进入最终运维收口。日常检查、更新、排错、配置回滚、源码异常处理和完整稳定版标准集中成固定流程，后续优先按这些流程操作。

v1.50.0 是 v1 完整稳定版最终发布。发布后 v1 主线进入长期维护：只做 bug 修复、策略微调、文档和运维补丁；新增大模块进入 v2 规划。判断服务器是否达标时，只看 Web“诊断报告”和 `stable-check`：长期运行就绪度为完整稳定版候选、服务器部署验收通过、问题中心无阻断、日志和审计干净，并保留稳定验收历史。

v1.50.1 是完整稳定版后的维护补丁。如果 `.env.oi` 里已经有 `WEB_ADMIN_TOKEN`，但进程环境里是空值，程序会以 `.env.oi` 的非空值为准，避免部署验收误判 Web 令牌未配置。遇到 Web 入口提示缺令牌时，优先执行 `bash scripts/update_server.sh --yes` 自动补齐并重启服务。

v1.50.2 修复服务器更新验收。如果 Web 控制台已经占用 8080，单元测试不会再读取真实 `.env.oi` 里的 Web 令牌后误连端口；部署验收也会优先按 `.env.oi` 里的 `WEB_HOST`、`WEB_PORT`、`WEB_ADMIN_TOKEN` 判断。

v1.50.3 修复部署验收快照。诊断报告会把 Web 配置写入 `config.web`，避免 `.env.oi` 已配置 `WEB_ADMIN_TOKEN` 但服务器部署验收仍显示未配置。

v1.50.4 优化诊断噪声。`coinpaprikaMarketCaps: ReadTimeout` 这类外部行情源偶发超时会归类为“网络超时/可自动重试”，不会再作为真实日志错误影响稳定版候选。

v1.51.0 开始 Web 后台 UI 工程化优化。这个版本不引入 Vue/React 构建链，也不改变后端核心逻辑；先在现有 Python Web 上借鉴 Naive Admin 的后台产品结构，统一侧栏、顶部栏、卡片、表格、状态标签和响应式布局，让后台更清楚、更稳定、更像完整管理产品。

v1.52.0 新增 Web「服务器状态」面板。后台会显示 CPU、系统负载、内存、Swap、磁盘空间、运行时间和主机信息，并用圆环、进度条和趋势图展示资源变化；顶部栏固定显示当前版本号和提交号，切换到其他页面也能一直看到。

v1.53.0 优化 Web UI 性能和实时监控。自动刷新改成按页面使用不同频率：服务器状态页每 3 秒刷新轻量接口，日志、总览、审计保持低频；刷新过程会防止接口叠加。CPU、内存、磁盘改成带指针的动态仪表盘，并在页面里用大白话说明这些指标代表什么、什么时候需要关注。
v1.53.1 修复诊断趋势误报。如果当前服务、日志、审计和部署验收都正常，单纯因为历史分数对比变差只作为观察提示，不再进入问题中心，也不会拉低长期运行就绪度评分。
v1.54.0 升级 Web 高级视觉质感。后台去掉偏塑料的浅灰大色块，改成钛灰、石墨、银色的金属磨砂层次；服务器状态里的 CPU、内存、磁盘仪表盘升级为带金属外圈、刻度、玻璃高光、中心轴和动态指针的设备式仪表。
v1.55.0 深化 Web Premium 暗色金属磨砂主题。后台全局切换为深灰/黑曜背景，用香槟金、银灰、古铜做点缀；按钮、表格、表单、状态标签、配置卡片、接口卡片和服务器仪表盘统一成深色毛玻璃与金属反光效果，减少浅色塑料感。
v1.55.1 按线上浏览器实测继续打磨 Web 观感。日志页筛选栏在桌面端不再纵向堆叠，移动端侧边栏改为紧凑横向导航；滚动条统一为暗色金属风格，拉丝纹理更轻，说明文字对比度更高。
v1.56.0 重排 Web 视觉方向。后台减少大面积金铜色和强纹理，改成黑曜、蓝黑、石墨主色；青蓝用于主要按钮和选中态，香槟金只保留少量提示点缀。侧边栏、顶部栏、卡片、表格、输入框和服务器仪表盘统一为更克制的专业运维后台风格。
v1.57.0 进行 Web UI Tabler 化重排。不引入外部 CDN、Vue 或 React，继续保留 Python 原生 Web；视觉改成浅灰工作区、白色卡片、深色侧栏、蓝色主操作、轻边框和低阴影，更接近成熟开源运维后台模板。
v1.57.1 修复服务器状态仪表盘指针遮挡百分比数字的问题。中心读数层级提高，指针保留在读数下方，CPU、内存、磁盘百分比更容易看清。
v1.58.0 进行 Web UI 极简收口。侧栏去掉菜单小字，顶部栏压缩，页面说明改为短说明和折叠详情；总览页默认只展示核心服务和错误入口，运行摘要、配置摘要默认折叠；服务器仪表盘改为更紧凑的资源卡。
v1.58.1 删除页面说明里的重复“完整说明”展开项，只保留一条短说明；真正有用的运行摘要、关键配置和高级排查仍保留折叠入口。
v1.59.0 重做 Web 后台整体布局。电脑浏览器保持左侧导航，不再过早切成顶部横向菜单；总览页新增 Telegram、Binance、CoinPaprika、Coinalyze、CoinMarketCap 平台 logo 条，外部接口来源更直观。

服务器命令行仍保留应急配置向导:

```bash
cd ~/paopao-crypto-radar
bash scripts/install_server.sh config
```

应急配置向导会提供这些功能:

```text
1. 修改 TG_BOT_TOKEN
2. 修改 TG_CHAT_ID / 群 ID
3. 修改 COINALYZE_API_KEY
4. 修改 Telegram 话题配置
5. Telegram / Coinalyze 全部重新填写
6. 清理旧 Telegram 话题路由
0. 保存并退出
```

如果修改了 `TG_CHAT_ID`，脚本会自动删除 `data/tg_topic_routes.json`。这是必要的，因为旧群的话题 ID 不能继续用于新群。服务重启后，bot 会按新群重新自动创建话题。

如果修改了 `TG_BOT_TOKEN`，建议确认新 bot 已经加入目标群，并且具备发送消息、管理话题、置顶消息权限。

`COINALYZE_API_KEY` 是可选清算历史辅助，直接回车表示关闭 Coinalyze；结构雷达仍会使用 Binance 免费盘口深度做外部确认。

如果走服务器命令行的应急配置向导，修改完成后向导会提示是否立即重启服务。也可以手动重启:

```bash
sudo systemctl restart paopao-radar
```

## 4. 快捷操作命令

安装脚本会自动写入 `/usr/local/bin/paopao`。以后在服务器任意目录输入:

```bash
paopao
```

会打开中文数字菜单。服务器日常只需要记住这一个入口命令。

菜单会显示:

```text
1. 查看正式访问入口
2. 设置后台账号密码
3. 查看 Web 控制台服务状态
4. 查看 Web 控制台实时日志
5. 重启 Web 控制台服务
6. 检查 GitHub 是否有更新
7. 更新项目代码
8. 查看当前版本
0. 退出
```

菜单顶部会显示正式入口、后台登录配置状态、项目版本，以及哪些功能应该去 Web 页面操作；默认不会明文打印后台密码、密码哈希、会话密钥或旧访问令牌。配置修改、服务启停、日志查看、测试消息、readiness、doctor、cleanup、结构复盘等控制功能已经移到 Web 控制台。

Web 控制台会作为 `paopao-web.service` 安装并启动，生产环境浏览器访问:

```text
公开前台: https://paoxx.com/
后台控制台: https://paoxx.com/admin
```

8080 仅作为本机/Nginx 反代后端入口，不作为公网访问入口。后台页面会要求输入用户名和密码。首次设置或重置后台账号密码:

```bash
cd /home/ubuntu/paopao-crypto-radar
.venv/bin/python main.py admin-password set
sudo systemctl restart paopao-web
```

设置密码时终端会明文显示输入内容，便于确认；请确保当前终端环境安全。如需隐藏输入，可使用 `.venv/bin/python main.py admin-password set --hidden`。

相关配置项:

```bash
WEB_HOST=0.0.0.0
WEB_PORT=8080
WEB_AUTH_MODE=password
WEB_ADMIN_USERNAME=admin
WEB_ADMIN_PASSWORD_HASH=
WEB_SESSION_SECRET=
WEB_SESSION_TTL_SEC=86400
WEB_AUTH_COOKIE_NAME=paopao_admin_session
```

如果 `WEB_ADMIN_PASSWORD_HASH` 或 `WEB_SESSION_SECRET` 尚未配置，后台登录页会提示先执行设置命令。`WEB_ADMIN_TOKEN` 仅保留给显式设置 `WEB_AUTH_MODE=token` 的旧模式兼容或紧急回滚，不再作为默认登录方式。

如果是从旧版本更新上来，想只安装快捷命令:

```bash
cd ~/paopao-crypto-radar
bash scripts/install_server.sh shortcut
```

### AI 助手 Bot 和价格提醒

v1.13.0 新增 `paopao-ai.service`。它使用独立的 `AI_BOT_TOKEN`，和群里推送雷达信号的 `TG_BOT_TOKEN` 分开：

```text
TG_BOT_TOKEN = 群话题推送雷达信号
AI_BOT_TOKEN = 私聊 AI 助手、手动价格提醒、个人提醒
```

推荐在 Web 控制台的「配置 -> AI 助手」里填写：

```bash
AI_ASSISTANT_ENABLE=true
AI_BOT_TOKEN=
AI_ADMIN_USER_IDS=你的Telegram用户ID
AI_PRICE_ALERTS_ENABLE=true
AI_ALERT_CHECK_INTERVAL_SEC=10
```

默认建议只用私聊。如果开启群内调用，需要同时配置：

```bash
AI_ALLOW_GROUP_CHAT=true
AI_ALLOWED_CHAT_IDS=-1001234567890,-1009876543210
```

`AI_ALLOWED_CHAT_IDS` 支持多个群/频道 ID，用英文逗号分隔，也可以填 `@channel_username`。群里即使开通了白名单，也不会读取一句话就回复，只有别人 `@机器人用户名` 或回复机器人消息时才会处理。

价格提醒不需要 AI API Key。打开 AI 助手 Bot 私聊，点击「设置价格提醒」，可选择目标价提醒、价格急涨急跌、持仓量变化、资金费率变化；支持 Binance、Bybit、OKX、Bitget、Gate 的现货或 USDT 合约价格源，并可选择提醒一次、重复提醒或持续每5分钟提醒。v1.25.0 起，Web 后台的价格提醒页支持按状态、类型和关键词筛选提醒，日志中心会显示筛选摘要和第一条错误。v1.26.0 起，Web 接口统一返回 `_meta` 元信息，页面操作结果会显示 HTTP 状态、接口路径、服务端时间和浏览器耗时，检查测试页也提供 Web API 自诊断入口。v1.27.0 起，Web 后台新增审计记录页，配置保存、备份恢复/删除、检查测试、服务启停、价格提醒管理和 AI 提示词操作都会记录操作摘要，不保存 Token、API Key 或提示词正文。v1.28.0 起，Web 后台新增诊断报告页，可一键复制安全运维快照，汇总服务状态、最近错误、失败审计和日志错误片段。v1.29.0 起，配置页保存前会显示影响预检，包括影响模块、自动重启服务、敏感/危险配置提醒和回滚说明。v1.30.0 起，前端错误边界会把接口失败显示成可读错误卡片，单个接口失败不会拖空 AI 助手和价格提醒整页。v1.30.1 起，AI 巡检成功日志里的空 errors 字段不会再被统计成日志错误。v1.30.2 起，诊断页复制按钮会在浏览器拒绝剪贴板时自动选中文本并提示手动复制。v1.30.3 起，少量 `getUpdates ReadTimeout` 会归类为 Telegram 网络抖动，服务会自动重试。v1.31.0 起，Web 页面结构和视觉进一步统一，空结果、局部失败和服务操作说明更清楚。v1.32.0 起，配置中心每个输入项都会说明用途、影响范围和生效方式，减少误改配置。v1.33.0 起，诊断报告会优先展示问题中心和建议动作，并能跳到相关日志或失败审计。v1.40.0 起，诊断报告还会给出长期运行就绪度评分，便于判断是否已经达到完整稳定候选。v1.41.0 起，`stable-check` 命令行也会显示这份就绪度评分。v1.42.0 起，验收历史也会保存这份评分。v1.43.0 起，系统会基于这份历史判断趋势变化。v1.44.0 起，趋势异常会进入问题中心。v1.45.0 起，问题处理状态也会进入诊断报告。v1.46.0 起，问题处理状态会自动复查。v1.47.0 起，收口路线也会进入诊断报告和验收历史。v1.48.0 起，部署验收也会进入诊断报告、命令行和验收历史。

v1.34.0 起，AI Bot 的用户侧错误提示更直白：临时网络超时会显示中文提示，DeepSeek 这类 AI 接口返回的 400/401/429 正文仍会保留，方便排查模型名、Key 或额度问题。

v1.35.0 起，如果你想判断服务器是不是已经进入“能长期放着跑”的状态，优先打开 Web 后台的“诊断报告”，看“稳定版自检”是否显示“达到稳定版标准”。

v1.36.0 起，`paopao update --yes` 更新结束时会自动输出稳定版自检结果；如果显示“有警告”或“未达标”，再打开 Web 后台“诊断报告”按建议处理。

v1.37.0 起，`stable-check` 默认会把结果写入 `data/stable_check_latest.json` 和 `data/stable_check_history.json`；临时查看不想保存时可执行 `python main.py stable-check --no-save`。v1.38.0 起，Web“诊断报告”会优先展示“问题中心总览”，比单独看日志片段更容易判断是否需要处理。v1.39.0 起，诊断页会给出可点击处理清单；处理完后可在 Web“检查测试”页执行稳定版验收。

v1.49.0 起，推荐固定按下面的运维流程走:

1. 日常检查：先打开 Web“总览”，再打开“诊断报告”。重点看长期运行就绪度、服务器部署验收、问题中心和处理清单。
2. 更新：服务器执行 `paopao update --yes`。脚本会同步配置、安装依赖、运行测试、清理运行垃圾、刷新后台服务并执行 stable-check。
3. 排错：不要先翻大段日志。先看诊断报告处理清单；服务问题进“雷达服务”，日志问题进“日志中心”，失败操作进“审计记录”，配置问题进“配置中心”。
4. 配置回滚：配置改错优先到 Web“配置中心 -> 备份恢复”，恢复最近 `.env.oi` 备份。
5. 源码异常：先复制诊断报告和 stable-check 输出，确认是代码、配置还是服务器环境问题；需要回到上一个稳定源码时，按 GitHub 上一个稳定提交处理，不直接删除服务器目录。
6. 完整标准：长期运行就绪度为“完整稳定版候选”、服务器部署验收通过、问题中心无阻断、近期日志和审计干净，并至少保留两次达标验收历史。

```text
BTC 现在多少钱
查 BTC
GWEI 怎么看
SOL 可以做多吗
我的提醒有哪些
暂停提醒 12
恢复提醒 12
删除提醒 12
分析这段：粘贴雷达信号或市场数据
直接粘贴启动雷达/结构雷达/资金流数据，机器人会自动按分析处理
```

私聊发送 `/start` 会打开中文按钮首页。v1.19.0 起，首页里的「设置价格提醒」会按固定步骤执行：选择监控类型 -> 输入币种 -> 识别可用现货/合约 -> 手动选择交易所 -> 按类型选择目标价或窗口/阈值/方向 -> 选择触发方式 -> 确认添加。只有点击「确认添加提醒」才会真正创建提醒。v1.21.3 起，AI Bot 只保留 `/start`，其它斜杠入口全部取消。查价格直接发送 `BTC`，看行情直接发送 `BTC 怎么看`，粘贴雷达/市场数据会自动进入专业分析，提醒管理点击首页「我的提醒」。v1.21.4 起，「我的提醒」显示的是当前列表序号，不再暴露数据库真实 ID。

自然语言不再创建价格提醒。你说“BTC 跌破 58000 提醒我”时，机器人会提示去点击「设置价格提醒」走手动选择流程；只转发雷达信号会自动走数据分析，不会乱建个人提醒。

如果要启用真正 AI 问答，再配置：

```bash
AI_PROVIDER_ENABLE=true
AI_API_KEY=
AI_BASE_URL=https://api.deepseek.com
AI_MODEL=deepseek-v4-pro
AI_REQUEST_TIMEOUT_SEC=90
AI_PROMPTS_FILE=ai_prompts.json
SIGNAL_EVENTS_FILE=signal_events.json
SIGNAL_EVENTS_DB_FILE=signals.db
SIGNAL_EVENTS_LIMIT=5000
SIGNAL_EVENTS_RETENTION_DAYS=60
```

`AI_MODEL` 只填写模型名本身，比如 `deepseek-v4-pro`，不要填写成 `AI_MODEL=deepseek-v4-pro`。使用 `deepseek-v4-pro` 或 `deepseek-v4-flash` 时，请求会自动按 DeepSeek v4 接口带上思考模式参数；如果接口返回 400，Web 和 AI Bot 会显示服务端返回的具体错误正文。`deepseek-v4-pro` 思考模式响应较慢，超时时可在 Web 后台把 `AI_REQUEST_TIMEOUT_SEC` 调到 120-180，或者临时改用 `deepseek-v4-flash`。

v1.16.0 起，AI Bot 支持自然语言查询币种雷达档案：例如“查 BTC”“GWEI 怎么看”“SOL 可以做多吗”。它会读取 `data/signal_events.json`、推送历史、启动雷达历史、结构复盘和资金费率状态，再结合当前 Binance 行情、OI、成交量、市值、流动性、结构和多交易所资金费率，输出偏多/偏空/观望/高风险观望。v1.19.0 起，AI Bot 首次打开或发送 `/start` 会显示按钮首页，价格提醒走多类型手动监控流程；v1.20.0 起，价格提醒扫描不再阻塞用户聊天和按钮处理，五大交易所价格源识别会并发执行并短时间缓存。v1.21.1 起，首页不再显示 AI 对话按钮，直接发消息即可自动分流到泡泡 AI 助手或专业分析师模式。v1.21.2 起，慢任务临时提示会在最终回复成功后自动删除。v1.21.3 起，只保留 `/start`，其它 Bot 功能全部去命令化。v1.21.4 起，提醒编号用当前列表序号，交易所/交易对字段按 Telegram HTML 优化展示。v1.21.5 起，按钮回调先静默 ACK，不再对每次按钮点击弹出“处理中...”。v1.22.0 起，AI Bot 热路径复用 Settings 与精确报价短缓存，并通过 `ai-assistant: slow_callback` / `slow_message` 日志定位慢请求。v1.22.1 起，统一意图分类器会先判断分析/市场数据，再判断查价，避免“当前价格”等字段误触发查价。v1.23.0 起，Web 后台菜单改为总览、AI 助手、价格提醒、雷达服务、配置中心、日志中心、检查测试、更新备份、功能说明。v1.24.0 起，总览和日志中心可 15 秒自动刷新，最近错误可一键跳转对应日志。v1.25.0 起，价格提醒页支持状态/类型/关键词筛选，日志中心会显示筛选摘要和第一条错误。v1.26.0 起，Web API 自带元信息和耗时显示，检查测试页可以一键做 Web API 自诊断。v1.27.0 起，Web 审计记录页可以查看后台关键操作流水，并按成功/失败和关键词筛选。v1.28.0 起，Web 诊断报告页可以复制安全运维快照，方便排查 bug。v1.29.0 起，Web 配置保存前会预检影响模块和自动重启服务。v1.30.0 起，Web API 契约和错误边界被测试固定，页面失败会显示重试和诊断入口。v1.30.1 起，诊断日志统计会过滤空错误字段误报。v1.30.2 起，诊断复制按钮支持 HTTP fallback，并继续减少日志误报。v1.30.3 起，Telegram 轮询超时会按可自动重试网络超时单独统计。v1.31.0 起，Web 后台核心页面统一入口说明、标签摘要、空状态和服务操作刷新逻辑。v1.32.0 起，配置中心继续工程化拆分，并把字段级说明写进页面。v1.33.0 起，诊断中心会生成 `issues` 问题列表，按严重程度和模块聚合排查入口。`SIGNAL_EVENTS_FILE` 继续给 AI 币种档案读取旧 JSON 索引；`SIGNAL_EVENTS_DB_FILE` 是 Web「信号推送」页使用的结构化 SQLite 记录，通常保持默认即可。v1.60.1 增加 `signals.db` 兼容视图 `signal_events`，实际写入表仍为 `signals`，旧验收 SQL 和人工排查可以查询 `signal_events`。v1.61.0 起新增 `data/jobs.db` 后台任务库，稳定版验收、doctor、readiness、cleanup、更新检查和 Web API 自检进入 Web「任务中心」异步执行；任务详情只保存脱敏后的 stdout/stderr tail。真正更新代码仍建议在服务器执行 `paopao update --yes`，避免 Web 自更新时重启自身。

Web 控制台的「AI 助手」页提供「编辑 AI 提示词」入口，可以编辑泡泡 AI 助手提示词和专业分析师提示词。泡泡 AI 助手用于日常问答、生活问题、状态解释和提醒说明，默认语气更轻松；专业分析师用于 `分析这段：...`、`帮我分析...` 以及自动识别出的雷达/市场数据。提示词默认保存在 `data/ai_prompts.json`，保存后会自动重启 `paopao-ai`。

没配置 `AI_BOT_TOKEN` 时，`paopao-ai.service` 会保持等待状态，不影响主雷达推送。

## 5. 版本号规则

项目根目录有一个 `VERSION` 文件，用来记录用户可读的版本号。当前为 `v1.62.1`，这是 v1 完整稳定版后的任务中心状态口径维护版本。

v1.62.0 起，Web「任务中心」会显示任务统计、失败摘要、复制任务报告、重跑任务、清理旧任务和同类型长任务并发保护。`failed` / `timeout` 任务会进入诊断报告和问题中心，排查时先打开任务中心看错误摘要、`stdout_tail`、`stderr_tail`，再去日志中心按时间点追原始日志。v1.62.1 起，`stable-check` 返回码 1 会显示为“关注”而不是失败，用于网络超时等非阻断观察项。`update-check` 只做结构化检查和展示当前版本/远端版本/建议动作，实际更新仍在服务器执行 `paopao update --yes`，避免 Web 自更新时重启自身导致任务中断。`data/jobs.db` 是运行数据，不应提交；生产服务器上的 `.env.oi.bak*` 备份文件也不要提交。

中文菜单里的“检查 GitHub 是否有更新”和“更新项目代码”会同时显示:

- 当前版本号
- GitHub 最新版本号
- 当前 git 提交号
- GitHub 最新 git 提交号

例如:

```text
当前版本 : v1 (d5a72c3)  Add interactive update check shortcut
GitHub版本: v1.5 (xxxxxxx)  Add xxx feature
```

以后如果只是小修复，也会保留 git 提交号作为精确定位；如果是功能变化，会同步升级 `VERSION`。

## 6. 更新时 `.env.oi` 的安全同步

中文菜单里的 `6. 更新项目代码` 会自动运行 `.env.oi` 安全同步:

- 会补充 `.env.oi.example` 里新增的普通配置项。
- 会自动升级明确写进迁移白名单的默认参数，例如资金摘要频率这类项目级默认值。
- 不会覆盖 `TG_BOT_TOKEN`、`TG_CHAT_ID`、`COINALYZE_API_KEY`、`TG_TOPIC_ID`、各类话题 ID。
- 如果你自己把某个参数改成了自定义值，脚本会尽量保留，不会用新默认值强行覆盖。

所以后续我优化配置参数后，你通常直接执行:

```bash
paopao
```

然后选择 `6. 更新项目代码`，即可完成代码更新、依赖检查、测试、`.env.oi` 安全同步和服务重启。

## 7. Telegram 话题推荐设置

推荐默认配置:

```bash
TELEGRAM_USE_TOPIC=true
TG_AUTO_CREATE_TOPICS=true
TG_TOPIC_INTRO_ENABLE=true
TG_TOPIC_INTRO_PIN=true
```

bot 需要在群里具备这些权限:

- 发送消息
- 管理话题
- 置顶消息

每个话题第一次真实推送前，项目会先发送一条中文说明消息，并尝试置顶。说明消息会解释这个话题推什么、怎么看信号。

启动预警话题里，同一币种如果先出现预警、后续又出现更高阶段信号，新消息会自动回复上一条该币启动消息，方便在 Telegram 里按一条回复链追踪。

## 8. 重新安装

如果你想完全重新安装，并备份旧目录:

```bash
cd ~
pkill -f "main.py daemon" || true

if [ -d paopao-crypto-radar ]; then
  mv paopao-crypto-radar "paopao-crypto-radar-old-$(date +%Y%m%d-%H%M%S)"
fi

git clone https://github.com/ouoawp-ship-it/paopao-crypto-radar.git
cd paopao-crypto-radar
bash scripts/install_server.sh
```

如果你想保留旧 `.env.oi`，先备份:

```bash
cp ~/paopao-crypto-radar/.env.oi /tmp/paopao.env.oi.backup
```

新项目 clone 完之后再恢复:

```bash
cp /tmp/paopao.env.oi.backup ~/paopao-crypto-radar/.env.oi
cd ~/paopao-crypto-radar
bash scripts/install_server.sh
```

## 9. 更新现有项目

```bash
cd ~/paopao-crypto-radar
bash scripts/update_server.sh
```

也可以用中文菜单:

```bash
paopao
```

进入菜单后选择 `5. 检查 GitHub 是否有更新` 或 `6. 更新项目代码`。

更新脚本会执行:

- `git fetch` 检查 GitHub 最新版本
- 显示当前版本和 GitHub 版本
- 有更新时询问是否更新
- `git pull --ff-only`
- 安全同步 `.env.oi`，保留 token、群 ID、key 和话题 ID
- 安装/刷新依赖
- 编译检查
- 单元测试
- 自动清理 pycache、临时文件、过期日志、过期结构图和根目录临时报告
- 安装/刷新 `paopao-radar.service` 主服务、`paopao-structure.service` 结构雷达独立服务、`paopao-web.service` Web 控制台服务和 `paopao-ai.service` AI 助手服务
- 即使当前代码已经是最新版，也会刷新快捷命令、补装 `paopao-structure.service`、`paopao-web.service`、`paopao-ai.service` 和 `paopao-cleanup.timer`，并重启已安装服务

结构雷达独立服务由 `paopao-structure.service` 管理，专门运行 `structure-loop`，用于每小时 55 分提前临界扫描和整点后 5 分收线确认。服务状态、日志和重启操作统一在 Web 控制台完成。

自动清理由 `paopao-cleanup.timer` 管理，默认每小时执行一次 `python main.py cleanup --force-cleanup`。手动立即清理可以在 Web 控制台执行。

查看自动清理 timer:

```bash
systemctl list-timers paopao-cleanup.timer
journalctl -u paopao-cleanup.service -n 80 --no-pager
```

## 10. 常用检查命令

```bash
cd ~/paopao-crypto-radar
. .venv/bin/activate

python main.py status
python main.py readiness
python main.py telegram-test --send --confirm-real-send
python main.py announcements-test
python main.py funding-alert
python main.py runtime-status
```

查看服务:

```bash
sudo systemctl status paopao-radar
journalctl -u paopao-radar -f
```

## 11. 手动启动方式

如果你不想用 systemd，也可以手动后台运行:

```bash
cd ~/paopao-crypto-radar
. .venv/bin/activate
pkill -f "main.py daemon" || true
nohup .venv/bin/python -u main.py daemon --send --confirm-real-send > data/runtime.log 2>&1 &
tail -f data/runtime.log
```

## 12. 闭合窗口与推送时间

涉及 OI、CVD、K 线涨跌的雷达默认不在刚整点立刻读取数据，而是等待数据源收线完成后再统计上一完整窗口。资金流雷达使用 Binance 免费公开数据，CVD 由 K 线主动买入成交额估算:

```bash
RADAR_SUMMARY_MIN_INTERVAL_SEC=21600   # 资金摘要 6 小时窗口
RADAR_SUMMARY_CLOSE_DELAY_SEC=300      # 收线后延迟 5 分钟
FLOW_INTERVAL_SEC=3600                 # 资金流 1 小时窗口
FLOW_CLOSE_DELAY_SEC=300               # 收线后延迟 5 分钟
FUNDING_ALERT_ENABLE=true              # 启用独立资金费率警报话题
FUNDING_ALERT_INTERVAL_SEC=180         # 资金费率警报默认 3 分钟扫描一次
FUNDING_ALERT_SCAN_LIMIT=120           # 按 Binance 成交额扫描前 N 个 USDT 合约
FUNDING_SCAN_CONCURRENCY=8             # 资金费率请求有界并发（建议 6-8）
FUNDING_REQUEST_TIMEOUT_SEC=8          # 单交易所请求超时秒数
FUNDING_MAX_SYMBOLS_PER_BATCH=120      # 单批最多扫描的币种数量
FUNDING_ALERT_EXCHANGES=BINANCE,OKX,BYBIT,BITGET,GATE
FUNDING_ALERT_EXTREME_NEGATIVE_PCT=-0.5 # 极负费率阈值
FUNDING_ALERT_SUPER_NEGATIVE_PCT=-1.0  # 超极负费率阈值
FUNDING_ALERT_EXTREME_POSITIVE_PCT=0.5 # 极正费率阈值
FUNDING_ALERT_MIN_EXCHANGE_COUNT=2     # 多交易所共振最少交易所数量
FUNDING_ALERT_DIVERGENCE_PCT=0.75      # 交易所之间费率偏离阈值
FUNDING_ALERT_REPLY_CHAIN_ENABLE=true  # 同币后续资金费率警报回复上一条
FUNDING_ALERT_DECAY_QUIET_SCANS=2      # 连续安静几轮后提示热度衰减
FUNDING_ALERT_END_QUIET_SCANS=5        # 连续安静几轮后标记观察结束
LAUNCH_CLOSE_DELAY_SEC=60              # 启动雷达 15m 收线后延迟 1 分钟
STRUCTURE_PRE_SCAN_MINUTE=55           # 结构突破雷达每小时提前临界扫描
STRUCTURE_CONFIRM_DELAY_SEC=300        # 结构突破雷达收线后延迟 5 分钟确认
STRUCTURE_MIN_SCORE=65                 # 结构雷达最低推送分，复盘提示假突破偏高时可提高
STRUCTURE_SEND_CHART_TOP_N=3           # 每轮最多给前 N 个结构信号发送 K 线图，信号太多时可降低
STRUCTURE_DELETE_CHART_AFTER_SEND=true # 真实图片推送成功后立即删除本地 PNG
STRUCTURE_CHART_RETENTION_HOURS=12     # dry-run/失败图片最多保留 12 小时
STRUCTURE_MAX_CHART_FILES=200          # 超过 200 张时只保留最新图片
STRUCTURE_REPLY_CHAIN_ENABLE=true      # 同币结构信号回复上一条结构消息
STRUCTURE_REVIEW_ENABLE=true           # 启用结构信号复盘统计
STRUCTURE_REVIEW_LOOKBACK_HOURS=24     # 默认复盘过去 24 小时信号
STRUCTURE_REVIEW_FORWARD_HOURS=4       # 最多跟踪信号后 4 小时
STRUCTURE_REVIEW_MIN_AGE_MINUTES=15    # 信号至少等待 15 分钟后复盘
STRUCTURE_REVIEW_MAX_REPORT_INTERVAL_SEC=3600 # 复盘报告真实推送最小间隔
LIQUIDITY_FALLBACK_ENABLE=true         # 启用结构雷达免费流动性辅助
LIQUIDITY_SCORE_MAX_DELTA=15           # 分数修正上限，避免压倒结构原始评分
LIQUIDITY_MIN_DISTANCE_PCT=0.5         # 买墙/卖墙距离现价至少 0.5%
LIQUIDITY_MAX_DISTANCE_PCT=8.0         # 买墙/卖墙距离现价最多 8%
BINANCE_ORDERBOOK_LIQUIDITY_ENABLE=true # 使用 Binance 免费盘口深度估算买墙/卖墙
BINANCE_ORDERBOOK_DEPTH_LIMIT=100      # 每个币读取的 Binance 盘口档位
COINALYZE_ENABLE=false                 # 可选：开启 Coinalyze 免费 Key 的清算历史辅助
COINALYZE_API_KEY=                     # 可选：Coinalyze 免费 API Key
ANNOUNCEMENT_PAGE_SIZE=50              # Binance 公告单页数量，公告测试会分页抓取多个分类
```

结构外部确认默认使用 Binance 免费合约深度快照估算上方卖墙/下方买墙；清算侧可选使用 Coinalyze 历史清算量做方向辅助，但它不是预测清算池，推送里会标明数据源。

结构雷达推送中的外部确认状态会显示完整中文：清算磁吸说明清算池方向，盘口流动性说明买墙/卖墙是否明显，流动性缺口说明哪一侧阻力或支撑更薄。Binance 免费盘口快照不是盘口热力图，只能看当前订单簿；如果挂单不集中、距离超出配置范围，或深度档位内没有明显墙，就会显示“暂无有效买墙/卖墙”。

如果修改这些参数，推荐使用 Web 控制台的“配置”页；保存成功后会自动应用新配置，不需要再手动重启。更新项目时脚本会保留 token、群 ID、Coinalyze key 和话题 ID。

结构突破雷达 v1.8 的单次 dry-run：

```bash
python main.py structure-radar --mode pre --save-charts
python main.py structure-radar --mode confirm --save-charts
python main.py structure-review
python main.py announcements-test
python main.py funding-alert
```

独立循环：

```bash
python main.py structure-loop
```

## 13. 排错

如果提示 `TG_BOT_TOKEN 缺失或格式无效`:

```bash
nano .env.oi
```

检查:

```bash
TG_BOT_TOKEN=你的bot_token
TG_CHAT_ID=你的群ID
```

如果把非数字内容错填到了 `TG_TOPIC_ID`，重新运行安装脚本即可。新脚本会检测到非数字话题 ID，并自动清空。

如果 Telegram 话题无法置顶，通常是 bot 缺少置顶消息或管理话题权限。推送本身不会因此停止。
## v1.63.0 Web Platform API Core

v1.63.0 adds `/api/dashboard` as a lightweight aggregation API. It only reads current service status, signals.db, jobs.db, resources, and update-check state; it does not trigger market scans or external update apply. The `api-self-test` background job now checks the Web API contract directly. `jobs.db` and `signals.db` remain runtime data and must not be committed. Production updates should still be run from the server with `paopao update --yes`.
## v1.75.1 运维说明

v1.75.1 用于降低首页 public API 偶发慢请求造成的提示噪声。部署后 `paopao-frontend.service` 应包含：

```ini
Environment=PAOXX_PUBLIC_API_INTERNAL_BASE=http://127.0.0.1:8080
Environment=PAOXX_PUBLIC_API_TIMEOUT_MS=15000
```

首页只有在核心公开数据全部不可用时才显示“公开数据暂时不可用”。如果只是某个统计接口慢或短时超时，页面会继续展示已拿到的真实数据，不再显示“部分数据暂时不可用”。部署命令仍为：

```bash
paopao update --yes || bash scripts/update_server.sh --yes
bash scripts/check_https_deploy.sh --with-stable-check
```

## v1.75.0 运维说明

v1.75.0 升级 Next.js 公开前台的数据接入。部署后 `paopao-frontend.service` 会带上：

```ini
Environment=PAOXX_PUBLIC_API_INTERNAL_BASE=http://127.0.0.1:8080
```

该变量只供 Next.js 服务端渲染读取本机 Python 后端的 `/public-api/*`。浏览器访问 `https://paoxx.com/` 时仍通过 Nginx 同域访问 `/public-api/*`，不会调用 `/api/*`，也不需要后台登录。

部署命令保持不变：

```bash
cd /home/ubuntu/paopao-crypto-radar
paopao update --yes || bash scripts/update_server.sh --yes
bash scripts/check_https_deploy.sh --with-stable-check
```

验收时建议确认：

```bash
systemctl cat paopao-frontend | grep PAOXX_PUBLIC_API_INTERNAL_BASE
curl -s https://paoxx.com/ | grep -E "Paoxx 信号雷达|最新信号卡片|决策分布|结果追踪|决策回测"
curl -s "https://paoxx.com/public-api/signals?limit=1" | head -c 300
curl -s -i "https://paoxx.com/api/backtest/decision" | head -n 8
```

公开前台所有用户可见状态为中文；如果 public API 暂时失败，会显示“数据暂时不可用，请稍后重试”，不会暴露后台配置、Token、Cookie、日志或审计字段。本版本不改 Telegram 主推送流程，不引入自动交易，不改数据库 schema，也不改后端 API contract。
