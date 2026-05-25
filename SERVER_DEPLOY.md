# 服务器部署说明

更新时间: 2026-05-25

## 1. 上传 GitHub 前

建议上传新项目代码，不上传本地状态和旧原型。

`.gitignore` 已忽略:

- `.env.oi`
- `.env`
- `.venv/`
- `data/*.json`
- `data/*.db`
- `data/*.txt`
- `*.log`
- `*.bat`
- `crypto_monitor_merged.py`

`crypto_monitor_merged.py` 可以继续留在本机当参考，但不建议部署到服务器。

## 2. 初始化 Git 仓库

在项目目录运行:

```bash
git init
git add .
git commit -m "Initial crypto radar rebuild"
git branch -M main
git remote add origin git@github.com:你的用户名/你的仓库名.git
git push -u origin main
```

如果用 HTTPS:

```bash
git remote add origin https://github.com/你的用户名/你的仓库名.git
git push -u origin main
```

## 3. 服务器安装

服务器建议使用 Linux。进入你准备放项目的目录:

```bash
git clone git@github.com:你的用户名/你的仓库名.git
cd 你的仓库名
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.oi.example .env.oi
```

然后编辑 `.env.oi`，填入服务器上的真实配置:

```bash
nano .env.oi
```

不要把 `.env.oi` 提交到 GitHub。

## 4. 服务器上线前验证

```bash
. .venv/bin/activate
python main.py status
python main.py readiness
python main.py observe --duration-minutes 0 --launch-interval 60 --launch-scan-limit 5 --records 20 --top 5
python main.py telegram-test --send --confirm-real-send
```

确认 Telegram 收到测试消息后，再启动真实 live。

## 5. 临时运行

用 tmux:

```bash
tmux new -s paopao
. .venv/bin/activate
python main.py live --send --confirm-real-send
```

退出 tmux 但不停止程序:

```text
Ctrl+B 然后按 D
```

重新进入:

```bash
tmux attach -t paopao
```

## 6. systemd 长期运行

创建服务文件:

```bash
sudo nano /etc/systemd/system/paopao-radar.service
```

示例内容，把路径和用户改成你服务器自己的:

```ini
[Unit]
Description=Paopao Crypto Radar
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/paopao-radar
ExecStart=/home/ubuntu/paopao-radar/.venv/bin/python /home/ubuntu/paopao-radar/main.py live --send --confirm-real-send
Restart=always
RestartSec=15
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

启动:

```bash
sudo systemctl daemon-reload
sudo systemctl enable paopao-radar
sudo systemctl start paopao-radar
```

查看状态:

```bash
sudo systemctl status paopao-radar
journalctl -u paopao-radar -f
```

查看程序自己的心跳:

```bash
. .venv/bin/activate
python main.py runtime-status
```

## 7. 更新部署

```bash
cd /home/ubuntu/paopao-radar
git pull
. .venv/bin/activate
pip install -r requirements.txt
python -m py_compile main.py config.py storage.py data_sources.py telegram.py radar.py maintenance.py
python -m unittest discover -s tests -v
sudo systemctl restart paopao-radar
```
