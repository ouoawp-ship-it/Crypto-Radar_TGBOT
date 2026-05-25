# 泡泡抓币中文安装目录

更新时间: 2026-05-25

这份说明用于第一次安装、重新安装、更新和排错。配置文件 `.env.oi` 不会提交到 GitHub，里面只放服务器自己的 token、群 ID 和 API key。

## 1. 第一次安装

在服务器执行:

```bash
cd ~
git clone https://github.com/ouoawp-ship-it/paopao-crypto-radar.git
cd paopao-crypto-radar
bash scripts/install_server.sh
```

安装脚本会进入中文向导，按顺序完成:

1. 显示安装目录和配置文件位置。
2. 安装 `git`、`python3`、`python3-venv`、`python3-pip`。
3. 创建 `.env.oi`。
4. 输入 Telegram bot token 和群 ID。
5. 默认启用 Telegram 话题自动分类，不手动填写话题 ID。
6. 可选输入 CoinGlass API key。
7. 创建 `.venv` 并安装 Python 依赖。
8. 运行编译检查和单元测试。
9. 生成 dry-run 启动观察历史。
10. 运行 readiness。
11. 安装并启动 systemd 服务。

## 2. 输入项说明

`TG_BOT_TOKEN`
: BotFather 给你的机器人 token，格式类似 `123456:ABC...`。

`TG_CHAT_ID`
: Telegram 群 ID，通常类似 `-1001234567890`，也可以是频道用户名 `@channel_username`。

`TG_TOPIC_ID` 以及其他 `TG_..._TOPIC_ID`
: 这是 Telegram 话题的数字 `message_thread_id`。默认不需要填，机器人有权限时会自动创建和记录话题。

`COINGLASS_API_KEY`
: CoinGlass API key。只在脚本提示 `COINGLASS_API_KEY 可选` 时填写。直接回车就是纯 Binance 数据版本。

## 3. Telegram 话题推荐设置

推荐默认配置:

```bash
TELEGRAM_USE_TOPIC=true
TG_AUTO_CREATE_TOPICS=true
TG_TOPIC_INTRO_ENABLE=true
TG_TOPIC_INTRO_PIN=true
```

bot 需要在群里具备这些权限:

- 发送消息
- 管理话题
- 置顶消息

每个话题第一次真实推送前，项目会先发送一条中文说明消息，并尝试置顶。说明消息会解释这个话题推什么、怎么看信号。

## 4. 重新安装

如果你想完全重新安装，并备份旧目录:

```bash
cd ~
pkill -f "main.py daemon" || true

if [ -d paopao-crypto-radar ]; then
  mv paopao-crypto-radar "paopao-crypto-radar-old-$(date +%Y%m%d-%H%M%S)"
fi

git clone https://github.com/ouoawp-ship-it/paopao-crypto-radar.git
cd paopao-crypto-radar
bash scripts/install_server.sh
```

如果你想保留旧 `.env.oi`，先备份:

```bash
cp ~/paopao-crypto-radar/.env.oi /tmp/paopao.env.oi.backup
```

新项目 clone 完之后再恢复:

```bash
cp /tmp/paopao.env.oi.backup ~/paopao-crypto-radar/.env.oi
cd ~/paopao-crypto-radar
bash scripts/install_server.sh
```

## 5. 更新现有项目

```bash
cd ~/paopao-crypto-radar
bash scripts/update_server.sh
```

更新脚本会执行:

- `git pull --ff-only`
- 安装/刷新依赖
- 编译检查
- 单元测试
- 如果存在 systemd 服务，则自动重启服务

## 6. 常用检查命令

```bash
cd ~/paopao-crypto-radar
. .venv/bin/activate

python main.py status
python main.py readiness
python main.py telegram-test --send --confirm-real-send
python main.py coinglass-test
python main.py runtime-status
```

查看服务:

```bash
sudo systemctl status paopao-radar
journalctl -u paopao-radar -f
```

## 7. 手动启动方式

如果你不想用 systemd，也可以手动后台运行:

```bash
cd ~/paopao-crypto-radar
. .venv/bin/activate
pkill -f "main.py daemon" || true
nohup .venv/bin/python -u main.py daemon --send --confirm-real-send > data/runtime.log 2>&1 &
tail -f data/runtime.log
```

## 8. 排错

如果提示 `TG_BOT_TOKEN 缺失或格式无效`:

```bash
nano .env.oi
```

检查:

```bash
TG_BOT_TOKEN=你的bot_token
TG_CHAT_ID=你的群ID
```

如果把 CoinGlass key 错填到了 `TG_TOPIC_ID`，重新运行安装脚本即可。新脚本会检测到非数字话题 ID，并自动清空。

如果 Telegram 话题无法置顶，通常是 bot 缺少置顶消息或管理话题权限。推送本身不会因此停止。
