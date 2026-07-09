# 泡泡抓币 Crypto Radar

## v1.76.0 说明

v1.76.0 新增 Binance-Centric Signal Lifecycle Tracker。系统会在一个币首次出现有效信号后自动创建 `data/lifecycle.db` 生命周期档案，并把后续同币种信号归并到同一生命周期，识别同级确认、周期升级、短线冷却、风险升高、启动失败等事件。

生命周期核心指标以 Binance 为主：价格、K 线、成交量、OI、合约 taker buy/sell 近似 CVD、现货 aggTrades 近似 CVD 和 funding rate。其他交易所仅作为旁路观察，最多展示当前价格、资金费率及与 Binance 的偏离，不参与 `lifecycle_score`、`risk_score` 或状态流转。

新增 CLI：

```bash
python main.py lifecycle-backfill --lookback-hours 168
python main.py lifecycle-scan --lookback-hours 24 --limit-symbols 80
python main.py lifecycle-status --symbol BTCUSDT
```

新增公开只读 API：`/public-api/lifecycle/summary`、`/public-api/lifecycle/list`、`/public-api/lifecycle/detail`、`/public-api/lifecycle/events`、`/public-api/lifecycle/metrics`。新增私有 API：`/api/lifecycle/summary`、`/api/lifecycle/list`、`/api/lifecycle/detail`、`/api/lifecycle/events`、`/api/lifecycle/run-scan`、`/api/lifecycle/run-backfill`。

Next.js 公开前台新增“生命周期”页面，首页和单币详情页也会展示生命周期跟随概览。生命周期 Telegram 跟随提醒是新增辅助消息，不改变现有 Telegram 主推送流程。该功能仅用于信号整理和风险提示，不构成投资建议，不执行自动交易。

## v1.74.5 说明

v1.74.5 继续加固生产 Nginx active 配置清理。v1.74.4 已能检测重复 `paoxx.com` server block，但清理逻辑仍可能漏掉部分 active 文件；本版本改为按 `/etc/nginx/sites-enabled` 和 `/etc/nginx/conf.d` 的实际 active 文件扫描，并用 `readlink -f` 对比 keep file，只保留 `/etc/nginx/conf.d/00-paoxx-frontend.conf`。

清理过程会覆盖 symlink 和普通文件：symlink 只删除 active 链接，普通文件改名为 `.disabled.<timestamp>`，所有被禁用项先备份到 `/etc/nginx/backup-paopao/duplicate-cleanup.<timestamp>/`。脚本会重新扫描 active 配置、执行 `nginx -t 2>&1`，如果仍有 `conflicting server name "paoxx.com"` 会失败退出。HTTP 80 保留 `/.well-known/acme-challenge/` 到 `/var/www/html`，不删除 Let's Encrypt 证书，也不破坏 certbot renew。

`scripts/check_https_deploy.sh` 现在在发现重复 server block 时会输出两条定位命令：

```bash
sudo grep -RIn "server_name .*paoxx.com" /etc/nginx/sites-enabled /etc/nginx/conf.d
sudo nginx -T 2>&1 | grep -nE "configuration file|server_name paoxx.com|listen 80|listen 443"
```

最终路由保持：`/` 和 `/_next/` 走 Next.js `127.0.0.1:3000`；`/admin`、`/api/`、`/public-api/` 走 Python 后端 `127.0.0.1:8080`。本版本不改 Telegram 主推送流程，不引入自动交易，不改数据库 schema，也不改后端 API contract。

## v1.74.4 说明

v1.74.4 清理生产 Nginx 中重复的 `paoxx.com` server block。安装和更新脚本现在只把 `/etc/nginx/conf.d/00-paoxx-frontend.conf` 作为 active 生产入口，并会禁用 `/etc/nginx/sites-enabled/default`、`/etc/nginx/sites-enabled/paoxx.com` 以及 `/etc/nginx/conf.d` 中其他声明 `server_name paoxx.com` 的旧入口。

被禁用的 active 文件会先备份到 `/etc/nginx/backup-paopao/`。如果旧入口是 symlink，只删除 symlink，保留原始 `sites-available` 历史文件；如果是普通文件，则改名为 `.disabled.<timestamp>`。脚本会执行 `nginx -t 2>&1` 和 `nginx -T 2>&1`，如果出现 `conflicting server name "paoxx.com"` 会失败并提示定位命令：

```bash
sudo grep -RIn "server_name .*paoxx.com" /etc/nginx/sites-enabled /etc/nginx/conf.d
```

最终路由保持不变：`/` 和 `/_next/` 走 Next.js `127.0.0.1:3000`；`/admin`、`/api/`、`/public-api/` 走 Python 后端 `127.0.0.1:8080`。本版本不改 Telegram 主推送流程，不引入自动交易，不改数据库 schema，也不改后端 API contract。

## v1.74.3 说明

v1.74.3 修复部署验收脚本的日志误判：`scripts/check_https_deploy.sh` 不再把 `OK observe_history`、`启动观察历史`、readiness 等正常运行日志，或已知可自动重试的单次网络 timeout 当作阻断错误。

日志扫描现在先应用部署验收专用白名单，再按明确错误规则判断阻断项，例如 `Traceback`、`Exception occurred during processing`、`Unhandled exception`、`RuntimeError`、`sqlite database is locked`、`no such table`、`EADDRINUSE`、`ECONNREFUSED`、`500 Internal Server Error`、明确的 `/api/ 500` / `/public-api/ 500` / `/admin 500`、`ERROR`、`CRITICAL` 等。脚本输出阻断日志时会显示服务名、匹配规则、判定原因和脱敏后的原始日志行，便于排查。

v1.74.2 的 Next.js 前台验收仍保留：`paopao-frontend` active、本机 3000、HTTPS 页面 marker、`nginx -T` active route、`/admin`、`/public-api` 和私有 `/api` 401 都继续检查。本版本不改 Telegram 主推送流程，不引入自动交易，不改数据库 schema 或后端 API contract。

## v1.74.2 说明

v1.74.2 修复 Next.js 公开前台在本机 3000 正常、但公网 `https://paoxx.com/` 仍返回旧 Python 前台的问题。原因是 v1.74.1 写入了 Nginx 站点模板，但线上实际生效的 server block 仍可能来自旧 default / legacy 配置，`/` 继续被反代到 `paopao-web` 的 8080。

安装和更新脚本现在会写入实际生效的 `/etc/nginx/conf.d/00-paoxx-frontend.conf`，并把旧的 `sites-enabled/default`、旧 paopao 站点和旧 `conf.d` 入口改名为 `*.disabled-by-paopao`，避免它们继续接管 `/`。生效路由固定为：`/admin`、`/api/`、`/public-api/` 走 Python 后端 `127.0.0.1:8080`，`/_next/` 和 `/` 走 Next.js `127.0.0.1:3000`。

`scripts/check_https_deploy.sh` 现在会读取 `nginx -T` 的 active config，确认同时存在 `proxy_pass http://127.0.0.1:3000;` 和 `proxy_pass http://127.0.0.1:8080;`，不再只依赖模板文件或本机 3000 健康状态。Telegram 主推送流程、后台 API 鉴权、数据库结构和自动交易能力均未改变；本版本没有引入自动交易。

## v1.74.1 说明

v1.74.1 修正 Next.js 公开前台的生产接线：更新和安装脚本会优先使用 `npm ci` 构建 `frontend/`，写入 `paopao-frontend.service`，用真实 `npm` 路径启动 Next.js，并确保服务只监听 `127.0.0.1:3000`。脚本会写入可重复执行的 Nginx 路由配置：`/admin`、`/api/`、`/public-api/` 继续反代到 Python 后端 `127.0.0.1:8080`，`/` 反代到 Next.js `127.0.0.1:3000`。

Next.js 页面新增隐藏标识 `paoxx-frontend=nextjs-dashboard`，部署验收脚本会同时检查本机 3000、HTTPS 公开前台、`paopao-frontend` systemd 服务和 Nginx 80/443，避免把旧 Python 公开前台误判为新前台成功。日志检查会输出匹配到的服务名和原始片段，便于定位真实阻断项。

## v1.74.0 说明

v1.74.0 新增 `frontend/` Next.js 公开前台，把 `https://paoxx.com/` 升级为专业加密数据仪表盘。公开前台使用 React、TypeScript、Tailwind CSS、App Router 和 Recharts，展示信号雷达、决策模型、结果追踪、决策回测、单币详情和公开 API 说明。

生产结构调整为：`paopao-frontend` 只监听 `127.0.0.1:3000`，负责公开前台；`paopao-web` 继续负责 `/admin`、`/api/*` 和 `/public-api/*`；Nginx 统一提供 HTTPS 入口，将 `/`、`/radar`、`/decision`、`/outcomes`、`/backtest`、`/coin/*` 指向 Next.js，将 `/admin`、`/api/`、`/public-api/` 指向 Python 后端。旧 Python 内嵌公开前台保留为 fallback，不删除。

新前台只调用 `/public-api/*`，不会读取后台 `/api/*`、Cookie、Authorization、后台配置、日志、审计、Telegram 私有字段或任何 token/secret。Python 后端继续负责 Telegram、数据采集、后台控制台、私有 API 和公开 API；本版本不改 Telegram 主推送流程，不实现自动交易，不接交易所下单 API，不修改 `signals.db` / `outcomes.db` / `jobs.db` schema。

部署后可用：

```bash
cd /home/ubuntu/paopao-crypto-radar
paopao update --yes || bash scripts/update_server.sh --yes
bash scripts/check_https_deploy.sh --with-stable-check
```

## v1.73.0 说明

v1.73.0 新增 Decision Backtest Dashboard，用 `data/outcomes.db` 的 `signal_outcomes` 统计不同决策在 1h / 4h / 24h / 72h 后的真实表现。看板按 decision_code、horizon、module、risk_level 和 confidence bucket 聚合，输出样本数、覆盖率、平均最终涨跌、平均最大涨幅、平均最大回撤、正收益比例、明显回撤比例、期望评分和样本质量。

新增公开只读 API：`/public-api/backtest/decision`、`/public-api/backtest/decision/matrix`、`/public-api/backtest/decision/detail`；新增后台私有 API：`/api/backtest/decision`、`/api/backtest/decision/matrix`、`/api/backtest/decision/detail`。公开 API 继续脱敏，不返回 payload_json、text_html、Telegram topic/message/reply、jobs、audit、config、logs 或任何 token/secret 字段。

公开前台和后台控制台新增“决策回测”入口。看板会展示决策表现卡片、决策 x 周期矩阵和模型诊断，帮助判断“可试仓”“风险警报”“禁止追高”“等待回踩”“观察”等决策是否符合后续表现。`pending` 只表示结果窗口未到期，`unavailable` 表示价格源无数据，`error` 表示系统异常；只有 `success` 样本参与收益、回撤和正收益比例统计。

本功能仅用于复盘统计和模型校准，不执行自动交易，不接交易所下单 API，不改 Telegram 主推送流程，不改 `signals.db` / `jobs.db` 结构，也不破坏已有 `outcomes.db` 数据。

## v1.72.2 说明

v1.72.2 修正 Signal Outcome Tracking 的价格源不可用口径：Binance HTTP 400、invalid symbol、Bad Request、symbol not found 和空 K 线数据会标记为 `unavailable / 数据不足`，不再归类为系统 `error`。扫描开始时会自动把旧库中 `HTTP Error 400` 这类历史误分类记录修复为 `unavailable`，`/public-api/outcomes/stats` 的 `error_count` 也不再包含这些价格源不支持的交易对。

`outcome-scan` 报告现在会单独输出“数据不足 / 价格源不可用摘要”，包含 symbol、horizon 和原因；真正的代码异常、数据库异常或不可预期解析异常才进入“错误摘要”。同一轮扫描会缓存无效交易对，避免同一 symbol 在 1h / 4h / 24h / 72h 多个窗口中反复请求价格源。

部分信号币种可能不在当前价格源可查询范围内，例如 1000 前缀合约、非 Binance 现货交易对或部分新币。v1.72.2 会把这类结果标记为“数据不足 / 价格源不可用”，不会视为系统错误；后续版本可补充公开 futures K 线或多交易所行情源。本版本不改 Telegram 主推送流程，不执行自动交易，不接交易所下单 API。

## v1.72.0 说明

v1.72.0 新增 Signal Outcome Tracking，用于追踪已发送结构化信号在 1h / 4h / 24h / 72h 后的价格表现。结果会写入独立运行库 `data/outcomes.db` 的 `signal_outcomes` 表，不迁移或破坏 `signals.db` / `jobs.db`，也不改变 Telegram 推送主流程。

每条 outcome 会记录 signal_id、symbol、signal_time、horizon、entry price、future price、最高价、最低价、最终涨跌、最高涨幅、最大回撤、结果标签、数据状态，并附带 outcome 扫描时的决策快照。结果标签包括：表现较强、小幅走强、震荡、小幅走弱、明显回撤、数据不足。v1 默认使用多头观察口径，仅用于复盘和模型校准。

新增 CLI：

```bash
.venv/bin/python main.py outcome-scan
.venv/bin/python main.py outcome-scan --limit 100
.venv/bin/python main.py outcome-scan --horizon 1h
.venv/bin/python main.py outcome-scan --symbol BTCUSDT
.venv/bin/python main.py outcome-scan --dry-run
.venv/bin/python main.py outcome-scan --backfill-days 7
```

新增 API：公开只读 `/public-api/outcomes`、`/public-api/outcomes/stats`、`/public-api/symbol-outcomes`；后台私有 `/api/outcomes`、`/api/outcomes/stats`、`/api/symbol-outcomes`、`POST /api/outcomes/scan`。公开 API 继续脱敏，不返回 payload_json、text_html、dedup_key、Telegram topic/message/reply、jobs、audit、config、logs 或任何 token/secret 字段。

公开前台和后台控制台新增“结果追踪”入口，展示最近追踪结果、最终涨跌、最高涨幅、最大回撤、数据状态、按币种的历史结果追踪以及后台手动触发扫描按钮。本功能不执行自动交易，不接交易所下单 API，不做仓位管理，不操作真实资金。

## v1.71.0 说明

v1.71.0 统一 Signal Decision Model 的 API 契约：`/public-api/decision` 和 `/api/decision` 现在都采用 `ok + data + _meta` 结构，旧的顶层 `decision/scores/reasons` 字段仍保留用于前端兼容。`/public-api/decisions` 和 `/api/decisions` 的 `data` 中补齐 `items`、`summary`、`distribution`、`filters` 和 `pagination`。

新增决策统计接口：公开 `/public-api/decisions/stats`，私有 `/api/decisions/stats`。统计会返回最近窗口内的决策分布、风险分布、可试仓列表、风险/禁止追高列表和摘要；私有接口额外返回模型权重、阈值和校准说明。

模型校准为 `signal-decision-v1.1`：BTC、ETH、SOL、BNB、XRP 等高频币种不会仅因信号数量多就直接判为风险警报；风险警报需要明确风险因子，例如资金费率拥挤、结算周期缩短、假突破、破位、失败/阻止信号增加等。强信号但无明确风险因子时更倾向于“等待回踩”或“禁止追高”。

每个决策结果新增 `factor_explanations` 和 `calibration`，用于说明信号强度、模块共振、信号密度、拥挤风险、结构确认、失败惩罚如何影响结论。公开前台和后台 API 仍然只做只读展示，本模型仅用于信号整理和风险提示，不构成投资建议，不执行自动交易。

## v1.70.3 说明

v1.70.3 加固后台账号密码登录：同一用户名和来源 IP 连续失败默认 5 次会锁定 10 分钟，失败计数窗口默认 15 分钟；登录成功后会清除该来源的失败计数。

后台认证审计会记录登录成功、登录失败、登录锁定、退出登录、会话过期、会话无效和密码变更等事件，运行数据保存在 `data/admin_auth_audit.json`，失败锁定状态保存在 `data/admin_auth_state.json`。审计只保存用户名、事件类型、结果、原因和 IP/User-Agent 哈希，不记录明文密码、密码哈希、Cookie、session secret 或旧访问令牌。

后台登录状态现在会显示当前用户、登录时间、会话到期时间和剩余时间；当会话剩余时间低于 TTL 的一半时，已登录访问后台 API 会安全续期并重新签发带 `HttpOnly`、`SameSite=Lax`、HTTPS 下 `Secure` 的会话 Cookie。写操作继续校验 `X-CSRF-Token`。

新增可调配置：`WEB_AUTH_MAX_FAILURES`、`WEB_AUTH_LOCKOUT_SEC`、`WEB_AUTH_FAILURE_WINDOW_SEC`、`WEB_AUTH_AUDIT_LIMIT`、`WEB_SESSION_REFRESH_THRESHOLD_RATIO`。如忘记密码，仍在服务器本地执行 `.venv/bin/python main.py admin-password set` 后重启 `paopao-web`。

## v1.70.2 说明

v1.70.2 调整后台账号密码设置命令的交互体验：执行 `.venv/bin/python main.py admin-password set` 时，密码输入会明文显示，便于在服务器终端确认输入内容。请确保当前终端环境安全。

系统仍然不会保存明文密码，只会写入 `WEB_ADMIN_PASSWORD_HASH=pbkdf2_sha256$...`。如果需要隐藏输入，可以执行 `.venv/bin/python main.py admin-password set --hidden`。

## v1.70.1 说明

v1.70.1 将后台控制台认证从默认 `WEB_ADMIN_TOKEN` 令牌输入改为自定义用户名 + 密码登录。首次部署或从旧版本升级后，在服务器执行：

```bash
cd /home/ubuntu/paopao-crypto-radar
.venv/bin/python main.py admin-password set
sudo systemctl restart paopao-web
```

设置密码时终端会明文显示输入内容，便于确认；请确保当前终端环境安全。如需隐藏输入，可使用 `.venv/bin/python main.py admin-password set --hidden`。密码不会明文保存，只保存 `PBKDF2-HMAC-SHA256` 哈希；登录成功后由服务端写入带签名的 `HttpOnly`、`SameSite=Lax` 会话 Cookie，HTTPS 反代下会设置 `Secure`。后台入口仍为 `https://paoxx.com/admin`，未登录访问 `/api/*` 会返回 `401 Unauthorized`；`/public-api/*` 继续公开只读、脱敏、不需要登录。

`WEB_ADMIN_TOKEN` 配置项保留为 `WEB_AUTH_MODE=token` 的旧模式兼容或紧急回滚用途，但不再作为默认后台登录方式。默认菜单、更新输出和后台页面都不会明文显示后台密码、密码哈希、会话密钥或旧访问令牌。

## v1.70.0 说明

v1.70.0 新增 Signal Decision Model v1，把 `signals.db` 中的结构化信号整理成只读决策状态：`观察`、`等待回踩`、`可试仓`、`禁止追高`、`风险警报`。模型输出包含决策等级、置信度、风险等级、主要依据、反向风险、下一步观察点、组成因子分数和最近相关信号。

本模型仅用于信号整理和风险提示，不构成投资建议，不执行自动交易，不接交易所下单 API，也不改变 Telegram 推送主流程。新增私有 API `/api/decision`、`/api/decisions`，新增公开脱敏 API `/public-api/decision`、`/public-api/decisions`。公开前台新增“决策模型”区域，信号卡片和币种详情会展示当前决策、置信度、风险等级和观察点。

## v1.69.1 说明

v1.69.1 修正生产入口显示和公开前台中文化：`paopao update`、安装完成提示和服务器中文菜单默认显示正式入口 `https://paoxx.com/` 与 `https://paoxx.com/admin`，不再把旧的服务器 8080 示例地址当作公网访问地址。8080 只作为本机/Nginx 反代后端入口。

服务器中文菜单首页不再明文打印后台访问令牌，只显示“已配置，默认不在菜单首页明文显示”；如需查看令牌，需要进入专门菜单项并确认当前终端环境安全。公开前台用户界面统一中文，品牌名保留 Paoxx，描述性文案使用中文；公开页面仍只调用 `/public-api/*` 并保持脱敏边界。

## v1.69.0 说明

v1.69.0 打磨公开前台体验：`https://paoxx.com/` 首页信号卡片升级为更清晰的 Signal Card，支持币种、模块、状态、时间窗口和关键词筛选；点击卡片可打开公开脱敏的信号详情弹窗，点击币种可进入单币详情视角。

公开前台新增轻量全市场时间线入口，复用 `/public-api/signal-timeline`，移动端下筛选栏、卡片、详情弹窗会自动单列适配。公开 API 脱敏边界保持不变，不返回后台配置、任务、日志、审计、Telegram topic/message/reply、`payload_json` 或原始全文。更新脚本末尾提示也改为正式 HTTPS 入口，不再把公网 8080 当作默认访问方式。

## v1.68.1 说明

v1.68.1 修复 HTTPS 部署验收脚本的误判：后台 `/admin` 页面改为 GET + `-L` 下载到临时文件，并用固定字符串匹配 `泡泡雷达控制台`、`brand-title` 或 `/admin`；失败时会显示 HTTP_CODE、下载字节数和页面前 8 行摘要，便于定位 Nginx/页面内容问题。

证书文件检查现在会优先使用 `sudo test -f`，普通用户无法读取 `/etc/letsencrypt/live` 下的证书符号链接时，会再用 `certbot certificates --cert-name paoxx.com` 或 `certbot renew --dry-run` 的成功结果兜底，避免把权限问题误判为证书缺失。`privkey.pem` 只检查存在，不读取内容。

## v1.68.0 说明

v1.68.0 增加固定生产 HTTPS 部署验收脚本 `scripts/check_https_deploy.sh`，用于服务器更新后验证正式入口、Nginx 80/443、公开前台、后台页面、公开 API、私有 API 401 隔离、systemd 服务、Let's Encrypt 证书文件、可选 stable-check、可选 certbot dry-run 和最近日志阻断关键词。

正式入口为：公开前台 `https://paoxx.com/`，后台控制台 `https://paoxx.com/admin`，公开 API `https://paoxx.com/public-api/*`，私有 API `https://paoxx.com/api/*` 需要后台登录后的安全会话 Cookie；未登录应返回 `401 Unauthorized`。验收命令：

```bash
bash scripts/check_https_deploy.sh
bash scripts/check_https_deploy.sh --with-stable-check
bash scripts/check_https_deploy.sh --with-certbot-dry-run
```

注意：`curl -I https://paoxx.com` 可能返回 `501 Unsupported method ('HEAD')`，因为当前 paopao-web 不支持 HEAD；页面验收请使用普通 GET 检查内容。生产环境中本机 8080 监听不等于公网暴露，云安全组应关闭公网 8080，只保留 80/443 作为正式入口。

## v1.67.0 说明

v1.67.0 将 Web 拆成同域双入口：`/` 是公开信号前台，显示 Paoxx 信号雷达的脱敏只读信号、统计、活跃币种和公开时间线；`/admin` 是原有后台控制台，继续使用 `WEB_ADMIN_TOKEN` 访问私有 `/api/*`。

新增 `/public-api/*` 公开只读接口，用于公开信号列表、信号详情、统计、币种详情、币种搜索和信号时间线。公开接口会裁剪 `dedup_key`、Telegram topic/message/reply 字段、`payload_json`、原始 `text_html`、配置、任务、日志、审计和服务控制信息；`/api/*` 仍是后台私有 API，不改变现有控制台功能。

## v1.66.0 说明

v1.66.0 深化 Signal Timeline：新增轻量全局「信号时间线」入口，并增强 Coin Detail 的按日期分组时间线。时间线支持币种、模块、状态、关键词和时间窗口筛选，Timeline item 可直接打开 Signal Detail，便于按事件顺序排查某个币种或全局信号的发送、跳过、失败和 Telegram 记录。
本版本只做 Web 展示和只读查询增强，数据仍然全部来自 `signals.db` / `signal_events` 兼容视图；不触发行情扫描，不新增外部数据源，不改变 `signals.db` / `jobs.db` 结构，也不改变 Telegram 推送主流程。

## v1.65.0 说明

v1.65.0 新增 Web「Coin Detail / 币种详情」页面：可以从 Signal Card 或 Dashboard 最新信号直接进入 BTC/BTCUSDT 等单币种视角，查看该币种最近信号时间线、模块分布、状态统计、最新同币种 Signal Card、Telegram message/topic 记录和单条信号详情。

本版本只做只读情报展示和查询体验增强，数据全部来自 `signals.db` / `signal_events` 兼容视图，不触发行情扫描，不新增外部行情 API 调用，不改变 `signals.db` / `jobs.db` 结构，不改变 Telegram 推送主流程。

## v1.64.0 说明

v1.64.0 将 Web「信号推送」页升级为 Signal Card UI：信号列表改为卡片展示，支持关键词、币种、模块、状态、时间窗口和排序筛选；点击卡片可打开详情面板，查看 Telegram topic/message、dedup_key、payload_json、同币种最近信号等结构化信息。Dashboard 也会展示最新信号简版卡片和 24h 信号统计。

本版本只增强 Web 展示和查询体验，不改变 `signals.db` / `jobs.db` 结构，不改变 `signal_events` 兼容视图，也不改变 Telegram 推送主流程或行情扫描策略。Signal Card 数据全部来自结构化 `signals.db`，不会触发行情扫描。

## v1.62.1 维护说明

v1.62.1 修正任务中心的 stable-check 展示口径：`stable-check` 返回码 1 代表“基本可运行，建议关注”，现在会显示为 `attention / 关注`，不再误判为失败任务，也不会进入 failed/timeout 问题中心；成功任务即使 stderr 里有 Git fetch 噪声，也不会生成错误摘要。

轻量级加密市场观察雷达。默认 dry-run，包含一个本地 Web 控制台用于查看状态、日志和修改关键配置，不包含自动交易。

## 功能和推送周期

- Binance 公告机会/风险监听：跟随主扫描，只推当天 CST 的可行动公告；识别 Alpha、上新、HODLer、Launchpool、Airdrop、下架、停止交易等，并按币种区分有无 Binance USDT 合约。
- 资金雷达汇总：默认 6 小时一次、每天最多 4 次，推送负费率榜、综合榜、埋伏榜、动量池、新币池、值得关注和数据质量。
- 启动雷达提醒：默认 3 分钟扫描一次，推送币种、阶段、分数、市值分档、流动性分档、价格/OI/成交量变化、资金费率/结算周期和触发原因；推送前会拉 Binance、OKX、Bybit、Bitget、Gate 五家公开资金费率，显示每家实时费率、当前周期和下次结算时间；资金费率极负会标注当前周期，例如 `-2.000%/1H`，结算周期从 8H→4H 或 4H→1H 会在信号里提示；同一币种后续更高阶段会回复上一条启动消息。
- 资金费率警报：v1.15 新增独立话题，默认每 3 分钟扫描 Binance 成交额前 120 个 USDT 合约，使用 Binance、OKX、Bybit、Bitget、Gate 免费公开数据，专门提示极负/极正费率、多交易所共振、结算周期缩短和交易所费率偏离；v1.15.3 起升级为跟踪型信号，首次发现会标注，同币后续信号会回复上一条，阶段会按首次异动、拥挤加剧、高危活跃、风险释放、热度衰减跟踪，并补充市值、24h 成交额、等宽交易所费率表和偏离解释。
- 五因子资金流雷达：默认每 1 小时收线后延迟 5 分钟推送一次，使用 Binance 免费公开数据，按上一完整窗口内的价格、OI、现货 CVD、合约 CVD、资金费率过滤资金流信号。
- 结构突破雷达：v1.8 新增，独立识别盘整箱体上沿/下沿、ATR/BB 压缩、临近突破、收线确认、假突破，并可生成 K线状态图。
- Web 控制台会说明每个外部接口在本项目里的用途，并用平台真实站点图标区分 Telegram、Binance、CoinPaprika、Coinalyze、CoinMarketCap：Telegram 必填，Binance/CoinPaprika 无需 Key，Coinalyze 仅作结构雷达历史清算辅助，CoinMarketCap 当前只是预留未接入；配置页按 Telegram、AI、雷达参数、资金费率、模块开关、外部接口、Web 控制台和备份恢复分类显示，并完整显示当前 Token / Key / Web 令牌。
- v1.23.0 起，Web 控制台按单人管理员运维后台重构：菜单分为总览、AI 助手、价格提醒、雷达服务、配置中心、日志中心、审计记录、诊断报告、检查测试、更新备份、功能说明；价格提醒从 AI 助手页拆出独立管理页，危险动作需要二次确认，总览不再直接铺大段 JSON，原始运行状态只放在高级排查折叠区。
- v1.24.0 起，Web 控制台增加实时运维闭环：总览和日志中心支持 15 秒自动刷新，最近错误可一键跳到对应日志，日志筛选增加 AI 助手和资金费率，检查测试/服务控制结果先显示大白话摘要，原始执行结果放到高级详情里。
- v1.25.0 起，Web 管理页继续工程化：日志中心增加筛选摘要、错误命中数和第一条错误提取；价格提醒页支持按状态、类型、关键词筛选，创建/暂停/恢复/删除结果也改为可读摘要加高级详情。
- v1.26.0 起，Web API 进入规范化底座：所有 JSON 接口都会附带 `_meta` 元信息，前端统一显示 HTTP 状态、接口路径、服务端时间和浏览器实测耗时；检查测试页新增「Web API 自诊断」，可一键检查总览、配置和 Web 日志接口是否正常。
- v1.27.0 起，Web 后台新增操作审计：配置保存、备份恢复/删除、检查测试、服务启停、价格提醒管理和 AI 提示词操作都会写入 `data/web_audit_log.json`，审计页可按成功/失败和关键词筛选；审计只保存操作摘要、结果、耗时和错误摘要，不保存 Token、API Key 或提示词正文。
- v1.28.0 起，Web 后台新增「诊断报告」：一键生成安全运维快照，汇总服务状态、健康检查、最近错误、关键配置摘要、失败审计和日志错误片段，并提供复制报告按钮；报告会脱敏 Token、API Key 和提示词正文。
- v1.29.0 起，Web 配置页新增保存前影响预检：预览和保存都会显示本次改动影响哪些模块、会自动重启哪些服务、敏感/危险配置提醒和回滚方式；后端 `/api/config-impact` 只分析不保存，审计也只记录字段名不记录敏感值。
- v1.30.0 起，Web 后台加固接口契约和前端错误边界：接口失败会显示统一错误卡片、重试、诊断报告和日志入口；AI 助手/价格提醒页支持局部失败展示，不会因为一个接口异常导致整页空白；测试会固定 JSON `_meta` 和错误返回格式。
- v1.30.1 起，诊断报告的日志错误统计会忽略正常 JSON 里的空 `errors: []` / `error: ""` 字段，避免把 AI 价格提醒巡检成功日志误判成错误。
- v1.30.2 起，诊断报告会继续过滤 `poll_timeout=5s` 这类字段名误报；复制报告/复制日志在 HTTP 访问下如果浏览器拒绝剪贴板权限，会自动选中文本并提示手动 `Ctrl+C`。
- v1.30.3 起，AI Bot 的 Telegram `getUpdates ReadTimeout` 会归类为“网络超时/可自动重试”，低频出现不再计入日志错误，也不会触发优先处理建议。
- v1.31.0 起，Web 后台 UI 成品化：总览、日志、配置、审计、诊断、检查测试、服务控制、更新备份、AI 助手和价格提醒页都有统一页面说明、状态标签和空状态提示；服务控制执行后会刷新当前页，后台更像可长期使用的管理产品，而不是工程测试页。
- v1.32.0 起，配置中心继续工程化：Telegram、AI Bot、价格提醒、主雷达参数、资金费率、结构雷达、行情源/外部接口、模块开关、Web 控制台和备份恢复拆成更细入口；每个配置项都会说明“做什么、影响什么、改完是否自动重启”。
- v1.33.0 起，诊断报告升级为问题中心：会把服务健康异常、runtime 最近错误、失败审计、日志错误和网络超时汇总成问题卡片，按严重程度、模块、出现次数和建议动作展示，并提供相关日志/审计跳转。
- v1.34.0 起，AI Bot 和价格提醒稳定性收口：按钮回调使用短超时静默确认，发送队列会自动重试临时失败，AI/价格/按钮错误会转成中文可读提示，价格提醒只有确认进入发送链路后才会标记触发。
- v1.35.0 是早期稳定版自检基线：诊断报告新增“稳定版自检”，按版本信息、后台服务、健康门禁、问题中心、日志稳定性、后台审计和关键配置判断当前部署是否达到长期运行标准。
- v1.36.0 起，更新后会自动执行稳定版验收：新增 `python main.py stable-check` 命令，`paopao update --yes` 完成安装、重启服务后会输出稳定版自检摘要。
- v1.37.0 起，稳定版验收结果会落盘保存：最近一次完整快照写入 `data/stable_check_latest.json`，精简历史写入 `data/stable_check_history.json`，Web 诊断报告会展示验收历史。
- v1.38.0 起，Web 诊断报告新增“问题中心总览”：先给出当前健康、需要关注或优先处理的结论，再聚合严重/警告数量、日志错误、网络超时、失败审计、稳定版门禁和下一步处理建议。
- v1.39.0 起，Web 诊断报告新增“处理清单”：把问题中心结论映射成可点击的下一步动作，例如打开雷达服务、日志中心、审计记录、配置中心或检查测试；Web“检查测试”页也支持直接执行稳定版验收并保存历史。
- v1.39.1 起，Web 控制台日志里的客户端断开连接（例如浏览器刷新、网络中断或外部探测导致的 `ConnectionResetError`）会归类为可忽略事件，不再把稳定版验收误标为需要关注。
- v1.40.0 起，Web 诊断报告新增“长期运行就绪度”：把当前稳定版验收、问题中心、验收历史、日志错误、失败审计和网络重试噪声合成一个完整稳定候选评分，直接显示“完整稳定版候选 / 准稳定候选 / 需要处理”和下一版本目标。
- v1.41.0 起，`python main.py stable-check` 和 `paopao update --yes` 的命令行输出也会显示“长期运行就绪度”：包含候选状态、评分、通过/警告/阻断计数、就绪度检查项和下一目标，和 Web 诊断报告保持同一口径。
- v1.42.0 起，稳定版验收历史会记录长期运行就绪度：每条历史包含候选状态、评分、警告/阻断计数和下一目标；Web 诊断报告的“验收历史”表也会显示长期就绪度和评分。
- v1.43.0 起，诊断报告和 `stable-check` 会显示“长期运行趋势”：对比最近两次长期就绪度历史，判断趋势变好、持平、变差或发生回退，并给出下一步处理建议。
- v1.44.0 起，长期运行趋势变差或发生回退会进入问题中心、建议动作和处理清单；Web 更新后的诊断报告会给出“查看趋势详情”入口，更新脚本也会提醒处理趋势告警。
- v1.45.0 起，Web 诊断报告的处理清单支持问题处理状态：每条处理项有稳定问题编号，可标记“已确认”或“已解决观察中”，最近处理记录写入 `data/problem_state.json`，复制报告时也会带上处理状态。
- v1.46.0 起，Web 诊断报告会自动复查已标记问题：已解决的问题如果仍在处理清单会标记“仍然存在”，如果当前消失会标记“已消失待复查”；stable-check 历史也会保存复查摘要。
- v1.47.0 起，项目进入 v1.50.0 完整稳定版收口路线：诊断报告会显示 v1.47-v1.50 的阶段表，当前阶段是“功能冻结和稳定性收口”；stable-check 会把问题复查、长期趋势和功能冻结边界一起纳入门禁，不再新增大模块。
- v1.48.0 起，诊断报告新增“服务器部署验收”：检查代码版本、后台服务、Web 入口、Telegram/AI 配置、stable-check、日志、审计和部署脚本；命令行 `python main.py stable-check` 也会输出同样的部署验收摘要。
- v1.49.0 起，Web“功能说明”和文档进入最终运维收口：日常检查、更新、排错、配置回滚、源码异常处理和完整稳定版标准集中成固定流程，避免说明分散。
- v1.50.0 定义为 v1 完整稳定版最终发布：诊断报告、stable-check、部署验收、运维流程和版本说明收口到同一口径；v1 主线进入长期维护，只做 bug 修复、策略微调、文档和运维补丁，新增大模块进入 v2 规划。
- v1.50.1 是完整稳定版后的维护补丁：`.env.oi` 里的非空配置会覆盖进程里的空环境变量，避免 `WEB_ADMIN_TOKEN` 已生成但部署验收仍误判未配置；Web 入口失败建议也会给出可直接执行的更新命令。
- v1.50.2 修复服务器更新验收：Web 服务已占用 8080 时，单元测试不会再读取真实 `.env.oi` 导致误连端口；部署验收会优先按 `.env.oi` 的 `WEB_*` 配置判断。
- v1.50.3 修复部署验收快照：诊断报告会把 Web 配置写入 `config.web`，避免 `.env.oi` 已配置 `WEB_ADMIN_TOKEN` 但部署验收仍显示未配置。
- v1.50.4 优化诊断噪声：`coinpaprikaMarketCaps: ReadTimeout` 这类外部行情源偶发超时会归类为“网络超时/可自动重试”，不再作为真实日志错误影响稳定版候选。
- v1.51.0 开始 Web 后台 UI 工程化优化：在不引入 Vue 构建链、不改后端核心逻辑的前提下，借鉴 Naive Admin 的后台产品结构，统一深色侧栏、顶部栏、卡片、表格、状态标签和响应式布局，让控制台更像长期可用的运维产品。
- v1.52.0 新增 Web「服务器状态」面板：显示 CPU、系统负载、内存、Swap、磁盘空间、运行时间和主机信息，并用圆环、进度条和趋势图展示资源变化；顶部栏固定显示当前版本号和提交号，切换页面也不会丢失。
- v1.53.0 优化 Web UI 性能和实时监控：自动刷新改成按页面使用不同频率，服务器状态页每 3 秒刷新轻量接口，其他运维页保持低频；刷新过程增加防重入锁，避免接口叠加；CPU、内存、磁盘升级为带指针的动态仪表盘，并补充白话说明。
- v1.53.1 修复诊断趋势误报：如果当前服务、日志、审计和部署验收都正常，单纯因为历史分数对比变差不会再进入问题中心，也不会拉低长期运行就绪度评分。
- v1.54.0 升级 Web 高级视觉质感：去掉偏塑料的浅灰大色块，改成钛灰、石墨、银色的金属磨砂层次，并把服务器状态仪表盘升级为带金属外圈、刻度、玻璃高光、中心轴和动态指针的设备式仪表。
- v1.55.0 深化 Web Premium 暗色金属磨砂主题：全局切换为深灰/黑曜背景，使用香槟金、银灰、古铜点缀；统一按钮、表格、表单、状态标签、配置卡片、接口卡片和服务器仪表盘的深色毛玻璃与金属反光效果，减少浅色塑料感。
- v1.55.1 按线上浏览器实测继续打磨 Web 观感：修复日志页筛选栏在桌面端纵向堆叠的问题，移动端侧边栏改成紧凑横向导航，统一暗色滚动条，降低拉丝纹理噪声，并提升说明文字对比度。
- v1.56.0 重排 Web 视觉方向：减少大面积金铜色和强纹理，改成黑曜/蓝黑/石墨主色，青蓝作为主要操作色、香槟金只做少量提示；侧边栏、顶部栏、卡片、表格、输入框和服务器仪表盘统一为更克制的专业运维后台风格。
- v1.57.0 进行 Web UI Tabler 化重排：不引入外部 CDN、Vue 或 React，保留 Python 原生 Web；视觉改为浅灰工作区、白色卡片、深色侧栏、蓝色主操作、轻边框和低阴影，整体更接近成熟开源运维后台模板。
- v1.57.1 修复服务器状态仪表盘指针遮挡百分比数字的问题：中心读数层级提高，指针保留在读数下方，CPU、内存、磁盘百分比更容易看清。
- v1.58.0 进行 Web UI 极简收口：侧栏去掉菜单小字、顶部栏压缩、页面说明改为短说明+折叠详情、总览页默认只展示核心服务和错误入口，运行摘要/配置摘要默认折叠，服务器仪表盘改为更紧凑的资源卡。
- v1.58.1 删除页面说明里的重复“完整说明”展开项，只保留一条短说明；真正有用的运行摘要、关键配置和高级排查仍保留折叠入口。
- v1.59.0 重做 Web 后台整体布局：电脑浏览器保持左侧导航，不再过早切成顶部横向菜单；总览页新增 Telegram、Binance、CoinPaprika、Coinalyze、CoinMarketCap 平台 logo 条，外部接口来源更直观。
- AI 币种档案：v1.16.0 新增 AI Bot 自然语言查币，v1.18.0 升级价格提醒为纯手动选择流程，v1.20.0 升级为异步队列/worker 架构。发送“查 BTC”“GWEI 怎么看”“SOL 可以做多吗”时，机器人会读取历史雷达信号、当前价格/OI/成交量/资金费率/市值/流动性和结构状态，先给本地多空证据结论，AI Key 开启后再生成增强研判；设置价格提醒时会手动选择现货/合约和 Binance、Bybit、OKX、Bitget、Gate 价格源。
- Web 配置页会同时识别 `.env.oi` 里的手动话题 ID 和 `data/tg_topic_routes.json` 里的自动创建话题 ID；自动话题会标注“自动话题”，避免误以为没有配置。
- OI/价格背离扫描：跟随资金雷达，跟踪建仓背离、多头共振、极端背离、持续/增强/消失状态。
- 自动清理：默认 1 小时检查一次，只清理可再生成的缓存、临时文件、坏 JSON 备份、过期日志、过长历史、过期结构图和根目录临时报告。

## 服务器一键部署

服务器能访问这个 GitHub 私有仓库后，直接运行:

```bash
git clone https://github.com/ouoawp-ship-it/paopao-crypto-radar.git
cd paopao-crypto-radar
bash scripts/install_server.sh
```

第一次运行会自动创建 `.env.oi`。如果没有填写 Telegram 配置，会直接在终端提示输入 `TG_BOT_TOKEN` 和 `TG_CHAT_ID`；token 输入会显示出来，方便确认粘贴成功。空回车或格式不对会反复提示，不会继续启动服务。随后会提示可选 `COINALYZE_API_KEY`；直接回车就是不启用历史清算辅助。

Telegram 群开启话题后，可以把不同推送分到不同话题，避免消息交叉：

```bash
TELEGRAM_USE_TOPIC=true
TG_RADAR_SUMMARY_TOPIC_ID=资金摘要话题ID
TG_LAUNCH_ALERT_TOPIC_ID=启动预警话题ID
TG_ANNOUNCEMENT_ALERT_TOPIC_ID=公告风险话题ID
TG_TEST_TOPIC_ID=测试消息话题ID
TG_FUNDING_ALERT_TOPIC_ID=资金费率警报话题ID
TG_AUTO_CREATE_TOPICS=true
```

没有配置专属话题的消息会先读取 `data/tg_topic_routes.json` 里已自动创建过的话题 ID；仍没有时，如果 `TG_AUTO_CREATE_TOPICS=true` 且 bot 有管理话题权限，会自动创建并记录话题。`TG_TOPIC_ID` 可作为默认兜底话题；所有话题都不可用时，消息发到群默认主聊天。

每个推送话题第一次真实发送前，会自动发一条“本话题功能说明/信号阅读方式/扫描发送频率”，并尝试置顶；如果后续版本的说明内容变化，会尽量删除旧说明并重新发送、置顶最新版。置顶和删除需要 bot 具备置顶消息、删除消息或管理话题权限。可用 `TG_TOPIC_INTRO_ENABLE=false` 或 `TG_TOPIC_INTRO_PIN=false` 关闭。

```bash
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
- 创建并启动 `paopao-radar`、`paopao-structure`、`paopao-web`、`paopao-ai` systemd 服务
- 定时自动清理临时文件、坏 JSON 备份、过期日志和过长历史

## 查看运行

```bash
sudo systemctl status paopao-radar
journalctl -u paopao-radar -f
python main.py runtime-status
python main.py about
python main.py cleanup --force-cleanup
```

## Web 控制台

Web 控制台默认作为 `paopao-web.service` 安装，由 Nginx 反代提供正式 HTTPS 入口。日常访问:

```text
公开前台: https://paoxx.com/
后台控制台: https://paoxx.com/admin
```

后台页面会要求输入自定义用户名和密码。首次设置或重置后台账号密码:

```bash
cd /home/ubuntu/paopao-crypto-radar
.venv/bin/python main.py admin-password set
sudo systemctl restart paopao-web
```

设置密码时终端会明文显示输入内容，便于确认；请确保当前终端环境安全。如需隐藏输入，可使用 `.venv/bin/python main.py admin-password set --hidden`。密码不会明文保存，只保存哈希；默认菜单和更新输出不会明文打印后台密码、密码哈希、会话密钥或旧访问令牌。8080 只作为本机/Nginx 反代后端入口，生产公网入口请使用 80/443。

服务器快捷入口只需要记住一个命令:

```bash
paopao
```

进入中文菜单后，用数字选择查看正式入口、设置后台账号密码、Web 服务状态、Web 实时日志、重启 Web 服务、检查更新、更新项目和查看版本。配置修改、主服务/结构雷达控制、测试消息、readiness、doctor、cleanup、结构复盘等日常动作在 Web 页面里完成。

前台调试启动仍然保留在脚本里，但正常使用不需要记任何 Web 子命令。

配置项:

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

控制台功能包括：服务状态、运行健康度、最近错误、实时日志、日志搜索筛选、runtime-status、readiness、Telegram 测试消息、doctor、稳定版验收、Binance 公告测试、资金费率警报扫描、结构信号复盘、cleanup、主服务/结构雷达重启、推送样例预览、GitHub 更新检查，以及 `.env.oi` 关键配置编辑。配置页按功能分类进入，支持保存前预览改动、保存后中文结果提示、最近 `.env.oi` 备份一键恢复/删除、真实模块开关，以及结构复盘参数建议一键应用。结构复盘推送里建议调整的 `STRUCTURE_MIN_SCORE` 和 `STRUCTURE_SEND_CHART_TOP_N` 可以在 Web 的“配置 -> 结构雷达”里直接修改。保存配置前会自动备份 `.env.oi`，保存成功后会自动应用新配置；主服务和结构雷达会自动重启，Web 端口或令牌变更会让 Web 控制台短暂重启。Web 接口异常时页面会显示可读错误卡片，并提供重试、诊断报告和日志中心入口。v1.31.0 起，每个核心页面都有统一入口说明、状态标签、空状态和动作说明，服务控制执行后会刷新当前服务页。v1.32.0 起，配置中心拆成 Telegram、AI Bot、价格提醒、主雷达参数、资金费率、结构雷达、行情源/外部接口、模块开关、Web 控制台和备份恢复；每个配置项都写明做什么、影响什么、保存后怎么生效。v1.33.0 起，诊断报告页优先显示问题中心，按严重程度汇总异常并提供相关日志或失败审计跳转。v1.38.0 起，诊断报告顶部会先显示“问题中心总览”，直接告诉你当前是否健康、是否需要关注、是否要优先处理。v1.39.0 起，诊断报告会显示可点击“处理清单”，把排查入口串起来。v1.40.0 起，诊断报告顶部会显示“长期运行就绪度”，用评分和候选状态判断当前部署是否已经适合当作完整稳定版继续长期运行。v1.41.0 起，稳定版验收命令行也会打印同样的长期就绪度摘要。v1.42.0 起，验收历史也会保存并展示长期就绪度分数。v1.43.0 起，诊断报告会判断长期运行趋势和回退。v1.44.0 起，趋势变差或回退会进入问题中心和处理清单。v1.45.0 起，处理清单里的问题可以标记确认或解决观察，方便后续复查。v1.46.0 起，处理状态会自动复查：仍存在和已消失待复查会直接显示出来。v1.47.0 起，诊断报告会显示 v1.50.0 收口路线，并把功能冻结、问题复查和长期趋势纳入 stable-check 门禁。v1.48.0 起，诊断报告和 stable-check 会显示服务器部署验收。Web 内置“功能说明”页，会说明每个页面的用途、版本号、提交号和安全规则。

如果 `WEB_ADMIN_PASSWORD_HASH` 或 `WEB_SESSION_SECRET` 尚未配置，后台登录页会提示先在服务器执行 `.venv/bin/python main.py admin-password set`。`WEB_ADMIN_TOKEN` 仅保留给显式设置 `WEB_AUTH_MODE=token` 的旧模式兼容或紧急回滚。

## AI 助手 Bot 和价格提醒

v1.13.0 新增独立 AI 助手服务 `paopao-ai.service`。它和群里的雷达推送 Bot 分开：

```text
TG_BOT_TOKEN  = 群话题推送雷达信号
AI_BOT_TOKEN  = 私聊 AI 助手、手动价格提醒、个人提醒
```

推荐用 BotFather 单独创建一个新的 Telegram Bot，填到 Web 控制台的「配置 -> AI 助手」里：

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

价格提醒不需要 AI API Key。v1.19.0 起，价格提醒升级为多类型监控：打开 AI 助手 Bot 私聊，点击「设置价格提醒」，可选择目标价提醒、价格急涨急跌、持仓量变化、资金费率变化；支持 Binance、Bybit、OKX、Bitget、Gate 的现货或 USDT 合约价格源，并可选择提醒一次、重复提醒或持续每5分钟提醒。v1.20.0 起，AI Bot 使用异步发送队列、后台价格提醒扫描和更新处理 worker；查询价格、输入币种识别交易所、AI 分析等慢任务会先回复“已收到/正在处理”，结果完成后再单独发送。v1.20.12 起，多交易所价格查询正文强制使用 Telegram HTML：价格表整块等宽显示，合约和现货共用列宽，表头简化为交易所/交易对/价格，并按 Binance、Bybit、OKX、Bitget、Gate 固定顺序展示；CoinGlass K线入口放在表格下方的文字链接里，不使用按钮，也不让链接破坏表格排版。v1.21.1 起，首页不再显示 AI 对话按钮，只保留设置价格提醒、我的提醒、查询价格和使用说明；日常/生活问题、交易/行情问题仍可直接发消息，系统会自动分流到泡泡 AI 助手或专业分析师提示词。Binance/Bybit 合约若普通交易对不存在，会自动尝试 `1000`、`10000`、`1000000` 前缀合约，并把这类合约报价折算成单币价格显示，交易对仍保留交易所原始名称。v1.21.2 起，慢任务的“已收到/正在处理”临时提示会在最终回复发送成功后自动撤回，聊天里只保留真正的结果消息。v1.21.3 起，AI Bot 去命令化，只保留 `/start` 打开首页；查价格、看行情、AI 分析都直接发消息，提醒管理在“我的提醒”按钮里完成。v1.21.4 起，提醒编号改为当前列表序号，删除后自动重排；提醒里的交易所名称加粗并跳转 CoinGlass K线，交易对使用等宽格式方便复制。v1.21.5 起，按钮点击会先静默确认 Telegram 回调，不再弹出“处理中...”提示，也避免配置/数据库加载拖住按钮加载圈。v1.22.0 起，AI Bot 进入极速响应目标模式：入口只做轻判断和分发，Settings 使用热缓存，交易所精确报价使用短 TTL 缓存，慢按钮/慢消息会写入耗时日志。v1.22.1 起，消息先走统一意图分类器，显式分析和长段市场数据优先进入 AI 分析，短句币种/价格才进入查价。v1.23.0 起，Web 后台把 AI 助手和价格提醒拆成独立页面，AI 页负责服务状态、意图分流和提示词入口，价格提醒页负责创建、暂停、恢复和删除提醒。v1.24.0 起，Web 总览和日志中心支持自动刷新，操作结果改成可读摘要加原始详情。v1.25.0 起，价格提醒页支持状态/类型/关键词筛选，日志中心会显示筛选摘要和第一条错误。v1.26.0 起，Web API 统一返回元信息，页面操作结果会显示接口耗时和 HTTP 状态，并提供 Web API 自诊断入口。v1.27.0 起，Web 审计记录会记录后台关键操作的时间、动作、对象、结果、耗时和错误摘要。v1.28.0 起，诊断报告页可以一键复制运维快照，便于排查 bug。v1.29.0 起，配置保存前会预检影响模块、自动重启服务和风险提醒。v1.30.0 起，Web 页面失败会显示统一错误卡片，AI 助手和价格提醒支持局部失败展示，不会因为单个接口异常整页空白。v1.30.1 起，诊断报告不会把正常巡检日志里的空 errors 字段当成错误。v1.30.2 起，复制报告和复制日志支持 HTTP fallback，`poll_timeout` 字段名不再误报。v1.30.3 起，Telegram 轮询超时会归类为可自动重试的网络超时。v1.31.0 起，Web 后台每个核心页都有统一说明、标签化摘要、空状态和更清楚的按钮说明，服务控制动作完成后刷新当前页。v1.32.0 起，配置中心每个配置项都会显示“做什么、影响什么、改完是否自动重启”，AI Bot 与价格提醒、结构雷达与行情源不再混在同一个入口里。v1.33.0 起，诊断报告会生成问题中心，把日志和审计信息整理成可操作的问题列表。

v1.34.0 起，AI Bot 稳定性继续收口：按钮回调用短超时静默确认，发送队列会对临时失败自动重试，AI/查价/按钮异常会转换为中文可读提示，价格提醒只有确认进入发送链路后才会标记触发，避免“触发了但消息没发出去”的状态错位。

v1.35.0 是早期稳定版自检基线。Web 诊断报告会显示“稳定版自检”，把服务运行、配置、日志、问题中心和审计记录合成一个结论：达到稳定版标准、基本可运行但建议关注，或未达稳定版标准。最终完整稳定版以 v1.50.0 为准。

v1.36.0 起，可以直接执行 `python main.py stable-check` 查看稳定版验收结果。服务器执行 `paopao update --yes` 后也会自动运行这项检查，更新完成时直接给出“通过 / 有警告 / 未达标”的中文摘要。

v1.37.0 起，`stable-check` 默认会保存验收记录。Web「诊断报告」会显示最近保存的验收状态和历史列表，方便确认上次更新后到底有没有达标。临时查看不想保存时可加 `--no-save`。

v1.38.0 起，Web「诊断报告」顶部新增“问题中心总览”。它会把稳定版自检、问题列表、日志错误、网络超时、失败审计和健康检查合成一个结论，优先告诉你“当前健康 / 需要关注 / 优先处理”。

v1.39.0 起，Web「诊断报告」会显示“处理清单”。如果是服务异常，会给雷达服务入口；如果是日志错误，会给日志中心入口；如果是后台操作失败，会给审计记录入口；如果是配置问题，会给配置中心入口。Web「检查测试」页也可以执行稳定版验收。

v1.39.1 起，Web 控制台收到连接后对方提前断开时，不再把 `ConnectionResetError: [Errno 104] Connection reset by peer` 算作真实错误。真实 Web 异常仍然会进入问题中心。

v1.40.0 起，Web「诊断报告」顶部新增“长期运行就绪度”。它会把当前 stable-check、问题中心、最近验收历史、日志错误、失败审计和网络重试噪声合成一个分数，并直接告诉你当前是“完整稳定版候选”“准稳定候选”还是“需要处理”。如果显示准稳定或需要处理，先按表格里的阻断项/警告项处理，再执行稳定版验收。

v1.41.0 起，`python main.py stable-check` 的普通文本输出会新增“长期运行就绪度”段落。服务器执行 `paopao update --yes` 后，也会在更新结束时直接看到候选状态、评分、阻断项、警告项和下一目标；Web 诊断报告和命令行使用同一套判断。

v1.42.0 起，稳定版验收保存历史时会把“长期运行就绪度”一起写入 `data/stable_check_history.json`。以后 Web「诊断报告」里的验收历史不只看当时稳定版是否达标，也能看到当时是不是完整稳定候选、准稳定候选或需要处理，以及对应评分。

v1.43.0 起，Web「诊断报告」和 `python main.py stable-check` 会显示“长期运行趋势”。它会对比最近两次验收历史的长期就绪度状态和评分，如果从候选状态掉到需要处理，会直接标记“发生回退”。

v1.44.0 起，长期运行趋势不只是展示信息：如果趋势变差或发生回退，它会进入 Web「诊断报告」的问题中心、建议动作和处理清单。更新脚本也会提醒你优先处理趋势告警。

v1.45.0 起，Web「诊断报告」里的处理清单支持问题状态跟踪。每个处理项都有一个稳定问题编号，可以标记“已确认”或“已解决观察中”；状态保存到 `data/problem_state.json`，只用于运维复查，不会改变雷达扫描、推送或 stable-check 的基础判断。

v1.46.0 起，Web「诊断报告」会自动复查这些处理状态：如果你标记“已解决观察中”的问题还在当前处理清单里，会显示“仍然存在”；如果当前处理清单里已经没有它，会显示“已消失待复查”。`stable-check` 保存的历史记录也会记录当时的复查摘要。

v1.47.0 起，项目进入 v1.50.0 完整稳定版收口路线。Web「诊断报告」会显示四个阶段：v1.47.0 功能冻结和稳定性收口、v1.48.0 服务器部署验收闭环、v1.49.0 文档说明和运维流程最终整理、v1.50.0 完整稳定版发布。收口期不新增大模块，只处理现有功能修复、稳定性、验收和文档。

v1.48.0 起，Web「诊断报告」新增“服务器部署验收”。它会单独检查代码版本、后台服务、Web 入口、Telegram/AI 配置、stable-check、日志、审计和部署脚本；`python main.py stable-check` 的命令行输出也会显示同样的部署验收摘要，服务器更新完成后不用再分别猜这些环节是否正常。

v1.49.0 起，Web「功能说明」新增“v1 完整稳定版收口指引”。日常先看总览和诊断报告；更新执行 `paopao update --yes`；排错按诊断报告处理清单从上到下走；配置改错优先用配置中心备份恢复；代码更新异常先复制诊断报告和 stable-check 输出，再按上一个稳定提交处理。

v1.50.0 是 v1 完整稳定版最终发布。发布后 v1 主线进入长期维护：只做 bug 修复、策略微调、文档和运维补丁；新增大模块不再塞进 v1，进入 v2 规划。判断服务器是否达标时，只看 Web「诊断报告」和 `stable-check`：长期运行就绪度为完整稳定版候选、服务器部署验收通过、问题中心无阻断、日志和审计干净，并保留稳定验收历史。

## v1 完整稳定版运维流程

日常检查:

1. 打开 Web 后台总览，确认主服务、结构雷达、Web 控制台和 AI 助手运行中。
2. 打开诊断报告，看长期运行就绪度、服务器部署验收、问题中心和处理清单。
3. 如果显示当前健康或完整稳定版候选，正常观察即可。

更新流程:

1. 服务器执行 `paopao update --yes`。
2. 更新脚本会同步配置、安装依赖、运行测试、清理运行垃圾、刷新后台服务并执行 stable-check。
3. 更新后打开 Web 诊断报告，确认服务器部署验收和长期运行就绪度。

排错流程:

1. 先看诊断报告，不要直接翻大段日志。
2. 服务问题进入“雷达服务”；日志问题进入“日志中心”；失败操作进入“审计记录”；配置问题进入“配置中心”。
3. 处理后重新执行 stable-check，保存新的验收历史。

回滚流程:

1. 配置改错优先到“配置中心 -> 备份恢复”恢复最近 `.env.oi` 备份。
2. 代码更新异常时，先复制诊断报告和 stable-check 输出，确认是代码、配置还是服务器环境问题。
3. 需要回到上一个稳定源码时，按 GitHub 上一个稳定提交处理，不直接删除服务器目录。

完整稳定版标准:

1. 长期运行就绪度为“完整稳定版候选”。
2. 服务器部署验收通过。
3. 问题中心没有阻断项。
4. 近期日志错误和失败审计干净。
5. 至少保留两次达标验收历史。

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

如果需要真正的 AI 问答，再开启兼容 OpenAI 格式的模型接口：

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

Web 控制台的「AI 助手」页面用于查看 `paopao-ai` 服务状态、意图分流和提示词入口；「价格提醒」页面用于查看提醒统计、新增 Web 提醒、按状态/类型/关键词筛选提醒，并暂停、恢复或删除提醒。Web 创建提醒需要填写接收提醒的 Telegram 用户 ID，或者先配置 `AI_DEFAULT_CHAT_ID`；从 Telegram 私聊创建提醒会自动识别当前私聊。`SIGNAL_EVENTS_FILE` 继续给 AI 币种档案读取旧 JSON 索引；`SIGNAL_EVENTS_DB_FILE` 是 Web「信号推送」页使用的结构化 SQLite 记录，通常保持默认即可。v1.60.1 增加 `signals.db` 兼容视图 `signal_events`，实际写入表仍为 `signals`，旧验收 SQL 和人工排查可以查询 `signal_events`。v1.61.0 起新增 `data/jobs.db` 后台任务库，稳定版验收、doctor、readiness、cleanup、更新检查和 Web API 自检进入 Web「任务中心」异步执行；任务详情只保存脱敏后的 stdout/stderr tail。v1.62.0 起任务中心增加任务统计、失败摘要、复制任务报告、重跑、旧任务清理和同类型长任务并发保护；failed/timeout 任务会进入诊断报告和问题中心。v1.62.1 起 stable-check 返回码 1 会显示为“关注”而不是失败，适合网络超时等非阻断观察项。更新检查会结构化显示当前版本、远端版本和建议动作。真正更新代码仍建议在服务器执行 `paopao update --yes`，避免 Web 自更新时重启自身。

Web 控制台的「AI 助手」页提供「编辑 AI 提示词」入口，可以编辑泡泡 AI 助手提示词和专业分析师提示词。泡泡 AI 助手用于日常问答、生活问题、状态解释和提醒说明，默认语气更轻松；专业分析师用于 `分析这段：...`、`帮我分析...` 以及自动识别出的雷达/市场数据。提示词默认保存在 `data/ai_prompts.json`，保存后会自动重启 `paopao-ai`。

## 闭合窗口参数

涉及 OI、CVD、K 线涨跌的雷达会按“上一完整收线窗口”计算，避免刚整点时抓到未收完的数据。资金流雷达的 CVD 来自 Binance K 线主动买入成交额估算：

```bash
RADAR_SUMMARY_MIN_INTERVAL_SEC=21600
RADAR_SUMMARY_CLOSE_DELAY_SEC=300
FLOW_INTERVAL_SEC=3600
FLOW_CLOSE_DELAY_SEC=300
LAUNCH_CLOSE_DELAY_SEC=60
STRUCTURE_PRE_SCAN_MINUTE=55
STRUCTURE_CONFIRM_DELAY_SEC=300
```

## 结构突破雷达 v1.8

单次 dry-run：

```bash
python main.py structure-radar --mode pre --top-symbols 80 --min-score 65 --save-charts
python main.py structure-radar --mode confirm --top-symbols 80 --min-score 65 --save-charts
```

独立循环：

```bash
python main.py structure-loop
```

真实推送仍必须显式确认：

```bash
python main.py structure-radar --mode pre --send --confirm-real-send
```

默认提前临界扫描在每小时 55 分附近运行，收线确认在整点后延迟 5 分钟运行。图片保存到 `data/charts/`，结构雷达状态保存到 `data/structure_state.json` 和 `data/structure_history.json`。
真实 Telegram 图片发送成功后默认会立即删除本地 PNG；dry-run 和发送失败的图片会暂时保留，并由 cleanup 按保留时间和数量上限清理：

```bash
STRUCTURE_DELETE_CHART_AFTER_SEND=true
STRUCTURE_CHART_RETENTION_HOURS=12
STRUCTURE_MAX_CHART_FILES=200
```

## 结构信号复盘 v1.8.3

结构雷达会把本轮信号写入 `data/structure_review.json`，后续通过 K 线渐进复盘 15m、1h、4h 后价格变化、有效突破、假突破、MFE/MAE，并生成聚合统计。

```bash
python main.py structure-review
python main.py structure-review --lookback-hours 24
python main.py structure-review --send --confirm-real-send
```

复盘报告保存到 `data/structure_review_report.txt`，聚合统计保存到 `data/structure_stats.json`。结构雷达同币种后续信号默认会回复上一条该币结构消息，形成 Telegram 追踪链。

```bash
STRUCTURE_REPLY_CHAIN_ENABLE=true
STRUCTURE_REVIEW_ENABLE=true
STRUCTURE_REVIEW_LOOKBACK_HOURS=24
STRUCTURE_REVIEW_FORWARD_HOURS=4
STRUCTURE_REVIEW_MIN_AGE_MINUTES=15
STRUCTURE_REVIEW_MAX_REPORT_INTERVAL_SEC=3600
```

## 结构雷达外部确认

结构雷达外部确认使用 Binance 免费合约盘口深度，可选叠加 Coinalyze 历史清算量。它只增强结构雷达，不替代原有结构算法。

本地测试：
```bash
python main.py structure-radar --mode pre --save-charts
```

增强字段包括上方卖墙、下方买墙、流动性缺口、清算历史方向辅助和分数修正。分数修正默认限制在 `-15 ~ +15`。

```bash
LIQUIDITY_FALLBACK_ENABLE=true
LIQUIDITY_SCORE_MAX_DELTA=15
LIQUIDITY_MIN_DISTANCE_PCT=0.5
LIQUIDITY_MAX_DISTANCE_PCT=8.0
BINANCE_ORDERBOOK_LIQUIDITY_ENABLE=true
BINANCE_ORDERBOOK_DEPTH_LIMIT=100
COINALYZE_ENABLE=false
COINALYZE_API_KEY=
```

流动性增强默认读取 Binance 免费合约盘口深度快照。可选配置 Coinalyze 免费 API Key 后，清算侧会补充 Coinalyze 历史清算量作为方向辅助；它不是预测清算池，推送里会标明数据源。

推送里的外部确认状态会使用中文解释：清算磁吸说明上方/下方清算池哪边更近或更强；盘口流动性说明当前是否识别到明显买墙/卖墙；流动性缺口说明订单簿哪一侧阻力或支撑更薄。Binance 免费盘口降级只读取当前深度快照，不是历史盘口热力图；如果订单挂单分散、距离不在配置范围内，或没有明显集中墙，就会显示“暂无有效买墙/卖墙”。

## v1.9.4 服务、公告和清理增强

更新脚本会安装/刷新两个 systemd 服务和一个清理 timer，即使当前代码已经是最新版，也会继续补装服务、刷新快捷命令并重启已安装服务：

```bash
paopao-radar      # 主服务：资金摘要、启动雷达、公告、资金流等
paopao-structure  # 结构雷达独立循环：55 分预警，整点后 5 分确认
paopao-web        # Web 控制台：状态、日志、配置和维护操作
paopao-ai         # AI 助手 Bot：私聊问答、手动价格提醒、个人提醒
paopao-cleanup.timer # 每小时自动清理运行垃圾
```

服务器快捷入口：

```bash
paopao
```

进入中文菜单后按数字查看 Web 地址/令牌、Web 服务状态、Web 实时日志、重启 Web 服务、检查更新、更新项目和查看版本。

Binance 公告抓取默认每个分类分页读取，单页数量从 20 提高到 50，并新增活动关键词识别。专门测试公告抓取和分类：

```bash
python main.py announcements-test
```

相关配置：

```bash
ANNOUNCEMENT_PAGE_SIZE=50
```

## 一键更新

```bash
bash scripts/update_server.sh
```

更新脚本每次运行后会自动执行一次安全清理：同步 `.env.oi`、清理 pycache/临时文件/过期日志/过期结构图/根目录临时报告，再重启服务。脚本还会安装/刷新 `paopao-structure.service`、`paopao-web.service`、`paopao-ai.service` 和 `paopao-cleanup.timer`。清理不会删除 `.env.oi`、`data/*.json` 状态文件、README、`docs/INSTALL_CN.md` 或源码。

## 安全规则

真实 Telegram 推送必须同时带:

```bash
--send --confirm-real-send
```

`.env.oi` 和 `data/` 状态文件不应提交到 GitHub。

更详细的安装、更新、配置和排错说明见 [docs/INSTALL_CN.md](docs/INSTALL_CN.md)。

## 中文安装目录

第一次安装、重新安装、配置项说明和常见排错见 [docs/INSTALL_CN.md](docs/INSTALL_CN.md)。

修改 bot token、群 ID、Coinalyze key 或 Telegram 话题配置，推荐在 Web 控制台的“配置”页完成。服务器命令行保留应急配置向导:

```bash
bash scripts/install_server.sh config
```

服务器安装后会写入快捷命令:

```bash
paopao
```

输入后会打开中文数字菜单。菜单里会详细说明正式访问入口、后台登录配置状态、项目版本，以及每个编号的用途；日常使用不需要记其它长命令。默认菜单不会明文打印后台密码、密码哈希、会话密钥或旧访问令牌。

中文菜单里的“更新项目代码”会在拉取新代码后安全同步 `.env.oi`：新增的普通配置项会自动补上，明确列入迁移白名单的默认参数会自动升级；`TG_BOT_TOKEN`、`TG_CHAT_ID`、`COINALYZE_API_KEY` 和各类话题 ID 不会被覆盖。

项目版本号写在 `VERSION` 文件里，当前为 `v1.63.0`；中文菜单检查/更新时会同时显示版本号和 git 提交号。
## v1.63.0 Web Platform API Core

v1.63.0 adds the Web Platform API Core for the next Web v2.0 stages. It introduces shared API response helpers, pagination/filter/sort/time-range parsing, normalized symbol filters, and the lightweight `/api/dashboard` aggregation endpoint. The `api-self-test` job now runs an API contract self-test for dashboard, signals, jobs, job stats, and update status. This is a backend foundation release for future Signals, Coin Detail, Timeline, and Dashboard work; it does not trigger scans and does not change the Telegram push path.
## v1.75.1 说明

v1.75.1 修复公开前台首页的生产超时提示噪声：当某个非核心 public API 请求偶发超时，但首页已经拿到其它真实数据时，不再在顶部显示“部分数据暂时不可用”。只有所有核心首页数据都不可用时，才显示“公开数据暂时不可用”错误提示。

同时，`paopao-frontend.service` 增加 `PAOXX_PUBLIC_API_TIMEOUT_MS=15000`，服务端读取本机 `/public-api/*` 的默认超时从 8 秒提高到 15 秒；首页最新信号改为直接读取稳定的 `/public-api/signals`，避免先探测可选 alias 造成额外等待。本版本不改 Telegram 主推送流程，不引入自动交易，不改数据库 schema，也不改后端 API contract。

## v1.75.0 说明

v1.75.0 对 Next.js 公开前台做真实数据水合和体验打磨。首页、信号雷达、决策模型、结果追踪、决策回测、单币详情和公开 API 页面继续只读取 `/public-api/*`，但现在会通过统一的 `frontend/lib/api.ts` 处理 `ok + data`、顶层 `items/summary`、非 2xx、JSON 解析失败、超时和空数据，页面会显示中文的加载、空状态、错误和重试提示。

生产环境的 `paopao-frontend.service` 新增 `PAOXX_PUBLIC_API_INTERNAL_BASE=http://127.0.0.1:8080`，Next.js 服务端渲染优先从本机 Python 后端读取公开 API，浏览器端仍使用同域相对路径 `/public-api/*`。公开前台不访问 `/api/*`，不读取后台 Cookie，不写入 Authorization，不包含任何后台 token 或 secret。

前台体验补齐：总览页展示今日信号数、风险警报、可试仓、等待回踩、结果追踪和回测摘要；各业务页支持真实筛选、中文错误/空状态、手机横向导航、单币 BTC/BTCUSDT 归一化查询、中文 404 和中文页面错误提示。本版本不改 Telegram 主推送流程，不引入自动交易，不改数据库 schema，也不改后端 API contract。
