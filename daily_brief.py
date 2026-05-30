"""每日多档推送：抓 AI / Web3 / 科技热议 / 实用软件 → 鸟哥风推文 → TG 推送 + 存 vault

阶段化节奏（按上线天数自动升级）：
  D1-D14：3 时段（早 8:30 / 午 12:30 / 晚 21:00），每档 2 条 → 6 条/天
  D15-D28：3 时段，每档 3 条 → 9 条/天
  D29+：4 时段（加 19:30），每档 3 条 → 12 条/天

时段领域分配：
  早 morning  : AI + Web3
  午 noon     : 科技热议 + 实用软件
  晚高峰 evening (D29+起): AI + 实用软件
  睡前 night  : Web3 + 科技热议

去重：SQLite 永久记录所有已推过的 URL 和标题指纹，跨天/跨档不重复。
"""
import hashlib
import json
import sys
import re
import sqlite3
from datetime import datetime
from pathlib import Path
import requests

from common import (
    MODEL_FAST, ai_chat, git_pull, git_commit_push,
    write_note, safe_filename, today_str, now_str, now_cn,
    tg_send,
)
try:
    from binance_square import publish_text as bnb_publish, SquareError
except Exception as _e:
    bnb_publish = None
    SquareError = Exception
    print(f"binance_square 模块不可用: {_e}")


UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

BOT_DIR = Path(__file__).parent
START_FILE = BOT_DIR / ".bot_start_date"
DEDUP_DB = BOT_DIR / ".pushed.sqlite3"
LEGACY_PUSHED_FILE = BOT_DIR / ".pushed_today.json"


# ---------------- 阶段 / 时段路由 ----------------

SLOT_ORDER = ["morning", "noon", "evening", "night"]

SLOT_LABEL = {
    "morning": "早报",
    "noon": "午刊",
    "evening": "晚高峰",
    "night": "夜读",
}

SLOT_DOMAINS = {
    "morning": ["ai", "crypto"],
    "noon": ["tech", "tools"],
    "evening": ["ai", "tools"],
    "night": ["crypto", "tech"],
}


def get_start_date() -> datetime:
    """读取/初始化上线日期"""
    if START_FILE.exists():
        try:
            return datetime.strptime(START_FILE.read_text().strip(), "%Y-%m-%d")
        except Exception:
            pass
    today = now_cn().strftime("%Y-%m-%d")
    START_FILE.write_text(today)
    return datetime.strptime(today, "%Y-%m-%d")


def days_online() -> int:
    start = get_start_date()
    today = datetime.strptime(now_cn().strftime("%Y-%m-%d"), "%Y-%m-%d")
    return (today - start).days + 1  # 第1天 = 1


def stage_config(day: int) -> dict:
    """根据上线天数返回当前阶段配置"""
    if day <= 14:
        return {"stage": 1, "slots": ["morning", "noon", "night"], "per_slot": 2}
    if day <= 28:
        return {"stage": 2, "slots": ["morning", "noon", "night"], "per_slot": 3}
    return {"stage": 3, "slots": ["morning", "noon", "evening", "night"], "per_slot": 3}


# ---------------- 去重（SQLite 永久） ----------------

def _normalize_url(u: str) -> str:
    """URL 简单归一：去查询参数和 fragment、统一小写域名、剥末尾斜杠"""
    if not u:
        return ""
    u = u.strip().split("#", 1)[0].split("?", 1)[0].rstrip("/")
    m = re.match(r"^(https?://)([^/]+)(.*)$", u)
    if m:
        u = m.group(1) + m.group(2).lower() + m.group(3)
    return u


def _title_fp(title: str) -> str:
    """标题指纹：小写、去标点、空白合并"""
    t = (title or "").lower()
    t = re.sub(r"[^\w一-鿿]+", "", t)
    return hashlib.md5(t.encode("utf-8")).hexdigest()[:16] if t else ""


def _db():
    conn = sqlite3.connect(str(DEDUP_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pushed (
            url_norm TEXT PRIMARY KEY,
            title_fp TEXT,
            domain TEXT,
            first_pushed_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_title_fp ON pushed(title_fp)")
    conn.commit()
    return conn


def is_pushed(url: str, title: str) -> bool:
    nu = _normalize_url(url)
    tf = _title_fp(title)
    if not nu and not tf:
        return False
    with _db() as c:
        if nu:
            row = c.execute("SELECT 1 FROM pushed WHERE url_norm=? LIMIT 1", (nu,)).fetchone()
            if row:
                return True
        if tf:
            row = c.execute("SELECT 1 FROM pushed WHERE title_fp=? LIMIT 1", (tf,)).fetchone()
            if row:
                return True
    return False


def mark_pushed(url: str, title: str, domain: str):
    nu = _normalize_url(url)
    tf = _title_fp(title)
    if not nu:
        return
    with _db() as c:
        c.execute(
            "INSERT OR IGNORE INTO pushed(url_norm, title_fp, domain, first_pushed_at) VALUES(?,?,?,?)",
            (nu, tf, domain, now_str()),
        )
        c.commit()


# ---------------- 信息源（每个返回候选列表，便于去重和取多条）----------------

def _hn_url(h: dict) -> str:
    """干净地拿 HN hit 的外链 URL；过滤掉 vote 等无效链接，回退到讨论页"""
    u = (h.get("url") or "").strip()
    if u and "news.ycombinator.com/vote" not in u and "news.ycombinator.com/login" not in u:
        return u
    return f"https://news.ycombinator.com/item?id={h.get('objectID')}"


def fetch_ai_list(n: int = 6) -> list:
    """AI 领域：仅取近期、有外链的 HN 故事帖"""
    try:
        r = requests.get(
            "https://hn.algolia.com/api/v1/search_by_date",
            params={
                "tags": "story",
                "query": "AI OR LLM OR GPT OR Claude OR Gemini OR Anthropic OR OpenAI",
                "numericFilters": "points>50",
                "hitsPerPage": 50,
            },
            timeout=20, headers=UA,
        )
        r.raise_for_status()
        hits = r.json().get("hits", [])
        def good(h):
            u = (h.get("url") or "").strip()
            if not u:
                return False
            bad = ["news.ycombinator.com/vote", "news.ycombinator.com/login",
                   "news.ycombinator.com/item"]
            return not any(b in u for b in bad)
        hits = [h for h in hits if good(h)]
        hits.sort(key=lambda h: h.get("points", 0), reverse=True)
        out = []
        for h in hits[:n * 3]:
            out.append({
                "domain": "ai",
                "title": h.get("title", ""),
                "url": h.get("url"),
                "extra": f"HackerNews {h.get('points',0)}赞 / {h.get('num_comments',0)}评",
            })
        return out
    except Exception as e:
        print(f"fetch_ai_list 失败: {e}")
        return []


def fetch_crypto_list(n: int = 6) -> list:
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={
                "vs_currency": "usd",
                "order": "price_change_percentage_24h_desc",
                "per_page": 50,
                "page": 1,
                "price_change_percentage": "24h,7d",
            },
            timeout=20, headers=UA,
        )
        r.raise_for_status()
        coins = r.json()
        filtered = [c for c in coins if (c.get("market_cap") or 0) > 50_000_000]
        if not filtered:
            filtered = coins[:10]
        out = []
        for c in filtered[:n * 3]:
            sym = (c.get("symbol") or "").upper()
            name = c.get("name", "")
            price = c.get("current_price", 0)
            chg24 = c.get("price_change_percentage_24h") or 0
            chg7d = c.get("price_change_percentage_7d_in_currency") or 0
            mcap = c.get("market_cap") or 0
            vol = c.get("total_volume") or 0
            out.append({
                "domain": "crypto",
                "title": f"{name} ({sym}) 24h {chg24:+.1f}%",
                "url": f"https://www.coingecko.com/en/coins/{c.get('id','')}",
                "extra": (
                    f"{name} ({sym}) 现价 ${price:,.4f} | "
                    f"24h {chg24:+.1f}% / 7d {chg7d:+.1f}% | "
                    f"市值 ${mcap/1e8:.2f}亿 / 成交 ${vol/1e8:.2f}亿"
                ),
            })
        return out
    except Exception as e:
        print(f"fetch_crypto_list 失败: {e}")
        return []


def fetch_tech_buzz_list(n: int = 6) -> list:
    candidates = []
    try:
        r = requests.get(
            "https://hn.algolia.com/api/v1/search",
            params={"tags": "front_page", "hitsPerPage": 30},
            timeout=20, headers=UA,
        )
        r.raise_for_status()
        for h in r.json().get("hits", []):
            candidates.append({
                "domain": "tech",
                "title": h.get("title", ""),
                "url": _hn_url(h),
                "_pts": h.get("points", 0),
                "_cmt": h.get("num_comments", 0),
                "extra": f"HN {h.get('points',0)}赞 / {h.get('num_comments',0)}评",
            })
    except Exception as e:
        print(f"HN front_page 失败: {e}")
    try:
        r = requests.get(
            "https://www.reddit.com/r/programming/hot.json?limit=20",
            timeout=20,
            headers={"User-Agent": "obsidian-bot/1.0"},
        )
        if r.ok:
            for c in r.json().get("data", {}).get("children", []):
                d = c.get("data", {})
                candidates.append({
                    "domain": "tech",
                    "title": d.get("title", ""),
                    "url": d.get("url_overridden_by_dest") or f"https://reddit.com{d.get('permalink','')}",
                    "_pts": d.get("score", 0),
                    "_cmt": d.get("num_comments", 0),
                    "extra": f"Reddit {d.get('score',0)}赞 / {d.get('num_comments',0)}评",
                })
    except Exception as e:
        print(f"Reddit 失败: {e}")
    candidates.sort(key=lambda x: x.get("_cmt", 0), reverse=True)
    return candidates[:n * 3]


def _is_product_url(u: str) -> bool:
    """判断是不是真·产品/项目入口（GitHub 仓库、产品官网、在线工具站点）"""
    u = (u or "").lower()
    if not u:
        return False
    # 明显的产品/工具站
    if "github.com/" in u and re.search(r"github\.com/[^/]+/[^/?#]+", u):
        return True
    # 排除纯文章、博客、论坛、新闻
    bad_hosts = [
        "medium.com", "substack.com", "dev.to", "hashnode.com", "wordpress.com",
        "wikipedia.org", "stackoverflow.com", "reddit.com", "ycombinator.com",
        "arxiv.org", "nytimes.com", "bbc.com", "cnn.com", "bloomberg.com",
        "techcrunch.com", "theverge.com", "wired.com", "ars-technica.com",
    ]
    for b in bad_hosts:
        if b in u:
            return False
    # 排除明显的博客路径（/blog/、/posts/、/article/）
    if re.search(r"/(blog|posts?|articles?|news|stories)/", u):
        return False
    return True


def _link_kind(u: str) -> str:
    """给 prompt 用：标明链接类型"""
    u = (u or "").lower()
    if "github.com/" in u:
        return "GitHub 开源仓库（可直接 clone / 看 README / 下载 release）"
    if any(x in u for x in [".app", ".dev", ".io", ".tools"]):
        return "产品官网（可直接试用/下载）"
    return "工具主页"


def fetch_tools_list(n: int = 6) -> list:
    """实用软件：HN Show HN，仅取近期、有外部链接、且是真·产品入口的"""
    try:
        r = requests.get(
            "https://hn.algolia.com/api/v1/search_by_date",
            params={
                "tags": "show_hn",
                "numericFilters": "points>30",
                "hitsPerPage": 80,
            },
            timeout=20, headers=UA,
        )
        r.raise_for_status()
        hits = r.json().get("hits", [])
        def good(h):
            u = (h.get("url") or "").strip()
            if not u:
                return False
            bad_substr = ["news.ycombinator.com/vote", "news.ycombinator.com/login",
                          "news.ycombinator.com/item"]
            if any(b in u for b in bad_substr):
                return False
            return _is_product_url(u)
        hits = [h for h in hits if good(h)]
        hits.sort(key=lambda h: h.get("points", 0), reverse=True)
        out = []
        for h in hits[:n * 3]:
            title = re.sub(r"^Show HN:\s*", "", h.get("title", ""))
            u = h.get("url")
            out.append({
                "domain": "tools",
                "title": title,
                "url": u,
                "link_kind": _link_kind(u),
                "extra": f"Show HN {h.get('points',0)}赞 / {h.get('num_comments',0)}评",
            })
        return out
    except Exception as e:
        print(f"fetch_tools_list 失败: {e}")
        return []


DOMAIN_FETCHERS = {
    "ai": fetch_ai_list,
    "crypto": fetch_crypto_list,
    "tech": fetch_tech_buzz_list,
    "tools": fetch_tools_list,
}


def pick_items(domain: str, count: int) -> list:
    """挑 count 条数据库里没推过的"""
    pool = DOMAIN_FETCHERS[domain](count)
    picked = []
    for it in pool:
        if is_pushed(it.get("url", ""), it.get("title", "")):
            continue
        picked.append(it)
        if len(picked) >= count:
            break
    return picked


# ---------------- 推文生成 ----------------

DOMAIN_STYLE = {
    "ai": {
        "label": "AI",
        "persona": "整天泡在AI圈、爱吹水的科技博主",
        "vibes": "炸裂感、'又被AI整不会了'、'卷王又出新活'、'OpenAI/Google又在打架'",
    },
    "crypto": {
        "label": "Web3",
        "persona": "币圈老哥，老韭菜，半夜盯K线那种",
        "vibes": "'兄弟们冲不冲'、'又一个百倍预定？'、'盘前盘后看看就行'，对涨跌有戏谑感、不要喊单",
    },
    "tech": {
        "label": "科技热议",
        "persona": "天天看HN和Reddit的码农八卦号",
        "vibes": "'今天群里又在吵这个'、'一觉醒来HN炸了'、带点吃瓜口吻",
    },
    "tools": {
        "label": "实用软件",
        "persona": "装机狂魔、什么新软件都想试一下",
        "vibes": "'又被一个小工具治好了'、'懒人福音'、'相见恨晚'",
    },
}


def make_brief_tweet(item: dict) -> str:
    style = DOMAIN_STYLE[item["domain"]]
    link_hint = ""
    if item.get("link_kind"):
        link_hint = f"\n链接性质：{item['link_kind']}（行动召唤要呼应这种'能直接用/直接下'的属性，比如'丢进去试一下'、'star一下回头玩'、'下载体验下'）"
    prompt = f"""你是一个混迹在中文科技圈的推特博主，具体角色：{style['persona']}。风格像"鸟哥|蓝鸟会"那种——口语化、有钩子、有人味。

现在要根据下面这条信息写一条**中文推文**，发布在朋友圈/推特上。

铁律（违反任何一条都算失败）：
1. **总字数严格不超过300字**，宁可少不要多。
2. **段落之间必须空一行**，让排版清爽。整篇大致结构：钩子段 → 说人话讲是啥 → 1-2 个亮点/槽点/数据 → 不正经的行动召唤。
3. **绝对不用 emoji**，一个都不许（包括 🔥⭐✨🚀💡✅❌📌👇 和带圈数字）。要列点用 "1. 2." 这种纯文本。
4. **开头第一句必须是钩子**，要有情绪，参考味道：{style['vibes']}。**禁止**"今天给大家推荐"、"为大家介绍"、"分享一个"、"近期发现"这种播音腔。
5. **说人话**，不堆专业术语，能用大白话就用大白话。可以带一点"我"、"兄弟们"、"说实话"、"绝了"、"有点东西"这种聊天感。
6. 如果有具体数字（涨幅、价格、stars、点赞数），**必须在推文里出现**，数字是说服力。
7. **纯文本**，不用 markdown 的 # 标题、不用 ** 加粗、不用 > 引用块、不用反引号。
8. 行动召唤随意点，参考："反正我是先 mark 了"、"自己看，别光信我"、"懂的都懂"、"准备搞来玩玩"、"链接在底下"。
9. 行动召唤之后**单独一行**附上链接：{item['url']}
10. **最后另起一行**，3-4 个相关中文 hashtag（# 开头空格分隔），贴合主题。

主题领域：{style['label']}
信息源：{item.get('extra','')}{link_hint}
标题：{item['title']}
链接：{item['url']}

直接输出推文正文，不要"好的我来写"这种废话开头。再次提醒：总字数≤300字，段落之间空一行。"""
    return ai_chat(prompt, model=MODEL_FAST, max_tokens=1200).strip()


# ---------------- 主流程 ----------------

DOMAIN_HEADERS = {
    "ai": "【AI 前沿】",
    "crypto": "【Web3 热币】",
    "tech": "【科技热议】",
    "tools": "【实用软件】",
}


def run_slot(slot: str):
    """跑指定时段（morning/noon/evening/night）"""
    cfg = stage_config(days_online())
    if slot not in cfg["slots"]:
        msg = f"⏭ {slot} 不在第{cfg['stage']}阶段的时段列表，跳过"
        print(msg)
        return msg

    git_pull()
    domains = SLOT_DOMAINS[slot]
    per_slot = cfg["per_slot"]
    # 按 per_slot 总条数在两个领域里平均分（per_slot=2 → 各1条；per_slot=3 → 一个2条一个1条，轮流偏向）
    half = per_slot // 2
    extra = per_slot - half * 2  # 0 或 1
    # 让"偏 1 条"在不同时段轮换，避免某个领域永远多
    primary = domains[0] if (datetime.strptime(today_str(), "%Y-%m-%d").toordinal() + SLOT_ORDER.index(slot)) % 2 == 0 else domains[1]
    secondary = domains[1] if primary == domains[0] else domains[0]
    counts = {primary: half + extra, secondary: half}
    if per_slot == 2:
        counts = {domains[0]: 1, domains[1]: 1}

    items = []
    for d in domains:
        n = counts.get(d, 0)
        if n <= 0:
            continue
        picked = pick_items(d, n)
        items.extend(picked)

    if not items:
        tg_send(f"⚠️ {today_str()} {SLOT_LABEL[slot]}：所有领域都没新内容（已推完）")
        return "无新内容"

    tweets = []
    for it in items:
        try:
            tw = make_brief_tweet(it)
            tweets.append((it, tw))
            mark_pushed(it.get("url", ""), it.get("title", ""), it.get("domain", ""))
        except Exception as e:
            print(f"生成推文失败 {it.get('domain')}: {e}")

    if not tweets:
        tg_send(f"⚠️ {today_str()} {SLOT_LABEL[slot]}：生成全失败")
        return "生成失败"

    # ---- 先尝试同步币安广场（黄金时段 noon / night 的 crypto） ----
    binance_results = []
    binance_failed_items = set()
    if slot in ("noon", "night") and bnb_publish is not None:
        for it, tw in tweets:
            if it["domain"] != "crypto":
                continue
            try:
                res = bnb_publish(tw)
                binance_results.append((it, res))
            except SquareError as e:
                binance_failed_items.add(id(it))
                tg_send(f"⚠️ 币安广场发帖失败：{it.get('title','')[:40]}\n{e}")
            except Exception as e:
                binance_failed_items.add(id(it))
                tg_send(f"⚠️ 币安广场异常：{e}")

    bnb_ok_ids = {id(it) for it, _ in binance_results}

    # ---- TG 推送：已发币安的只推成功通知，其他推完整代码块（用于复制到 X） ----
    header = (
        f"📰 {today_str()} {SLOT_LABEL[slot]}（D{days_online()} · 阶段{cfg['stage']}）\n"
        f"本档 {len(tweets)} 条\n"
    )
    tg_send(header)

    for it, tw in tweets:
        domain_tag = DOMAIN_HEADERS.get(it["domain"], "")
        if id(it) in bnb_ok_ids:
            res = next(r for i, r in binance_results if id(i) == id(it))
            link = res.get("shareLink") or f"id={res.get('id')}"
            tg_send(f"🪙 已发币安广场 · {domain_tag} {it['title'][:40]}\n{link}")
        else:
            safe_tw = tw.replace("```", "'''")
            tg_send(f"{domain_tag}\n```\n{safe_tw}\n```", parse_mode="Markdown")

    # ---- 写 vault（追加到当天文件）----
    bnb_map = {id(it): res for it, res in binance_results}
    body_parts = []
    for it, tw in tweets:
        domain_tag = DOMAIN_HEADERS.get(it["domain"], "")
        bnb_line = ""
        if id(it) in bnb_map:
            res = bnb_map[id(it)]
            link = res.get("shareLink") or f"id={res.get('id')}"
            bnb_line = f"\n> 🪙 币安广场: {link}"
        body_parts.append(
            f"### {domain_tag} {it['title']}\n\n"
            f"> {it.get('extra','')}\n> 🔗 {it['url']}{bnb_line}\n\n"
            f"```\n{tw}\n```\n"
        )
    section = (
        f"\n## {SLOT_LABEL[slot]}（{now_cn().strftime('%H:%M')}）\n\n"
        + "\n".join(body_parts)
    )

    fname = safe_filename(f"{today_str()}_推文")
    fp = (BOT_DIR / "vault" / "Daily" / f"{fname}.md")
    if fp.exists():
        old = fp.read_text(encoding="utf-8")
        new_md = old + section
        fp.write_text(new_md, encoding="utf-8")
    else:
        md = f"""---
type: daily_brief
date: {today_str()}
created: {now_str()}
day_online: {days_online()}
stage: {cfg['stage']}
tags: [daily, brief, tweets]
---

# {today_str()} 推文集（D{days_online()} 阶段{cfg['stage']}）

> 节奏：第{cfg['stage']}阶段，每档{cfg['per_slot']}条
""" + section
        write_note("Daily", fname, md)

    ok = git_commit_push(f"daily {slot}: {fname}")
    return f"✅ {SLOT_LABEL[slot]} {len(tweets)}条已推{'（同步）' if ok else '（未同步）'}"


def main():
    if len(sys.argv) > 1 and sys.argv[1] in SLOT_ORDER:
        slot = sys.argv[1]
    else:
        # 没传参就按当前小时猜时段
        h = now_cn().hour
        if h < 11:
            slot = "morning"
        elif h < 16:
            slot = "noon"
        elif h < 20:
            slot = "evening"
        else:
            slot = "night"
    print(run_slot(slot))


if __name__ == "__main__":
    main()
