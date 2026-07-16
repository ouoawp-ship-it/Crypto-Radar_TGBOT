# v1.87.0 工程级信号工作台

## 发布目标

把“自动扫描与推送系统”升级为“异常机会雷达与信号验证工作台”，组合 Mercu 的异常情报表达、DeepCat 的单币上下文与泡泡雷达的自动推送/运维闭环，同时保持不自动交易、不恢复研究型 Web 平台的边界。

## 主要变化

- 信号库 schema v3：为每条信号增加稳定、不可枚举的 `public_ref`，旧数字 ID 保持兼容。
- Telegram 单币推送直接打开对应 Web 信号详情；Web 可跳转 AI 分析和预填币种的提醒流程。
- 新增市场 snapshot、信号 context、情报排名、单币上下文、自选批量快照和公开健康接口。
- 雷达新增三类排名、五窗口跨模块共振、六态生命周期和四类机会榜。
- 新增 `/coin/[symbol]` 与 `/watchlist`；自选仅保存在浏览器 localStorage。
- 公开 API 增加普通/聚合分级限流、可信代理校验、安全响应头、P95、缓存与匿名错误计数。
- CI 同时执行 305 项 Python 回归、TypeScript、production build、Chromium 1440px/768px/360px/自选闭环和 npm 高危漏洞门禁。

## 新配置

```dotenv
PAOXX_PUBLIC_BASE_URL=https://paoxx.com
AI_BOT_USERNAME=
PUBLIC_API_RATE_LIMIT_PER_MINUTE=180
PUBLIC_API_HEAVY_RATE_LIMIT_PER_MINUTE=30
PUBLIC_API_TRUSTED_PROXY_IPS=127.0.0.1,::1
```

更新脚本会保留现有 `.env.oi` 值。若未配置 `AI_BOT_USERNAME`，Web 仍可正常工作，只隐藏 AI/提醒深链按钮。

## 数据迁移

首次访问 SQLite 信号库时自动升级到 schema v3：

- 新信号的 `public_ref` 由 dedup key 与 symbol 确定，重写同一信号不会改变引用。
- 旧信号按原 dedup key 与 symbol 一次性补齐同一套确定性稳定引用。
- 原信号、Telegram message ID、去重索引和数据目录不删除。

## 发布门禁

1. Python 全量测试和 compileall。
2. 前端 typecheck、production build、Playwright E2E、npm audit。
3. GitHub Actions 全绿后合并。
4. 服务器运行 `paopao update --yes`。
5. 运行 `bash scripts/check_https_deploy.sh --with-stable-check`。
6. 验证 `/public-api/health`、安全响应头、五个 systemd 服务、公开/后台页面和私有 API 401。

## 回滚

若发布出现阻断：

1. 保留 `data/` 与 `.env.oi`，不要删除 SQLite 数据库。
2. 切回上一已验证 Git 提交并重新运行更新脚本。
3. Nginx 使用脚本创建的备份配置恢复并执行 `nginx -t`。
4. 重新运行 HTTPS 验收；确认 Telegram 主推送不受影响。

schema v3 对旧代码的主要风险是旧版本不认识 `public_ref`，但旧查询所需列仍保留；回滚前应先备份 `data/signals.db`。
