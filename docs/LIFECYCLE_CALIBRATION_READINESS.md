# Lifecycle Calibration Readiness

## 只读准入判断

`lifecycle-calibration-readiness` 只回答“当前成熟、已关联的数据是否足以进入模型校准验证”，不会修改 Decision Model 阈值、Lifecycle Intelligence 权重或任何线上模型参数。未达到门槛时继续积累并补算数据，而不是降低门槛或把 unavailable 当成亏损。

## 默认门槛

```dotenv
LIFECYCLE_CALIBRATION_MIN_24H_SUCCESS=50
LIFECYCLE_CALIBRATION_MIN_72H_SUCCESS=30
LIFECYCLE_CALIBRATION_MIN_DUE_RESOLUTION_RATIO=0.90
LIFECYCLE_CALIBRATION_MIN_LIFECYCLE_MATURITY_RATIO=0.60
LIFECYCLE_CALIBRATION_MAX_ERROR_RATIO=0.01
```

准入必须同时满足：

- 24h success 与 72h success 达到最低样本数。
- 到期候选解决率、生命周期成熟率达到最低比例。
- 真实错误率不超过上限。
- 重复 link、多 primary、孤立 link 均为 0。
- generic `no_outcome_row` 为 0。

返回结构：

```json
{
  "ready": false,
  "label": "暂未达到模型校准条件",
  "passed": [],
  "blocked": [],
  "warnings": [],
  "current": {},
  "required": {}
}
```

## 使用方式

```bash
python main.py lifecycle-calibration-readiness --pretty
curl -s https://paoxx.com/public-api/lifecycle/calibration-readiness
```

公开 API 只读取预计算质量与一致性结果，不扫描外部行情。前台“模型校准条件”卡会分别显示当前 24h/72h success、到期解决率、生命周期成熟率、错误率和一致性检查，并固定说明：此处仅判断数据是否足够，不会自动修改模型。

只有同时满足“已关联 + 已到期 + success”的样本才可以进入成熟收益统计。not_due 不是错误，pending 不是失败，unavailable 不等于亏损，ineligible 信号不进入 Outcome 分母。

系统仅用于信号整理、研究和风险提示，不构成投资建议，不执行自动交易。
