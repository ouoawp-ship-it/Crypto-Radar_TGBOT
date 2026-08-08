# 山寨合约异动雷达 P2 运维说明

P2 为 `altcoin_contract_anomaly` 增加动态候选清单消费和多因子实时确认，但当前只允许人工启动、受限时长的 Dry-run。用户可见名称仍为“山寨合约异动雷达”。

## 安全边界

- 默认关闭；总开关和 P2 实时开关缺一不可。
- 只输出本地 JSON，不发送 Telegram，不读取或创建“山寨合约异动”话题。
- 不注册主 BOT 调度，不修改 `paopao-radar.service` 或其他 systemd 单元。
- 不启用 CoinGlass；全市场 OI 字段继续保持与 Binance 单交易所 OI 分离。
- 只消费已经通过 P1 校验并持久化的候选 Manifest；P2 命令本身不请求 CMC，也不要求 CMC Key。在线刷新 Manifest 应先独立执行 P1 扫描。
- 复用项目现有 Binance 实时连接管理能力；单个进程中仍只有一个 `WebSocketApp`、一套连接生命周期和一份 `!forceOrder@arr`。
- 不接受永久运行参数。每次启动都必须指定 30 到 3600 秒的运行时长。
- `--send`、`--confirm-real-send`、`--cache-only` 和 `--preview-telegram` 均不能与 P2 模式同时使用。

受限命令会在自己的本地进程中启动上述唯一连接。运行前必须确认本机没有正在运行的 `main.py market-stream` 或另一份 P2 Dry-run；两者并存会形成两个进程、各自一条连接。P2 尚未提供跨进程附着能力，不得在生产 `paopao-market-stream` 正常运行时并行启动该人工命令，也不要把 P2 开关长期写成服务器常驻配置。

## 启用配置

仅在隔离的本地验收环境中启用下面两个开关；服务器常驻配置必须保持 P2 开关为 `false`：

```dotenv
ALTCOIN_CONTRACT_ANOMALY_ENABLE=true
ALTCOIN_CONTRACT_ANOMALY_REALTIME_ENABLE=true
```

两个开关默认都为 `false`。`ALTCOIN_CONTRACT_ANOMALY_SMOKE_DURATION_SEC=900` 记录建议的人工验收时长，但不会自动启动进程；命令行仍必须明确给出实际时长。

本地验收优先使用当前终端的进程级环境变量启动命令，命令结束后变量随终端会话清理；不要为了 Smoke 重启或改动生产 `paopao-market-stream` 服务。

P2 状态与事件使用独立文件：

```dotenv
ALTCOIN_CONTRACT_ANOMALY_REALTIME_STATE_FILE=altcoin_contract_anomaly_p2_state.json
ALTCOIN_CONTRACT_ANOMALY_REALTIME_EVENT_FILE=altcoin_contract_anomaly_p2_events.jsonl
```

相对路径统一解析到项目数据目录，不能把两个文件配置成同一路径。状态文件是原子更新的 JSON；事件文件是只追加的 JSONL，每行一个结构化事件。

## 人工 Dry-run

运行 15 分钟并在标准输出获得一份 JSON：

```bash
python main.py altcoin-anomaly --realtime-duration-sec 900 --json
```

同时原子保存相同 JSON：

```bash
python main.py altcoin-anomaly \
  --realtime-duration-sec 900 \
  --json \
  --output data/altcoin-contract-anomaly-p2-session.json
```

P2 的标准输出只保留最终 JSON；诊断信息进入标准错误。JSON 禁止 NaN 和无穷值，不包含 Token、Chat ID、话题 ID、API Key、私有地址或原始供应商响应。

## 停止和退出码

到达指定时长后，运行器停止新增处理并关闭本次 WebSocket；该连接上的临时订阅随连接关闭自动解除，然后输出最终统计。人工按 `Ctrl+C` 时执行同一清理路径，最终状态为 `interrupted`，进程退出码为 `130`。

- `0`：Dry-run 正常完成；没有正式异动不等于失败。
- `1`：内部错误，或运行资源未能安全收尾。
- `2`：开关、阈值、时长或互斥参数错误。
- `3`：候选清单或必需行情数据暂不可用。
- `130`：人工中断，已进入优雅停止路径。

合法的空候选池应在结果中明确标记，不伪造订阅或异动；已有候选但运行期没有收到可用实时事件、闭合特征未完整形成或最后一轮 OI/资金费率等确认数据不完整时，应作为数据不可用处理，不能把零事件伪装成成功。

## 集中配置

候选清单和订阅控制：

- `MANIFEST_POLL_SEC=5`、`MANIFEST_MAX_AGE_SEC=1200`；
- `SUBSCRIPTION_BATCH_SIZE=50`、`SUBSCRIPTION_MIN_INTERVAL_SEC=1.0`；
- `SUBSCRIPTION_ACK_TIMEOUT_SEC=10`、`MAX_STREAMS=300`；
- `REALTIME_DATA_MAX_AGE_SEC=120`。

实时 OI：

- `OI_REFRESH_SEC=300`、`REALTIME_OI_MAX_AGE_SEC=600`；
- `REALTIME_OI_WORKERS=4`、`REALTIME_OI_REQUEST_BUDGET=50`。

特征窗口和数据完整度：

- `FEATURE_1M_WINDOW_SEC=60`、`FEATURE_5M_WINDOW_SEC=300`；
- `VOLUME_BASELINE_BUCKETS=10`、`VOLUME_MIN_SAMPLES=8`；
- `VOLUME_MIN_COVERAGE=0.8`。

这组三项是为 12 到 15 分钟冷启动 Smoke 设置的影子初值：最多检查前 10 个闭合 1 分钟桶，其中至少 8 个有效桶即可形成基线，避免短时验收因基线永远不完整而出现假 0。它尚未经历史回测或生产影子数据校准，P2 验收结果只能用于检查数据链路，不能据此认定阈值已经适合生产。

现有共享实时库固定写入 60 秒桶，因此 P2 当前只接受 `60/300` 这一组窗口值；保留集中配置键是为了明确数据契约，不能把它们改成共享存储尚不支持的任意周期。

实时确认阈值：

- `PRICE_1M_MOVE_RATIO=0.01`、`PRICE_5M_MOVE_RATIO=0.02`；
- `VOLUME_EXPANSION_RATIO=2.0`；
- `AGGRESSIVE_BUY_RATIO=0.60`、`AGGRESSIVE_SELL_RATIO=0.40`；
- `OPEN_INTEREST_MOVE_RATIO=0.03`；
- `FUNDING_POSITIVE_RATE=0.0005`、`FUNDING_CHANGE_RATIO=0.0001`；
- `LIQUIDATION_MIN_USD=100000`；
- `PRICE_STALL_RATIO=0.003`；
- `WEAKENING_VOLUME_RATIO=1.2`、`WEAKENING_WINDOWS=2`。

以上变量完整名称都以 `ALTCOIN_CONTRACT_ANOMALY_` 开头。数值、非有限值和跨字段关系会在建立网络连接前严格校验；错误信息只报告配置项，不回显原值。

默认 Manifest 新鲜度为 1200 秒，比 900 秒 Smoke 多 5 分钟启动与轮询余量。配置校验至少要求 Manifest 新鲜度不短于 Smoke 时长，避免会话尚未结束就因默认值必然降级。P2 会先确认各基础字段在 P1 生成 Manifest 时满足各自的新鲜度门槛；运行期则以 Manifest 自身寿命以及 P2 实时采集的 markPrice、资金费率和 OI 为准，不让一个生成时合格的闭合 OI 时间点在会话中途单独使 Manifest 失效。

实际启动前还会检查当前 Manifest 的剩余寿命是否覆盖整段命令时长；不足时在创建 WebSocket 前以退出码 `3` 拒绝运行。需要更长的人工观察时，必须先生成新鲜 P1 Manifest，并显式同步提高该年龄上限。

## 回滚

将下面配置恢复为 `false`，并停止当前人工命令即可：

```dotenv
ALTCOIN_CONTRACT_ANOMALY_REALTIME_ENABLE=false
```

P2 没有 systemd 注册、Telegram 消息或话题副作用。回滚时不要删除 P1 候选池、其他雷达状态、共享实时数据库或 Telegram 历史。
