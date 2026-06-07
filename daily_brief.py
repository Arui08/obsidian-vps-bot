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
    """AI 领域：用多个关键词分别按时间倒序查 + 取并集（HN Algolia 的 OR 不可靠）"""
    keywords = ["AI", "LLM", "GPT", "Claude", "Gemini", "Anthropic", "OpenAI", "agent"]
    candidates = []
    for kw in keywords:
        try:
            r = requests.get(
                "https://hn.algolia.com/api/v1/search_by_date",
                params={
                    "tags": "story",
                    "query": kw,
                    "numericFilters": "points>30",
                    "hitsPerPage": 15,
                },
                timeout=15, headers=UA,
            )
            r.raise_for_status()
            candidates.extend(r.json().get("hits", []))
        except Exception as e:
            print(f"fetch_ai_list kw={kw} 失败: {e}")
    # 去重（按 objectID）
    seen = set()
    uniq = []
    for h in candidates:
        oid = h.get("objectID")
        if oid in seen:
            continue
        seen.add(oid)
        uniq.append(h)

    def good(h):
        u = (h.get("url") or "").strip()
        if not u:
            return False
        bad = ["news.ycombinator.com/vote", "news.ycombinator.com/login",
               "news.ycombinator.com/item"]
        return not any(b in u for b in bad)
    uniq = [h for h in uniq if good(h)]
    uniq.sort(key=lambda h: h.get("points", 0), reverse=True)
    out = []
    for h in uniq[:max(n * 4, 30)]:
        out.append({
            "domain": "ai",
            "title": h.get("title", ""),
            "url": h.get("url"),
            "extra": f"HackerNews {h.get('points',0)}赞 / {h.get('num_comments',0)}评",
        })
    return out


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
        for h in hits[:max(n * 3, 30)]:
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
    link_hint = ""
    if item.get("link_kind"):
        link_hint = f"\n链接性质：{item['link_kind']}（写的时候要把链接当成'可直接用/可下载/可 star 的入口'，行动召唤要具体，比如 star、clone、下载、丢进去试，不要泛泛说链接在下面）"

    domain_instruction = {
        "ai": "不要写成AI新闻摘要。要写成AI正在改变工作流、职业饭碗、老板算盘、普通人焦虑的真人观点。",
        "tools": "不要上来介绍项目。先说它解决了什么烦人的破事，再说为什么值得试。重点是痛点、工作流、少加班，而不是项目参数。",
        "tech": "不要写成HN/Reddit新闻播报。要把它翻译成中文互联网能懂的科技代梗、职场吐槽、大厂吃瓜或社会话题。",
        "crypto": "不要喊单。写盘面情绪、老韭菜心态、追高风险和资金关注点，像人在复盘。",
    }.get(item["domain"], "写成人的判断，不要写成资讯摘要。")

    prompt = f"""你是一个混迹中文 X/Twitter 的内容博主，具体角色：{style['persona']}。风格参考"鸟哥|蓝鸟会"：口语化、有钩子、有真实判断、有一点聊天感，但不要油腻，不要像营销号，不要每条都一个套路。

现在要根据下面这条信息写一条中文推文。信息源只是背景材料，最终要写成一个人的观点、吐槽、故事或判断，不要写成资讯摘要。

本条叙事角度：{angle}
本条内容形态：{shape_name}
本条形态规则：{shape_rule}
领域写法要求：{domain_instruction}

硬规则（违反任何一条都算失败）：
1. 全文 350-520 个中文字符左右（包含链接和hashtag）；可以比普通短推长一点，但不要水，不要写成小作文。
2. 段落之间必须空一行。每条必须有 2-4 个核心信息点，但表达方式必须跟随“本条形态规则”，不要所有内容都套同一种格式。
3. 只有形态规则明确允许时，才可以使用 1️⃣ 2️⃣ 3️⃣；如果形态规则写了“禁止使用 1️⃣2️⃣3️⃣”，正文里绝对不能出现这三个序号emoji。
4. 除 1️⃣2️⃣3️⃣ 外，禁止其他 emoji（比如火箭、火、笑脸、箭头、手指等）。
5. 开头第一句必须从人的感受、痛点、场景、吐槽或判断切入。参考方向：{style['vibes']}。
6. 严禁以下开头或近似表达："一觉醒来HN又炸了"、"HN上又吵起来了"、"HackerNews刷到"、"Reddit评论区炸了"、"今天看到一个"、"这个项目"、"近期发现"、"分享一个"、"今天给大家推荐"、"为大家介绍"。
7. 不要写 HN/Reddit 的点赞数、评论数、热度数字，也不要写"评论区炸了"。这些是信息源痕迹，容易暴露AI感。可以使用真正有内容价值的数字：价格、涨幅、版本号、节省比例、stars、token、时间成本等。
8. 不要直接复述标题。要提炼核心内容：发生了什么、为什么重要、普通人/开发者/交易者该怎么看。
9. 纯文本，不用 markdown 的 # 标题、不用 ** 加粗、不用 > 引用块、不用任何反引号；技术词直接裸写，比如 fork()+exec()。
10. 结尾必须有一个真人式互动/行动召唤：比如"这事你怎么看"、"我先 mark 周末试"、"你会接还是等回踩"、"别光听我吹，自己跑一遍"。
11. 行动召唤之后单独一行附链接：{item['url']}
12. 最后另起一行，3-4 个相关中文 hashtag（# 开头空格分隔），贴合主题。

主题领域：{style['label']}
信息源数据（只用于判断，不要照抄热度数字）：{item.get('extra','')}{link_hint}
标题：{item['title']}
链接：{item['url']}

直接输出推文正文，不要"好的我来写"这种废话开头。输出前自查：去掉HN/Reddit痕迹、去掉废话、严格遵守本条内容形态，不要把所有推文都写成同一个模板。"""
    return ai_chat(prompt, model=MODEL_FAST, max_tokens=1800).strip()


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
