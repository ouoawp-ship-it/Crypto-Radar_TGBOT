# OAR-P5E 地址情报中心

## 边界

地址情报是 OAR 的可选离线增强层，不是链上事实源。Base RPC 仍是 Transfer
事实的唯一来源；长期 `watch-live` 只把完整扫描中的未知地址写入本地队列，
不会同步调用 Dune、OLI、BaseScan 或 Arkham。

即使所有外部 Provider 都未配置或暂时失败，Token Activity、行为分析、钱包
候选群组、Watch、DeepSeek、Telegram Dry-run 和受控真实发送仍可继续运行。
标签不足时保留 Transfer 事实，方向分类降级为 `unclassified`，并报告
`insufficient_cex_coverage`。

## Provider

- `local_approved`：已人工批准的本地私有标签；
- `dune_cex`：Dune `cex.addresses` 的人工 CSV 或显式 API 同步；
- `dune_cex_deposit`：Dune `cex.deposit_addresses` 的人工 CSV 或显式 API 同步；
- `oli`：人工导入 OLI Base（`eip155:8453`）Parquet；
- `basescan_manual`：人工整理的 BaseScan CSV；
- `arkham_optional`：可选 Arkham 精确地址证据；
- `behavior_inference`：本地行为角色候选，不提供真实机构身份。

Dune 和 Arkham Key 为空时状态为 `optional_disabled`，网络调用为 0。网络
Provider 只有在操作者显式运行候选发现并提供 `--allow-network` 时才能请求。
OLI Parquet 使用可选 `pyarrow` 读取；服务器未安装时只拒绝本次 OLI 导入，
不会把该依赖加入核心运行环境。

## 未知地址队列

完整 Watch 扫描会选择非零、非 Token 合约且未识别的地址，并按下列字段
确定性排序：

1. 触发信号次数；
2. 出现的完整窗口数；
3. 累计 Token 数量；
4. 关联钱包数；
5. 地址。

队列只写入 `data/onchain/address_intelligence.json`。同一个扫描窗口幂等，
文件权限为 600，父目录权限不高于 700。队列中不保存 Provider 响应、凭据
或 Authorization Header。

## 候选与冲突

所有来源统一写入 Pending 候选。候选只保存归一化字段和证据 SHA-256，不
保存完整响应。相同地址的有效身份候选发生实体、类型或角色冲突时会标记
`conflicted`，审批门禁 fail closed。

候选可为 `pending`、`approved`、`rejected`、`expired` 或 `conflicted`。
过期来源不能批准；来源撤销会移除与候选 `evidence_hash` 精确匹配的生产行，
保留候选审计。

`behavior_inference` 只允许生成
`deposit_candidate`、`collector_candidate`、`hot_wallet_candidate`、
`fanout_candidate`、`treasury_candidate`、`bridge_candidate` 或
`contract_candidate`。它不会猜测 Binance、OKX、Coinbase 等真实实体，
也不能直接批准为 CEX 身份。

## 人工审核

进入：

```text
paopao
→ 链上活动雷达
→ 地址情报中心
```

批准前必须输入完整短语：

```text
批准地址标签
```

批准使用文件锁、修改前备份和原子替换写入私有 CSV。生产行包含 `source`、
`confidence`、有效期、`evidence_hash` 和 `review_status=approved`。Pending
候选不会进入生产分类。

常用离线命令：

```text
python onchain_main.py address-intelligence providers
python onchain_main.py address-intelligence queue --limit 50
python onchain_main.py address-intelligence candidates --status pending
```

显式联网发现：

```text
python onchain_main.py address-intelligence discover \
  --provider all --max-addresses 50 --allow-network
```

人工导入：

```text
python onchain_main.py address-intelligence import-dune \
  --file <csv> --table cex.addresses
python onchain_main.py address-intelligence import-oli --file <parquet>
python onchain_main.py address-intelligence import-basescan --file <csv>
```

这些命令只生成候选，不会自动批准或自动写入生产标签。

## 回滚

停止候选发现不需要停止 Watch。可以保留候选审计并拒绝或暂缓候选。生产
标签写入失败会恢复原 CSV；撤销某个已批准来源时只删除证据 Hash 匹配行，
不会删除 Automation DB、Watch、Registry 或 Transfer 历史。
