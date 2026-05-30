# 定制指南

改信息源、调推文风格、加新领域、换推送时段，都在这里说清。

## 改时段或条数

`daily_brief.py` 顶部：

```python
def stage_config(day: int) -> dict:
    if day <= 14:
        return {"stage": 1, "slots": ["morning", "noon", "night"], "per_slot": 2}
    if day <= 28:
        return {"stage": 2, "slots": ["morning", "noon", "night"], "per_slot": 3}
    return {"stage": 3, "slots": ["morning", "noon", "evening", "night"], "per_slot": 3}
```

改阶段天数 / 每档条数 / 启用哪些时段。

要改 cron 触发的具体时间，编辑 `scripts/crontab.txt` 后重新 `crontab` 即可。

## 改时段对应的领域

```python
SLOT_DOMAINS = {
    "morning": ["ai", "crypto"],
    "noon": ["tech", "tools"],
    "evening": ["ai", "tools"],
    "night": ["crypto", "tech"],
}
```

每个时段绑两个领域，per_slot 条总量在两个领域里平均分，多余 1 条按日期/时段轮换偏向。

## 加一个新领域

举例：加"独立开发"领域，抓 IndieHackers 热帖。

1. 在 `daily_brief.py` 加 fetcher：

```python
def fetch_indie_list(n: int = 6) -> list:
    r = requests.get("https://www.indiehackers.com/api/...", timeout=20)
    # 解析返回 [{domain:"indie", title, url, extra}, ...]
```

2. 注册到 `DOMAIN_FETCHERS`：

```python
DOMAIN_FETCHERS = {
    "ai": fetch_ai_list,
    "crypto": fetch_crypto_list,
    "tech": fetch_tech_buzz_list,
    "tools": fetch_tools_list,
    "indie": fetch_indie_list,  # 新增
}
```

3. 加到 `SLOT_DOMAINS` 某个时段：

```python
"evening": ["ai", "indie"],   # 把 tools 换成 indie
```

4. 加风格配置：

```python
DOMAIN_STYLE = {
    ...
    "indie": {
        "label": "独立开发",
        "persona": "在小作坊单枪匹马做产品的 indie 开发者",
        "vibes": "'刚把订阅做上 1k MRR'、'又被一个老外的小生意惊到'、有点羡慕，又有点跃跃欲试",
    },
}
```

5. 加 TG header：

```python
DOMAIN_HEADERS = {
    ...
    "indie": "【独立开发】",
}
```

完事。

## 改推文风格

`make_brief_tweet()` 的 prompt 是核心。要点：

- **铁律 1-10 严格执行**——AI 见到铁律会照做，模糊的"温和建议"它会忽略
- **vibes** 字段决定开头钩子的"调性"，写得越具体越像
- **persona** 提供身份代入，影响整体语气
- **link_kind**（仅 tools 域）告诉 AI 链接类型，让行动召唤更准

如果想要更"硬核技术博主"风格，把 vibes 换成"'我跑了一遍 benchmark'、'这优化思路有点秀'"
之类即可。

## 关掉 emoji 限制（如果你想用 emoji）

铁律 3 改掉就行——但实测加 emoji 会让推文更像营销号，不像鸟哥风。

## 关掉币安广场同步

不配 `BINANCE_SQUARE_OPENAPI_KEY` 即可，binance_square 模块会跳过。

## 改"上线日期"重置阶段

```bash
echo "2026-06-01" > /opt/obsidian-bot/.bot_start_date
```

之后 daily_brief 就从那天算 D1。

## 重置去重数据库

```bash
rm /opt/obsidian-bot/.pushed.sqlite3
```

下次跑就是一张白纸（慎用，可能再推已发过的内容）。

如果只想清掉某个 URL 让它能再发一次：

```bash
sqlite3 /opt/obsidian-bot/.pushed.sqlite3 "DELETE FROM pushed WHERE url_norm LIKE '%关键词%';"
```

## 加链接类型识别

`_is_product_url()` / `_link_kind()` 控制工具类 URL 的"产品入口"判定。
要排除新的博客域名，往 `bad_hosts` 加；要识别新的产品域名后缀，扩 `_link_kind`。

## 改 AI 模型

`.env` 里改 `MODEL_FAST` / `MODEL_SMART`，配合 `OPENAI_BASE_URL` 切换到任何
OpenAI 兼容服务（Anthropic 中转、deepseek、智谱等）。

## TG 不再发完整推文（全部走币安）

把 `run_slot()` 里下面这段：

```python
else:
    safe_tw = tw.replace("```", "'''")
    tg_send(f"{domain_tag}\n```\n{safe_tw}\n```", parse_mode="Markdown")
```

改成只发标题 + 链接。完整推文还在 vault 里能查。
