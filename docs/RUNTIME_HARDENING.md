# Runtime Cache & File Lock Hardening

本文记录 v1.76.4 的运行稳定性加固。本版本只处理文件并发写入、Dashboard 短缓存和公开前台请求去重，不新增业务功能。

## 原子 JSON 与文件锁

`paopao_radar.atomic_json` 提供以下通用能力：

- `locked_read_json(path, default)`：在目标文件锁内读取 JSON，文件不存在或损坏时返回默认值。
- `locked_write_json(path, data)`：在锁内写入同目录临时文件，执行 `flush + fsync` 后用 `os.replace` 原子替换。
- `locked_update_json(path, update_fn, default)`：在同一个锁区间内完成 read-modify-write，避免并发更新丢失。
- `append_jsonl(path, record, max_lines=None)`：提供 append-only JSONL 写入，并兼容首次读取旧 JSON 数组。
- `atomic_write_text(path, text)`：为普通文本提供相同的原子替换保证。

锁文件位于 `<target>.lock`。Linux 生产环境使用 `fcntl.flock`；Windows 使用 `msvcrt.locking`，并保留进程内线程锁作为兼容保护。临时文件使用 `<target>.tmp.<pid>`，与目标文件位于同一目录。

现有 JSON 数组无需强制迁移。Telegram 推送历史继续兼容旧数组：非 sent 审计记录使用 1000 条总容量内的剩余空间，仍参与 cooldown、小时限额和每日限额的 sent 决策记录不会被审计噪声挤出；旧信号事件历史上限为 500 条；stable-check 历史保持既有 30 条上限。它们的写入均在单个文件锁内完成，不会因 stale read/save 丢失并发记录。上述改造不改变 Telegram 的发送条件、去重规则、消息内容或推送节奏。

## Dashboard 短缓存

`paopao_radar.runtime_cache` 是线程安全的进程内 TTL 缓存，提供 `get_or_set`、`invalidate`、`clear` 和 `stats`。同一 key 的并发调用共享一次 loader；TTL 到期后刷新；loader 抛错时不写入失败结果；缓存失效期间尚未完成的旧 loader 也不会重新污染缓存。

当前 TTL：

| 数据 | TTL |
|---|---:|
| systemd 服务 active/enabled 状态 | 5 秒 |
| Git 版本、commit、branch、最近提交 | 30 秒 |
| stable-check 最近结果与历史摘要 | 10 秒 |

服务启动、停止或重启，配置保存或恢复，更新检查，stable-check、doctor、readiness、cleanup 等任务开始和完成后，会主动失效相关缓存。日志正文、token、Cookie、Authorization、chat/topic/message id 和其他敏感配置不进入缓存。

连续两次 Dashboard 请求的验收标准是：第一次正常采集 systemctl/Git 信息，TTL 内第二次不新增这些子进程调用。

## 公开前台请求缓存

Next.js API client 只允许 `/public-api/*` 使用短缓存：

| 数据 | revalidate |
|---|---:|
| 普通列表、生命周期概览 | 10 秒 |
| summary/stats | 15 秒 |
| 回测与 Outcome 统计 | 30 秒 |

稳定排序后的 URL 与查询参数作为 in-flight key，同一资源的并发请求复用一个 Promise。SSR 首屏继续读取真实服务端数据；浏览器请求和显式 `bypassCache` 使用 `no-store`，因此用户主动刷新可以绕过缓存。后台私有 `/api/*` 同时受到 TypeScript 类型和运行时路径检查的隔离，不进入此缓存。

## Git 与运行文件边界

`.env`、`.env.*`、`*.bak`、`*.bak.*`、运行数据库、日志、`.next`、`node_modules` 和运行期 `.lock` 文件均被忽略；`.env.example` 与 `.env.oi.example` 显式保留。服务器已有 `.env.oi.bak.*` 不会被删除，只是不再显示为待提交文件。

## 本地验收

```bash
python -m compileall paopao_radar
python -m unittest discover -s tests
git diff --check

cd frontend
npm run typecheck
npm run build
```

Phase 2 小规模回归：

```bash
python scripts/benchmark_funding_scan.py --symbols 30 --latency-ms 2 --concurrency 8
python scripts/benchmark_outcome_scan.py --symbols 30 --request-delay-ms 2
python scripts/benchmark_lifecycle_phase2.py --symbols 30 --repeats 2 --provider-delay-ms 0.1
python scripts/benchmark_api_phase2.py --symbols 6 --rows-per-symbol 4 --blob-bytes 1000 --samples 5
```

除绝对耗时外，还必须确认 Funding 峰值并发接近 8、Outcome 保持单批事务、Lifecycle 保持低连接数、Signals 列表不带回大字段，以及 Decision 每请求数据库连接数仍为 1。

## 生产验收

```bash
cd /home/ubuntu/paopao-crypto-radar
paopao update --yes || bash scripts/update_server.sh --yes
cat VERSION
git rev-parse --short HEAD
git status --short
bash scripts/check_https_deploy.sh --with-stable-check
```

还需确认全部 systemd 服务 active、`nginx -t` 无 conflicting server name、公开前台 marker 存在、公开 API 返回 `ok=true`、私有 API 未登录返回 401，以及 web/frontend/radar 日志无阻断错误、公开响应无敏感字段。

## 明确边界

- 不改变 Telegram 主推送流程或语义。
- 不改变生命周期评分和 decision model 阈值。
- 不改变 `/api` 鉴权或 `/public-api` contract。
- 不改变数据库核心 schema。
- 不引入自动交易或交易所下单 API。
