# Obsidian VPS Bot

把 Obsidian 知识库 + Telegram + AI + 自动化推文生成串成一条流水线。

部署在 VPS 上的常驻 Bot，让你在手机上随时把内容丢进 Obsidian 笔记库；
同时按你设定的节奏，每天自动抓多领域热点、用 AI 写成口语化中文推文，
推到 Telegram 让你复制发 X，并把 Web3 内容自动同步到币安广场。

## 能干嘛

### 一、Telegram 笔记助手（bot.py）

把链接/文字/语音发给 Bot，AI 整理成结构化笔记并 commit 到 vault：

- **GitHub 链接** → 抓 README + 元信息 → 笔记结构（项目简介/核心功能/技术栈/适用场景/亮点）
- **X / Twitter 推文** → 通过 fxtwitter 抓原文 → 提炼笔记
- **微信公众号文章** → 移动版 UA 抓正文 → 文章脉络/关键观点/延伸思考
- **普通网页** → BeautifulSoup 抓正文 → 一句话总结/关键观点/细节/延伸方向
- **YouTube / B 站** → 抓标题描述 → 推断主题和看点
- **纯文字** → 快速记入 Inbox/

### 二、Vault 问答（ask.py）

`/ask 我之前看过哪些 AI agent 相关内容` —— 在你的 vault 里检索相关笔记，
拼上下文给 AI 生成回答，自动跳过 `随记/`、`客户信息/` 等敏感目录。

### 三、每日多档推文（daily_brief.py，重点）

每天分时段抓 4 个领域的热点 → AI 写成"鸟哥风"中文推文 → 同步到 TG / 币安广场 / vault。

**阶段化节奏（按上线天数自动升级）**

| 阶段 | 时段 | 每档条数 | 每天总量 |
|------|------|---------|---------|
| D1-D14 | 8:30 / 12:30 / 21:00 | 2 | 6 条 |
| D15-D28 | 8:30 / 12:30 / 21:00 | 3 | 9 条 |
| D29+ | 8:30 / 12:30 / 19:30 / 21:00 | 3 | 12 条 |

**时段领域分配**

| 时段 | 领域 |
|------|------|
| 早 8:30 | AI + Web3 |
| 午 12:30 | 科技热议 + 实用软件 |
| 晚高峰 19:30（D29+） | AI + 实用软件 |
| 睡前 21:00 | Web3 + 科技热议 |

**信息源（全部免 token）**

- HackerNews Algolia API（AI 故事 / front_page / Show HN）
- CoinGecko `/coins/markets` 24h 涨幅榜（市值 5000 万刀以上过滤土狗）
- Reddit r/programming JSON

**推文风格（鸟哥/蓝鸟会风）**

- 不超过 300 字，段落之间空一行
- 零 emoji，列点用纯文本数字
- 开头钩子句，禁止"今天给大家推荐"类播音腔
- 数字、痛点共鸣、不正经的行动召唤
- 每条带 3-4 个相关 hashtag

**永久去重**

SQLite 记录所有已推 URL（归一化）+ 标题指纹，跨天/跨档/跨周不重复。

**币安广场自动同步**

noon / night 时段的 crypto 推文自动发到币安广场（基于
[binance-skills-hub/square-post](https://github.com/binance/binance-skills-hub/tree/main/skills/binance/square-post)），
TG 只发"已发币安"通知 + 链接，不重复推全文。

### 四、其他自动化

- `trending.py`：每日 GitHub Trending → 鸟哥风推文
- `weekly.py`：每周 vault 复盘（扫近 7 天笔记 → 关注主题/知识吸收/思维变化）
- `healthcheck.sh`：每小时检查 bot 进程，挂了就 systemctl restart

## 架构

```
┌─────────────┐         ┌──────────────────────────────┐
│  Telegram   │ ←──────→│  bot.py (systemd, 长驻)      │
│ (你 + Bot)  │         └──────────────────────────────┘
└─────────────┘                       │
       ↑                              ↓
       │                       ┌──────────────┐
       │                       │  Obsidian    │
       │   (定时推送)           │  vault (git) │
       │                       └──────────────┘
┌──────────────┐                      ↑
│ daily_brief  │                      │
│   .py        │ ─── 写笔记 ──────────┘
│ (cron 4 档)  │
└──────────────┘
       │
       ├── HN Algolia / CoinGecko / Reddit  抓信息
       ├── Gemini (OpenAI 兼容协议)         生成推文
       ├── Telegram                         推给你复制发 X
       └── 币安广场 OpenAPI                  自动发 Web3 推文
```

## 快速开始

完整教程见 [docs/setup.md](docs/setup.md)，简版：

```bash
# 1. clone 到 VPS
git clone https://github.com/Arui08/obsidian-vps-bot.git /opt/obsidian-bot
cd /opt/obsidian-bot

# 2. 安装依赖
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# 3. 配置
cp .env.example .env && chmod 600 .env
vim .env   # 填 TG_BOT_TOKEN / OPENAI_API_KEY 等

# 4. 准备 vault（git 仓库挂在 vault/）
git clone <你的 obsidian-vault 仓库> vault

# 5. 部署 systemd + cron
sudo cp systemd/obsidian-bot.service /etc/systemd/system/
sudo systemctl enable --now obsidian-bot
crontab scripts/crontab.txt

# 6. Bot 已就绪，给它发 /start 试试
```

## 文件结构

```
.
├── bot.py                Telegram Bot 主程序
├── common.py             共用工具（env / AI / TG / git / 抓取）
├── daily_brief.py        每日多档推文生成
├── binance_square.py     币安广场发帖封装
├── trending.py           GitHub Trending 推文
├── weekly.py             每周 vault 复盘
├── ask.py                vault 问答检索
├── scripts/
│   ├── healthcheck.sh    每小时进程健康检查
│   └── crontab.txt       cron 模板
├── systemd/
│   └── obsidian-bot.service
├── docs/
│   ├── setup.md          完整部署教程
│   ├── architecture.md   架构与设计取舍
│   └── customize.md      改信息源 / 改风格 / 加新领域
├── .env.example          环境变量样板
├── .gitignore            排除凭证 / 笔记 / 日志
└── requirements.txt
```

## 安全说明

- `.env`、`vault/`、`*.sqlite3`、`logs/` 全部在 `.gitignore`，不会进仓库
- TG Bot 通过 `is_allowed()` 只接受配置的 `TG_CHAT_ID`，陌生人发不进来
- VPS 推 vault 用 deploy key（仅 obsidian-vault 仓库可 push），不用全权限 PAT
- 币安 API key 仅当 .env 里有 `BINANCE_SQUARE_OPENAPI_KEY` 时才启用

## 致谢

- [binance-skills-hub](https://github.com/binance/binance-skills-hub) 提供币安广场发帖接口
- [fxtwitter](https://github.com/FxEmbed/FxEmbed) 让推文抓取免 X API token
- HackerNews Algolia / CoinGecko / Reddit 提供免费数据源

## 协议

MIT
