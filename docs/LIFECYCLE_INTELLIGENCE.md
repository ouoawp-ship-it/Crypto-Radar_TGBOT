# Lifecycle Intelligence

## v1.78.1 Outcome 置信度口径

智能评价从持久化的 lifecycle Outcome coverage 读取关联状态、成熟度、已成熟 horizon、尚未到期 horizon 与不可用 horizon。缺失、pending 或 not_due 不会扣减既有 `intelligence_score`；成熟样本不足只降低 `confidence_label`。模型权重和既有生命周期评分规则保持不变。

历史统计与相似案例只把已经到期且 `data_status=success` 的 Outcome 放入收益分母。pending/not_due 不参与收益统计，unavailable 单独计数；成熟样本不足时返回 `insufficient_mature_samples`，不会显示虚假的 0% 成功率。

## 模型边界

`lifecycle-intelligence-v1` 生成独立的 `intelligence_score`（0–100）以及质量、阶段、动能、资金确认、风险、成熟度和置信度标签。它不会覆盖既有 `lifecycle_score` / `risk_score`，不会自动修改 decision model 或任何生产阈值。

质量标签属于研究标签，不是买卖建议。系统仅用于信号整理、研究和风险提示，不构成投资建议，不执行自动交易。

## 评分组成

- 既有生命周期强度：25%
- 周期升级质量：20%
- 价格结构：15%
- 成交量确认：10%
- OI 确认：10%
- Spot CVD：10%
- Futures CVD：5%
- Funding 健康度：5%

风险扣分覆盖 funding 过热、OI 增长而价格走弱、合约 CVD 增强但现货不确认、大周期确认前快速拉升、多次假突破、连续走弱和长期无升级。阶段判断综合首信号/最高周期、价格与回撤、成交量、OI、CVD、funding 和完整事件序列，不依赖单个关键词。

## 分层标签

| 分数 | 质量标签 |
|---:|---|
| 90–100 | 强趋势确认 |
| 80–89 | 高质量启动 |
| 70–79 | 启动有效 |
| 60–69 | 启动观察 |
| 40–59 | 动能不足 |
| 20–39 | 风险升高 |
| 0–19 | 启动失败 |

阶段包括 discovery、early_launch、confirmed_launch、timeframe_upgrade、trend_expansion、cooling、distribution_risk、failure 和 closed。

## 分析与相似案例

analytics 缓存首信号周期、升级路径、模块来源、资金确认组合及风险提示后表现，并可生成脱敏报告 `docs/generated/lifecycle_analytics_latest.json`。`lifecycle-similarity-v1` 使用可解释规则加权距离，只在已完成且具备成熟 success Outcome 的历史样本中搜索；样本不足时返回 `insufficient_mature_samples`，不会显示虚假的 0% 成功率。

历史相似样本仅用于研究，不代表未来结果。
