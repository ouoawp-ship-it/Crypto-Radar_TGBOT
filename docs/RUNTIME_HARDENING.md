# 运行时加固

## 网络边界

- 公网只开放 SSH、80 和 443。
- Next.js 仅监听 `127.0.0.1:3000`。
- Python 后端 8080 只允许 Nginx 或可信内网访问。
- 后台 `/api/*` 使用密码会话认证，写操作同时校验 CSRF。
- 公开 `/public-api/*` 只提供脱敏后的信号数据。

## 进程边界

主雷达、结构雷达、Web、前台和 AI 助手使用独立 systemd 服务。单个模块异常不应直接终止其他服务。后台任务通过 `jobs.db` 留存状态、输出尾部和错误摘要。

## 数据边界

- 配置与密钥只保存在 `.env.oi`。
- 结构化信号保存在 `signals.db`。
- 后台任务保存在 `jobs.db`。
- 价格提醒保存在 `price_alerts.db`。
- 数据库、日志、推送历史和运行状态不提交到 Git。

## 缓存与限流

- 公共信号接口使用短 TTL，避免前台刷新产生重复查询。
- Telegram 推送保留去重、冷却、每小时总量和重试限制。
- 网络请求保留超时、退避、缓存和熔断设置。
- 后台任务同类型并发提交会复用已有排队或运行任务。

## 验收

```bash
python main.py doctor
python main.py readiness
python main.py stable-check
bash scripts/check_https_deploy.sh --with-stable-check
```

同时确认 systemd 服务、Nginx 反代、公开信号 API、后台 401 门禁、日志与审计记录均符合预期。
