# 泡泡抓币 v1.8.1 结构图清理补丁报告

## 1. 本次修复的问题

v1.8 结构突破雷达会在 `data/charts/` 生成 K线状态图 PNG。长期运行 `structure-loop` 时，图片会持续累积，可能占用服务器磁盘。

本次 v1.8.1 补丁只处理结构图清理：

- 真实 Telegram 图片发送成功后，立即删除对应 PNG。
- dry-run 默认保留图片，方便本地查看。
- 图片发送失败时暂时保留。
- 每轮 `structure-radar` / `structure-loop` 结束后执行一次轻量图片清理。
- `python main.py cleanup --force-cleanup` 会清理过期或超量结构图。
- 不删除 `structure_state.json`、`structure_history.json`、`structure_report.txt`。
- 只清理 `data/charts/` 目录下的 `.png` 文件。

## 2. 修改文件

- `paopao_radar/config.py`：新增结构图删除和保留配置。
- `paopao_radar/maintenance.py`：新增 `cleanup_structure_charts()`，并接入现有 cleanup 流程。
- `paopao_radar/cli.py`：结构图真实发送成功后删除；每轮结构雷达结束后调用结构图清理。
- `.env.oi.example`：新增结构图清理配置模板。
- `README.md`、`docs/INSTALL_CN.md`：补充结构图清理说明。
- `VERSION`：升级为 `v1.8.1`。
- `tests/test_maintenance.py`：新增结构图清理测试。
- `tests/test_structure_radar.py`：新增发送成功/失败/dry-run 删除行为测试。

## 3. 新增配置项

```env
STRUCTURE_DELETE_CHART_AFTER_SEND=true
STRUCTURE_CHART_RETENTION_HOURS=12
STRUCTURE_MAX_CHART_FILES=200
```

含义：

- `STRUCTURE_DELETE_CHART_AFTER_SEND=true`：真实 Telegram 图片发送成功后，立即删除本地 PNG。
- `STRUCTURE_CHART_RETENTION_HOURS=12`：dry-run 或发送失败遗留的结构图最多保留 12 小时。
- `STRUCTURE_MAX_CHART_FILES=200`：如果 `data/charts/` 图片数量超过 200，只保留最新 200 张。

## 4. 图片删除规则

- 只在 `send_photo()` 返回 `status="sent"` 且 `sent=True` 时删除。
- dry-run 返回 `status="dry_run"`，不会删除图片。
- 发送失败返回 `status="failed"`，不会立即删除图片。
- 删除失败不会中断主流程，只在终端输出跳过原因。

## 5. cleanup 清理规则

新增 `cleanup_structure_charts(chart_dir, retention_hours, max_files)`：

- 只扫描 `chart_dir.glob("*.png")`。
- 超过保留时间的旧 PNG 会删除。
- 如果剩余 PNG 数量超过上限，按修改时间删除最旧图片。
- 不递归删除目录。
- 不处理 JSON、TXT、日志或其他文件。

`cleanup_runtime_artifacts()` 已接入该函数，因此：

```bash
python main.py cleanup --force-cleanup
```

会同时清理结构图。

## 6. 测试结果

已执行：

```bash
python -m unittest discover -s tests
```

结果：

```text
Ran 80 tests
OK
```

新增覆盖：

- 真实发送成功后删除图片。
- dry-run 不删除图片。
- 发送失败不立即删除图片。
- cleanup 删除超过保留时间的旧图片。
- cleanup 超过数量上限时删除最旧图片。
- cleanup 不删除 `structure_state.json`、`structure_history.json`、`structure_report.txt`。

## 7. 服务器更新方法

服务器直接运行：

```bash
paopao update
```

或手动：

```bash
cd ~/paopao-crypto-radar
git pull
bash scripts/update_server.sh --yes
```

更新脚本会保留 `.env.oi` 里的 `TG_BOT_TOKEN`、`TG_CHAT_ID`、`COINGLASS_API_KEY` 和话题 ID，并自动补上新增配置项。
