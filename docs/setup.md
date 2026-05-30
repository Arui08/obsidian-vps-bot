# 部署教程

从零部署一台干净 VPS（Debian/Ubuntu）跑这套 Bot。

## 0. 你需要准备

- 一台 VPS（1 核 1G 够用，国外节点首选——能直连 Telegram / OpenAI / GitHub）
- 一个 Telegram Bot Token（@BotFather 申请）
- 一个 OpenAI 兼容的 API key 和 base_url（推荐 Gemini 中转）
- 一个 GitHub 仓库用作 Obsidian vault 的同步后端

## 1. 系统准备

```bash
sudo timedatectl set-timezone Asia/Shanghai
sudo apt update && sudo apt install -y python3-venv git ffmpeg
```

`ffmpeg` 仅 bot.py 处理语音消息时用得上（语音 → 转码 → 多模态识别）。

## 2. 拉代码

```bash
sudo git clone https://github.com/Arui08/obsidian-vps-bot.git /opt/obsidian-bot
cd /opt/obsidian-bot
sudo python3 -m venv venv
sudo ./venv/bin/pip install -r requirements.txt
sudo mkdir -p logs
```

## 3. 配置 .env

```bash
sudo cp .env.example .env
sudo chmod 600 .env
sudo vim .env
```

| 变量 | 怎么来 |
|------|--------|
| `TG_BOT_TOKEN` | @BotFather 创建 Bot 时给你的字符串 |
| `TG_CHAT_ID` | 给 Bot 发任意消息后，访问 `https://api.telegram.org/bot<TOKEN>/getUpdates` 找 `chat.id` |
| `OPENAI_API_KEY` | OpenAI 兼容服务的 key（用 Gemini 走中转更便宜） |
| `OPENAI_BASE_URL` | 中转服务的地址，如 `https://your-host/v1` |
| `MODEL_FAST` | 快模型，默认 `gemini-2.5-flash` |
| `MODEL_SMART` | 慢模型（周报/复杂任务），默认 `gemini-2.5-pro` |
| `VAULT_PATH` | vault 在 VPS 上的路径，默认 `/opt/obsidian-bot/vault` |
| `BINANCE_SQUARE_OPENAPI_KEY` | 币安创作者中心 OpenAPI Key（可选） |

## 4. 挂载 vault

vault 是一个 git 仓库，VPS 会从这里读笔记，bot 写入新笔记后自动 commit + push。

```bash
# 用 deploy key（推荐，权限只限这一个仓库）
sudo ssh-keygen -t ed25519 -f /root/.ssh/obsidian_deploy -N ""
cat /root/.ssh/obsidian_deploy.pub
# 把公钥加到 GitHub 仓库 Settings → Deploy Keys（勾选 Allow write）

# SSH config（强制用这把 key）
sudo tee -a /root/.ssh/config <<EOF
Host github.com
  IdentityFile /root/.ssh/obsidian_deploy
  IdentitiesOnly yes
EOF

# clone vault
sudo git clone git@github.com:你的账号/你的-vault-仓库.git /opt/obsidian-bot/vault
```

> Obsidian 端用 obsidian-git 插件配同一仓库，手机改完笔记会自动 push，VPS 端 bot
> 写新笔记前会 git pull，写完 git push，避免冲突。

## 5. 配 systemd 让 bot.py 常驻

```bash
sudo cp systemd/obsidian-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now obsidian-bot
sudo systemctl status obsidian-bot
```

这个 service 已配 `Restart=always` + `RestartSec=10`，崩了 10 秒内自动拉起。

## 6. 配 cron 跑定时任务

```bash
# 编辑 scripts/crontab.txt 确认时段没问题，然后写入
sudo crontab scripts/crontab.txt
sudo crontab -l
```

每小时一次的 `healthcheck.sh` 是兜底——万一 systemd 失效，cron 这层补救。

## 7. 测试

在 Telegram 里给 Bot 发：

- `/start` — 应回欢迎语
- `/ping` — 应回 pong + 时间戳
- 一条 GitHub 链接 — 应抓 README → 笔记 → 反馈
- `/ask 我最近在关注什么` — 应在 vault 里检索回答

手动跑一档推文：

```bash
cd /opt/obsidian-bot
./venv/bin/python daily_brief.py morning
```

TG 应该收到当档推文，vault 里出现 `Daily/2026-XX-XX_推文.md`。

## 8. 常用排错

```bash
# 看 bot 状态 / 日志
sudo systemctl status obsidian-bot
sudo journalctl -u obsidian-bot -n 100 --no-pager
tail -f /opt/obsidian-bot/logs/bot.log

# 看 daily_brief 历史
tail -100 /opt/obsidian-bot/logs/daily_brief.log

# 看健康检查日志
tail /opt/obsidian-bot/logs/healthcheck.log

# 手动跑某档（不走 cron）
cd /opt/obsidian-bot && ./venv/bin/python daily_brief.py noon
```

## 9. 升级

```bash
cd /opt/obsidian-bot
sudo git pull
sudo ./venv/bin/pip install -r requirements.txt   # 如果依赖变了
sudo systemctl restart obsidian-bot
```

## 10. 备注

- daily_brief 第一次运行会自动写 `.bot_start_date`，标记 D1，从此每天阶段化升级
- 已推过的 URL/标题写在 `.pushed.sqlite3`（不进 git），永久去重
- 想改时段？编辑 `scripts/crontab.txt` 重新 `crontab` 即可
- 想加新领域？参考 [docs/customize.md](customize.md)
