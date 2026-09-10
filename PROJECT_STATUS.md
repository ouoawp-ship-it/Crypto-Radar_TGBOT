# Altcoin Hunter 项目状态

更新时间：2026-09-10。统计口径见 [PLANS.md](PLANS.md)，版本1；状态以代码、测试、运行记录和GitHub证据为准。

## 当前结果

- 目标：Production Candidate v1.0；尚未完成。
- 总体工程完成度：40/100，P1A25 + P1B-I15；其余阶段尚未通过，不计分。
- 当前阶段：P1B-II公共Transport与有界Smoke，in_progress；真实行情尚未连接。
- 当前分支：codex/altcoin-hunter-p1b-ii-public-transport。
- 已验收PR #173 head：6525ea2d4ca738704cbaeda935388cf8318c0179；包含原两个代码提交和长期计划文档提交。
- 预期main及当前阶段精确起点：722f7bf79f614e8e3f7eb91bc96a89740769ee08。不得接受外部漂移或隐式继承额外提交。
- 部署、真实Telegram、服务安装/启停、生产配置/Migration、交易功能：关闭，未授权自动执行。

## 已完成能力与证据

- P1A：类型化数据合同、正交Universe、离线聚合/六窗口/动态基线、七表独立SQLite、prepare/commit/ack、只读离线查询、回放与容量。PR #172已Merge Commit合并；merge e7622becdec46c179d97820f0769790b9a49e3af。
- P1B-I：Binance公共报文纯解析、UM/CM隔离、路由分片、ACK双策略、离线连接/过渡/epoch/Coverage、REST预算/身份退休/Completion准入及独立ExchangeInfo大小合同。
- PR [#173](https://github.com/ouoawp-ship-it/Crypto-Radar_TGBOT/pull/173)：初版56e47fbe6c456d162a2e081e74fc807b892ac6c5；hardening444724461d9e27f843117cd36e47dced0b153daa。开始本任务时OPEN/Draft/MERGEABLE，67files/2commits，comments/reviews/threads为空。
- 对应head的 [Tests run 33973291382](https://github.com/ouoawp-ship-it/Crypto-Radar_TGBOT/actions/runs/33973291382)：Linux1430/1430通过、131.436s。Windows全量1430项、1414通过/16原有平台skip、330.092s；Hunter388/388、199.945s。编译与diff检查通过。
- 100次稳定恢复后第101次可继续；100次计划轮换不耗失败预算；4100历史身份不堵新币；600/1000/1500离线规划无漏订阅。上述不是实网验证。
- 根AGENTS已读取；初始工作区/暂存区/未跟踪干净；没有Git操作/相关运行进程；80个ignored运行时文件hash不变，P1A核心/旧策略无修改。其他worktree未操作。
- 独立最终代码审查通过：P1A/旧策略隔离、连接连续失败与过渡、ACK双策略、REST关联/退休、有界协议检查未发现阻断问题。计划文档最终head CI已成功，PR #173已完成Ready/复核/Merge Commit。
- 本轮最终验证：Windows全量1430执行、1414通过、16原有平台skip、0失败（330.245s）；Hunter388/388（216.971s）；compile与diff通过。Linux文档head [CI34477598486](https://github.com/ouoawp-ship-it/Crypto-Radar_TGBOT/actions/runs/34477598486) 1430/1430（116.411s）。
- #173于2026-09-10T12:41:56Z合并；merge722f7bf79f614e8e3f7eb91bc96a89740769ee08的parents准确为e7622becdec46c179d97820f0769790b9a49e3af、6525ea2d4ca738704cbaeda935388cf8318c0179。main [CI34478193348](https://github.com/ouoawp-ship-it/Crypto-Radar_TGBOT/actions/runs/34478193348) completed/success。保留本地和远端阶段分支。
- 详细历史数据见 [Hunter验收记录](radars/altcoin_hunter/VALIDATION.md)。

## 当前阶段进度（尚未验收）

- P1B-II新增公共传输候选、有限budget ticket、独立CLI/watchdog、临时路径保护和两级Smoke policy；实际网络实现仅在runtime，新功能默认关闭。
- 新专项119/119通过；Windows全量1549执行、1533通过、16原有平台skip、0失败（290.270s），compile通过。旧三类离线CLI、新公共help与关闭门禁通过。Hunter专项507/507通过（176.493s，0skip/0失败）；Linux CI尚待提交触发。
- 尚未执行任何真实Binance预检或Smoke；没有任何实网吞吐/完整性结论。P1B-II不计分，总体仍40/100。

## 未完成能力

- 真实公共Transport和5币3分钟/最多20币10分钟Smoke；没有任何真实行情运行证据。
- P1C生产候选恢复/保留/实时读取/服务定义及真实长期验证。
- P2五类信号、三评分；P3八状态/事件簇/Outcome；P4Topic消息生命周期；P5Web；P6多源联动、校准和完整候选验收。
- 任何生产部署/服务启动/真实Telegram均未执行，也不是本轮自动完成能力。

## 当前风险与停止条件

- Binance当前ACK互通、路由、真实载荷大小和共享IP预算未测；出现协议不兼容/429/418/完整性异常立即停止，不自动放宽门禁。
- 稳定窗口、tombstone、基线及评分候选阈值未经真实市场校准；BBO不是depth，清算snapshot不是完整逐笔流水，OI增加不是新增多头。
- 独立DB当前只证明离线恢复边界；长期增长、保留、在线读取与服务故障隔离待P1C。
- 用户仅自动允许不超过20币/10分钟真实Smoke。真实长期soak和任何生产动作需人工确认；不能用虚拟长时回放替代实测。
- 测试失败、外部提交/他人工作或main漂移也必须停止并等待人工确认。

## 下一阶段入口

P1B-II已从精确已合并main722f7bf...开始。只有该阶段完整离线验证、两级有界真实Smoke、独立Draft PR及CI通过并合并后，才可进入P1C。任何测试、协议、限流或完整性异常立即停止等待人工。

每阶段结束更新本文件：固定完成度、已完成/未完成、风险、PR/commit/CI、运行证据和下一入口。合并后证据写入独立执行日志，并在下一阶段首个文档提交固化，不重写历史。
