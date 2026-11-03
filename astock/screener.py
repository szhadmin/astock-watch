# -*- coding: utf-8 -*-
"""选股器：先用确定性规则把 5000 只票缩到 ~60 只「有看头」的候选，
再交给 AI 去读懂它们的形态并挑选。

这样做的原因：把全市场 K 线塞给模型既贵又不可靠；
但让模型在「已经过滤掉垃圾股」的小池子里做选择，才是它的强项。
"""
from __future__ import annotations

from . import feed, indicators as ind

DEFAULT_SCREEN = {
    "universe_size": 300,       # 按成交额取前 N 只作为视野
    "min_amount_yi": 2.0,       # 成交额下限（亿）
    "min_price": 2.0,
    "max_price": 40.0,          # 小资金买得起
    "min_mktcap_yi": 20.0,
    "max_mktcap_yi": 1500.0,
    "min_turnover": 0.8,
    "max_turnover": 20.0,
    "max_abs_pct": 8.5,         # 剔除接近涨跌停
    "max_scan": 60,             # 逐只下载 K 线的上限（控制耗时）
    "top": 20,                  # 最终交给 AI 的候选数
}


def _category(feat):
    """给候选打形态标签，便于 AI 快速理解「为什么它值得看」。"""
    tags = []
    if feat["breakout"]:
        tags.append("突破新高")
    if feat["pullback"]:
        tags.append("回踩均线")
    if feat["oversold"]:
        tags.append("超跌反弹")
    if feat["volume_surge"]:
        tags.append("放量异动")
    if feat["uptrend"]:
        tags.append("多头排列")
    return "/".join(tags) if tags else "盘整"


def features(code, name, quote, bars):
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    vols = [b["vol"] for b in bars]
    close = closes[-1]
    ma5, ma10, ma20, ma60 = (ind.sma(closes, n)[-1] for n in (5, 10, 20, 60))
    atr14 = ind.atr(highs, lows, closes, 14)[-1] or close * 0.02
    avg5 = sum(vols[-6:-1]) / 5 if len(vols) >= 6 else sum(vols) / len(vols)
    vr = vols[-1] / avg5 if avg5 else 1.0
    rsi6 = ind.rsi(closes, 6)[-1]
    dif, dea, hist = ind.macd(closes)
    pctpos, lo60, hi60 = ind.price_position(closes, 60)
    dev20 = (close / ma20 - 1) * 100 if ma20 else 0
    high20 = max(highs[-21:-1]) if len(highs) > 21 else max(highs[:-1])
    low60 = min(lows[-60:]) if len(lows) >= 60 else min(lows)

    feat = {
        "code": code, "name": name, "price": round(close, 2),
        "pct": quote.get("pct"),
        "amount_yi": round((quote.get("amount") or 0) / 1e8, 2),
        "turnover": quote.get("turnover"),
        "mktcap_yi": round((quote.get("mktcap") or 0) / 1e8, 1),
        "ma5": round(ma5, 2) if ma5 else None,
        "ma10": round(ma10, 2) if ma10 else None,
        "ma20": round(ma20, 2) if ma20 else None,
        "ma60": round(ma60, 2) if ma60 else None,
        "dev_ma20": round(dev20, 1),
        "pct_pos60": pctpos,
        "rsi6": round(rsi6, 1) if rsi6 is not None else None,
        "macd_hist": round(hist[-1], 3) if hist[-1] is not None else None,
        "macd_up": bool(dif[-1] > dea[-1]) if dif[-1] is not None else None,
        "vol_ratio5": round(vr, 2),
        "atr": round(atr14, 3),
        "atr_pct": round(atr14 / close * 100, 2),
        "support20": round(min(lows[-20:]), 2) if len(lows) >= 20 else None,
        "low60": round(low60, 2),
        "high20_prev": round(high20, 2),
        "chg5": round((close / closes[-6] - 1) * 100, 1) if len(closes) > 6 else None,
    }
    feat["breakout"] = close > high20 and vr >= 1.3
    feat["pullback"] = (ma20 is not None and abs(dev20) <= 3.5 and ma20 > (ma60 or 0)
                        and 30 <= pctpos <= 75)
    feat["oversold"] = pctpos <= 15 and (rsi6 or 50) < 32
    feat["volume_surge"] = vr >= 2.0 and (quote.get("pct") or 0) > 0
    feat["uptrend"] = bool(ma5 and ma10 and ma20 and ma5 > ma10 > ma20)
    feat["category"] = _category(feat)
    # 「新鲜度」打分：越靠前越值得 AI 细看
    s = 0
    if feat["breakout"]:
        s += 30
    if feat["pullback"]:
        s += 25
    if feat["volume_surge"]:
        s += 20
    if feat["oversold"]:
        s += 18
    if feat["uptrend"]:
        s += 12
    if 2 <= (feat["amount_yi"] or 0) <= 30:
        s += 8
    if (feat["atr_pct"] or 0) > 8:
        s -= 10                     # 波动太剧烈，不适合 1.5 万小账户
    s += max(0, 10 - abs(dev20))     # 越贴近 MA20 越好
    feat["screen_score"] = round(s, 1)
    return feat


def screen(cfg_screen=None, verbose=True):
    """返回候选列表（已按 screen_score 降序，含形态标签与关键价位）。"""
    sc = {**DEFAULT_SCREEN, **(cfg_screen or {})}
    uni = feed.get_universe(size=int(sc["universe_size"]))
    if verbose:
        print("  全市场快照 %d 只 → 规则初筛…" % len(uni))

    pre = []
    for q in uni:
        name = q.get("name") or ""
        if any(t in name for t in ("ST", "*ST", "退", "N ", "C ")):
            continue
        px = q.get("price")
        if not px or not (sc["min_price"] <= px <= sc["max_price"]):
            continue
        if (q.get("amount") or 0) / 1e8 < sc["min_amount_yi"]:
            continue
        to = q.get("turnover") or 0
        if not (sc["min_turnover"] <= to <= sc["max_turnover"]):
            continue
        cap = (q.get("mktcap") or 0) / 1e8
        if not (sc["min_mktcap_yi"] <= cap <= sc["max_mktcap_yi"]):
            continue
        if abs(q.get("pct") or 0) >= sc["max_abs_pct"]:
            continue
        pre.append(q)

    if verbose:
        print("  初筛通过 %d 只 → 逐只读 K 线（最多 %d 只）…" % (len(pre), sc["max_scan"]))

    feats = []
    for q in pre[:int(sc["max_scan"])]:
        try:
            bars = feed.get_kline(q["code"], 101, limit=90, adjust=1)
        except Exception:
            continue
        if len(bars) < 70:
            continue
        f = features(q["code"], q["name"], q, bars)
        if f["category"] == "盘整" and f["screen_score"] < 15:
            continue
        feats.append(f)

    feats.sort(key=lambda x: -x["screen_score"])
    return feats[:int(sc["top"])]


def brief_for_ai(feats):
    """压成给模型看的紧凑结构（省 token 又保留关键信息）。"""
    out = []
    for f in feats:
        out.append({
            "code": f["code"], "name": f["name"], "price": f["price"],
            "pct": f["pct"], "成交额亿": f["amount_yi"], "换手%": f["turnover"],
            "市值亿": f["mktcap_yi"],
            "均线": "MA5 %.2f / MA10 %.2f / MA20 %.2f / MA60 %.2f"
                    % (f["ma5"] or 0, f["ma10"] or 0, f["ma20"] or 0, f["ma60"] or 0),
            "距MA20%": f["dev_ma20"], "60日分位%": f["pct_pos60"],
            "RSI6": f["rsi6"], "MACD柱": f["macd_hist"], "MACD多头": f["macd_up"],
            "量能倍数": f["vol_ratio5"], "ATR%": f["atr_pct"],
            "近20日支撑": f["support20"], "前20日高点": f["high20_prev"],
            "5日涨幅%": f["chg5"], "形态": f["category"],
        })
    return out
