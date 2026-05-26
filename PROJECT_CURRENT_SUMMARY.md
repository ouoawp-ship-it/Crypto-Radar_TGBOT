# 泡泡抓币项目当前功能与逻辑总结

## 1. 项目基本信息

- 项目路径：`C:\Users\多多\Desktop\泡泡抓币`
- 当前版本：`v1.5`，见 `VERSION`
- Git 提交：以仓库当前 `git log --oneline -1` 为准，本报告按生成时项目结构整理。
- 主入口：`main.py` -> `paopao_radar.cli.main()`
- 依赖：`requests>=2.31.0`、`httpx>=0.27.0`
- 本次检查方式：只读扫描源码、文档、配置模板、测试文件；没有启动 daemon/live，没有真实发送 Telegram。
- 本地 `.env.oi` 脱敏状态：`TG_BOT_TOKEN=已配置`，`TG_CHAT_ID=已配置`，各专属 topic ID 未配置，`COINGLASS_ENABLE=未设置`，`COINGLASS_API_KEY=未配置`。服务器配置可能不同，需要以服务器 `.env.oi` 为准。
- 静态检查：AST 语法检查通过 10 个 Python 文件；单元测试 `python -m unittest discover -s tests` 通过 60 个测试。

## 2. 目录结构

| 路径 | 作用 |
| --- | --- |
| `main.py` | 最小入口，捕获 `KeyboardInterrupt`，转发到 CLI。 |
| `paopao_radar/cli.py` | 命令行入口、运行模式、循环调度、readiness、状态输出、推送编排。 |
| `paopao_radar/config.py` | `.env.oi` 配置读取、默认值、路径配置、脱敏状态输出。 |
| `paopao_radar/data_sources.py` | Binance、CoinGlass、HTTP 缓存、重试、熔断、请求预算。 |
| `paopao_radar/radar.py` | 资金雷达、启动雷达、公告机会/风险、OI/价格背离、Telegram 文本模板。 |
| `paopao_radar/flow_radar.py` | CoinGlass 五因子资金流雷达：价格、OI、现货 CVD、合约 CVD、费率。 |
| `paopao_radar/telegram.py` | Telegram 发送、topic 路由、自动建话题、话题说明置顶、去重限流。 |
| `paopao_radar/storage.py` | JSON 状态读写、坏 JSON 自动改名、追加历史。 |
| `paopao_radar/maintenance.py` | 自动清理 pycache、临时文件、坏 JSON 备份、旧日志、过长历史。 |
| `scripts/install_server.sh` | 中文服务器安装向导、配置输入、venv、测试、systemd 服务。 |
| `scripts/update_server.sh` | 检查 GitHub 版本、fast-forward 更新、测试、重启服务。 |
| `scripts/paopao_menu.sh` | 服务器快捷命令 `paopao` 菜单。 |
| `tests/` | 单元测试，覆盖数据源、雷达逻辑、Telegram、维护、CLI。 |
| `docs/` | 安装、部署、结构、运行说明。 |
| `data/` | 本地运行状态、推送历史、观察池、runtime status 等。 |

## 3. 运行入口

| 命令 | 逻辑 | 是否真实推送 |
| --- | --- | --- |
| `python main.py about` | 输出功能说明。 | 否 |
| `python main.py status` | 输出配置脱敏状态和状态文件概览。 | 否 |
| `python main.py doctor` | 输出环境、数据目录、配置摘要。 | 否 |
| `python main.py readiness` | 检查真实推送门禁：Telegram 配置、观察历史、推送压力。 | 否 |
| `python main.py telegram-test` | 默认 dry-run；加 `--send --confirm-real-send` 才发测试消息。 | 默认否 |
| `python main.py coinglass-test` | 测试 CoinGlass 配置和接口。会调用 CoinGlass。 | 否 |
| `python main.py flow-radar` | 运行五因子资金流雷达。 | 默认否 |
| `python main.py once` | 运行一轮资金摘要、启动雷达、公告、可选 flow。 | 默认否 |
| `python main.py trial` | 启动雷达试跑多轮。 | 默认否 |
| `python main.py observe` | 强制 dry-run 观察启动雷达，保存观察报告。 | 否 |
| `python main.py loop` / `daemon` | 持续循环，默认仍需 `--send --confirm-real-send` 才发。 | 取决于参数 |
| `python main.py live --send --confirm-real-send` | 先过 readiness，再进入真实循环。 | 是 |
| `bash scripts/install_server.sh` | Linux 安装并创建 systemd 服务。 | 安装阶段默认不发测试消息 |
| `paopao` | 服务器快捷菜单。 | 菜单中测试命令会真实发送 |

当前没有 Windows `.bat` 启动脚本；文档说明已砍掉 Windows 一键启动。

## 4. 当前功能清单

| 功能 | 文件/函数 | 数据 | 触发条件 | 推送内容 | 默认启用 |
| --- | --- | --- | --- | --- | --- |
| 资金雷达摘要 | `radar.py::build_money_radar_summary` | Binance 合约 24h ticker、premiumIndex、OI 历史、K线、市值 | `once` 或 `loop` 摘要周期 | 负费率榜、综合榜、埋伏池、动量池、新币池、背离雷达、值得关注 | 是 |
| 负费率榜 | `radar.py::_append_negative` | funding rate、24h 涨跌、市值、价格 | funding < 0，按费率从负到正 | 币种链接、费率趋势、24h、市值、现价 | 是 |
| 综合榜 | `combined_score` | 费率、市值、横盘天数、OI | 分数 >= 25 | 评分和四类指标 | 是 |
| 庄家埋伏池 | `ambush_score` | 市值、OI、横盘、费率 | 分数 >= 35 且横盘 >=45 天或暗流 | 低市值/横盘/OI/费率 | 是 |
| 动量池 | `momentum_score` | OI、24h 涨跌、成交额、费率 | 分数 >= 35 | OI、24h、Vol、历史天数 | 是 |
| 新币池 | `new_score` | 历史天数、OI、24h、成交额、费率 | 历史 < 30 天 | 新币评分和指标 | 是 |
| OI/价格背离 | `_classify_divergence_item` | 6h OI、24h 价格 | `abs(divergence)>=6` 或 `abs(OI)>=5` | 背离类型、等级、状态 | 是 |
| 启动雷达 | `build_launch_alerts` / `_analyze_launch_symbol` | 15m K线、15m OI、成交量 | 分数跨阶段且冷却结束 | 阶段、分数、价格/OI/成交量、判断 | 是 |
| 公告机会/风险 | `build_announcement_alerts` | Binance 公告 | 当天 CST 公告，命中关键词且有明确币种 | 公告机会/风险、币种、有无合约、公告链接 | 是 |
| 五因子资金流 | `flow_radar.py` | Binance 候选 + CoinGlass markets/CVD | CoinGlass 开启且 key 存在 | 真启动、吸筹、空头燃料、合约拉盘等 | 需启用 CoinGlass |
| Telegram 推送 | `telegram.py::send` | 本地 `.env.oi`、状态 JSON | CLI 编排调用 | HTML/文本消息 | 只有加真实发送参数 |
| 话题自动分类 | `telegram.py` | Telegram Bot API | topic ID 未手填且允许自动创建 | 自动建 5 个话题并置顶说明 | 默认配置开启 |
| 自动清理 | `maintenance.py` | 本地文件 | `cleanup` 或循环中到期 | 清理结果写状态 | 默认开启 |
| 本地状态/报告 | `cli.py`、`storage.py` | JSON | `watchlist`、`launch-history`、`launch-report` | 终端输出 | 是 |

## 5. 数据源与接口

| 数据源 | 调用位置 | 接口用途 | Key | 缓存/重试/限速 | 降级 |
| --- | --- | --- | --- | --- | --- |
| Binance Futures | `data_sources.py::BinanceDataSource` | `exchangeInfo`、`ticker/24hr`、`premiumIndex`、`openInterestHist`、`klines`、`fundingRate` | 不需要 | HTTP 缓存默认 10 秒；重试默认 2；OI/K线/Funding 有请求预算 | 失败返回空列表，诊断记录失败 |
| Binance marketing symbol list | `market_caps()` | 市值估算 | 不需要 | 同 HTTP 缓存/重试 | 失败后用流通量*价格或成交额/OI 估算市值 |
| Binance 公告 | `announcements()` | Alpha、上新、活动、下架/停止交易 | 不需要 | 按 catalog 48/161/93 拉公告，缓存/去重 | 失败无公告 |
| CoinGlass | `CoinglassDataSource`、`flow_radar.py` | coins markets、OI、爆仓列表、现货/合约 CVD、funding 历史 | 需要 `COINGLASS_API_KEY` | `COINGLASS_REQUEST_BUDGET` 默认 60；HTTP 缓存；重试 | 未启用时 flow 雷达返回“无法计算 CVD” |
| Telegram Bot API | `telegram.py` | sendMessage、createForumTopic、pinChatMessage、deleteMessage | 需要 Bot Token | 发送重试、分片、冷却、每日/每小时限流 | HTML 400 时退回纯文本 |
| 本地 JSON | `storage.py` | 状态、去重、历史、topic route | 不需要 | 原子写入临时文件再替换 | 坏 JSON 自动改名 `.corrupt.timestamp` |

未找到 OKX、Bybit、CoinGecko、CMC、DefiLlama、DexScreener 的直接 API 调用。代码只读取 Binance 返回字段中的 `CMCCirculatingSupply`，不是直接调用 CMC。

## 6. 信号计算逻辑

| 逻辑 | 计算方式 |
| --- | --- |
| 横盘天数 | `estimate_sideways_days()` 从日 K 倒序统计，累计高低区间超过 80% 后停止。不是严格箱体识别。 |
| 费率分 | `score_funding()`：<-0.5%=25，<-0.1%=22，<-0.05%=18，<-0.03%=14，<-0.01%=10，<0=5。 |
| 市值分 | `score_mcap()`：<5000万最高，市值越大分越低，>=10亿为 0。 |
| 横盘分 | `score_sideways()`：>=120天最高，90/75/60/45 天递减。 |
| OI 分 | `score_oi()` 使用 OI 绝对变化：>=15%最高，8/5/3/2%递减。 |
| 综合榜 | 费率25 + 市值25 + 横盘25 + OI25，分数 >=25 入选。 |
| 埋伏池 | 市值35 + OI30 + 横盘20 + 费率15；OI>2 且价格波动<5% 加暗流分。 |
| 动量池 | OI35 + 24h涨跌25 + 成交额25 + 负费率15。 |
| 新币池 | OI30 + 24h涨跌25 + 成交额25 + 负费率20，历史 <30天。 |
| 背离度 | `divergence = oi_6h - price_24h`。 |
| 背离等级 | 极端：`abs(divergence)>=20` 或 `abs(price)>=15`；建仓：OI>=6 且价格 -3~3；多头共振：OI>=5 且价格>=4；另有增仓下跌、减仓上涨、恐慌抛售。 |
| 背离状态 | 首次、持续、增强、减弱、重新出现；缺失超过 12 轮删除。 |
| 启动雷达分 | 15m价>=4 加25；1h价>=5 加15；突破近4h高点加25；成交>=2倍均值加20；15m OI>=3 加15；1h OI>=6 加15；1h OI>=3且1h价格<=2 加暗流15。最高 130。 |
| 启动阶段 | <45 未触发；45-59 提前观察；60-74 提前预警；75-89 启动确认；>=90 启动瞬间。 |
| 启动冷却 | 同币同阶段默认 6 小时，见 `LAUNCH_STAGE_COOLDOWN_SEC`。 |
| 五因子 flow | 用价格、OI、现货 CVD、合约 CVD、费率、成交额打分，分类为真启动候选、吸筹观察、空头燃料、合约拉盘、挤空/止损、诱多/派发、恐慌下跌。 |

未找到 S/A/B/C 等级体系；现有等级是分数、阶段、中文类别和图标。

## 7. Telegram 推送逻辑

- Token：`Settings.load()` 从 `.env.oi` 的 `TG_BOT_TOKEN` 读取。
- Chat ID：从 `TG_CHAT_ID` 读取。
- 话题 ID：支持 `TG_TOPIC_ID`、`TG_RADAR_SUMMARY_TOPIC_ID`、`TG_LAUNCH_ALERT_TOPIC_ID`、`TG_ANNOUNCEMENT_ALERT_TOPIC_ID`、`TG_TEST_TOPIC_ID`、`TG_FLOW_RADAR_TOPIC_ID`。
- 自动话题：`TG_AUTO_CREATE_TOPICS=true` 时可调用 `createForumTopic` 创建资金摘要、启动预警、公告风险、测试消息、资金流雷达。
- 话题说明：每个 topic 第一次真实发送前，会发 intro，并按配置尝试 `pinChatMessage`。
- 推送格式：业务推送主要用 HTML；`telegram-test` 使用纯文本；默认 `send()` parse_mode 参数是 Markdown，但调用处多传 HTML。
- 图片推送：当前未实现；未找到 `sendPhoto`、`sendDocument`、图片生成模块。
- 重试：`TG_PUSH_RETRY` 默认 2；429/5xx 会重试；HTML 400 会转纯文本 fallback。
- 分片：`TG_PUSH_SPLIT_LIMIT` 默认 3800。
- 限流：dedup 冷却、模板每日上限、全局每小时上限 `TG_GLOBAL_HOURLY_LIMIT` 默认 20。
- 历史：`tg_push_history.json` 记录 template、dedup_key、topic、状态、message_id、预览。
- 命令交互：未找到 Telegram `getUpdates` 或 bot 命令监听；只有服务器 CLI/`paopao` 菜单。

## 8. 定时循环逻辑

| 能力 | 当前状态 |
| --- | --- |
| 单次运行 | 已支持，`python main.py once`。 |
| 自动循环 | 已支持，`loop/daemon/live`。 |
| 资金摘要周期 | 默认读取 `RADAR_SUMMARY_MIN_INTERVAL_SEC=21600`（6 小时）；模板每日上限默认 4 次。 |
| 启动雷达周期 | `--launch-interval` 默认 180 秒。 |
| flow 雷达周期 | `FLOW_INTERVAL_SEC` 默认 3600 秒，daemon/live 对齐整点附近推送，需 CoinGlass 启用。 |
| 自动清理周期 | `CLEANUP_INTERVAL_SEC` 默认 3600 秒。 |
| 每天固定时间 | 未找到。 |
| 每小时 55 分扫描 | 当前未接入。 |
| 整点推送 | 资金流雷达已对齐整点附近推送；其他周期仍按相对间隔。 |
| Windows bat | 当前未找到，文档说明不保留 Windows 一键启动脚本。 |
| systemd | 已支持，`install_server.sh` 写入 `paopao-radar.service`，`Restart=always`，`RestartSec=15`。 |
| nohup 后台 | 文档中有手动 `nohup .venv/bin/python -u main.py daemon ...` 示例。 |

## 9. 图表/K线功能

| 功能 | 当前状态 |
| --- | --- |
| K线数据读取 | 已有：Binance `klines()` 用于计算，不用于画图。 |
| K线图生成 | 当前未实现。 |
| Telegram 发送图片 | 当前未实现。 |
| 支撑阻力画线 | 当前未实现。 |
| 箱体上沿/下沿画线 | 当前未实现。 |
| 成交量柱图 | 当前未实现。 |
| OI 曲线图 | 当前未实现。 |
| Funding 曲线图 | 当前未实现。 |
| K线状态图推送 | 当前未实现。 |

## 10. 盘整突破/关键位临界功能检查

| 检查项 | 结论 | 依据 |
| --- | --- | --- |
| 自动识别盘整箱体 | 部分已有 | `estimate_sideways_days()` 只估算宽区间横盘天数，不输出箱体上下沿。 |
| 自动识别阻力位/支撑位 | 未实现 | 未找到 support/resistance/支撑/阻力计算。 |
| 自动识别波动率压缩 | 未实现 | 未找到 volatility compression。 |
| Bollinger Band 宽度压缩 | 未实现 | 未找到 Bollinger/BB/布林逻辑。 |
| ATR 压缩 | 未实现 | 未找到 ATR 逻辑。 |
| 价格接近箱体上沿/下沿 | 未实现 | 没有箱体边界字段。 |
| 突破临界 | 部分已有 | 启动雷达判断 `close > previous_high`，但不是临界/接近预警。 |
| 假突破识别 | 未实现 | 只有文案提示“跌回突破位则启动失败”，没有假突破状态机。 |
| 每小时 55 分扫描 | 未实现 | 资金流雷达已支持整点推送，但没有 55 分钟扫描机制。 |
| 推送 K线状态图 | 未实现 | 无图表生成和 `sendPhoto`。 |

## 11. 当前优点

| 优点 | 说明 |
| --- | --- |
| 核心链路完整 | 数据抓取、信号计算、Telegram 推送、状态保存、服务器部署都已有。 |
| 安全推送边界清楚 | 默认 dry-run，真实发送必须 `--send --confirm-real-send`，live 还要 readiness。 |
| Telegram topic 成熟 | 支持按模板话题路由、自动建话题、intro、置顶、fallback。 |
| 状态/去重已有 | 推送历史、启动状态、背离状态、公告 seen、topic route 都用 JSON 保存。 |
| 接口保护存在 | HTTP 缓存、重试、熔断、请求预算、数据质量统计。 |
| Binance 数据可用 | 合约 ticker、OI、K线、费率、公告、市值都有封装。 |
| CoinGlass 已有基础接入 | 已封装 markets、OI、爆仓、CVD、funding history，flow 雷达已用 CVD。 |
| 测试覆盖较好 | 50 个单元测试覆盖核心逻辑、Telegram、安装/命令、维护。 |
| 部署体验完善 | 中文安装向导、配置修改、快捷菜单、版本更新检查。 |

## 12. 当前缺点

| 缺点 | 影响 |
| --- | --- |
| 没有图表系统 | 无法推送 K线状态图、箱体、支撑阻力、OI/Funding 曲线。 |
| 没有关键位系统 | 不能做接近阻力/支撑的临界预警。 |
| 没有压缩突破系统 | ATR、布林带宽、波动率压缩未实现。 |
| 横盘逻辑较粗 | 只是统计 80% 宽区间横盘天数，不能替代箱体识别。 |
| 调度不是整点型 | 不能保证 00:55 扫描、接近 01:00 推送。 |
| flow 依赖 CoinGlass | 本地 `.env.oi` 未启用 CoinGlass 时五因子 CVD 不工作。 |
| 无回测 | 阈值无法用历史数据验证误报率。 |
| Telegram 无交互命令 | 只能本地/服务器 CLI 操作，群内不能查询。 |
| 无盘口/热力图 | 虽有 CoinGlass 爆仓接口封装，但未接清算热力图、盘口流动性热力图。 |
| 核心 radar 文件偏大 | `radar.py` 同时承担多种业务，继续加图表和关键位会变重。 |

## 13. 适合升级的方向

| 方向 | 适合程度 | 推荐落点 |
| --- | --- | --- |
| 盘整突破预警 | 适合 | 新建 `paopao_radar/structure_radar.py`，复用 `BinanceDataSource.klines()`。 |
| 压缩突破雷达 | 适合 | 新模块计算 ATR、布林带宽、成交量压缩，避免塞进 `radar.py`。 |
| 关键位临界信号 | 适合 | 新建结构模块，输出 support/resistance/box/high-low levels。 |
| 每小时 55 分扫描 | 适合 | 在 `cli.py::run_loop` 增加单独 scheduler，但不建议直接替换现有间隔循环。 |
| K线状态图推送 | 适合但需新增依赖 | 新建 `charting.py` 和 Telegram `sendPhoto`；需要图表库。 |
| CoinGlass 清算热力图 | 适合但需确认 API 权限 | `data_sources.py` 已有 liquidation exchange list，不等于热力图。 |
| CoinGlass 盘口流动性热力图 | 适合但需确认 API 权限 | 需新增接口封装，不要假设当前已有。 |
| Funding + 多空比过滤 | 适合 | Funding 已有；多空比当前未接，需要 Binance/Coinglass 新接口。 |
| 主动买卖/Taker Buy Sell | 适合 | Binance futures 有相关接口可新增封装；当前未实现。 |
| 突破评分系统 | 适合 | 新模块输出分数，再由 `cli.py` 编排推送。 |

## 14. 不建议直接改动的地方

- 不建议继续把新功能直接塞进 `paopao_radar/radar.py`；它已经包含资金摘要、公告、背离、启动雷达。
- 不建议改 `telegram.py::send()` 的安全门禁；真实推送边界现在比较清楚。
- 不建议破坏 `JsonStore` 的原子写入和坏 JSON 保护。
- 不建议绕过 `DataSource` 直接在业务逻辑里写 requests；否则缓存、重试、预算、诊断会失效。
- 不建议把 API key、token、chat id 写进代码或提交到 GitHub。
- 不建议让新图表功能默认真实推送；应先 dry-run 保存本地图片预览。

## 15. 给后续升级的建议

1. 先新建结构模块：`structure_radar.py`，只负责 K线结构、箱体、关键位、压缩、突破临界。
2. 再新建图表模块：`charting.py`，输出本地 PNG，先不接 Telegram。
3. 给 Telegram 新增 `send_photo()`，但保持默认 dry-run 和真实发送确认。
4. 在 `cli.py` 增加新命令，例如 `structure-radar`，不要直接混进 `once`。
5. 做每小时 55 分扫描时，新增独立 scheduler，例如 `next_hourly_scan_at(minute=55)`，不要影响现有 3 分钟启动雷达。
6. 新增测试：箱体识别、ATR/BB 压缩、接近上沿/下沿、突破/假突破、图表文件生成、sendPhoto dry-run。
7. CoinGlass 热力图和盘口流动性功能需要先确认当前套餐/API 是否有对应端点；当前项目未实现，不应直接假设可用。
8. 升级顺序建议：结构识别 -> 文本预警 -> 小规模 dry-run -> K线图 -> Telegram 图片 -> CoinGlass 热力增强。
