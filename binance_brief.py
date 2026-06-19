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
    "mid_morning": "涨幅快报",
    "noon": "午盘热门币",
    "afternoon": "合约情绪",
    "late_noon": "热门盘点",
    "evening": "ETH晚间",
    "night": "夜盘复盘",
    "recap": "全天复盘",
}

SLOT_ORDER = ["pre_market", "morning", "mid_morning", "noon", "afternoon", "late_noon", "evening", "night", "recap"]


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

    raise ValueError(f"未知 slot: {slot}")


def render_data(item: dict) -> str:
    d = item["data"]
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
    """AI 返回空内容或发帖接口判空时的本地兜底模板。"""
    d = item["data"]
    slot_label = SLOT_LABEL.get(slot, slot)

    # 全天复盘兜底
    if "btc" in d and "eth" in d and "top" in d:
        b, e = d["btc"], d["eth"]
        t = d.get("top")
        top_line = f"今日最强 ${t['base']}：{_fmt_pct(t['priceChangePercent'])}，成交{t['quoteVolume']/1e8:.2f}亿" if t else ""
        return f"""睡前扫一眼今天盘面。

BTC 收在 {_fmt_price(b['current'])}，全天 {_fmt_pct(b['change24'])}，高低点 {_fmt_price(b['high24'])} / {_fmt_price(b['low24'])}。
ETH 收在 {_fmt_price(e['current'])}，全天 {_fmt_pct(e['change24'])}，高低点 {_fmt_price(e['high24'])} / {_fmt_price(e['low24'])}。
{top_line}

今天最关键的信号是：BTC 有没有站稳关键位，ETH 有没有跟着大饼走。

明天你最先关注哪个？

#BTC #ETH #全天复盘 #币圈"""

    if "btc" in d and "eth" in d:
        b, e = d["btc"], d["eth"]
        return f"""开盘前扫一眼：BTC 在 {_fmt_price(b['current'])}（{_fmt_pct(b['change24'])}），ETH 在 {_fmt_price(e['current'])}（{_fmt_pct(e['change24'])}）。

BTC 24h 高 {_fmt_price(b['high24'])} 低 {_fmt_price(b['low24'])}，趋势 {b['trend']}。
ETH 24h 高 {_fmt_price(e['high24'])} 低 {_fmt_price(e['low24'])}，趋势 {e['trend']}。

开盘最该盯的不是涨跌，是BTC能不能站稳关键位、ETH有没有跟着动。

今天开盘你们先盯大饼还是先盯山寨？

#BTC #ETH #早盘 #行情前瞻"""

    symbol = item["symbol"]
    if "current" in d:
        price = _fmt_price(d["current"])
        pct = _fmt_pct(d["change24"])
        high = _fmt_price(d["high24"])
        low = _fmt_price(d["low24"])
        volume = f"{d['quoteVolume']/1e8:.2f}亿USDT"
    else:
        price = _fmt_price(d["lastPrice"])
        pct = _fmt_pct(d["priceChangePercent"])
        high = _fmt_price(d["highPrice"])
        low = _fmt_price(d["lowPrice"])
        volume = f"{d['quoteVolume']/1e8:.2f}亿USDT"

    return f"""${symbol} 这波盘面有点值得盯一下，不是单纯看涨跌，而是看资金有没有继续接。

现在价格在 {price}，24h涨跌 {pct}，日内高点 {high}，低点 {low}，成交额大概 {volume}。这个位置最怕的是情绪上头直接追，结果刚好追在短线压力附近。

我的看法很简单：如果能在高位附近继续放量站稳，说明资金还没走；如果冲高后量跟不上，就要小心回踩确认。尤其是{slot_label}这个时间段，很多人容易被一根线带节奏。

你觉得 ${symbol} 这里是在蓄势突破，还是短线诱多？

#{symbol} #行情分析 #币圈 #风险控制"""


def _clean_content(content: str) -> str:
    return (content or "").strip()


def make_square_post(slot: str, item: dict) -> str:
    data_text = render_data(item)
    slot_label = SLOT_LABEL.get(slot, slot)
    cm = item.get("content_mode", "")

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
7.结尾留一个让人想聊的点："你有没有扛过单？"、"你最大的一笔学费是多少？"、"你有没有类似的经历？"之类。
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
7.结尾留争议问题让人站队，比如"你是多军还是空军？"、"这个位置你会多还是空？"、"评论区说说你的方向？"。
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
7.结尾留一个互动问题，让人想回复，比如"今天开盘你们先盯大饼还是先盯山寨？"、"这个位置你觉得能不能站稳？"。
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
7.结尾用一句话互动："早盘这波你追了吗？"、"这个位置你还敢追吗？"之类。
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
7.结尾互动："下午ETH你是空仓看戏，还是短线搞一波？"之类。
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
7.结尾互动："这个币明天你还看好吗？"、"今天吃到这波了吗？"之类。
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
7.结尾一句话互动："早盘你们怎么看的？"、"这个位置你动手了吗？"之类。
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
7.结尾互动："这个币午盘你还敢追吗？"、"你是上午就进去了还是在等回调？"之类。
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
7.结尾互动："今晚ETH你盯着哪个位置？"、"这个位置你接还是等？"之类。
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
7.结尾互动："今天你吃到这波了吗？"、"明天你最关注哪个币？"、"夜盘你还在盯吗？"之类。
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
7.结尾互动："今天你们打到猎物了吗？"、"明天你最关注哪个币？"之类。
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
6.结尾留互动。
7.零emoji，纯文本。

主题：{item['title']}
数据：
{data_text}

直接输出正文。"""

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
        msg = f"⚠️ 币安广场 {SLOT_LABEL[slot]} 生成失败：${item['symbol']}\n{e}"
        tg_send(msg)
        return msg

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
