# BOT-only 架构边界

## 保留模块

| 模块 | 作用 |
| --- | --- |
| `data_sources.py` / `funding_sources.py` | 交易所 REST 数据、缓存、限流与降级 |
| `realtime_market.py` / `realtime_intelligence.py` | 多交易所成交、清算、CVD 与实时异动 |
| `radar.py` | 资金摘要、启动预警和公告分类 |
| `flow_radar.py` | 多因子资金流信号 |
| `funding_alert.py` | 极端资金费率与跨所分歧 |
| `market_cockpit.py` | BOT 需要的市场快照与窗口比较；不再对外提供网页 API |
| `bot_market_context.py` | 给 Telegram 推送补充实时行情、新闻与市场证据 |
| `signal_store.py` / `symbol_dossier.py` | 信号事实、生命周期和币种上下文 |
| `telegram.py` | 推送、话题路由、去重、冷却、限流与重试 |
| `cli.py` | 运维命令、readiness 与安全发送门禁 |

## 已移除边界

- Next.js 前端、Playwright 和视觉基准。
- Python Web/API/SSE 服务与管理后台。
- 用户、登录、收藏、主题与浏览器遥测。
- 独立 AI 助手和 AI 价格提醒服务。
- Web 任务队列、Web 鉴权与 Web-only 聚合接口。
- Web/Frontend/AI systemd 服务和网站发布流程。

`market_cockpit.py` 名称暂时保留，因为它是 Telegram 市场上下文的持久化计算层；改名只会制造无价值的大范围改动。

## 生产进程

```text
paopao-market-stream
    └─ 写入 realtime_features.db

paopao-radar
    ├─ 扫描 REST / 公告 / 资金费率
    ├─ 读取实时与历史上下文
    ├─ 生成、去重并记录信号
    └─ 推送 Telegram
```
