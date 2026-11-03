# -*- coding: utf-8 -*-
"""技术指标计算（纯 Python 实现，不依赖 numpy/pandas）。

所有函数返回与输入等长的列表，数据不足处为 None，便于按下标与 K 线对齐。
"""
from __future__ import annotations


def sma(values, n):
    out, s = [], 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        out.append(round(s / n, 4) if i >= n - 1 else None)
    return out


def ema(values, n):
    out, k = [], 2.0 / (n + 1)
    prev = None
    for v in values:
        prev = v if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def macd(closes, fast=12, slow=26, signal=9):
    """返回 (dif, dea, hist)。hist = 2*(dif-dea)，与国内软件口径一致。"""
    ef, es = ema(closes, fast), ema(closes, slow)
    dif = [a - b for a, b in zip(ef, es)]
    dea = ema(dif, signal)
    hist = [2 * (a - b) for a, b in zip(dif, dea)]
    return ([round(x, 4) for x in dif],
            [round(x, 4) for x in dea],
            [round(x, 4) for x in hist])


def rsi(closes, n=6):
    """Wilder 平滑 RSI。"""
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        ch = closes[i] - closes[i - 1]
        gains += max(ch, 0.0)
        losses += max(-ch, 0.0)
    ag, al = gains / n, losses / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(ch, 0.0)) / n
        al = (al * (n - 1) + max(-ch, 0.0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return [None if v is None else round(v, 2) for v in out]


def kdj(highs, lows, closes, n=9, k_period=3, d_period=3):
    """返回 (K, D, J)。"""
    k_list, d_list, j_list = [None] * len(closes), [None] * len(closes), [None] * len(closes)
    k = d = 50.0
    for i in range(len(closes)):
        if i < n - 1:
            continue
        hh = max(highs[i - n + 1:i + 1])
        ll = min(lows[i - n + 1:i + 1])
        rsv = 50.0 if hh == ll else (closes[i] - ll) / (hh - ll) * 100
        k = (k_period - 1) / k_period * k + 1.0 / k_period * rsv
        d = (d_period - 1) / d_period * d + 1.0 / d_period * k
        k_list[i], d_list[i], j_list[i] = round(k, 2), round(d, 2), round(3 * k - 2 * d, 2)
    return k_list, d_list, j_list


def boll(closes, n=20, k=2.0):
    mid = sma(closes, n)
    up, low = [None] * len(closes), [None] * len(closes)
    for i in range(n - 1, len(closes)):
        window = closes[i - n + 1:i + 1]
        m = mid[i]
        sd = (sum((x - m) ** 2 for x in window) / n) ** 0.5
        up[i], low[i] = round(m + k * sd, 4), round(m - k * sd, 4)
    return up, mid, low


def atr(highs, lows, closes, n=14):
    """平均真实波幅。"""
    trs = [0.0]
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    out = [None] * len(closes)
    if len(trs) <= n:
        return out
    val = sum(trs[1:n + 1]) / n
    out[n] = round(val, 4)
    for i in range(n + 1, len(trs)):
        val = (val * (n - 1) + trs[i]) / n
        out[i] = round(val, 4)
    return out


# --------------------------------------------------------------------------- #
# 结构 / 形态
# --------------------------------------------------------------------------- #
def pivot_lows(lows, w=2):
    """局部低点（左右各 w 根都不低于它）。返回 [(index, value), ...]。"""
    out = []
    for i in range(w, len(lows) - w):
        seg = lows[i - w:i + w + 1]
        if lows[i] <= min(seg):
            out.append((i, lows[i]))
    return out


def pivot_highs(highs, w=2):
    out = []
    for i in range(w, len(highs) - w):
        seg = highs[i - w:i + w + 1]
        if highs[i] >= max(seg):
            out.append((i, highs[i]))
    return out


def cluster_levels(points, tol=0.015, min_touch=2):
    """把靠近的价位聚成“支撑/阻力带”。

    points: [(index, price)]
    返回 [{'price': 中枢价, 'touches': 次数, 'first': 首次出现下标, 'last': 最近下标}]，按价格降序。
    """
    if not points:
        return []
    pts = sorted(points, key=lambda x: x[1])
    groups = []
    for idx, price in pts:
        placed = False
        for g in groups:
            if abs(price - g["price"]) / max(g["price"], 1e-9) <= tol:
                g["_items"].append((idx, price))
                g["price"] = sum(p for _, p in g["_items"]) / len(g["_items"])
                placed = True
                break
        if not placed:
            groups.append({"_items": [(idx, price)], "price": price})
    out = []
    for g in groups:
        items = g["_items"]
        if len(items) < min_touch:
            continue
        out.append({
            "price": round(g["price"], 3),
            "touches": len(items),
            "first": min(i for i, _ in items),
            "last": max(i for i, _ in items),
        })
    return sorted(out, key=lambda x: -x["price"])


def price_position(closes, lookback=60):
    """当前收盘价在近 lookback 日区间中的分位（0=最低，100=最高）。"""
    seg = closes[-lookback:] if len(closes) > lookback else closes
    lo, hi = min(seg), max(seg)
    if hi == lo:
        return 50.0, lo, hi
    return round((closes[-1] - lo) / (hi - lo) * 100, 1), lo, hi


def slope_pct(values, n=5):
    """最近 n 根的线性斜率（相对均价的百分比），用于判断均线走向。"""
    seg = [v for v in values[-n:] if v is not None]
    if len(seg) < 2:
        return 0.0
    avg = sum(seg) / len(seg)
    return round((seg[-1] - seg[0]) / avg * 100, 2) if avg else 0.0


def candle_shape(bar):
    """单根 K 线形态描述。"""
    o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
    rng = max(h - l, 1e-9)
    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - l
    return {
        "body": round(body, 3),
        "upper": round(upper, 3),
        "lower": round(lower, 3),
        "body_pct": round(body / rng * 100, 1),
        "lower_pct": round(lower / rng * 100, 1),
        "upper_pct": round(upper / rng * 100, 1),
        "is_yang": c >= o,
        "is_doji": body / rng < 0.15,
        "is_hammer": lower / rng >= 0.5 and body / rng <= 0.35,
        "is_shooting": upper / rng >= 0.5 and body / rng <= 0.35,
        "range_pct": round(rng / max(o, 1e-9) * 100, 2),
    }
