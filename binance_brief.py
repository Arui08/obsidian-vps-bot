"""币安广场专用内容流：BTC/ETH 走势 + 币安涨幅榜 + 热门盘面

说明：
- VPS 所在 IP 访问 api.binance.com / fapi.binance.com 会 451；
- 使用 Binance Vision 公开数据域 data-api.binance.vision 的现货行情，稳定免鉴权。
- TG 只发成功/失败通知，不推正文。
"""
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

DATA_BASE = "https://data-api.binance.vision"

STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USTC", "USD1", "USDE"}
MAJORS = {"BTC", "ETH"}

SLOT_LABEL = {
    "morning": "早盘",
    "noon": "午盘涨幅榜",
    "evening": "晚间盘面",
    "night": "夜盘观察",
}


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
        if not _pushed_recent(r["base"], days=3):
            return r
    return rows[0] if rows else None


def pick_hot_symbol() -> dict:
    rows = [r for r in spot_24h() if r["quoteVolume"] > 100_000_000]
    rows.sort(key=lambda r: (math.log10(r["quoteVolume"] + 1), abs(r["priceChangePercent"])), reverse=True)
    for r in rows:
        if r["base"] not in MAJORS and not _pushed_recent(r["base"], days=2):
            return r
    return rows[0] if rows else None


# ---------------- 选题 ----------------

def build_item(slot: str) -> dict:
    if slot == "morning":
        snap = market_snapshot("BTCUSDT")
        return {"topic": "btc_open", "symbol": "BTC", "title": "BTC 早盘走势", "data": snap}

    if slot == "noon":
        g = pick_top_gainer()
        if g:
            return {"topic": "top_gainer", "symbol": g["base"], "title": f"{g['base']} 涨幅榜", "data": g}
        snap = market_snapshot("ETHUSDT")
        return {"topic": "eth_noon", "symbol": "ETH", "title": "ETH 午盘走势", "data": snap}

    if slot == "evening":
        snap = market_snapshot("ETHUSDT")
        return {"topic": "eth_evening", "symbol": "ETH", "title": "ETH 晚间走势", "data": snap}

    if slot == "night":
        h = pick_hot_symbol()
        if h:
            return {"topic": "hot_symbol", "symbol": h["base"], "title": f"{h['base']} 热门盘面", "data": h}
        snap = market_snapshot("BTCUSDT")
        return {"topic": "btc_night", "symbol": "BTC", "title": "BTC 夜盘观察", "data": snap}

    raise ValueError(f"未知 slot: {slot}")


def render_data(item: dict) -> str:
    d = item["data"]
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


def make_square_post(slot: str, item: dict) -> str:
    data_text = render_data(item)
    prompt = f"""你是币安广场上的中文行情观察博主，风格像老韭菜盘面复盘：说人话，有判断，但不喊单。

请根据下面币安现货盘面数据，写一条适合币安广场的中文短帖。

硬规则：
1. 只输出正文，不要解释写作过程。
2. 260-480字，段落之间空一行。
3. 不要emoji，不要markdown标题，不要加粗。
4. 开头第一句必须有钩子，不要"今日为大家分析"这种播音腔。
5. 必须出现 ${item['symbol']}，结尾放3-5个相关标签。
6. 不能写"必涨、稳赚、梭哈、无脑多、无脑空"，不能给杠杆建议。
7. 必须结合具体数据：24h涨跌、价格位置、成交额、高低点/支撑压力、追高风险。
8. 结尾留互动问题，比如"这个位置你会接，还是等回踩？"、"你觉得这是突破前洗盘，还是诱多？"。
9. 语气要像币圈老哥聊天：有盘感，有风险提醒，别像新闻稿。

时段：{SLOT_LABEL.get(slot, slot)}
主题：{item['title']}
数据：
{data_text}

直接输出正文。"""
    return ai_chat(prompt, model=MODEL_FAST, max_tokens=1200).strip()


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

    try:
        res = publish_text(content)
    except SquareError as e:
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
    print(run_slot(slot))


if __name__ == "__main__":
    main()
