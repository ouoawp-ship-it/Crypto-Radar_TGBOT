# 五雷达模块边界

主项目按业务拆成五个独立目录，但仍由一个主进程调度，并共用一个 Telegram
机器人。此次拆分只调整代码职责，不改变已有评分、阈值、状态文件和真实发送门禁。

```text
radars/
├─ launch_warning/radar.py      启动预警
├─ market_summary/radar.py      资金摘要
├─ funding_alert/radar.py       资金费率警报
├─ capital_flow/radar.py        五因子资金流
└─ announcement_risk/radar.py   公告风险
```

## 共用部分

- `shared/telegram.py`：唯一推送入口，负责话题、去重、冷却、限流和真实发送双门禁。
- `shared/binance_data.py`：统一 Binance 数据访问。
- `radars/common.py`：只放多个雷达都会使用的纯计算和排版辅助函数。
- `shared/funding_presentation.py`：启动预警和资金费率雷达共用的费率表格排版。
- `runtime/radar_engine.py`：统一调度五个雷达，不放具体评分算法。
- `config/settings.py`：读取并校验公共配置。

## 隔离原则

1. 每个雷达的算法只放在自己的目录。
2. 一个雷达不能导入另一个雷达的实现文件；需要共用的纯函数上移到公共模块。
3. 五个雷达分别记录最近运行、下次运行、候选数和错误码。
4. 公告风险失败不会停止启动预警、资金摘要、资金费率或资金流。
5. 公告可作为启动预警辅助证据，但永远不改变启动分数。
6. 旧的 `paopao_radar` 包已移除；源码只保留一套权威路径。
7. 状态文件保持原路径，避免升级后重复推送或丢失生命周期。

## 后续优化顺序

先逐个优化，不同时改五套算法：启动预警 → 资金流 → 资金费率 → 资金摘要 →
公告风险。每次只修改一个雷达目录和必要的共用接口，并运行全量回归测试。
