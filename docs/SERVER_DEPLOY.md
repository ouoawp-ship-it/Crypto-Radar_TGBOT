# 服务器一键部署说明

更新时间: 2026-05-25

## 最短部署命令

服务器能访问这个 GitHub 私有仓库后，执行:

```bash
git clone https://github.com/ouoawp-ship-it/paopao-crypto-radar.git
cd paopao-crypto-radar
bash scripts/install_server.sh
```

第一次运行时，如果 `.env.oi` 不存在，脚本会自动从 `.env.oi.example` 创建它。
如果 `TG_BOT_TOKEN` 或 `TG_CHAT_ID` 为空，脚本会直接在终端提示你输入。空回车、占位符或格式不对会反复提示，不会继续安装或启动服务。

## 脚本会自动做什么

`scripts/install_server.sh` 会自动执行:

- 安装 Linux 系统依赖: `git`、`python3`、`python3-venv`、`python3-pip`
- 创建 `.venv`
- 安装 `requirements.txt`
- 编译检查核心 Python 文件
- 跑单元测试
- 生成 dry-run 启动观察历史
- 执行 `python main.py readiness`
- 写入 systemd 服务 `/etc/systemd/system/paopao-radar.service`
- 启动并设置开机自启
- 启动后由程序定时清理临时文件、坏 JSON 备份、过期日志和过长历史

如果当前不是交互式终端，脚本无法安全读取配置，会停下提示你手动编辑 `.env.oi`，不会启动真实推送。

## 私有仓库 clone 问题

当前仓库是 private。服务器第一次 clone 需要具备 GitHub 访问权限。

可以用以下任一方式:

- 在服务器上配置 GitHub CLI: `gh auth login`
- 使用 SSH key，把公钥加到 GitHub
- 临时使用带权限的 HTTPS token clone

服务器能正常 `git clone` 之后，后面部署就是一条命令。

## 常用环境变量

安装但不自动启动服务:

```bash
AUTO_START=0 bash scripts/install_server.sh
```

安装时发送一条 Telegram 测试消息:

```bash
RUN_TELEGRAM_TEST=1 bash scripts/install_server.sh
```

减少 dry-run 预热轮数:

```bash
BOOTSTRAP_CYCLES=2 bash scripts/install_server.sh
```

修改 systemd 服务名:

```bash
SERVICE_NAME=paopao-radar-prod bash scripts/install_server.sh
```

## 查看运行状态

```bash
sudo systemctl status paopao-radar
journalctl -u paopao-radar -f
python main.py runtime-status
python main.py about
python main.py cleanup --force-cleanup
```

## 一键更新

```bash
cd /home/ubuntu/paopao-crypto-radar
bash scripts/update_server.sh
```

更新脚本会执行:

- `git pull --ff-only`
- 安装依赖
- 编译检查
- 单元测试
- 重启 systemd 服务

## 手动停止和启动

```bash
sudo systemctl stop paopao-radar
sudo systemctl start paopao-radar
sudo systemctl restart paopao-radar
```
