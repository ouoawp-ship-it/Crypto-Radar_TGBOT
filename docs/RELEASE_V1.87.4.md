# v1.87.4 生产安全响应头归一化

## 原因

v1.87.2 生产复查发现 Nginx 与上游 Next.js/Python 同时发送安全响应头，公网响应会把同名值合并为重复值，例如 `nosniff,nosniff`、`DENY,DENY` 和两个 HSTS 值。浏览器通常仍能解析，但这会造成代理层职责不清、验收误报和后续策略漂移。

## 修复

- Nginx 反向代理在入口统一隐藏五个上游安全响应头，再由 HTTPS server 块各发送一次。
- 安装脚本和更新脚本使用相同规则，避免新装与升级结果不一致。
- HTTPS 验收不再只检查“至少存在”，而是要求 API 的 `nosniff`/`DENY` 与前台的 `nosniff`/`DENY`/HSTS 各恰好出现一次。

## 部署影响

公开 API、前端页面和业务逻辑不变。更新脚本会重写 active Nginx 配置并安全 reload；部署后重新运行 HTTPS 验收即可验证归一化结果。
