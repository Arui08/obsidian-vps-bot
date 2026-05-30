# 架构与设计取舍

记录这套系统每个组件为什么这样选，避免后人（或未来的你）踩坑。

## 整体设计

把"内容收集 + 知识沉淀 + 内容输出"三件事接到一起：

- **收集端**：Telegram 是手机最顺手的入口，链接/文字/语音随手丢
- **沉淀端**：Obsidian vault（git 仓库）作为单一事实源，跨设备共享
- **输出端**：定时任务自动产出推文 → TG 转人工发 X / 自动发币安广场

vault 同时是"记忆库"——`/ask` 让 AI 翻检你过去笔记回答问题。

## 关键技术取舍

### 为什么不用 X API 抓推特热点？

X API 升级后免费层基本不能用，付费贵且额度紧。改用：

- **HackerNews Algolia API**：免 token，覆盖 AI / 科技 / 工具
- **CoinGecko**：免 token，币市数据全
- **Reddit JSON**（公共子版块）：免 token，码农圈八卦

代价：抓不到中文 X 热点。但鸟哥/蓝鸟会风格本来就是消化英文内容输出中文，
信息源在英文圈反而更新更快。

### 为什么用 Gemini 而不是 GPT-4？

- 中文质感比 GPT-4o 更接近"人话"，少一些播音腔
- OpenAI 兼容协议有现成中转，价格只有 GPT-4 的零头
- 长上下文（1M token）方便处理整篇 README / 公众号长文

`MODEL_FAST=gemini-2.5-flash` 跑日常任务，
`MODEL_SMART=gemini-2.5-pro` 跑周报这种需要综合的。

### 为什么阶段化节奏（D1-14 / D15-28 / D29+）？

推特新号有 spam 检测，第一周每天疯狂发 20 条容易直接限流。
渐进节奏让账号"看起来像正常人"，同时给你时间观察哪类内容跑数据好。

> 阶段切换不靠 cron 改，而是 daily_brief.py 启动时读 `.bot_start_date`
> 算上线天数，自己判断今天该跑几条 / 哪些时段跑。这样 cron 永远不用动。

### 为什么 SQLite 持久去重而不是 JSON 文件？

最初版本用 `.pushed_today.json` 跨天清空，导致同一篇 HN 帖隔天又被推。
SQLite 优点：

- 跨天 / 跨档 / 跨周永久不重
- URL 归一化（去 query/fragment、域名小写）防止同一链接因参数差异被认作新的
- 标题指纹兜底（不同源同一新闻也能识别）
- 单文件、零配置、Python 内置

### 为什么币安广场只发 noon / night 时段？

这两个时段对应中文用户活跃高峰（午饭、睡前），覆盖大多数币圈用户。
早 8:30 大家在通勤刷推特，但币安广场流量不在此时段；
19:30 是 D29 后才启用的"补量档"，重要性不如黄金时段。

### 为什么不用 Webhook 而用 long polling？

`bot.py` 跑 `app.run_polling()`：

- 不需要公网 IP / 域名 / SSL 证书，部署简单
- 不需要 Telegram → VPS 的回调，避免防火墙问题
- 单用户使用，长轮询的少量延迟无所谓

如果你想做多用户 / 高并发版本，再换 webhook 不迟。

### 为什么 systemd + cron + healthcheck 三层？

| 层 | 作用 | 失效场景 |
|---|------|---------|
| systemd Restart=always | bot.py 崩溃 10 秒内拉起 | systemd 自身炸了 |
| cron healthcheck.sh | 每小时进程检查兜底 | cron 服务停了 |
| crond enabled | 服务器重启自动启动 cron | 服务器挂了 |

一层失效另一层补，"永久生效"的真正含义。

## 数据流

### 写笔记流（用户 → vault）

```
TG 消息 → bot.py 路由识别 → 抓取 (fetch_url/github/tweet)
       → AI 整理 (ai_chat) → write_note(folder, fname, md)
       → git pull / commit / push → TG 反馈
```

### 推文流（cron → TG / 币安）

```
cron 触发 → daily_brief.py <slot>
        → stage_config(days_online) 决定时段和条数
        → SLOT_DOMAINS[slot] 决定领域
        → fetch_*_list(n) 抓候选 → is_pushed() 过滤
        → make_brief_tweet(item) 生成推文
        → mark_pushed() 入 SQLite
        → noon/night 且 crypto → bnb_publish() 发币安
        → 其他 → tg_send 代码块
        → write_note + git push
```

### 问答流（用户 → vault → 用户）

```
/ask 问题 → ask.answer(question)
        → list_vault_notes() 跳过敏感目录
        → tokenize + score_note 打分前 8 篇
        → 拼上下文 → ai_chat(MODEL_SMART)
        → tg_send 分块回复
```

## 文件即数据

| 文件 | 内容 | 是否进 git |
|------|------|-----------|
| `*.py` | 代码 | ✅ |
| `.env` | 凭证 | ❌ |
| `vault/` | 用户笔记 | ❌（vault 自己是另一个 git 仓库） |
| `.bot_start_date` | 上线日期 | ❌ |
| `.pushed.sqlite3` | 去重历史 | ❌ |
| `logs/*.log` | 运行日志 | ❌ |

`.gitignore` 严格守住边界。
