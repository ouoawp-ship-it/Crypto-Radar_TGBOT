# Mercu 风格驾驶舱 V2 · Phase 4 工程验收与运行手册

## 1. 目标

Phase 4 把 V2 从“功能可用”收口为“可灰度、可观测、可回滚、可迁移、可验收”。本手册定义发布前门禁、上线步骤、事故回滚、SSE 运维、数据保留和故障处置。

当前记录仅代表本地工程版本；在获得单独批准之前，不提交 GitHub、不合并、不部署服务器。

## 2. 运行模式

环境变量 `PAOXX_COCKPIT_V2_MODE`：

| 模式 | 前台行为 | V2 API | 旧信号/Bot/后台 |
| --- | --- | --- | --- |
| `enabled` | 正常显示 V2 导航和页面 | 启用 | 保持启用 |
| `preview` | 显示 V2，并标记 PREVIEW | 启用 | 保持启用 |
| `disabled` | 隐藏 V2 专属导航，雷达使用旧信号页 | 返回 `feature_disabled` | 保持启用 |

开关同时影响 Python 运行时和 Next.js 编译结果。每次修改都必须重建前台并重启 Web/前台服务，禁止只重启其中一端。

## 3. 发布门禁

合并或部署前必须全部满足：

1. `python -m unittest discover -s tests -p "test_*.py"` 通过；
2. `npm run typecheck`、`npm run build`、`npm run e2e` 通过；
3. `enabled` 和 `disabled` 两种前台生产构建均通过；
4. Bash 语法检查通过：`install_server.sh`、`update_server.sh`、`check_https_deploy.sh`；
5. SQLite 旧库迁移、去重、索引和保留策略测试通过；
6. 公开 API 不包含密钥、密码、Cookie、IP、内部路径和异常正文；
7. SSE 握手、心跳、重连、增量刷新、暂停和轮询兜底通过；
8. 360/390/768/1280px 关键页面无横向溢出，键盘焦点可见；
9. `git diff --check` 无空白错误，秘密扫描无命中；
10. 变更清单不包含用户自有文档、导出素材、运行数据库和 `.env.oi`。

## 4. 推荐灰度流程

### 4.1 上线前备份

```bash
cd ~/paopao-crypto-radar
mkdir -p ~/paopao-backups
tar -czf ~/paopao-backups/pre-v2-$(date +%Y%m%d-%H%M%S).tar.gz .env.oi data
git rev-parse HEAD
```

备份必须留在仓库外或受保护目录，不能提交 Git。

### 4.2 Preview 部署

```bash
cd ~/paopao-crypto-radar
sed -i 's/^PAOXX_COCKPIT_V2_MODE=.*/PAOXX_COCKPIT_V2_MODE=preview/' .env.oi
paopao update --yes || bash scripts/update_server.sh --yes
bash scripts/check_https_deploy.sh --with-stable-check
```

观察至少一个完整业务周期，重点检查真实 Telegram 推送、公开 API P95、SSE 错误、上游失败、数据库增长、Web 审计和后台失败任务。

### 4.3 正式启用

```bash
sed -i 's/^PAOXX_COCKPIT_V2_MODE=.*/PAOXX_COCKPIT_V2_MODE=enabled/' .env.oi
bash scripts/update_server.sh --yes
bash scripts/check_https_deploy.sh --with-stable-check
```

如果证书续期环境已修复，可额外执行 `--with-certbot-dry-run`。证书 dry-run 失败属于独立部署阻断，不能用 V2 功能验收掩盖。

## 5. 紧急回滚

### 5.1 功能开关回滚（首选）

```bash
cd ~/paopao-crypto-radar
sed -i 's/^PAOXX_COCKPIT_V2_MODE=.*/PAOXX_COCKPIT_V2_MODE=disabled/' .env.oi
bash scripts/update_server.sh --yes
bash scripts/check_https_deploy.sh --with-stable-check
```

随后验证：

- `/public-api/signals` 返回 `ok=true`；
- `/radar` 显示旧信号雷达；
- V2 专属页面显示功能已回滚，而不是白屏；
- Telegram 实际测试消息与主雷达推送正常；
- `/admin` 可登录，日志/审计/任务中心可用。

### 5.2 代码版本回滚

只有功能开关无法止损时才回到上一个已验收提交。先保留当前日志和数据库副本，再由维护者选择明确提交执行更新；不得使用 `git reset --hard` 覆盖服务器上的未知改动。

## 6. SSE 运行要求

`/public-api/stream` 使用 SSE：

- Nginx 关闭代理缓冲与缓存，使用 HTTP/1.1，读取超时至少 70 秒；
- 服务端只发送脱敏的稳定信号引用，不在事件中广播完整敏感上下文；
- 无 `Last-Event-ID` 的新连接从最新位置开始，避免重放全部历史；
- 客户端收到 signal 后强制绕过短期缓存，并将新条目放入“待更新”提示；
- 连接失败显示 RECONNECTING，用户可暂停，30 秒轮询始终作为兜底；
- `/public-api/health` 记录打开、关闭、活动和错误计数，不记录客户端身份。

## 7. 数据迁移与保留

- `market_snapshots.db`：启动时自动补齐新字段、去重历史事实并创建唯一索引；
- `news_events.db`：默认保留 90 天且最多 5000 条，清理事件时同步删除孤立币种关联；
- `agent_insights.db`：保留结构化 Agent 结果、规则版本、生成和过期时间；
- 所有生产数据库均为运行数据，不提交 Git；
- 迁移前后应记录文件大小、表行数和最新时间戳，禁止把缺失字段回填成虚构事实。

## 8. 事故排查顺序

1. 检查 `systemctl status` 五个服务；
2. 检查 `/public-api/health` 的缓存、P95、SSE 和错误聚合；
3. 检查后台日志中心、审计记录和失败任务；
4. 判断故障属于上游数据、SSE/Nginx、前端构建、数据库迁移还是 Telegram 发送；
5. 单一 V2 故障优先切 `disabled`，不要停止 Bot 主服务；
6. 保存日志和验收结果后再修复；
7. 修复后先 `preview`，不要直接恢复 `enabled`。

## 9. 本地工程验收记录

本阶段已实现并验证：

- V2 三态灰度和旧雷达回退；
- SSE 服务端投影、续传、心跳、指标和 Nginx 专用反代；
- 浏览器端 LIVE/RECONNECTING/PAUSED、新事件提示和缓存一致性修复；
- 新闻保留、市场旧库迁移和孤立关系清理；
- 全局键盘焦点、移动端导航和响应式关键页；
- 部署脚本 V2 API 响应预算、SSE 握手和 rollback-aware 检查；
- API、安装、测试域与本运行手册文档。

最终测试数量、构建结果和安全检查在本地全量验收完成后写入交付摘要，不在此处硬编码可能过期的数字。
