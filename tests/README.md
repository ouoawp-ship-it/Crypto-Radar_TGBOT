# 测试范围

测试套件只覆盖 Telegram BOT 运行时：

- 行情数据源、资金费率、实时成交与清算。
- 资金雷达、启动预警、公告风险和信号生命周期。
- Telegram 去重、冷却、路由、重试与持久化。
- 市场快照、BOT 上下文、数据质量和降级路径。
- CLI 安全门禁、BOT-only 部署脚本和配置同步。

运行：

```bash
python -m unittest discover -s tests -p 'test_*.py'
```

Web、Next.js、Playwright、用户系统和独立 AI 助手测试已随对应产品代码移除。
