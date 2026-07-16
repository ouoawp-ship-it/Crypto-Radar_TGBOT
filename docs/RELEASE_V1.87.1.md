# v1.87.1 生产性能与安全响应热修

## 原因

v1.87.0 在真实生产信号量下完成部署后，外网验收发现两个本地模拟数据未暴露的问题：

- `/public-api/radar/intelligence` 返回约 1.71 MB，60 秒仍未下载完成，雷达首屏会等待前端 15 秒超时；
- Python API 已返回安全头，但 Next.js 前台 HTML 未稳定继承 Nginx server 级安全头。

这两个问题不影响 Telegram 扫描与推送服务，但会影响 Web 雷达可用性，因此作为发布阻断处理。

## 修复

- 信号列表改为单一 `data` 信封，不再在顶层重复序列化 `items`。
- 卡片 display 改为字段和长度白名单，去除未使用 badges 与重复长文本。
- 情报接口支持 `refs`，最多只返回当前 40 条信号对应的紧凑情报；机会榜仍保留。
- 信号详情继续从内部完整情报读取排名方法、生命周期依据和共振解释，不牺牲可解释性。
- 公开 API JSON 使用紧凑序列化。
- Next.js 对所有前台路由直接设置 nosniff、DENY、Referrer-Policy、Permissions-Policy 与 HSTS。
- Nginx 模板启用 JSON/文本 gzip，并补充 HSTS。
- HTTPS 验收新增前台 HTML 安全头和情报接口 256 KiB/超时预算门禁。

## 兼容性

- 数字信号 ID 与 `public_ref` 均继续支持。
- API 业务字段不变；`/public-api/signals` 的分页元数据统一移动到 `data` 内。
- Telegram、AI Bot、SQLite schema v3 和浏览器 localStorage 自选无需迁移。

## 验收

1. Python 全量测试、compileall。
2. TypeScript、Next.js production build、npm audit、四条 Chromium E2E。
3. GitHub Actions 全绿后合并。
4. 服务器更新到 v1.87.1。
5. `bash scripts/check_https_deploy.sh --with-stable-check --with-certbot-dry-run` 阻断为 0。
6. 外网复测情报接口体积、耗时、前台安全头和桌面/移动核心链路。
