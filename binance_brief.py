"""币安广场专用内容流：BTC/ETH 走势 + 币安涨幅榜 + 热门盘面

说明：
- VPS 所在 IP 访问 api.binance.com / fapi.binance.com 会 451；
- 使用 Binance Vision 公开数据域 data-api.binance.vision 的现货行情，稳定免鉴权。
- TG 只发成功/失败通知，不推正文。
"""
import hashlib
import json
import math
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

import requests

from common import (
    MODEL_FAST, ai_chat, today_str, now_str, now_cn,
    tg_send, git_pull, git_commit_push, write_note, safe_filename,
)
from binance_square import publish_text, SquareError


BOT_DIR = Path(__file__).parent
DEDUP_DB = BOT_DIR / ".binance_pushed.sqlite3"
UA = {"User-Agent": "obsidian-bot/1.0"}

# 同一天内已用币种，避免同一币出现在不同时段
def _today_used_coins() -> set:
    with _db() as c:
        rows = c.execute(
            "SELECT DISTINCT symbol FROM pushed WHERE pushed_at LIKE ?",
            (f"{today_str()}%",),
        ).fetchall()
    return {r[0] for r in rows}

DATA_BASE = "https://data-api.binance.vision"

STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USTC", "USD1", "USDE"}
MAJORS = {"BTC", "ETH"}

SLOT_LABEL = {
    "pre_market": "开盘前瞻",
    "morning": "ETH早盘",
    "philo_morning": "投资哲学·晨思",
    "mid_morning": "涨幅快报",
    "noon": "午盘热门币",
    "philo_noon": "投资哲学·午悟",
    "afternoon": "合约情绪",
    "late_noon": "热门盘点",
    "philo_afternoon": "投资哲学·日省",
    "evening": "ETH晚间",
    "night": "夜盘复盘",
    "recap": "全天复盘",
    "philo_night": "投资哲学·夜读",
}

SLOT_ORDER = ["pre_market", "morning", "philo_morning", "mid_morning", "noon", "philo_noon", "afternoon", "late_noon", "philo_afternoon", "evening", "night", "recap", "philo_night"]


# ---------------- 去重 ----------------

def _db():
    conn = sqlite3.connect(str(DEDUP_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pushed (
            key TEXT PRIMARY KEY,
            symbol TEXT,
            slot TEXT,
            topic TEXT,
            pushed_at TEXT,
            share_link TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_symbol_time ON pushed(symbol, pushed_at)")
    conn.commit()
    return conn


def _slot_key(slot: str, topic: str, symbol: str) -> str:
    return f"{today_str()}:{slot}:{topic}:{symbol}"


def _already_slot(slot: str, topic: str, symbol: str) -> bool:
    with _db() as c:
        return bool(c.execute("SELECT 1 FROM pushed WHERE key=? LIMIT 1", (_slot_key(slot, topic, symbol),)).fetchone())


def _pushed_recent(symbol: str, days: int = 3) -> bool:
    if symbol in MAJORS:
        return False
    cutoff = (now_cn() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with _db() as c:
        return bool(c.execute(
            "SELECT 1 FROM pushed WHERE symbol=? AND pushed_at>=? LIMIT 1",
            (symbol, cutoff),
        ).fetchone())


def _mark_pushed(slot: str, topic: str, symbol: str, share_link: str):
    with _db() as c:
        c.execute(
            "INSERT OR IGNORE INTO pushed(key, symbol, slot, topic, pushed_at, share_link) VALUES(?,?,?,?,?,?)",
            (_slot_key(slot, topic, symbol), symbol, slot, topic, now_str(), share_link),
        )
        c.commit()


# ---------------- 数据源 ----------------

def _get(path: str, params: dict = None, timeout: int = 20):
    r = requests.get(f"{DATA_BASE}{path}", params=params or {}, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _base_symbol(symbol: str) -> str:
    return symbol[:-4] if symbol.endswith("USDT") else symbol


def _fmt_pct(x) -> str:
    try:
        return f"{float(x):+.2f}%"
    except Exception:
        return "未知"


def _fmt_price(x) -> str:
    try:
        v = float(x)
        if v >= 1000:
            return f"{v:,.0f}"
        if v >= 10:
            return f"{v:,.2f}"
        if v >= 1:
            return f"{v:,.4f}"
        return f"{v:.6f}"
    except Exception:
        return str(x)


def spot_24h() -> list:
    data = _get("/api/v3/ticker/24hr")
    out = []
    for x in data:
        sym = x.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        base = _base_symbol(sym)
        if base in STABLES:
            continue
        try:
            quote_vol = float(x.get("quoteVolume") or 0)
            pct = float(x.get("priceChangePercent") or 0)
            last = float(x.get("lastPrice") or 0)
        except Exception:
            continue
        if quote_vol < 20_000_000 or last <= 0:
            continue
        out.append({
            "symbol": sym,
            "base": base,
            "lastPrice": last,
            "priceChangePercent": pct,
            "quoteVolume": quote_vol,
            "highPrice": float(x.get("highPrice") or 0),
            "lowPrice": float(x.get("lowPrice") or 0),
        })
    return out


def ticker(symbol: str) -> dict:
    return _get("/api/v3/ticker/24hr", {"symbol": symbol})


def klines(symbol: str, interval: str = "1h", limit: int = 48) -> list:
    raw = _get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    return [{
        "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
        "close": float(k[4]), "volume": float(k[5]), "quoteVolume": float(k[7]),
    } for k in raw]


def market_snapshot(symbol: str) -> dict:
    t = ticker(symbol)
    ks = klines(symbol, "1h", 48)
    highs = [k["high"] for k in ks]
    lows = [k["low"] for k in ks]
    closes = [k["close"] for k in ks]
    current = float(t.get("lastPrice") or closes[-1])
    ma6 = sum(closes[-6:]) / 6
    ma24 = sum(closes[-24:]) / 24
    return {
        "symbol": symbol,
        "base": _base_symbol(symbol),
        "current": current,
        "change24": float(t.get("priceChangePercent") or 0),
        "quoteVolume": float(t.get("quoteVolume") or 0),
        "high24": max(highs[-24:]),
        "low24": min(lows[-24:]),
        "high48": max(highs),
        "low48": min(lows),
        "trend": "偏强" if ma6 > ma24 else "偏弱",
    }


def pick_top_gainer() -> dict:
    rows = [r for r in spot_24h() if r["priceChangePercent"] > 3 and r["quoteVolume"] > 30_000_000]
    rows.sort(key=lambda r: (r["priceChangePercent"], math.log10(r["quoteVolume"] + 1)), reverse=True)
    for r in rows:
        if r["base"] in _today_used_coins():
            continue
        if not _pushed_recent(r["base"], days=7):
            return r
    return rows[0] if rows else None


def pick_early_gainer() -> dict:
    """早盘快报：涨幅>3% 且成交额>3000万，按涨幅排序，优先未推过的小币。"""
    rows = [r for r in spot_24h() if r["priceChangePercent"] > 3 and r["quoteVolume"] > 30_000_000]
    rows.sort(key=lambda r: (r["priceChangePercent"], math.log10(r["quoteVolume"] + 1)), reverse=True)
    for r in rows:
        if r["base"] not in MAJORS and r["base"] not in _today_used_coins() and not _pushed_recent(r["base"], days=5):
            return r
    for r in rows:
        if r["base"] not in _today_used_coins():
            return r
    return None


def pick_hot_symbol() -> dict:
    rows = [r for r in spot_24h() if r["quoteVolume"] > 100_000_000]
    rows.sort(key=lambda r: (math.log10(r["quoteVolume"] + 1), abs(r["priceChangePercent"])), reverse=True)
    for r in rows:
        if r["base"] not in MAJORS and r["base"] not in _today_used_coins() and not _pushed_recent(r["base"], days=5):
            return r
    return rows[0] if rows else None


def pick_day_recap() -> dict:
    """当日热门盘点：成交额最大+有明显涨幅的币。"""
    rows = [r for r in spot_24h() if r["quoteVolume"] > 80_000_000 and abs(r["priceChangePercent"]) > 2]
    rows.sort(key=lambda r: (math.log10(r["quoteVolume"] + 1), abs(r["priceChangePercent"])), reverse=True)
    for r in rows:
        if r["base"] not in MAJORS and r["base"] not in _today_used_coins() and not _pushed_recent(r["base"], days=3):
            return r
    return rows[0] if rows else None


def pick_big_mover() -> dict:
    """找一个7天内剧烈波动的币，用于行情故事：涨幅或跌幅>15%、成交额>5000万。"""
    rows = [r for r in spot_24h() if abs(r["priceChangePercent"]) > 5 and r["quoteVolume"] > 50_000_000]
    if len(rows) < 3:
        rows = [r for r in spot_24h() if r["quoteVolume"] > 80_000_000]
    rows.sort(key=lambda r: (abs(r["priceChangePercent"]), math.log10(r["quoteVolume"] + 1)), reverse=True)
    for r in rows:
        if r["base"] not in MAJORS and r["base"] not in _today_used_coins() and not _pushed_recent(r["base"], days=7):
            return r
    return rows[0] if rows else None


def pick_debate_coin() -> dict:
    """找一个有争议话题性的币：最近推过+成交额仍活跃+涨跌明显。"""
    rows = [r for r in spot_24h() if r["quoteVolume"] > 200_000_000]
    rows.sort(key=lambda r: abs(r["priceChangePercent"]), reverse=True)
    if not rows:
        rows = sorted(spot_24h(), key=lambda r: r["quoteVolume"], reverse=True)[:10]
    for r in rows:
        if r["base"] not in MAJORS and r["base"] not in _today_used_coins() and not _pushed_recent(r["base"], days=3):
            return r
    return rows[0] if rows else rows[0]


def _content_mode(slot: str) -> str:
    """根据日期+时段决定今天用原始行情还是新内容类型。保证每天同一时段走同一种模式。"""
    seed = f"{today_str()}|{slot}"
    idx = int(hashlib.md5(seed.encode("utf-8")).hexdigest(), 16) % 2
    return "original" if idx == 0 else "alt"


PSYCH_ANGLE_POOL = [
    "扛单教训：明明方向错了但就是舍不得割，最后越扛越深",
    "踏空比亏钱难受：看对了趋势却没上车，眼睁睁看着别人赚",
    "第一次爆仓的夜晚：以为自己能抄底，结果被市场教育",
    "看对方向但管不住手：频繁操作、追涨杀跌，最后利润全还给市场",
    "消息面追涨的代价：看到利好就冲进去，结果接在高点",
    "止损的重要性：一次不止损可以毁掉十次正确的判断",
    "仓位管理才是生存之道：不是方向问题，是下了太多",
    "合约上瘾：本来只想小玩一下，结果越开越大",
    "大饼涨山寨不跟的焦虑：别人都在赚，我的币为什么不动",
    "牛市亏钱比熊市更扎心：牛市的波动才是真正的绞肉机",
    "不要试图预测顶部和底部：市场永远比你以为的更能折磨你",
    "复利思维被一根针击碎：攒了很久的利润，一夜归零",
    "情绪交易是最贵的手续费：愤怒、贪婪、恐惧都是送钱",
    "从合约转到现货才睡得着觉：杠杆是双刃剑",
    "相信自己是天选之子：新手运是最危险的错觉",
    "跟单大V翻车：别人说的方向不一定是你的方向",
    "卖飞之后的自我和解：赚了就比亏了好",
    "暴跌时不敢买、暴涨时不敢卖：人性永远在阻碍交易",
    "用闲钱和用生活费做交易，心态完全不一样",
    "币圈最残酷的不是亏钱，是亏了之后还得假装淡定",
]


def _psych_angle(slot: str) -> str:
    """按日期+时段稳定选一个交易心理角度，保证不重复、有变化。"""
    seed = f"{today_str()}|{slot}|psych"
    idx = int(hashlib.md5(seed.encode("utf-8")).hexdigest(), 16) % len(PSYCH_ANGLE_POOL)
    return PSYCH_ANGLE_POOL[idx]


PHILO_ANGLE_POOL = [
    {"icon": "📖", "source": "《股票作手回忆录》", "angle": "利弗莫尔说：不要试图捕捉市场的每一个波动，等大行情来了再动手。我在币圈才真正理解这句话——频繁操作的磨损比一次大亏还可怕。"},
    {"icon": "🧠", "source": "《穷查理宝典》", "angle": "芒格的多元思维模型：别只用一把锤子看世界。做交易也一样，只看K线不够，你还得看情绪、看资金、看宏观。单一视角最容易亏钱。"},
    {"icon": "🎲", "source": "《黑天鹅》", "angle": "塔勒布说黑天鹅不可预测，但你可以让自己在极端事件中活下来。在币圈，你永远不知道下一秒会不会插针、会不会下架。仓位控制不是赚钱用的，是保命用的。"},
    {"icon": "🪞", "source": "《反脆弱》", "angle": "塔勒布区分了脆弱、坚固、反脆弱。好的交易系统不是不会被市场打，而是每次被打完都能变得更稳。爆过仓不可怕，可怕的是什么都没学到。"},
    {"icon": "📏", "source": "《投资中最重要的事》", "angle": "霍华德·马克斯讲第二层思维：别人看到涨了就追，你看到涨了要想——谁在卖？为什么要卖？第一层思维让你随大流，第二层让你不接盘。"},
    {"icon": "⏳", "source": "巴菲特名言语录", "angle": "巴菲特说股市是把钱从没耐心的人转移到有耐心的人的工具。在币圈这句话放大十倍。熬得住的人赚熬不住的人的钱，就这么简单。"},
    {"icon": "💰", "source": "《随机漫步的傻瓜》", "angle": "塔勒布说区分运气和实力。币圈很多人把一轮牛市的运气当成了自己的实力，到了熊市才开始怀疑人生。诚实面对自己的成绩单，别把beta当alpha。"},
    {"icon": "🐢", "source": "索罗斯反身性理论", "angle": "索罗斯说市场总是错的。你既是市场的观察者，也是参与者。你买的动作本身就会影响价格。所以永远不要觉得“这次不一样”，每次都一样。"},
    {"icon": "🔍", "source": "达利欧的《原则》", "angle": "达利欧把每次失败都写成原则。我在币圈也开始记'每次亏损的原因'。翻回去一看，大部分错误都在重复犯。不是技术问题，是人性问题。"},
    {"icon": "⚖️", "source": "芒格逆向思维", "angle": "芒格说：反过来想，总是反过来想。大家都在讨论怎么在币圈暴富，你应该问——怎么才能在币圈不亏光。先求不败，再求胜。"},
    {"icon": "🌊", "source": "《浪潮之巅》", "angle": "每轮周期的本质都一样：新叙事→资金涌入→泡沫→崩溃→遗忘→重新再来。不同的只是讲故事的词。去年叫DeFi，今年叫AI+区块链。"},
    {"icon": "🛡️", "source": "利弗莫尔交易箴言", "angle": "利弗莫尔说亏损最多的交易，往往不是判断错误，而是没有果断止损。他的原话是：导致大多数交易者亏损的原因，是他们不愿意承认自己错了。"},
    {"icon": "🎯", "source": "《刻意练习》", "angle": "交易不是靠天赋，是靠刻意练习。每天复盘每一笔操作，写下来你为什么买、为什么卖、仓位多少、当时什么情绪。三个月后回看，你会被自己蠢到。"},
    {"icon": "🕯️", "source": "《思考，快与慢》", "angle": "卡尼曼说人的大脑有两套系统：快思考是直觉，慢思考是理性。做交易时快思考让你追涨杀跌，慢思考让你等。多等五分钟，省几万。"},
    {"icon": "🔗", "source": "《纳瓦尔宝典》", "angle": "Naval说长期主义是最大的杠杆。在币圈长期主义不等于死扛，等于你有自己的框架、有纪律、不被每一根线带节奏。真正能穿越牛熊的不是信念，是规则。"},
    {"icon": "🪙", "source": "《货币的非国家化》", "angle": "哈耶克几十年前就说过货币可以不是国家发行的。比特币把这个理论变成了现实。但现实是，绝大多数人买比特币不是因为信仰，是因为想以更高价卖给别人。"},
    {"icon": "👁️", "source": "塔勒布《Skin in the Game》", "angle": "塔勒布说要相信有皮肤在游戏中的人。在币圈，你要看喊单的人自己买了没有、买了多少。没持仓的建议一文不值。自己的钱自己负责。"},
    {"icon": "🗿", "source": "《孙子兵法》", "angle": "孙子说：先为不可胜，以待敌之可胜。翻译成币圈的话：先保证自己不死，再等别人犯错。等市场恐慌到极致的时候，才是你的时机。"},
    {"icon": "🌙", "source": "《禅与摩托车维修艺术》", "angle": "交易到一定阶段，技术分析反而没那么重要了。重要的是你有没有在安静地观察市场，不被噪音干扰。静下心来读一读这些'无用'的书，比看一百个KOL喊单有用。"},
    {"icon": "📊", "source": "《非对称风险》", "angle": "好的交易不是赚得多，是赚的时候赚够了，亏的时候亏得少。这个非对称的比例，决定了你能不能留在牌桌上。追求胜率不如追求盈亏比。"},
    {"icon": "💡", "source": "《邓普顿教你逆向投资》", "angle": "邓普顿说：牛市在悲观中诞生，在怀疑中成长，在乐观中成熟，在狂热中死亡。你现在觉得市场在哪个阶段？这个问题的答案，决定了你现在的仓位。"},
    {"icon": "🔄", "source": "《周期》", "angle": "霍华德·马克斯说市场像钟摆，永远在极端之间摇摆。币圈的钟摆幅度更大。你能做的不是预测摆到哪，是在它摆过头的时候站在对面。"},
    {"icon": "☕", "source": "晨读随感", "angle": "今早突然想通一件事：交易最大的敌人不是市场，是你自己给自己的压力。越想翻倍越容易翻车。放慢一点，反而走得远。"},
    {"icon": "📝", "source": "交易笔记里的觉悟", "angle": "翻到三个月前的交易笔记，发现当时的判断大部分是对的方向，但都被自己反复操作给毁了。频繁调整仓位、反复止损止盈，最后什么都没抓住。少动就是多赚。"},
    {"icon": "💤", "source": "深夜反思", "angle": "以前总觉得自己不够聪明，所以赚不到钱。后来发现正相反——是太聪明了，总想找到最优解，结果错过了一个又一个简单的机会。在这个市场，笨一点反而是优势。"},
    {"icon": "🗣️", "source": "市场噪音与独立思考", "angle": "群里永远有人在喊'这波不一样'。但回头看，每轮牛熊的基本剧本从来没变过。不一样的是讲故事的人，一样的是最后亏钱的人。"},
    {"icon": "🧘", "source": "交易与修心", "angle": "做交易三年，最大的收获不是赚了多少钱，是学会了跟自己和解。止损不丢人，乱止损才丢人。看错不丢人，不承认才丢人。诚实面对自己，比任何技术指标都管用。"},
    {"icon": "🌅", "source": "凌晨4点盯盘有感", "angle": "凌晨四点被爆仓提醒惊醒，那种感觉，经历过的人才懂。后来我把所有仓位都设了止损，再也没有被半夜叫醒过。一个好的交易系统，应该让你睡得着觉。"},
    {"icon": "🧭", "source": "《论持久战》", "angle": "这是一场持久战。不是你死我活的短线博弈，是你自己的纪律跟人性的长期拉锯。每次手痒想追的时候，问自己一个字：急什么？"},
    {"icon": "🪶", "source": "读过无数交易书的感悟", "angle": "读了几十本投资书，最后发现所有道理都指向同一个东西：管住自己。不是市场不给你机会，是你太想抓住每一个机会。轻仓、少动、等机会，九个字够了。"},
]


def _pick_philo_coin() -> dict:
    """为投资哲学帖选一个当天有代表性的币种。优先 BTC/ETH，其次当日波动最大的非稳定币。"""
    rows = [r for r in spot_24h() if r["quoteVolume"] > 50_000_000 and abs(r["priceChangePercent"]) > 1]
    rows.sort(key=lambda r: (abs(r["priceChangePercent"]), math.log10(r["quoteVolume"] + 1)), reverse=True)
    # 优先选 BTC 或 ETH
    for r in rows:
        if r["base"] in MAJORS:
            return r
    # 其次选今天还没在哲学帖里用过的
    for r in rows:
        if not _pushed_recent(r["base"], days=0):
            return r
    return rows[0] if rows else None


def _philo_angle(slot: str) -> dict:
    """按日期+时段+slot稳定选一条投资哲学，4个时段每天不重复。"""
    idx_pool = list(range(len(PHILO_ANGLE_POOL)))
    # 把池子按日期分成4份，每个时段从不同份里取
    ni = int(hashlib.md5(today_str().encode("utf-8")).hexdigest(), 16) % len(PHILO_ANGLE_POOL)
    quarter = len(PHILO_ANGLE_POOL) // 4
    slot_offset = {"morning": 0, "noon": 1, "afternoon": 2, "night": 3}
    offset = slot_offset.get(slot.split("_")[-1], 0)
    idx = (ni + offset * quarter) % len(PHILO_ANGLE_POOL)
    return PHILO_ANGLE_POOL[idx]


# ---------------- 选题 ----------------

def build_item(slot: str) -> dict:
    if slot == "pre_market":
        snap_btc = market_snapshot("BTCUSDT")
        snap_eth = market_snapshot("ETHUSDT")
        return {"topic": "pre_market", "symbol": "BTC/ETH",
                "title": "开盘前瞻", "data": {"btc": snap_btc, "eth": snap_eth}}

    if slot == "morning":
        # pre_market 已经覆盖了 BTC+ETH，morning 固定写 ETH 避免跟开盘前瞻撞车
        snap = market_snapshot("ETHUSDT")
        return {"topic": "eth_morning", "symbol": "ETH", "title": "ETH 早盘走势", "data": snap}

    if slot == "mid_morning":
        mode = _content_mode("mid_morning")
        if mode == "alt":
            g = pick_big_mover()
            if g:
                sym = g["base"]
                return {"topic": "coin_story", "symbol": sym,
                        "title": f"复盘${sym}这波行情", "data": g, "content_mode": "story"}
        # fallback to original
        g = pick_early_gainer()
        if g:
            return {"topic": "early_gainer", "symbol": g["base"],
                    "title": f"早盘涨幅：{g['base']} +{g['priceChangePercent']:.1f}%", "data": g}
        snap = market_snapshot("ETHUSDT")
        return {"topic": "eth_mid", "symbol": "ETH", "title": "ETH 早盘走势", "data": snap}

    if slot == "noon":
        g = pick_top_gainer()
        if g:
            return {"topic": "top_gainer", "symbol": g["base"], "title": f"{g['base']} 午盘热门", "data": g}
        snap = market_snapshot("ETHUSDT")
        return {"topic": "eth_noon", "symbol": "ETH", "title": "ETH 午盘走势", "data": snap}

    if slot == "afternoon":
        mode = _content_mode("afternoon")
        if mode == "alt":
            snap = market_snapshot("ETHUSDT")
            return {"topic": "trading_psychology", "symbol": "ETH",
                    "title": "交易心理提醒", "data": snap, "content_mode": "psychology"}
        snap = market_snapshot("ETHUSDT")
        return {"topic": "contract_sentiment", "symbol": "ETH", "title": "ETH 合约情绪", "data": snap}

    if slot == "late_noon":
        mode = _content_mode("late_noon")
        if mode == "alt":
            h = pick_debate_coin()
            if h:
                return {"topic": "hot_debate", "symbol": h["base"],
                        "title": f"讨论${h['base']}：多空分歧最大", "data": h, "content_mode": "debate"}
            snap = market_snapshot("BTCUSDT")
            return {"topic": "btc_debate", "symbol": "BTC",
                    "title": "BTC多空争议", "data": snap, "content_mode": "debate"}
        # original
        h = pick_day_recap()
        if h:
            return {"topic": "day_recap", "symbol": h["base"], "title": f"今日热门：{h['base']}", "data": h}
        snap = market_snapshot("ETHUSDT")
        return {"topic": "eth_recap", "symbol": "ETH", "title": "ETH 日内复盘", "data": snap}

    if slot == "evening":
        snap = market_snapshot("ETHUSDT")
        return {"topic": "eth_evening", "symbol": "ETH", "title": "ETH 晚间走势", "data": snap}

    if slot == "night":
        h = pick_hot_symbol()
        if h:
            return {"topic": "hot_symbol", "symbol": h["base"], "title": f"{h['base']} 夜盘复盘", "data": h}
        snap = market_snapshot("ETHUSDT")
        return {"topic": "eth_night", "symbol": "ETH", "title": "ETH 夜盘观察", "data": snap}

    if slot == "recap":
        btc = market_snapshot("BTCUSDT")
        eth = market_snapshot("ETHUSDT")
        top = pick_top_gainer()
        sym = top["base"] if top else "?等"
        return {"topic": "day_recap", "symbol": "BTC/ETH",
                "title": f"全天复盘：BTC/ETH/{sym}",
                "data": {"btc": btc, "eth": eth, "top": top}}

    # ---- 投资哲学帖（4条，不涉及行情，去重靠日期+时段+角度的稳定分配）----
    if slot.startswith("philo_"):
        vibe = slot.split("_", 1)[1]
        angle = _philo_angle(slot)
        coin = _pick_philo_coin()
        sym = coin["base"] if coin else "BTC"
        return {
            "topic": f"philosophy_{vibe}",
            "symbol": sym,
            "title": f"投资哲学·{SLOT_LABEL[slot].split('·')[1]}",
            "data": {"vibe": vibe, "angle": angle, "coin": coin} if coin else {"vibe": vibe, "angle": angle},
        }

    raise ValueError(f"未知 slot: {slot}")


def render_data(item: dict) -> str:
    d = item["data"]
    # 投资哲学帖——附带当天代表性币种走势
    if "angle" in d:
        a = d["angle"]
        lines = [f"来源：{a['source']}", f"角度：{a['angle']}"]
        c = d.get("coin")
        if c:
            lines.append(f"【今日盘面案例】${c['base']} 当前{_fmt_price(c['lastPrice'])}，24h {_fmt_pct(c['priceChangePercent'])}，成交{c.get('quoteVolume',0)/1e8:.2f}亿USDT")
        return "\n".join(lines)
    # 全天复盘：BTC + ETH + 最强币
    if "btc" in d and "eth" in d and "top" in d:
        lines = ["【BTC全天】"]
        b = d["btc"]
        lines.extend([
            f"当前：{_fmt_price(b['current'])}（{_fmt_pct(b['change24'])}）",
            f"全天高点：{_fmt_price(b['high24'])}  低点：{_fmt_price(b['low24'])}",
            f"成交额：{b['quoteVolume']/1e8:.2f}亿USDT  趋势：{b['trend']}",
        ])
        lines.append("【ETH全天】")
        e = d["eth"]
        lines.extend([
            f"当前：{_fmt_price(e['current'])}（{_fmt_pct(e['change24'])}）",
            f"全天高点：{_fmt_price(e['high24'])}  低点：{_fmt_price(e['low24'])}",
            f"成交额：{e['quoteVolume']/1e8:.2f}亿USDT  趋势：{e['trend']}",
        ])
        t = d.get("top")
        if t:
            lines.append(f"【今日最强】${t['base']}")
            lines.extend([
                f"当前：{_fmt_price(t['lastPrice'])}（{_fmt_pct(t['priceChangePercent'])}）",
                f"成交额：{t['quoteVolume']/1e8:.2f}亿USDT",
            ])
        return "\n".join(lines)
    # 开盘前瞻：BTC + ETH 双数据
    if "btc" in d and "eth" in d:
        lines = ["【BTC】"]
        b = d["btc"]
        lines.extend([
            f"当前价：{_fmt_price(b['current'])}",
            f"24h涨跌：{_fmt_pct(b['change24'])}",
            f"24h高点：{_fmt_price(b['high24'])}  低点：{_fmt_price(b['low24'])}",
            f"成交额：{b['quoteVolume']/1e8:.2f}亿USDT  趋势：{b['trend']}",
        ])
        lines.append("【ETH】")
        e = d["eth"]
        lines.extend([
            f"当前价：{_fmt_price(e['current'])}",
            f"24h涨跌：{_fmt_pct(e['change24'])}",
            f"24h高点：{_fmt_price(e['high24'])}  低点：{_fmt_price(e['low24'])}",
            f"成交额：{e['quoteVolume']/1e8:.2f}亿USDT  趋势：{e['trend']}",
        ])
        return "\n".join(lines)
    if "current" in d:
        return "\n".join([
            f"币种：${item['symbol']}",
            f"当前价：{_fmt_price(d['current'])}",
            f"24h涨跌：{_fmt_pct(d['change24'])}",
            f"24h高点：{_fmt_price(d['high24'])}",
            f"24h低点：{_fmt_price(d['low24'])}",
            f"48h高点：{_fmt_price(d['high48'])}",
            f"48h低点：{_fmt_price(d['low48'])}",
            f"短线趋势：{d['trend']}",
            f"24h成交额：{d['quoteVolume']/1e8:.2f}亿USDT",
        ])
    return "\n".join([
        f"币种：${d['base']}",
        f"当前价：{_fmt_price(d['lastPrice'])}",
        f"24h涨跌：{_fmt_pct(d['priceChangePercent'])}",
        f"24h高点：{_fmt_price(d['highPrice'])}",
        f"24h低点：{_fmt_price(d['lowPrice'])}",
        f"24h成交额：{d['quoteVolume']/1e8:.2f}亿USDT",
    ])


def fallback_square_post(slot: str, item: dict) -> str:
    """AI 返回空内容或发帖接口判空时的本地兜底模板。每个时段有专属模板。"""
    d = item["data"]
    slot_label = SLOT_LABEL.get(slot, slot)

    # ---- 投资哲学兜底 ----
    if "angle" in d:
        a = d["angle"]
        c = d.get("coin")
        coin_tag = f"${c['base']}" if c else ""
        coin_bit = ""
        if c:
            coin_bit = (
                f"\n\n拿今天的 ${c['base']} 举个例子："
                f"现在 {_fmt_price(c['lastPrice'])}，24h {_fmt_pct(c['priceChangePercent'])}。"
                f"这种盘面刚好印证了上面的道理。不是什么巧合，是人性在盘面上的重复。"
            )
        return (
            f"今早翻书看到一句话，让我在屏幕前愣了好一会。\n\n"
            f"{a['source']}里面讲到一个道理：\n"
            f"{a['angle']}{coin_bit}\n\n"
            f"越想越觉得，交易做到最后，比的不是技术，是心性。\n"
            f"先记下来，过段时间再回头看。\n\n"
            f"#{c['base'] if c else 'BTC'} #投资哲学 #交易心态"
        )

    def _snap_price(dd):
        if "current" in dd:
            return _fmt_price(dd["current"]), _fmt_pct(dd.get("change24", 0)), _fmt_price(dd.get("high24", 0)), _fmt_price(dd.get("low24", 0)), f"{dd.get('quoteVolume', 0)/1e8:.2f}亿USDT"
        return _fmt_price(dd["lastPrice"]), _fmt_pct(dd.get("priceChangePercent", 0)), _fmt_price(dd.get("highPrice", 0)), _fmt_price(dd.get("lowPrice", 0)), f"{dd.get('quoteVolume', 0)/1e8:.2f}亿USDT"

    # ---- 全天复盘 ----
    if "btc" in d and "eth" in d and "top" in d:
        b, e = d["btc"], d["eth"]
        bp, bpct, bh, bl, bv = _snap_price(b)
        ep, epct, eh, el, ev = _snap_price(e)
        t = d.get("top")
        tbase = t["base"] if t else "?"
        tp, tpct = (_fmt_price(t["lastPrice"]), _fmt_pct(t.get("priceChangePercent", 0))) if t else ("?", "?")
        tv = f"{t.get('quoteVolume', 0)/1e8:.2f}亿" if t else "?"
        x = f"今天最猛的是 ${tbase}，全天涨了 {tpct}"
        return (
            f"睡前把今天盘面捋了一遍。说实话，今天走得挺有信息量的。\n\n"
            f"BTC 今天从 {bl} 到 {bh} 之间晃，最后收在 {bp}，全天 {bpct}。"
            f"这个走势最值得看的不是涨跌本身，而是成交额有没有跟上。今天 {bv}，说实话不算活跃，说明市场情绪还是比较谨慎的。\n\n"
            f"ETH 这边稍微{'强' if epct.startswith('+') else '弱'}一点，全天 {epct}，收在 {ep}，"
            f"波动区间 {el} 到 {eh}。"
            f"和大饼的联动还是很明显，大饼不动它也很难独立走。\n\n"
            f"{x}，成交 {tv}。"
            f"这种走势要么是资金提前埋伏，要么就是情绪博弈放大了波动。\n\n"
            f"今天最核心的信号：BTC 能不能在关键位置放量，决定接下来的方向。"
            f"明天我会重点看 BTC 的 xxx 位置能不能守住。\n\n"
            f"今天你们打到猎物了吗？明天你最关注哪个币？\n\n"
            f"#BTC #ETH #全天复盘 #币圈"
        )

    # ---- 开盘前瞻 ----
    if "btc" in d and "eth" in d:
        b, e = d["btc"], d["eth"]
        bp, bpct, bh, bl, bv = _snap_price(b)
        ep, epct, eh, el, ev = _snap_price(e)
        bdir = "偏强" if bpct.startswith("+") else "偏弱"
        edir = "偏强" if epct.startswith("+") else "偏弱"
        return (
            f"开盘前扫一眼。BTC 现在卡在 {bp}，24h {bpct}，整体 {bdir}。"
            f"ETH 在 {ep} 附近，24h {epct}，也是 {edir}。\n\n"
            f"BTC 夜里走的区间是 {bl} 到 {bh}，这个位置挺关键的。"
            f"如果开盘能放量站稳 {bh} 附近，短线情绪会好很多；"
            f"反过来如果开盘就往 {bl} 下面砸，那今天大概率是个震荡日。\n\n"
            f"ETH 这边更看 BTC 脸色。大饼不给方向，以太很难自己独立走。"
            f"成交额 {ev}，不算活跃，说明大家都在等开盘的信号。\n\n"
            f"今天我不会一开盘就动手。先看半小时，确认方向再说。"
            f"你们开盘先盯大饼还是先盯山寨？\n\n"
            f"#BTC #ETH #早盘 #行情前瞻"
        )

    symbol = item["symbol"]

    # ---- 各时段专属兜底 ----
    if "current" in d:
        price, pct, high, low, volume = _fmt_price(d["current"]), _fmt_pct(d["change24"]), _fmt_price(d["high24"]), _fmt_price(d["low24"]), f"{d['quoteVolume']/1e8:.2f}亿USDT"
    else:
        price, pct, high, low, volume = _fmt_price(d["lastPrice"]), _fmt_pct(d["priceChangePercent"]), _fmt_price(d["highPrice"]), _fmt_price(d["lowPrice"]), f"{d['quoteVolume']/1e8:.2f}亿USDT"

    if slot == "morning":
        return (
            f"ETH 早盘现在在 {price} 晃，24h {pct}。\n\n"
            f"昨晚高低点 {high} / {low}，整体在区间内震荡。"
            f"早盘最关键的看点是能不能守住 {low} 这个位置——"
            f"如果破了，下面空间就打开了；如果稳住了，短线可以看一波小反弹。\n\n"
            f"成交额 {volume}，量不算大，说明早盘资金还在观望，没有明显的方向性选择。"
            f"这时候最忌讳急着动手，先看清楚再出手。\n\n"
            f"ETH 早盘你们准备怎么操作？\n\n"
            f"#ETH #早盘 #行情分析 #币圈"
        )

    if slot == "mid_morning":
        return (
            f"早盘扫了一圈，${symbol} 今天有点意思，直接拉了 {pct}。\n\n"
            f"现在价格在 {price}，日内高低点 {high} / {low}。"
            f"成交额 {volume}，说明有资金在主动买，不是散户瞎冲。\n\n"
            f"这种早盘突然放量的币，两种情况最常见："
            f"要么是有利好提前被资金嗅到了，要么是主力在试盘。"
            f"不管是哪种，这个时候最怕的就是无脑追。"
            f"已经涨了这么多，追进去风险大于机会。\n\n"
            f"如果你已经在里面了，盯好 {low} 这个位置，破了一定要走。"
            f"如果还没进，建议等回调到有支撑的位置再考虑。\n\n"
            f"早盘这波你追了吗？\n\n"
            f"#早盘 #涨幅榜 #{symbol} #行情"
        )

    if slot == "noon":
        return (
            f"午饭时间扫一眼盘。${symbol} 今天走势挺强的，24h 涨了 {pct}。\n\n"
            f"现在价格 {price}，日内高 {high} 低 {low}，成交额 {volume}。"
            f"这个量能说明不是小打小闹，确实有资金在关注它。\n\n"
            f"但我还是要说一句：午盘追高是有代价的。"
            f"很多币中午冲一波，下午就开始回落。"
            f"如果你看好它，与其现在冲进去，不如等下午确认一下承接再说。\n\n"
            f"午盘这个位置，你是已经上车了还是在等回调？\n\n"
            f"#{symbol} #午盘 #涨幅榜 #币圈"
        )

    if slot == "afternoon":
        return (
            f"下午盘面走到这，ETH 在 {price} 附近，24h {pct}。\n\n"
            f"日内区间 {low} 到 {high}，成交额 {volume}。"
            f"从盘面看，多空双方现在都没有太大的动作，都在等一个信号。\n\n"
            f"如果下午能放量突破 {high}，说明多头还有点想法；"
            f"反过来如果回踩 {low} 还站不住，那这一波可能就要告一段落了。"
            f"合约的朋友这个时候最忌讳的就是重仓赌方向，容易被一根线带走。\n\n"
            f"下午这行情你是空仓看戏，还是短线搞一波？\n\n"
            f"#ETH #合约 #资金费率 #行情"
        )

    if slot == "late_noon":
        return (
            f"快收盘了，今天 ${symbol} 确实值得聊一下。\n\n"
            f"全天涨了 {pct}，成交额 {volume}，是今天盘面里最活跃的币之一。"
            f"价格从 {low} 一路拉到 {high}，现在回落到 {price} 附近。\n\n"
            f"这种走势说明资金还没走，但短线获利盘也在出。"
            f"明天最关键的看点是能不能在 {price} 附近继续放量。"
            f"如果量跟不上，大概率会回踩一下；如果继续放量，那空间就打开了。\n\n"
            f"今天吃到这波了吗？明天这个币你还看好吗？\n\n"
            f"#{symbol} #今日热门 #行情复盘 #币圈"
        )

    if slot == "evening":
        return (
            f"ETH 晚间现在在 {price}，24h {pct}。\n\n"
            f"今天全天区间 {low} 到 {high}，成交额 {volume}。"
            f"晚间这个时间段，ETH 最容易跟着 BTC 的节奏走。"
            f"如果大饼突然拉升，ETH 大概率会跟一波；"
            f"反过来大饼跳水，ETH 也很难独善其身。\n\n"
            f"晚间想动手的话，建议盯好 {low} 这个支撑位。"
            f"站稳了可以考虑轻仓试试，破了就等下一个支撑。"
            f"最怕的就是晚上情绪上头，没想清楚就冲进去。\n\n"
            f"今晚 ETH 你最关注哪个位置？\n\n"
            f"#ETH #晚间行情 #币圈 #以太坊"
        )

    if slot == "night":
        return (
            f"夜深了，睡前最后扫一眼 ${symbol}。\n\n"
            f"全天 {pct}，收在 {price}，高低点 {high} / {low}。"
            f"成交额 {volume}，今天这个量能算是比较有诚意的。\n\n"
            f"从盘面看，今天最大的信号是 {symbol} {'有资金在关注' if pct.startswith('+') else '还在消化压力'}。"
            f"明天如果能{'站稳' + price if pct.startswith('+') else '在' + low + '附近找到支撑'}，"
            f"行情可能还有延续的空间。"
            f"但如果明天开盘直接往下砸，那今天这波大概率就是个短炒。\n\n"
            f"今晚不熬夜了。明天你最先关注的币是什么？\n\n"
            f"#{symbol} #夜盘 #行情复盘 #币圈"
        )

def _clean_content(content: str) -> str:
    return (content or "").strip()


# DeepSeek 去AI话指令（所有提示词共享）
_DEEPSEEK_STYLE = """
【写的时候死也不能做的事】
- 不许用"从...来看"、"从盘面来看"、"从数据来看"、"综上所述"、"总的来看"、"值得注意的是"、"需要指出的是"、"毫无疑问"、"显而易见"——这些全是AI套话。
- 不许用"为...提供了有力支撑"、"起到了关键作用"、"充分说明了"、"印证了"——这是研报腔。
- 不许结尾总结。你不是在写作文。说完就说完，不要最后来个"总的来说"。
- 不许用"因此"、"所以"来连接每一段——人说话不这样。
- 写短句。10-20个字一句。偶尔三五个字的超短句制造节奏。
- 数据必须融进句子里，不要单独一句"成交额xxx"。
- 像在群里发了条消息。不是写了篇文章。"""

# 结尾方式池（禁止所有时段用固定句式收尾）
_ENDING_RULES = """
【结尾规则——违反算失败】
- 结尾不许用"你怎么看？"、"你敢追吗？"、"你会接吗？"、"你动手了吗？"、"你怎么操作？"、"你还在盯吗？"、"你吃到这波了吗？"、"你关注哪个币？"——这些是机器人提问。
- 结尾可以是一句实话、一个自嘲、一个判断、一个反问、一个感叹。不许用"这个位置你X还是Y？"的格式。
- 参考结尾类型（每次选不一样的）：
  自嘲型："反正我是不敢动了。"
  判断型："这位置我不碰。"
  反问型："谁敢接这种飞刀？"
  提醒型："别追。等确认。"
  感叹型："这盘看着真累。歇了。"
  行动型："我先挂着，睡了。"
  问题型（唯一允许的）："你们呢？"（只许这三个字，不许加"你怎么看/你怎么操作"）
- 结尾不准超过10个字。短才像人。"""


def make_square_post(slot: str, item: dict) -> str:
    data_text = render_data(item)
    slot_label = SLOT_LABEL.get(slot, slot)
    cm = item.get("content_mode", "")

    now = now_cn()
    weekday_map = ["周一","周二","周三","周四","周五","周六","周日"]
    today_cn = f"{now.strftime('%Y年%m月%d日')} {weekday_map[now.weekday()]} {now.strftime('%H:%M')}"
    _TIME_CONTEXT = f"\n【当前北京时间：{today_cn}。如果内容涉及\"今天\"\"明天\"\"周末\"\"下周\"等时间表述，必须以此为准。今天是{weekday_map[now.weekday()]}不是周末。】"

    # ---- 投资哲学帖（4条，不分析行情，去重靠角度池稳定分配）----
    if slot.startswith("philo_"):
        pdata = item["data"]
        a = pdata.get("angle", {})
        source = a.get("source", "经典")
        angle_desc = a.get("angle", "")
        hour_map = {"morning": "早上", "noon": "午后", "afternoon": "傍晚", "night": "深夜"}
        h = hour_map.get(pdata.get("vibe", ""), "今天")
        coin = pdata.get("coin")
        coin_line = ""
        if coin:
            coin_line = (
                f"今天盘面上 ${coin['base']} 当前 {_fmt_price(coin['lastPrice'])}，"
                f"24h {_fmt_pct(coin['priceChangePercent'])}，"
                f"成交 {coin.get('quoteVolume',0)/1e8:.2f} 亿USDT。"
            )
        prompt = f"""你是币安广场上一个分享投资感悟的人。不装老师、不说教。就像一个交易了几年的老韭菜，读到某本书、某句话，跟自己的经历对上号了，顺手分享出来。

现在写一条投资哲学短帖。时段：{h}。

内容要求：
- 切入来源：{source}
- 核心角度：{angle_desc}
- 必须用今天 ${item['symbol']} 的盘面作为"现学现用"的例子。{coin_line}把投资理念和这个盘面自然结合起来——不是分析它接下来怎么走，是"你看，今天这盘面刚好说明了这个道理"。
- 用自己的话讲，不要照抄原话。如果引用原文，要给出处。

铁律：
1.只输出正文。
2.开头必须有钩子。像突然想到什么就说出来。不要"今天跟大家分享"。
3.每句独立成行，句间空一行。全文150-280字。
4.必须出现 ${item['symbol']} 至少一次。必须结合今天的盘面数据来阐释投资理念。
5.不能喊单、不能预测方向、不能写"这次不一样"。
6.结尾自然收。不许用"你怎么看？""你觉得呢？"。可以是"先记下来"、"分享给你"、"就这些"这种。
7.标签：#${item['symbol']} #投资哲学 #交易心态 #{h}读 #{h}思。
8.零emoji，纯文本。

主题：{item['title']}

直接输出正文。"""

    # ---- 3 种新内容类型提示词（与原有行情交替出现）----

    if cm == "story":
        prompt = f"""你是一个在币安广场做行情复盘的老韭菜。风格：说人话、讲故事、有情绪、有反思，不要像AI念数据。

现在写一条币圈行情复盘故事。挑一个最近波动大的币，讲一段它的行情故事。

铁律：
1.只输出正文，不要"今天给大家讲个故事"这种开场白。
2.开头就是钩子，像在群里跟兄弟分享一件刚发生的事，比如"前两天$xxx这波，说实话我是真没想到"。
3.每句独立成行，句间空一行。全文180-320字，短句为主。
4.正文要有：这个币最近发生了什么（涨了还是跌了多少、为什么）、一段行情故事（谁在买/谁在跑/情绪怎么变的）、一个教训或反思。
5.数据可以提但不堆砌，让人感受到数字背后的情绪。
6.不能写"必涨、稳赚、梭哈、无脑多、无脑空"。
7.结尾要有人味，不喊单。可以是一个感悟、一个自嘲、或者一个给读者的提醒。
8.标签3-4个：#行情复盘 #币圈故事 #${item['symbol']} #交易心态。
9.零emoji，纯文本。

主题：{item['title']}
数据：
{data_text}

直接输出正文。"""

    elif cm == "psychology":
        psych_angle = _psych_angle(slot)
        prompt = f"""你是币安广场上一个聊交易心态的老韭菜。风格：像过来人跟新韭菜聊天，不是老师，不是说教，是自己踩过坑的真实感受。

写一条交易心理/风险提醒帖。

本条切入角度：{psych_angle}
请围绕这个角度写，不要偏题。用第一人称或第三人称（某个老哥/某个兄弟）都可以，要让人觉得这是真实发生过的故事或感悟。

铁律：
1.只输出正文。
2.开头用一句跟这个角度有关的感悟或场景切入，像在群里跟兄弟聊天。
3.每句独立成行，句间空一行。160-320字。
4.正文要有一个真实感的故事或场景（可以是虚构的，但要像真的）：某个人做了什么操作、结果怎样、教训是什么。
5.不要列"几条建议"，要像聊天一样自然带出观点。
6.不能写杠杆建议，不喊单。
7.结尾按结尾规则来。
8.标签：#交易心态 #合约 #风险控制 #币圈。
9.零emoji，纯文本。

主题：{item['title']}
当前盘面背景（仅参考，不要直接写）：
{data_text}

直接输出正文。"""

    elif cm == "debate":
        prompt = f"""你是币安广场上一个爱聊行情争议的号。风格：有自己观点但留余地，抛出话题让人讨论，不是下结论。

写一条多空争议帖。

铁律：
1.只输出正文。
2.开头点出一个争议话题，比如"现在最分裂的币就是${item['symbol']}，有人说明天就起飞，有人说这波就是诱多"。
3.每句独立成行，句间空一行。180-300字。
4.正文要写出两种对立观点：多头怎么看、空头怎么看，各1-2句。然后说一句自己的倾向但不把话说死。
5.数据可以提但不堆砌，用来支撑两边的观点。
6.不能喊单，不能用"必涨"、"肯定跌"之类断语。
7.结尾按结尾规则来。
8.标签：#${item['symbol']} #多空博弈 #币圈 #行情讨论。
9.零emoji，纯文本。

主题：{item['title']}
数据：
{data_text}

直接输出正文。"""

    # ---- 其他时段（按 slot 分支）----

    if cm:
        # content_mode 已处理，直接跳到生成
        pass
    elif slot.startswith("philo_"):
        # 投资哲学帖已在上面生成 prompt，跳过
        pass
    elif slot == "pre_market":
        prompt = f"""你是一个混币安广场的行情观察号，说话风格像一个盯了几年盘的老韭菜。不装神，不喊单，但敢说自己的判断。

现在开盘前，写一条开盘前瞻帖。

铁律：
1.只输出正文，不要废话开场。
2.开头必须是钩子句。用一句话把BTC和ETH的盘前位置讲清楚，比如"大饼现在卡在 xxx，以太稍微强一点，在 xxx 晃"。
3.正文150-300字就够了。每句话独立成行，句间空一行。用短句，像在群里吹水。
4.必须有2-3个观点或分析：BTC关键位置在哪、ETH关键位置在哪、开盘后最可能怎么走。
5.结合具体数据写（价格、涨跌、高低点、成交额、趋势），但数字不要罗列，要融进句子里。
6.不能写"必涨、稳赚、梭哈、无脑多、无脑空"。
7.结尾按结尾规则来。
8.正文末尾放3-4个标签：#BTC #ETH #早盘 #行情前瞻。
9.不用任何emoji，不用markdown。

主题：开盘前瞻
数据：
{data_text}

直接输出正文。"""

    elif slot == "mid_morning":
        prompt = f"""你是币安广场上一个盯早盘的行情号。说话风格：看到了什么就说什么，有数字有判断，不写官样文章。

现在写一条早盘涨幅快报。

铁律：
1.只输出正文。
2.开头必须是钩子，比如"开盘两个小时，今天最先拉的不是大饼，是 $xxx"、"早盘这根线有点意思"。
3.每句单独成行，句间空一行。全文150-280字，短句为主。
4.正文必须有：哪个币涨得最猛（具体数据）、为什么可能被资金盯上（1-2句判断）、早盘追进去风险在哪（1句提醒）。
5.数据融进句子，不要列清单。
6.不能写"必涨、梭哈、无脑冲"。
7.结尾按结尾规则来。
8.放3-4个标签：#早盘 #涨幅榜 #{item['symbol']} #行情。
9.零emoji，纯文本。

主题：{item['title']}
数据：
{data_text}

直接输出正文。"""

    elif slot == "afternoon":
        prompt = f"""你是币安广场上聊合约情绪的行情号。说话像老韭菜在复盘：有数据、有观点、不说套话。

现在写一条午后ETH合约情绪帖。

铁律：
1.只输出正文。
2.开头钩子：用一句话点出ETH当前合约氛围——多头亢奋还是空头压着？比如"ETH这资金费率，多头有点上头"。
3.每句单独成行，句间空一行。全文150-280字。
4.正文要有：ETH当前多空氛围分析（结合趋势、成交额、价格位置）、如果费率偏高提醒一句风险、下午可能怎么走（1-2句判断）。
5.数字融进句子，不要列清单。
6.不能写杠杆建议，不能喊单。
7.结尾按结尾规则来。
8.标签：#ETH #合约 #资金费率 #行情。
9.零emoji，纯文本。

主题：{item['title']}
数据：
{data_text}

直接输出正文。"""

    elif slot == "late_noon":
        prompt = f"""你是币安广场上一个做盘后复盘的行情号。风格：实话实说，有观点，不灌水。

现在写一条当日热门币盘点。

铁律：
1.只输出正文。
2.开头钩子：一句话讲今天盘面最有记忆点的东西，比如"今天最猛的不是大饼，是 $xxx，一根线拉了 xx%"。
3.每句单独成行，句间空一行。全文180-320字。
4.正文要有：今天哪个币最值得讨论（具体涨跌和成交额）、为什么它能走出来（1-2句分析）、这个位置明天怎么看（1句判断+1句风险提醒）。
5.数字融入叙述，不要堆数据。
6.不能写"明天必涨"，不能喊单。
7.结尾按结尾规则来。
8.标签：3-4个，含 #{item['symbol']}。
9.零emoji，纯文本。

主题：{item['title']}
数据：
{data_text}

直接输出正文。"""

    elif slot == "morning":
        prompt = f"""你是币安广场上一个每天盯早盘的老韭菜。风格：看了盘就直接说，有判断但不装，数字融在话里不堆砌。

现在写一条ETH早盘帖。观众都是币圈老炮，别写官样文章。

铁律：
1.只输出正文。
2.开头用一句话点出ETH现在的状态和关键位置，比如"ETH今早在1700附近晃，看着像在等大饼给方向"。
3.每句独立成行，句间空一行。150-280字，短句像在群里吹水。
4.正文要有：ETH现在什么位置、关键支撑和压力在哪、早盘最可能怎么走、有什么风险。
5.数据（价格、涨跌、高低点、成交额、趋势）必须出现但不罗列，融进句子。
6.不能写"必涨、稳赚、梭哈、无脑多、无脑空"。
7.结尾按结尾规则来。
8.标签：#${item['symbol']} #早盘 #行情分析 #币圈。
9.零emoji，纯文本。

主题：{item['title']}
数据：
{data_text}

直接输出正文。"""

    elif slot == "noon":
        prompt = f"""你是币安广场上一个做午盘热币分析的号。风格：看到什么说什么，不搞理论那一套，就讲钱往哪走了。

现在写一条午盘热门币帖。

铁律：
1.只输出正文。
2.开头钩子：用一句话点出午盘最值得关注的币和它为什么被盯上，比如"午饭回来扫一眼盘，今天资金全堆在$xxx，一根线拉了xx%"。
3.每句独立成行，句间空一行。150-280字。
4.正文要有：什么币最猛（具体涨跌和成交额）、为什么资金选它（1-2句判断）、午盘追高风险在哪（1句提醒）。
5.数字融进句子，不要列清单。
6.不能写"必涨、梭哈、无脑冲"。
7.结尾按结尾规则来。
8.标签：3-4个，含#{item['symbol']} #午盘 #涨幅榜 #币圈。
9.零emoji，纯文本。

主题：{item['title']}
数据：
{data_text}

直接输出正文。"""

    elif slot == "evening":
        prompt = f"""你是币安广场上一个专门盯ETH晚间走势的号。风格：像晚上边吃饭边刷盘的感觉，轻松但专业。

现在写一条ETH晚间走势帖。

铁律：
1.只输出正文。
2.开头钩子：用一句话说ETH晚间在干嘛，比如"ETH今晚有点意思，在xxx附近晃了一整天，现在开始动了"。
3.每句独立成行，句间空一行。150-280字。
4.正文要有：ETH现在在哪、关键位置（支撑和压力）、晚间可能的方向、如果BTC变脸ETH会怎么跟。
5.数据融进句子，不要罗列。
6.不能写"必涨、稳赚、梭哈"。
7.结尾按结尾规则来。
8.标签：#ETH #晚间行情 #币圈 #以太坊。
9.零emoji，纯文本。

主题：{item['title']}
数据：
{data_text}

直接输出正文。"""

    elif slot == "night":
        prompt = f"""你是币安广场上一个做夜盘复盘的老韭菜。风格：睡前扫一眼盘，总结一下今天，想一下明天。

现在写一条夜盘复盘帖。

铁律：
1.只输出正文。
2.开头钩子：用一句话总结今天盘面最值得记住的事，比如"今天这盘走得，大饼没动，山寨倒是各玩各的"。
3.每句独立成行，句间空一行。160-300字。
4.正文要有：今天最值得关注的币或走势（具体数据）、今天盘面告诉了我们什么（1-2句判断）、明天可能怎么走、有什么要小心的。
5.数据融入叙述，不要堆数字。
6.不能写明天必涨，不能喊单。
7.结尾按结尾规则来。
8.标签：3-4个，含 #{item['symbol']} #夜盘 #行情复盘 #币圈。
9.零emoji，纯文本。

主题：{item['title']}
数据：
{data_text}

直接输出正文。"""

    elif slot == "recap":
        prompt = f"""你是币安广场上一个做全天复盘的老韭菜。风格：诚实回顾今天盘面，不夸大、不装神、像睡前跟兄弟聊天总结。

现在写一条全天复盘帖。不需要预测准确率，不需要打分。

铁律：
1.只输出正文。
2.开头用一句话概括今天盘面，比如"今天这盘走得，大饼全天在6万4上下晃，以太稍微强一点"。
3.每句独立成行，句间空一行。200-350字。
4.正文必须有：BTC全天走势回顾（高低点、收盘位置、方向）、ETH怎么走的（跟大饼还是独立）、今天最猛的币是谁（涨了多少、为什么）、今天市场教会我们什么。
5.数据融进句子，不堆数字。
6.不能写"明天的方向"，可以写"明天最值得盯的"。不能喊单。
7.结尾按结尾规则来。
8.标签：#全天复盘 #BTC #ETH #币圈。
9.零emoji，纯文本。

主题：{item['title']}
数据：
{data_text}

直接输出正文。"""

    else:
        # 未知 slot 的兜底
        prompt = f"""你是币安广场上的中文行情观察博主。风格：说人话，有判断，不喊单。

写一条行情短帖。

铁律：
1.只输出正文。
2.开头有钩子，不要播音腔。
3.每句独立成行，句间空一行。150-300字。
4.数据融进句子不堆砌。
5.不能喊单。
6.结尾按结尾规则来。
7.零emoji，纯文本。

主题：{item['title']}
数据：
{data_text}

直接输出正文。"""

    prompt += _TIME_CONTEXT
    prompt += _DEEPSEEK_STYLE
    prompt += _ENDING_RULES
    content = _clean_content(ai_chat(prompt, model=MODEL_FAST, max_tokens=1200))
    if len(content) < 30:
        print(f"AI生成内容过短 len={len(content)}，使用兜底模板")
        return fallback_square_post(slot, item)
    return content


# ---------------- 主流程 ----------------

def run_slot(slot: str) -> str:
    if slot not in SLOT_LABEL:
        return f"未知 slot: {slot}"

    try:
        item = build_item(slot)
    except Exception as e:
        msg = f"⚠️ 币安广场 {SLOT_LABEL[slot]} 构建数据失败：{e}"
        tg_send(msg)
        return msg

    if _already_slot(slot, item["topic"], item["symbol"]):
        msg = f"⏭ 币安广场 {SLOT_LABEL[slot]} 已发过：${item['symbol']}"
        print(msg)
        return msg

    try:
        content = make_square_post(slot, item)
    except Exception as e:
        print(f"AI生成失败，使用兜底模板: {e}")
        content = fallback_square_post(slot, item)
        tg_send(f"⚠️ 币安广场 {SLOT_LABEL[slot]} AI生成失败，已用兜底模板\n${item['symbol']}")

    content = _clean_content(content)
    print(f"准备发币安广场: slot={slot} symbol={item['symbol']} content_len={len(content)}")

    try:
        res = publish_text(content)
    except SquareError as e:
        # 偶发：AI/接口中间返回空内容导致 Binance 判空。用本地模板兜底重试一次。
        if "Content cannot be empty" in str(e) or len(content) < 30:
            print(f"发帖被判空，使用兜底模板重试: {e}")
            content = fallback_square_post(slot, item)
            try:
                res = publish_text(content)
            except Exception as e2:
                msg = f"⚠️ 币安广场 {SLOT_LABEL[slot]} 兜底重试失败：${item['symbol']}\n{e2}"
                tg_send(msg)
                return msg
        else:
            msg = f"⚠️ 币安广场 {SLOT_LABEL[slot]} 发帖失败：${item['symbol']}\n{e}"
            tg_send(msg)
            return msg
    except Exception as e:
        msg = f"⚠️ 币安广场 {SLOT_LABEL[slot]} 异常：${item['symbol']}\n{e}"
        tg_send(msg)
        return msg

    link = res.get("shareLink") or f"id={res.get('id')}"
    _mark_pushed(slot, item["topic"], item["symbol"], link)

    # TG 只发成功通知，不推正文
    tg_send(f"✅ 币安广场已发 · {SLOT_LABEL[slot]} · ${item['symbol']}\n{link}")

    try:
        git_pull()
        md = f"""---
type: binance_square
date: {today_str()}
slot: {slot}
symbol: {item['symbol']}
topic: {item['topic']}
created: {now_str()}
share_link: {link}
tags: [binance-square, crypto]
---

# {SLOT_LABEL[slot]} · ${item['symbol']}

> 币安广场：{link}

## 数据

```json
{json.dumps(item['data'], ensure_ascii=False, indent=2)}
```

## 正文

{content}
"""
        fname = safe_filename(f"{today_str()}_{slot}_${item['symbol']}")
        write_note("BinanceSquare", fname, md)
        git_commit_push(f"binance square {slot}: {item['symbol']}")
    except Exception as e:
        print(f"保存 vault 失败: {e}")

    return f"✅ 币安广场 {SLOT_LABEL[slot]} ${item['symbol']} 已发"


def main():
    slot = sys.argv[1] if len(sys.argv) > 1 else "morning"
    if slot not in SLOT_LABEL:
        print(f"未知 slot: {slot}，可用: {list(SLOT_LABEL.keys())}")
        return
    print(run_slot(slot))


if __name__ == "__main__":
    main()
