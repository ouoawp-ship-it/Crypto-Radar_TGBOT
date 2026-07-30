# OAR-P5D Arkham 辅助 CEX 标签审核

Arkham 在本模块中只提供 Base CEX 地址标签候选，不是 Transfer 事实源、
Watch 扫描源、行为分析源或 Telegram 触发源。`watch-live` 不会调用
Arkham；所有网络调用都必须由操作者显式执行并携带
`--allow-network`。

## 安全配置

`.env.onchain` 支持：

```text
ARKHAM_API_BASE_URL=https://api.arkm.com
ARKHAM_API_KEY=
ARKHAM_API_TIMEOUT_SEC=15
ARKHAM_API_MAX_RETRIES=1
OAR_LABEL_CANDIDATE_MAX_ADDRESSES=50
OAR_LABEL_CANDIDATES_FILE=label_candidates.json
```

API Key 通过 stdin 读取，不进入 argv。FinalShell 输入行按部署者要求明文
显示；保存后的状态只显示 `configured` / `not_configured`。Base URL 必须
为不含凭据、query 或 fragment 的 HTTPS URL。单次显式流程的 Arkham
请求硬上限为 6。

## 候选发现

```bash
python onchain_main.py label-candidates provider-check --allow-network
python onchain_main.py label-candidates discover \
  --chain base \
  --contract <已审核合约> \
  --window 4h \
  --max-addresses 50 \
  --allow-network
python onchain_main.py label-candidates list --status pending
```

发现命令先完成一次只读 Token Activity 查询，按出现次数、累计 Token 数量
和地址确定性排序。只有 Arkham 返回精确 Base 地址，并满足下列证据之一，
才会写入私有候选审计：

- 非空 `depositServiceID`；
- 明确 `exchange` / `cex` 实体，且 `service=true` 或存在明确 CEX 标签。

`predictedEntity`、名称相似、非 Base、地址不一致和不完整响应均不能生成
可批准候选。没有候选时最多执行两次受控 CEX seed 查询，两次请求间隔至少
1.1 秒，不分页。

候选保存在 `data/onchain/label_candidates.json`，目录权限 700、文件权限
600。文件只保存有界证据字段和 Hash，不保存 Provider 原始响应、错误正文、
Header 或 Key。

## 人工审核

操作者应先核对 Pending 候选的地址、实体、角色和证据类型。中文菜单批准
前要求完整输入：

```text
批准CEX标签
```

CLI 的离线审核命令为：

```bash
python onchain_main.py label-candidates approve --candidate-id <id>
python onchain_main.py label-candidates reject --candidate-id <id>
```

批准后才会在文件锁内备份并原子更新当前配置的私有 CEX CSV。写入固定使用
`entity_type=cex`、`source=arkham_api_exact+manual_review` 和
`confidence=0.95`，随后立即运行既有 CSV Loader 和 Live Validator；任何
失败都会恢复原文件。默认示例 CSV、重复地址、符号链接和未充分证据均拒绝
写入。

## 就绪与边界

```bash
python onchain_main.py labels-check
```

生产分类就绪要求 `classification_eligible_cex_count >= 1` 且
`synthetic_fixture=0`。标签库就绪不代表查询窗口实际命中 CEX 地址：
没有命中时应记录 `cex_direction_observed=false`。即使命中，入所也不等于
已经卖出，提币不等于已经买入或必然上涨。

回滚只需恢复批准前的私有 CSV 备份；候选审计保留。该流程不会修改主
`data/signals.db`、Chain Cursor、OAR Watch 状态、AI 或 Telegram 配置。
