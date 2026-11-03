# -*- coding: utf-8 -*-
"""策略内核（纯函数，无 IO）——回测与实盘 agent 共用同一套决策逻辑。

设计要点：
1) 只使用「当前 bar 及之前」的数据，绝不偷看未来（枢轴点只用已确认的）；
2) 输出不是「买/卖」这种模糊信号，而是**目标仓位百分比**（0~100），
   由执行层换算成股数，天然支持分批、减仓、空仓；
3) 每一条加减分都能追溯到具体指标，便于复盘「当时为什么这么判断」。

评分六维度（满分 100）：趋势 25 / 动能 20 / 量能 15 / 资金 15 / 位置 15 / 结构 10
"""
from __future__ import annotations

from . import indicators as ind

DEFAULT_PARAMS = {
    "ma_fast": 5,
    "ma_mid": 10,
    "ma_slow": 20,
    "ma_long": 60,

    # 评分 → 目标仓位（占总资金百分比）分档
    "bands": [(75, 100), (65, 80), (55, 50), (45, 25), (0, 0)],

    # 风控
    "stop_atr": 2.5,          # 初始止损 = max(入场价 - N×ATR, 支撑×0.99)
    "trail_atr": 2.5,         # 吊灯止损：最高价 - N×ATR
    "tp1_atr": 3.0,           # 第一目标 = 入场价 + N×ATR（卖一半）
    "tp1_ratio": 0.5,
    "max_chase_pct": 6.0,     # 已涨超过 N%（相对 MA20）不再追高
    "block_limit_pct": 9.0,   # 当日涨跌幅超过该值不交易（避开涨跌停/异动）
    "min_vol_ratio": 0.8,     # 放量条件（量/5日均量）
    "min_hold_days": 2,       # 最短持有天数（T+1 之上再加一天，抑制日内进出）
    "stop_cooldown": 1,       # 止损后 N 个交易日内不再开新仓

    # 开关
    "use_capital": True,      # 资金流维度（有数据才计分）
    "require_uptrend": True,  # 强制：收盘在 MA20 下方时目标仓位不超过 25%
}


def _cluster(vals, tol=0.015):
    """把价位聚成带，返回按价格降序的 [(price, touches)]。"""
    if not vals:
        return []
    groups = []
    for v in sorted(vals):
        for g in groups:
            if abs(v - g["p"]) / max(g["p"], 1e-9) <= tol:
                g["items"].append(v)
                g["p"] = sum(g["items"]) / len(g["items"])
                break
        else:
            groups.append({"p": v, "items": [v]})
    return sorted([(round(g["p"], 3), len(g["items"])) for g in groups],
                  key=lambda x: -x[0])


def confirmed_supports(lows, i, lookback=90, tol=0.015, min_touch=2):
    """只用「已确认」的枢轴低点（j <= i-2）计算支撑带。"""
    lo = max(0, i - lookback)
    pts = []
    for j in range(lo + 2, i - 1):            # 右侧两根已出现才算确认
        seg = lows[j - 2:j + 3]
        if len(seg) == 5 and lows[j] <= min(seg):
            pts.append(lows[j])
    clusters = _cluster(pts, tol)
    return [p for p, n in clusters if n >= min_touch]


def evaluate(bars, i, flows=None, params=None):
    """对第 i 根 bar（含）做出决策。

    bars : [{date,open,close,high,low,vol,...}] 升序
    flows: {date: 主力净额(元)} 或 None
    """
    p = {**DEFAULT_PARAMS, **(params or {})}
    if i < max(p["ma_long"], 30):
        return {"score": 0, "target_pct": 0, "signal": "WARMUP", "reasons": ["数据不足"],
                "levels": {}, "buckets": {}}

    window = bars[:i + 1]
    closes = [b["close"] for b in window]
    highs = [b["high"] for b in window]
    lows = [b["low"] for b in window]
    vols = [b["vol"] for b in window]
    close = closes[-1]
    bar = window[-1]

    ma = {k: ind.sma(closes, p["ma_%s" % k])[-1]
          for k in ("fast", "mid", "slow", "long")}
    ma_fast_series = ind.sma(closes, p["ma_fast"])
    ma_slow_series = ind.sma(closes, p["ma_slow"])
    dif, dea, hist = ind.macd(closes)
    rsi6 = ind.rsi(closes, 6)[-1]
    rsi12 = ind.rsi(closes, 12)[-1]
    k, d, j = ind.kdj(highs, lows, closes)
    atr14 = ind.atr(highs, lows, closes, 14)[-1] or close * 0.02
    avg5 = sum(vols[-6:-1]) / 5 if len(vols) >= 6 else sum(vols) / len(vols)
    vr = vols[-1] / avg5 if avg5 else 1.0

    sup = confirmed_supports(lows, i)
    support = [s for s in sup if s <= close * 1.02][:3] or \
              ([round(min(lows[-20:]), 2)] if len(lows) >= 20 else [])
    res_candidates = [ma["mid"], ma["slow"], ma["long"],
                      round(max(highs[-20:]), 2)]
    resistance = sorted({round(x, 2) for x in res_candidates if x and x > close * 1.005})[:3]

    reasons = []
    buckets = {}

    # ---------------- 趋势 25 ----------------
    t = 0
    if ma["slow"] and close > ma["slow"]:
        t += 8
        reasons.append("收盘站上 MA%d" % p["ma_slow"])
    if ma["fast"] and ma["mid"] and ma["fast"] > ma["mid"]:
        t += 7
        reasons.append("MA%d 上穿 MA%d" % (p["ma_fast"], p["ma_mid"]))
    if ma["long"] and close > ma["long"]:
        t += 5
        reasons.append("站上 MA%d（中期趋势线）" % p["ma_long"])
    slope = ind.slope_pct([x for x in ma_slow_series[-6:]], 5)
    if slope > 0.3:
        t += 5
        reasons.append("MA%d 斜率 +%.1f%%，趋势向上" % (p["ma_slow"], slope))
    elif slope < -0.3:
        reasons.append("MA%d 斜率 %.1f%%，趋势向下" % (p["ma_slow"], slope))
    buckets["trend"] = min(25, t)

    # ---------------- 动能 20 ----------------
    m = 0
    if hist[-1] is not None and hist[-1] > 0:
        m += 8
        reasons.append("MACD 红柱（%.3f）" % hist[-1])
        if len(hist) > 1 and hist[-1] > hist[-2]:
            m += 4
            reasons.append("红柱放大，动能加速")
    elif len(hist) > 1 and hist[-1] is not None and hist[-2] is not None and hist[-1] > hist[-2]:
        m += 4
        reasons.append("MACD 绿柱缩短，跌势放缓")
    if dif[-1] > dea[-1]:
        m += 4
        reasons.append("DIF 在 DEA 上方")
    if rsi6 is not None:
        if 45 <= rsi6 <= 75:
            m += 4
            reasons.append("RSI6=%.0f 强势区" % rsi6)
        elif rsi6 < 25:
            m += 2
            reasons.append("RSI6=%.0f 超卖（左侧机会）" % rsi6)
        elif rsi6 > 85:
            reasons.append("RSI6=%.0f 过热，追高风险" % rsi6)
    buckets["momentum"] = min(20, m)

    # ---------------- 量能 15 ----------------
    v = 0
    yang = close >= bar["open"]
    if vr >= 1.5 and yang:
        v += 10
        reasons.append("放量阳线（%.1f 倍量）" % vr)
    elif vr >= 1.2 and yang:
        v += 6
        reasons.append("温和放量收阳（%.1f 倍）" % vr)
    elif vr < 0.8 and not yang:
        v += 4
        reasons.append("缩量下跌，抛压有限")
    elif vr < 0.8 and yang:
        v += 3
        reasons.append("缩量收阳，买盘不足")
    elif not yang and vr >= 1.5:
        reasons.append("放量阴线，警惕出货")
    if 1 <= (bar.get("turnover") or 0) <= 8:
        v += 5
    buckets["volume"] = min(15, v)

    # ---------------- 资金 15 ----------------
    c = 0
    if flows and p["use_capital"]:
        dates = [b["date"] for b in window[-10:]]
        vals = [flows.get(dd) for dd in dates]
        vals = [x for x in vals if x is not None]
        if vals:
            d1 = vals[-1]
            d3 = sum(vals[-3:])
            if d1 > 0:
                c += 8
                reasons.append("主力净流入 %.0f 万" % (d1 / 1e4))
            elif len(vals) > 1 and d1 > vals[-2]:
                c += 3
                reasons.append("主力流出收窄（%.0f 万）" % (d1 / 1e4))
            if d3 > 0:
                c += 7
                reasons.append("3 日主力累计净流入 %.0f 万" % (d3 / 1e4))
            else:
                reasons.append("3 日主力净流出 %.0f 万" % (abs(d3) / 1e4))
    else:
        c = 7                                     # 无资金数据时给中性分
        reasons.append("无资金流数据，按中性计分")
    buckets["capital"] = min(15, c)

    # ---------------- 位置 15 ----------------
    s = 0
    seg = closes[-60:] if len(closes) > 60 else closes
    lo60, hi60 = min(seg), max(seg)
    pctpos = (close - lo60) / (hi60 - lo60) * 100 if hi60 > lo60 else 50
    if pctpos <= 30:
        s += 8
        reasons.append("处于 60 日区间下沿（分位 %.0f%%）" % pctpos)
    elif pctpos >= 85:
        s += 2
        reasons.append("接近 60 日区间上沿（分位 %.0f%%），性价比下降" % pctpos)
    else:
        s += 4
    dev = (close / ma["slow"] - 1) * 100 if ma["slow"] else 0
    if dev > p["max_chase_pct"]:
        reasons.append("偏离 MA%d 达 %.1f%%，禁止追高" % (p["ma_slow"], dev))
    elif -3 <= dev <= 3:
        s += 7
        reasons.append("贴合 MA%d（偏离 %.1f%%），性价比好" % (p["ma_slow"], dev))
    elif dev < -8:
        s += 5
        reasons.append("低于 MA%d 达 %.1f%%，超跌区" % (p["ma_slow"], abs(dev)))
    buckets["position"] = min(15, s)

    # ---------------- 结构 10 ----------------
    st = 0
    if support:
        near = min(support, key=lambda x: abs(close - x))
        dist_atr = (close - near) / atr14
        if 0 <= dist_atr <= 0.6:
            st += 10
            reasons.append("紧贴支撑 %.2f（%.1f ATR）" % (near, dist_atr))
        elif 0 <= dist_atr <= 1.5:
            st += 6
        elif close < near:
            reasons.append("已跌破支撑 %.2f" % near)
    if resistance:
        gap = (resistance[0] / close - 1) * 100
        if gap < 1.5:
            st = max(0, st - 2)
            reasons.append("上方 %.2f 处有阻力（距离 %.1f%%）" % (resistance[0], gap))
    buckets["structure"] = min(10, st)

    score = sum(buckets.values())

    # ---------------- 评分 → 目标仓位 ----------------
    target = 0
    for thr, pct in p["bands"]:
        if score >= thr:
            target = pct
            break
    # 风控收紧：下跌趋势中不允许重仓
    if p["require_uptrend"] and ma["slow"] and close < ma["slow"]:
        target = min(target, 25)
        if target > 0:
            reasons.append("收盘在 MA%d 下方，目标仓位压至 ≤25%%" % p["ma_slow"])
    # 涨跌停/异动不交易
    if abs(bar.get("pct") or 0) >= p["block_limit_pct"]:
        target = 0
        reasons.append("当日振幅过大（%.1f%%），暂停交易" % (bar.get("pct") or 0))
    # 过热不追
    if dev > p["max_chase_pct"]:
        target = min(target, 25)

    # 信号命名
    if target <= 0:
        sig = "AVOID"
    elif score >= 65:
        sig = "BUY"
    elif score >= 55:
        sig = "ADD"
    elif score >= 45:
        sig = "WATCH"
    else:
        sig = "AVOID"

    # 止损/目标位（供执行层与报告使用）
    if support:
        stop = round(min(support) * 0.99, 2)
    else:
        stop = round(close - p["stop_atr"] * atr14, 2)
    levels = {
        "close": round(close, 2),
        "support": [round(x, 2) for x in support],
        "resistance": resistance,
        "stop": stop,
        "atr": round(atr14, 3),
        "target1": round(close + p["tp1_atr"] * atr14, 2),
        "trail": round(close - p["trail_atr"] * atr14, 2),
        "ma": {"fast": ma["fast"], "mid": ma["mid"],
                "slow": ma["slow"], "long": ma["long"]},
        "pct_pos": round(pctpos, 0),
        "vol_ratio5": round(vr, 2),
        "rsi6": rsi6, "rsi12": rsi12,
        "macd": {"dif": dif[-1], "dea": dea[-1], "hist": hist[-1]},
    }
    return {"score": score, "buckets": buckets, "target_pct": target,
            "signal": sig, "reasons": reasons, "levels": levels,
            "date": bar["date"], "price": close}
