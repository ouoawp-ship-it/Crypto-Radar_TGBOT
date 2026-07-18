# Mercu 登录态视觉验收

Radar、Info、Funds 的视觉验收必须比较 Mercu 登录态目标截图与 Paoxx 实际截图。Playwright 的常规 `toHaveScreenshot` 只用于发现 Paoxx 自身视觉回归，不能作为 Mercu 1:1 证明。

## 必需目标图

将同一浏览器、同一缩放比例下采集的 Mercu 登录态截图放入 `frontend/e2e/mercu-targets/`：

- `mercu-radar-1440x900.png`
- `mercu-radar-1920x1080.png`
- `mercu-info-1440x900.png`
- `mercu-info-1920x1080.png`
- `mercu-funds-1440x900.png`
- `mercu-funds-1920x1080.png`

目标截图目录只用于本地授权验收，不提交到 Git。

## Paoxx 实际图

先用固定数据夹具生成 Paoxx 截图：

```powershell
cd frontend
npm.cmd exec -- playwright test e2e/public-workflow.spec.ts --grep "workstation visual fixtures"
```

实际图位于 `frontend/e2e/public-workflow.spec.ts-snapshots/`。

## 严格差分

```powershell
python -m pip install -r requirements-visual.txt
cd frontend
npm.cmd run visual:mercu
```

默认验收门禁为严格模式：像素容差、允许变化比例和平均误差全部为零。任一目标图缺失、尺寸不等或存在像素差异，命令都会失败。差分热力图和机器可读报告输出到 `frontend/e2e/mercu-diffs/`。

调试时可以临时放宽阈值定位主要结构差异，但放宽结果不能作为 1:1 完成证据：

```powershell
python ../scripts/mercu_visual_diff.py --pixel-threshold 16 --max-changed-ratio 0.05 --max-mean-error 0.02
```
