# v1.90.0

## 主要变化

- 新增 Binance、Bybit、OKX 公共 WebSocket 实时市场采集，按交易所持久化封闭分钟成交、CVD、OHLC 与可公开获得的强平事实。
- 新增 Surge 加速、短周期潜伏、5m/15m/1h 方向共振、三类排名和异常生命周期。
- 新增有最小样本门槛的 5m/15m/1h 离线方向结果统计，不恢复通用回测或交易执行平台。
- 雷达榜单在实时数据就绪时追加 CVD、强平、Surge 与短周期潜伏；实时失败时继续保留 REST 榜单。
- 新增独立 `paopao-market-stream` systemd 服务、逐交易所健康检查和生产验收门禁。

## 数据与升级

- `data/realtime_features.db` 自动创建；旧版实时表会幂等补齐分钟 OHLC 字段。
- 新增环境变量会由更新脚本从 `.env.oi.example` 同步；Bybit/OKX 默认启用且不需要 API Key。
- 如果服务器所在区域不能访问某个公共流，可显式设置 `REALTIME_BYBIT_ENABLE=false` 或 `REALTIME_OKX_ENABLE=false`；不能在启用状态下忽略其健康失败。

## 验收口径

- Python 全套单元/集成测试、TypeScript、Next.js 生产构建和 Playwright 桌面/移动端流程必须通过。
- 发布后至少等待两个完整分钟桶，并确认 `/public-api/health` 中所有已启用实时交易所为 `ready`。
- `scripts/check_https_deploy.sh --with-stable-check` 会把实时服务缺失、非 active 或实时分钟特征未就绪作为阻断项。
