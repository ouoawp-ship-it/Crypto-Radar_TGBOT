# 项目结构

当前目录按运行职责整理：

```text
paopao-crypto-radar/
  main.py                  # 兼容入口，继续支持 python main.py ...
  paopao_radar/            # 核心代码包
    cli.py                 # 命令行、调度、状态、报告
    config.py              # 配置和环境变量
    data_sources.py        # Binance 接口、请求缓存、重试、预算、熔断
    radar.py               # 资金雷达、启动雷达、公告和背离业务逻辑
    telegram.py            # Telegram 推送、dry-run、去重、限流
    storage.py             # JSON 状态读写、原子写、损坏隔离
    maintenance.py         # 运行垃圾清理、旧状态迁移
  scripts/                 # 服务器部署和更新脚本
    install_server.sh
    update_server.sh
  docs/                    # 说明文档
    SERVER_DEPLOY.md
    FINAL_RUNBOOK.txt
    PROJECT_STRUCTURE.md
  tests/                   # 单元测试
  data/                    # 本地运行状态，不上传 GitHub
  .env.oi                  # 本地真实配置，不上传 GitHub
  .env.oi.example          # 配置模板
  requirements.txt
  README.md
```

这样整理后，顶层只保留入口、配置模板、依赖文件和 README；运行代码、说明、脚本、测试互相分开，不影响性能，也不改变部署命令。
