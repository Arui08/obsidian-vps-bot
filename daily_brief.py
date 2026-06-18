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
from bs4 import BeautifulSoup

from common import (
    MODEL_FAST, ai_chat, git_pull, git_commit_push,
    write_note, safe_filename, today_str, now_str, now_cn,
    tg_send,
)


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


def _fetch_rss_items(feed_url: str, source: str, limit: int = 20) -> list:
    """轻量 RSS/Atom 解析，不额外引入 feedparser。"""
    try:
        r = requests.get(feed_url, headers=UA, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "xml")
        nodes = soup.find_all("item") or soup.find_all("entry")
        out = []
        for node in nodes[:limit]:
            title_el = node.find("title")
            title = title_el.get_text(strip=True) if title_el else ""
            link = ""
            link_el = node.find("link")
            if link_el:
                link = link_el.get_text(strip=True) or link_el.get("href", "")
            guid_el = node.find("guid")
            if not link and guid_el:
                link = guid_el.get_text(strip=True)
            if title and link:
                out.append({"title": title, "url": link, "source": source})
        return out
    except Exception as e:
        print(f"RSS 抓取失败 {source}: {e}")
        return []


def fetch_ai_list(n: int = 6) -> list:
    """AI 领域：优先中文 AI 源（量子位/机器之心），HN 英文源只做兜底。"""
    out = []
    feeds = [
        ("https://www.qbitai.com/feed", "量子位"),
        ("https://www.jiqizhixin.com/rss", "机器之心"),
    ]
    seen = set()
    for url, source in feeds:
        for it in _fetch_rss_items(url, source, limit=20):
            key = it["url"] or it["title"]
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "domain": "ai",
                "title": it["title"],
                "url": it["url"],
                "extra": f"中文AI源：{source}",
                "source_lang": "zh",
            })
    if len(out) >= max(n * 3, 18):
        return out[:max(n * 4, 30)]

    # 兜底：英文 HN 只当雷达，prompt 会转译成中文语境
    keywords = ["AI", "LLM", "GPT", "Claude", "Gemini", "Anthropic", "OpenAI", "agent"]
    candidates = []
    for kw in keywords:
        try:
            r = requests.get(
                "https://hn.algolia.com/api/v1/search_by_date",
                params={"tags": "story", "query": kw, "numericFilters": "points>30", "hitsPerPage": 15},
                timeout=15, headers=UA,
            )
            r.raise_for_status()
            candidates.extend(r.json().get("hits", []))
        except Exception as e:
            print(f"fetch_ai_list kw={kw} 失败: {e}")
    uniq = []
    hseen = set()
    for h in candidates:
        oid = h.get("objectID")
        if oid in hseen:
            continue
        hseen.add(oid)
        u = (h.get("url") or "").strip()
        if not u or any(b in u for b in ["news.ycombinator.com/vote", "news.ycombinator.com/login", "news.ycombinator.com/item"]):
            continue
        uniq.append(h)
    uniq.sort(key=lambda h: h.get("points", 0), reverse=True)
    for h in uniq[:max(n * 4, 30)]:
        key = h.get("url") or h.get("title", "")
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "domain": "ai",
            "title": h.get("title", ""),
            "url": h.get("url"),
            "extra": "英文AI雷达：HN（已转译中文语境）",
            "source_lang": "en",
        })
    return out[:max(n * 4, 30)]


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
        for c in filtered[:max(n * 3, 30)]:
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
    """科技代梗：优先中文 V2EX 热榜，英文 HN/Reddit 只兜底。"""
    candidates = []
    seen = set()
    try:
        r = requests.get("https://www.v2ex.com/api/topics/hot.json", timeout=20, headers=UA)
        r.raise_for_status()
        allowed_nodes = {"程序员", "职场话题", "问与答", "分享创造", "互联网", "iPhone", "Apple", "Google Gemini", "酷工作", "远程工作", "Python", "JavaScript", "Linux"}
        tech_keywords = "AI 模型 编程 程序员 开发者 代码 工具 软件 开源 GitHub Claude GPT Gemini Kimi DeepSeek iPhone 键盘 Mac Linux 远程 工作 职场"
        for d in r.json()[:40]:
            title = d.get("title", "")
            url = d.get("url", "") or f"https://www.v2ex.com/t/{d.get('id')}"
            node = (d.get("node") or {}).get("title", "")
            if node not in allowed_nodes and not any(k.lower() in title.lower() for k in tech_keywords.split()):
                continue
            replies = d.get("replies", 0)
            key = url or title
            if not title or key in seen:
                continue
            seen.add(key)
            candidates.append({
                "domain": "tech",
                "title": title,
                "url": url,
                "_pts": replies,
                "_cmt": replies,
                "extra": f"中文技术社区：V2EX · {node}",
                "source_lang": "zh",
            })
    except Exception as e:
        print(f"V2EX hot 失败: {e}")

    # 中文源够用时直接返回，避免英文 HN 把 V2EX 挤下去
    if len(candidates) >= 5:
        candidates.sort(key=lambda x: x.get("_cmt", 0), reverse=True)
        return candidates[:max(n * 3, 30)]

    # 兜底：HN front_page
    try:
        r = requests.get(
            "https://hn.algolia.com/api/v1/search",
            params={"tags": "front_page", "hitsPerPage": 30},
            timeout=20, headers=UA,
        )
        r.raise_for_status()
        for h in r.json().get("hits", []):
            title = h.get("title", "")
            url = _hn_url(h)
            key = url or title
            if not title or key in seen:
                continue
            seen.add(key)
            candidates.append({
                "domain": "tech",
                "title": title,
                "url": url,
                "_pts": h.get("points", 0),
                "_cmt": h.get("num_comments", 0),
                "extra": "英文科技雷达：HN（已转译中文语境）",
                "source_lang": "en",
            })
    except Exception as e:
        print(f"HN front_page 失败: {e}")
    candidates.sort(key=lambda x: x.get("_cmt", 0), reverse=True)
    return candidates[:max(n * 3, 30)]


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
    """GitHub/工具：以 GitHub Trending 热门仓库为主，不要求中文。"""
    out = []
    try:
        r = requests.get("https://github.com/trending?since=daily", timeout=30, headers=UA)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        for article in soup.select("article.Box-row")[:max(n * 3, 30)]:
            a = article.select_one("h2 a")
            if not a:
                continue
            full_name = a.get_text(strip=True).replace("\n", "").replace(" ", "")
            desc_el = article.select_one("p")
            desc = desc_el.get_text(" ", strip=True) if desc_el else ""
            lang_el = article.select_one('span[itemprop="programmingLanguage"]')
            lang = lang_el.get_text(strip=True) if lang_el else ""
            stars_el = article.select_one('a[href$="/stargazers"]')
            stars = stars_el.get_text(strip=True) if stars_el else "0"
            today_el = article.select_one("span.d-inline-block.float-sm-right")
            today_stars = today_el.get_text(" ", strip=True) if today_el else ""
            url = f"https://github.com/{full_name}"
            title = f"{full_name} - {desc}" if desc else full_name
            out.append({
                "domain": "tools",
                "title": title,
                "url": url,
                "link_kind": "GitHub Trending 热门仓库（可直接 star / clone / 看 README）",
                "extra": f"GitHub Trending · {lang} · stars {stars} · {today_stars}".strip(),
                "source_lang": "en",
            })
        if out:
            return out
    except Exception as e:
        print(f"GitHub Trending 失败: {e}")

    # 兜底：Show HN 产品入口
    try:
        r = requests.get(
            "https://hn.algolia.com/api/v1/search_by_date",
            params={"tags": "show_hn", "numericFilters": "points>30", "hitsPerPage": 80},
            timeout=20, headers=UA,
        )
        r.raise_for_status()
        hits = r.json().get("hits", [])
        def good(h):
            u = (h.get("url") or "").strip()
            if not u:
                return False
            bad_substr = ["news.ycombinator.com/vote", "news.ycombinator.com/login", "news.ycombinator.com/item"]
            if any(b in u for b in bad_substr):
                return False
            return _is_product_url(u)
        hits = [h for h in hits if good(h)]
        hits.sort(key=lambda h: h.get("points", 0), reverse=True)
        for h in hits[:max(n * 3, 30)]:
            title = re.sub(r"^Show HN:\s*", "", h.get("title", ""))
            u = h.get("url")
            out.append({
                "domain": "tools",
                "title": title,
                "url": u,
                "link_kind": _link_kind(u),
                "extra": "英文工具雷达：Show HN（已转译中文使用场景）",
                "source_lang": "en",
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
        "vibes": "AI又开始改造打工人、老板开始算账、程序员/产品经理/设计师的工作流被重写",
    },
    "crypto": {
        "label": "Web3",
        "persona": "币圈老哥，老韭菜，半夜盯K线那种",
        "vibes": "盘面情绪、老韭菜心态、追高风险、割完就拉、合约人真实痛点",
    },
    "tech": {
        "label": "科技代梗",
        "persona": "会把科技新闻讲成社畜梗和大厂吃瓜的中文科技号",
        "vibes": "打工人荒诞现实、大厂离谱操作、平台垄断吐槽、AI时代职场焦虑、科技圈吃瓜",
    },
    "tools": {
        "label": "实用工具",
        "persona": "装机狂魔、工作流洁癖、什么新工具都想试一下",
        "vibes": "懒人福音、少加班工具、工作流改造、原始人突然开窍、相见恨晚",
    },
}

ANGLE_POOL = {
    "ai": [
        "打工人视角：这东西怎么影响普通人的工作饭碗、效率和焦虑感",
        "老板视角：它会不会变成降本增效的新借口，最后压到团队身上",
        "产品落地视角：别讲模型多强，只讲能不能真的干活、能不能稳定交付",
        "开发者视角：它怎么改变写代码、调试、查资料、做决策这些日常动作",
        "普通人视角：AI不是科幻，它已经开始挤进每天的工作流和KPI",
    ],
    "crypto": [
        "老韭菜盘感：涨跌背后是情绪，不要无脑冲",
        "合约心态：最怕的不是亏钱，是追在最热的时候",
        "盘面故事：这个币今天为什么被资金盯上",
        "风险提醒：涨起来的币更要看承接，不要只看涨幅",
    ],
    "tech": [
        "科技圈吃瓜：把新闻讲成一个离谱现实，不做资讯播报",
        "打工人翻译器：把技术新闻翻译成普通上班族能感受到的压力或荒诞",
        "大厂吐槽：公司/平台/巨头的操作哪里让人绷不住",
        "社会话题：技术变化背后是教育、就业、隐私、垄断这些现实问题",
        "代梗视角：把新闻翻译成中文互联网能接住的梗和吐槽",
    ],
    "tools": [
        "痛点切入：先说这个工具解决了什么烦人的破事",
        "少加班视角：它能不能让人少一点班味",
        "工作流改造：它怎么把原本复杂的流程压成一步",
        "懒人福音：用完会怀疑以前是不是在手搓原始社会",
        "开发者实用视角：star/clone/下载后到底能干嘛",
    ],
}

SHAPE_POOL = {
    "ai": [
        {"name": "三点拆解型", "rule": "必须使用 1️⃣ 2️⃣ 3️⃣，分别写发生了什么、为什么重要、我怎么看。"},
        {"name": "打工人翻译器型", "rule": "禁止使用 1️⃣2️⃣3️⃣。用'表面看/打工人翻译/老板视角/我的判断'这种自然段结构。"},
        {"name": "反直觉型", "rule": "禁止使用 1️⃣2️⃣3️⃣。先写表面看法，再反转到真正关键点。"},
        {"name": "一句话观点延展型", "rule": "禁止使用 1️⃣2️⃣3️⃣。用一个强观点开头，再用2-3段自然展开。"},
    ],
    "tech": [
        {"name": "打工人翻译器型", "rule": "禁止使用 1️⃣2️⃣3️⃣。必须出现'新闻原文/打工人翻译/老板或平台视角/我的判断'类似结构。"},
        {"name": "反直觉型", "rule": "禁止使用 1️⃣2️⃣3️⃣。把科技新闻从表层事实翻到社会现实或大厂逻辑。"},
        {"name": "大厂吐槽型", "rule": "禁止使用 1️⃣2️⃣3️⃣。像在吐槽一个大厂/平台的离谱操作，用自然段推进。"},
        {"name": "三点拆解型", "rule": "允许使用 1️⃣ 2️⃣ 3️⃣，但语气要像人拆瓜，不要像报告。"},
    ],
    "tools": [
        {"name": "痛点故事型", "rule": "禁止使用 1️⃣2️⃣3️⃣。先讲以前怎么麻烦，再讲这个工具怎么省事，最后说适合谁。"},
        {"name": "清单型", "rule": "禁止使用 1️⃣2️⃣3️⃣。可以用 - 短清单列3-4个观察点。"},
        {"name": "工作流改造型", "rule": "禁止使用 1️⃣2️⃣3️⃣。围绕'原来几步/现在一步/我会怎么用'来写。"},
        {"name": "三点拆解型", "rule": "允许使用 1️⃣ 2️⃣ 3️⃣，分别写解决什么痛点、适合谁、我会怎么试。"},
    ],
    "crypto": [
        {"name": "盘面复盘型", "rule": "禁止使用 1️⃣2️⃣3️⃣。用老韭菜复盘语气写走势、情绪和风险。"},
        {"name": "风险提醒型", "rule": "禁止使用 1️⃣2️⃣3️⃣。重点写为什么不能只看涨幅、追高哪里危险。"},
        {"name": "反直觉型", "rule": "禁止使用 1️⃣2️⃣3️⃣。表面看涨跌，实际讲资金情绪和承接。"},
        {"name": "三点拆解型", "rule": "允许使用 1️⃣ 2️⃣ 3️⃣，分别写涨跌、成交/承接、我的处理方式。"},
    ],
}


def _stable_pick(pool: list, item: dict, salt: str):
    if not pool:
        return "人的观点/吐槽视角：不要像资讯摘要，要像一个人在表达判断"
    seed = f"{today_str()}|{salt}|{item.get('domain','')}|{item.get('title','')}"
    idx = int(hashlib.md5(seed.encode("utf-8")).hexdigest(), 16) % len(pool)
    return pool[idx]


def _pick_angle(item: dict) -> str:
    """按日期+标题稳定挑叙事角度，保证有变化但可复现。"""
    return _stable_pick(ANGLE_POOL.get(item.get("domain"), []), item, "angle")


def _pick_shape(item: dict) -> dict:
    """按日期+标题稳定挑内容形态，避免每条都一个模板。"""
    return _stable_pick(SHAPE_POOL.get(item.get("domain"), []), item, "shape")


def make_brief_tweet(item: dict) -> str:
    style = DOMAIN_STYLE[item["domain"]]
    angle = _pick_angle(item)
    shape = _pick_shape(item)
    shape_name = shape.get("name", "自然观点型") if isinstance(shape, dict) else str(shape)
    shape_rule = shape.get("rule", "按自然段写，不要套固定模板。") if isinstance(shape, dict) else str(shape)

    # ---- 链接策略 ----
    # 只在以下情况附链接：GitHub/开源项目、中文源、CoinGecko数据页
    # 英文新闻/文章/Show HN 不贴链接——中文读者看不懂，贴了像广告
    url = item.get("url", "")
    src_lang = item.get("source_lang", "")
    domain = item.get("domain", "")
    link_kind = item.get("link_kind", "")

    include_link = True  # 默认贴
    if src_lang == "en" and domain == "tech":
        include_link = False  # 英文科技新闻→不贴
    if src_lang == "en" and domain == "tools" and "GitHub" not in link_kind and "github" not in url.lower():
        include_link = False  # 英文 Show HN 工具→不贴
    # GitHub、中文源、CoinGecko 都贴

    link_line = f"\n{url}" if include_link else ""
    link_instruction = ""
    if include_link:
        if link_kind and "GitHub" in link_kind:
            link_instruction = "结尾必须单独一行放这个链接（这是个GitHub项目，你可以star/clone/下载），链接之后放hashtag。"
        else:
            link_instruction = "结尾单独一行放这个链接，链接之后放hashtag。"
    else:
        link_instruction = "不要放链接，正文就是纯观点+hashtag。"

    # ---- 新提示词（去AI话） ----
    domain_instruction = {
        "ai": "像刷到一条AI新闻后跟朋友聊天的感觉。重点不是播报新闻，是你看了之后的第一反应——这东西对打工人意味着什么、会不会又变成老板裁人的理由、跟国产大模型比差在哪。不要用'AI正在改变XXX'这种套话开头。",
        "tools": "像你发现了一个省事的工具，在群里跟兄弟分享。先说痛点——以前干这破事多折腾，再说这东西怎么把事压成一步。不要写成产品介绍，要写成'我用了一下觉得还行，你们看看'。",
        "tech": "像在茶水间跟同事吐槽。把技术新闻翻译成普通人的感受，可以是吃瓜、可以是扎心、可以是荒诞。不要做新闻播报，要做'这件事翻译过来就是...'。",
        "crypto": "像盯盘老韭菜在群里闲聊。不喊单，但敢说自己的判断。讲盘面情绪、追高风险、资金往哪走。不要写'今日行情分析'，要写'今天这盘看着...'。",
    }.get(item.get("domain", ""), "像跟朋友聊天，不是写稿子。")

    prompt = f"""写一条发在X/Twitter上的中文内容。不要写成文章、不要写成新闻稿、不要写成AI总结。就像你在群里跟朋友分享一个刚看到的东西。

{domain_instruction}

背景信息（不是让你翻译它，是让你有东西可聊）：
标题：{item['title']}
补充信息：{item.get('extra','')}

写的时候注意：
- 开头不要用"今天看到"、"分享一个"、"最近发现"这种。直接切进你想说的点。
- 不要提HN、Reddit、V2EX这些信息源名字。你不是在做搬运，你在表达观点。
- 如果有具体数字（价格、涨幅、stars、版本号、省了多少时间），可以提一个当论据，但别堆数字。
- 句子有长有短。偶尔用一个很短的句子制造停顿。
- 绝对不用反引号包裹技术词，不用markdown列表（不要用 - 开头的行），纯文本、纯自然段。技术名词直接裸写，比如写 iroh 不要写 `iroh`。
- 全文严格250-400字，写完之后数一下，超过400字就删废话。
- {link_instruction}
- 正文最后另起一行放3-4个中文hashtag（#开头空格分隔）。

内容形态参考：{shape_rule}
叙事角度参考：{angle}
{link_line}

直接输出正文。"""
    return ai_chat(prompt, model=MODEL_FAST, max_tokens=1200).strip()


# ---------------- 主流程 ----------------

DOMAIN_HEADERS = {
    "ai": "【AI 前沿】",
    "crypto": "【Web3 热币】",
    "tech": "【科技代梗】",
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
    short_by = {}  # 记录每个领域少抓了几条
    for d in domains:
        n = counts.get(d, 0)
        if n <= 0:
            continue
        picked = pick_items(d, n)
        items.extend(picked)
        if len(picked) < n:
            short_by[d] = n - len(picked)

    # 兜底：某个领域候选不足，从兄弟领域多抓
    if short_by:
        for short_d, missing in short_by.items():
            others = [d for d in domains if d != short_d]
            for od in others:
                extra_picked = pick_items(od, missing)
                # 排除已经在 items 里的
                existing_urls = {it.get("url") for it in items}
                extra_picked = [e for e in extra_picked if e.get("url") not in existing_urls]
                items.extend(extra_picked[:missing])
                missing -= len(extra_picked[:missing])
                if missing <= 0:
                    break

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

    # ---- TG 推送：每条都发完整代码块（用于复制到 X）----
    header = (
        f"📰 {today_str()} {SLOT_LABEL[slot]}（D{days_online()} · 阶段{cfg['stage']}）\n"
        f"本档 {len(tweets)} 条\n"
    )
    tg_send(header)

    for it, tw in tweets:
        domain_tag = DOMAIN_HEADERS.get(it["domain"], "")
        safe_tw = tw.replace("```", "'''")
        tg_send(f"{domain_tag}\n```\n{safe_tw}\n```", parse_mode="Markdown")

    # ---- 写 vault（追加到当天文件）----
    body_parts = []
    for it, tw in tweets:
        domain_tag = DOMAIN_HEADERS.get(it["domain"], "")
        body_parts.append(
            f"### {domain_tag} {it['title']}\n\n"
            f"> {it.get('extra','')}\n> 🔗 {it['url']}\n\n"
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
