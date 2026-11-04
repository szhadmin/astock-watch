# -*- coding: utf-8 -*-
"""信号引擎：把行情数据翻译成「评分 + 操作价位 + 操作建议」。

设计原则：
1) 只做技术面与资金面的客观描述，不预测，只给「若…则…」的条件式指令；
2) 每个结论都能追溯到具体指标数值（notes 里逐条列出）；
3) 关键价位优先使用 config.json 里的人工设定（我给出的分析结论），
   没有设定时用「近 20/60 日结构 + 均线 + ATR」自动推导。
"""
from __future__ import annotations

from datetime import datetime, time as dtime

from . import feed, indicators as ind

# 量能、资金等阈值的默认值（可被 config 覆盖）
DEFAULTS = {
    "account": {"total_capital": 0, "risk_budget_pct": 2.0, "max_position_pct": 45.0},
    "alerts": {
        "cooldown_sec": 900,
        "pct_5min": 1.5,
        "pct_day": 5.0,
        "vol_ratio": 2.5,
        "amount_yi": 5.0,
    },
}


def _num(x, nd=2):
    return None if x is None else round(float(x), nd)


# --------------------------------------------------------------------------- #
# 关键价位
# --------------------------------------------------------------------------- #
def derive_levels(daily, quote, cfg_symbol):
    """推导支撑/阻力/止损/目标位。cfg_symbol 中人工设定优先。"""
    closes = [b["close"] for b in daily]
    highs = [b["high"] for b in daily]
    lows = [b["low"] for b in daily]
    price = quote.get("price") or closes[-1]

    ma5 = ind.sma(closes, 5)
    ma10 = ind.sma(closes, 10)
    ma20 = ind.sma(closes, 20)
    ma60 = ind.sma(closes, 60)
    atr14 = ind.atr(highs, lows, closes, 14)
    atr_now = atr14[-1] or max(price * 0.02, 0.01)

    # --- 自动推导：结构层（近 120 日的枢轴低点聚类 + 近 20 日区间） ---
    recent_n = min(120, len(daily))
    base = len(daily) - recent_n
    pl = [(i + base, v) for i, v in ind.pivot_lows(lows[-recent_n:], 2)]
    ph = [(i + base, v) for i, v in ind.pivot_highs(highs[-recent_n:], 2)]
    sup_clusters = [c for c in ind.cluster_levels(pl, tol=0.015, min_touch=2)
                    if c["price"] <= price * 1.02]
    res_clusters = [c for c in ind.cluster_levels(ph, tol=0.015, min_touch=2)
                    if c["price"] >= price * 0.99]

    auto_support = [c["price"] for c in sorted(sup_clusters, key=lambda c: -c["price"])[:3]]
    low20 = min(lows[-20:])
    high20 = max(highs[-20:])
    if not auto_support:
        auto_support = [low20]

    auto_resist = []
    for ma in (ma10[-1], ma20[-1], ma60[-1]):
        if ma and ma > price * 1.005:
            auto_resist.append(round(ma, 2))
    for c in sorted(res_clusters, key=lambda c: c["price"])[:2]:
        if c["price"] > price * 1.005:
            auto_resist.append(c["price"])
    auto_resist = sorted(set(round(x, 2) for x in auto_resist))[:3]

    support = cfg_symbol.get("support") or auto_support[:3]
    resist = cfg_symbol.get("resistance") or auto_resist
    stop = cfg_symbol.get("stop")
    if stop is None:
        stop = round(min(support[0] * 0.99, price - 1.2 * atr_now), 2) if support else \
            round(price - 2 * atr_now, 2)
    targets = cfg_symbol.get("targets") or resist or [round(price * 1.1, 2)]

    return {
        "support": [round(float(x), 2) for x in support],
        "resistance": [round(float(x), 2) for x in resist],
        "stop": round(float(stop), 2),
        "targets": [round(float(x), 2) for x in targets],
        "add_trigger": cfg_symbol.get("add_trigger"),
        "low20": round(low20, 2),
        "high20": round(high20, 2),
        "atr": round(atr_now, 3),
        "auto_support": auto_support,
        "auto_resistance": auto_resist,
        "ma": {"5": _num(ma5[-1]), "10": _num(ma10[-1]),
               "20": _num(ma20[-1]), "60": _num(ma60[-1])},
    }


# --------------------------------------------------------------------------- #
# 评分
# --------------------------------------------------------------------------- #
def score_all(daily, quote, flow, levels):
    """返回 (scores dict, notes list)。总分 100。"""
    closes = [b["close"] for b in daily]
    highs = [b["high"] for b in daily]
    lows = [b["low"] for b in daily]
    vols = [b["vol"] for b in daily]
    price = quote.get("price") or closes[-1]
    ma = levels["ma"]
    notes = []

    dif, dea, hist = ind.macd(closes)
    rsi6 = ind.rsi(closes, 6)[-1]
    rsi12 = ind.rsi(closes, 12)[-1]
    k, d, j = ind.kdj(highs, lows, closes)
    k, d, j = k[-1], d[-1], j[-1]

    # ---------------- 趋势（30） ----------------
    tr = 0
    for key, pts in (("5", 4), ("10", 6), ("20", 8), ("60", 6)):
        v = ma.get(key)
        if v and price > v:
            tr += pts
    if ma["5"] and ma["10"] and ma["20"] and ma["5"] > ma["10"] > ma["20"]:
        tr += 6
        notes.append(("趋势", "均线多头排列（MA5>MA10>MA20），趋势健康 +6"))
    elif ma["5"] and ma["10"] and ma["20"] and ma["5"] < ma["10"] < ma["20"]:
        notes.append(("趋势", "均线空头排列（MA5<MA10<MA20），趋势仍在下行"))
    above = sum(1 for v in ma.values() if v and price > v)
    if above == 0:
        notes.append(("趋势", "股价位于全部均线之下（MA5 %.2f / MA20 %.2f / MA60 %.2f）" %
                      (ma["5"] or 0, ma["20"] or 0, ma["60"] or 0)))
    tr = max(0, min(30, tr))

    # ---------------- 位置（20） ----------------
    pos = 0
    pct_pos, lo60, hi60 = ind.price_position(closes, 60)
    dd = (hi60 - price) / hi60 * 100 if hi60 else 0
    if dd >= 30:
        pos += 5
        notes.append(("位置", "距近 60 日高点回撤 %.1f%%，处于深度调整区 +5" % dd))
    elif dd >= 15:
        pos += 3
        notes.append(("位置", "距近 60 日高点回撤 %.1f%% +3" % dd))
    dev20 = (price / ma["20"] - 1) * 100 if ma["20"] else 0
    if dev20 <= -8:
        pos += 5
        notes.append(("位置", "价格低于 MA20 达 %.1f%%，超跌 +5" % abs(dev20)))
    elif dev20 <= -5:
        pos += 3
        notes.append(("位置", "价格低于 MA20 达 %.1f%% +3" % abs(dev20)))
    if pct_pos <= 20:
        pos += 5
        notes.append(("位置", "处于近 60 日价格区间的 %.0f%% 分位（接近区间下沿）+5" % pct_pos))
    elif pct_pos <= 40:
        pos += 3
        notes.append(("位置", "近 60 日区间分位 %.0f%% +3" % pct_pos))
    # 距支撑带用 ATR 衡量（低价股用百分比会失真）
    below = [s for s in levels["support"] if s <= price * 1.001]
    atr = levels["atr"] or price * 0.02
    if below:
        sup_near = max(below)
        dist_atr = (price - sup_near) / atr
        if dist_atr <= 0.5:
            pos += 5
            notes.append(("位置", "现价距支撑 %.2f 仅 %.2f 个 ATR，处于可低吸区域 +5"
                          % (sup_near, dist_atr)))
        elif dist_atr <= 1.0:
            pos += 3
            notes.append(("位置", "现价距支撑 %.2f 约 %.2f 个 ATR，接近可低吸区域 +3"
                          % (sup_near, dist_atr)))
        else:
            notes.append(("位置", "现价距最近支撑 %.2f 还有 %.1f 个 ATR，未到低吸位"
                          % (sup_near, dist_atr)))
        if price < sup_near * 0.995:
            notes.append(("位置", "⚠ 现价已跌穿最近支撑 %.2f" % sup_near))
    pos = max(0, min(20, pos))

    # ---------------- 动能（20） ----------------
    mo = 0
    if hist[-1] is not None and hist[-1] > 0:
        mo += 6
        notes.append(("动能", "MACD 柱在零轴上方 +6"))
    elif len(hist) > 1 and hist[-1] is not None and hist[-2] is not None:
        if hist[-1] > hist[-2]:
            mo += 3
            notes.append(("动能", "MACD 绿柱缩短（%.3f→%.3f），下跌动能衰减 +3" % (hist[-2], hist[-1])))
        else:
            notes.append(("动能", "MACD 绿柱仍在放大（%.3f→%.3f），下跌动能未止" % (hist[-2], hist[-1])))
    if dif[-1] > dea[-1]:
        mo += 4
        notes.append(("动能", "DIF 上穿 DEA，MACD 金叉中 +4"))
    else:
        notes.append(("动能", "MACD 未金叉（DIF %.3f < DEA %.3f）" % (dif[-1], dea[-1])))
    if rsi6 is not None:
        if rsi6 < 20:
            mo += 4
            notes.append(("动能", "RSI6=%.1f 极度超卖，短线反弹概率提升 +4" % rsi6))
        elif rsi6 < 45:
            mo += 2
        elif rsi6 <= 70:
            mo += 3
            notes.append(("动能", "RSI6=%.1f 处于健康区间 +3" % rsi6))
    if k is not None and j is not None:
        if j < 0:
            mo += 3
            notes.append(("动能", "KDJ 的 J=%.1f 超卖 +3" % j))
        elif k > d:
            mo += 2
    mo = max(0, min(20, mo))

    # ---------------- 量能（15） ----------------
    vo = 0
    avg5 = sum(vols[-6:-1]) / 5 if len(vols) >= 6 else sum(vols) / len(vols)
    vr = vols[-1] / avg5 if avg5 else 1
    yang = closes[-1] >= (daily[-1]["open"])
    if vr >= 1.5 and yang:
        vo += 8
        notes.append(("量能", "放量阳线（量能为 5 日均量的 %.2f 倍）+8" % vr))
    elif vr >= 1.5 and not yang:
        notes.append(("量能", "放量阴线（%.2f 倍），有出货嫌疑" % vr))
    elif vr <= 0.8 and yang:
        vo += 5
        notes.append(("量能", "缩量收阳（%.2f 倍），抛压衰竭但买盘也不强 +5" % vr))
    elif vr <= 0.8:
        vo += 4
        notes.append(("量能", "缩量下跌（%.2f 倍），恐慌盘有限 +4" % vr))
    else:
        vo += 4
    vratio = quote.get("vol_ratio")
    if vratio:
        if vratio >= 2:
            vo += 4
            notes.append(("量能", "量比 %.2f，明显活跃 +4" % vratio))
        elif vratio >= 1:
            vo += 2
    to = quote.get("turnover") or daily[-1]["turnover"]
    if 1 <= to <= 5:
        vo += 3
        notes.append(("量能", "换手率 %.2f%%，流动性适中 +3" % to))
    elif to < 1:
        vo += 1
    vo = max(0, min(15, vo))

    # ---------------- 资金（15） ----------------
    ca = 0
    if flow:
        today_flow = flow[-1]["main"]
        d3 = sum(f["main"] for f in flow[-3:])
        d5 = sum(f["main"] for f in flow[-5:])
        d10 = sum(f["main"] for f in flow[-10:])
        if today_flow > 0:
            ca += 6
            notes.append(("资金", "主力今日净流入 %.0f 万 +6" % (today_flow / 1e4)))
        else:
            prev = flow[-2]["main"] if len(flow) > 1 else 0
            if prev < 0 and today_flow > prev:
                ca += 3
                notes.append(("资金", "主力今日净流出 %.0f 万，但流出幅度收窄（前一日 %.0f 万）+3"
                              % (abs(today_flow) / 1e4, abs(prev) / 1e4)))
            else:
                notes.append(("资金", "主力今日净流出 %.0f 万" % (abs(today_flow) / 1e4)))
        if d3 > 0:
            ca += 5
            notes.append(("资金", "近 3 日主力累计净流入 %.0f 万 +5" % (d3 / 1e4)))
        else:
            notes.append(("资金", "近 3 日主力累计净流出 %.0f 万" % (abs(d3) / 1e4)))
        if d5 > d10 / 2 and d5 < 0:
            ca += 2
            notes.append(("资金", "近 5 日流出速度慢于 10 日均速，资金面边际改善 +2"))
        elif d5 > 0:
            ca += 4
    else:
        today_flow = d3 = d5 = d10 = 0
        notes.append(("资金", "资金流数据不可用"))
    ca = max(0, min(15, ca))

    scores = {"trend": tr, "position": pos, "momentum": mo, "volume": vo, "capital": ca,
              "total": tr + pos + mo + vo + ca}
    extra = {
        "rsi6": rsi6, "rsi12": rsi12, "kdj": [k, d, j],
        "macd": {"dif": _num(dif[-1], 3), "dea": _num(dea[-1], 3),
                 "hist": _num(hist[-1], 3),
                 "prev_hist": _num(hist[-2], 3) if len(hist) > 1 else None,
                 "cross_up": dif[-1] > dea[-1]},
        "vol_ratio5": round(vr, 2), "pct_pos60": pct_pos,
        "drawdown60": round(dd, 1), "dev_ma20": round(dev20, 1),
        "flow": {"today": today_flow, "d3": d3, "d5": d5, "d10": d10},
        "shape": ind.candle_shape(daily[-1]),
    }
    return scores, notes, extra


def side_scores(scores):
    """拆成两套视角：

    右侧分（顺势做多）= 趋势 + 动能，衡量「趋势是否已转强、能否加仓」
    左侧分（抄底价值）= 位置 + 量能 + 资金，衡量「跌得够不够深、支撑是否有效」
    """
    right = (scores["trend"] + scores["momentum"]) / 45 * 100
    left = (scores["position"] / 20 * 45 + scores["volume"] / 15 * 30
            + scores["capital"] / 15 * 25)
    return round(right, 0), round(left, 0)


ACTION_LABEL = {
    "BUY": "加仓 / 建仓",
    "BUY_CAUTION": "可建仓，但位置偏高，控制仓位",
    "HOLD_ADD": "持有，可小幅加仓",
    "HOLD": "持有观察，不加不减",
    "HOLD_PROBE": "左侧支撑区：仅保留试探仓，禁止加仓，严格止损",
    "TRIM": "反弹减仓",
    "EXIT": "离场观望",
}


def verdict_of(scores, stop_hit=False):
    """双维度决策矩阵：右侧决定「能不能加」，左侧决定「值不值得守」。"""
    right, left = side_scores(scores)
    total = scores["total"]

    if stop_hit:
        return ({"level": "破位", "action": "EXIT",
                 "text": "现价已跌破止损线，周期低点假设失效 → 按纪律离场",
                 "right": right, "left": left})
    if right >= 60:
        if left >= 50:
            text = "右侧信号成立（趋势 %d / 位置 %d）：技术面转强，可加仓" % (right, left)
            action = "BUY"
        else:
            text = "趋势转强但位置已不便宜（右侧 %d / 左侧 %d）：可参与，别追高" % (right, left)
            action = "BUY_CAUTION"
        level = "强势"
    elif right >= 40:
        if left >= 55:
            text = "趋势回暖且位置仍低（右侧 %d / 左侧 %d）：持有并可小幅加仓" % (right, left)
            action = "HOLD_ADD"
        else:
            text = "趋势中性（右侧 %d / 左侧 %d）：持有观察" % (right, left)
            action = "HOLD"
        level = "偏多"
    elif right >= 20:
        if left >= 50:
            text = ("左侧支撑区、右侧未确认（右侧 %d / 左侧 %d）："
                    "可保留试探仓，禁止加仓，等放量站上 MA10 再谈" % (right, left))
            action = "HOLD_PROBE"
            level = "中性偏弱"
        else:
            text = "趋势偏弱且位置不占优（右侧 %d / 左侧 %d）：反弹减仓为宜" % (right, left)
            action = "TRIM"
            level = "偏弱"
    else:
        if left >= 50:
            text = ("深度超跌但趋势极弱（右侧 %d / 左侧 %d）：只留试探仓 + 严格止损，"
                    "不可补仓摊薄" % (right, left))
            action = "HOLD_PROBE"
            level = "弱势筑底"
        else:
            text = "趋势与位置双弱（右侧 %d / 左侧 %d）：离场观望" % (right, left)
            action = "EXIT"
            level = "弱势"
    return {"level": level, "text": "综合 %d｜%s" % (total, text),
            "action": action, "right": right, "left": left}


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def build_report(code, cfg=None, *args, **kwargs):
    """带磁盘缓存的 build_report：同一天同一只票只算一次（结果落 SQLite）。"""
    from datetime import datetime as _dt
    try:
        from . import diskcache
    except Exception:
        return _build_report_raw(code, cfg, *args, **kwargs)
    key = "rep:%s:%s" % (code, _dt.now().strftime("%Y-%m-%d"))
    hit = diskcache.get(key, ttl=600)
    if hit is not None:
        return hit
    out = _build_report_raw(code, cfg, *args, **kwargs)
    try:
        diskcache.put(key, out)
    except Exception:
        pass
    return out


def _build_report_raw(code, cfg):
    code = str(code).strip()
    sym_cfg = (cfg.get("symbols") or {}).get(code, {}) or {}
    pos_cfg = (cfg.get("positions") or {}).get(code)
    acct = {**DEFAULTS["account"], **(cfg.get("account") or {})}

    quote = feed.get_quote(code)
    daily = feed.get_kline(code, 101, limit=300, adjust=1)
    try:
        flow = feed.get_fundflow(code, limit=15)
    except Exception:
        flow = []
    try:
        weekly = feed.get_kline(code, 102, limit=120, adjust=1)
    except Exception:
        weekly = []

    levels = derive_levels(daily, quote, sym_cfg)
    scores, notes, extra = score_all(daily, quote, flow, levels)
    price = quote.get("price") or daily[-1]["close"]
    verdict = verdict_of(scores, stop_hit=price <= levels["stop"])
    try:
        indices = feed.get_indices()
    except Exception:
        indices = []

    # 右侧确认清单
    shape = extra["shape"]
    checklist = [
        {"name": "放量阳线", "ok": extra["vol_ratio5"] >= 1.5 and shape["is_yang"],
         "detail": "量能 %.2f 倍 / 5 日均量，%s" % (extra["vol_ratio5"], "收阳" if shape["is_yang"] else "收阴")},
        {"name": "收复 MA10", "ok": bool(levels["ma"]["10"] and price > levels["ma"]["10"]),
         "detail": "现价 %.2f vs MA10 %.2f" % (price, levels["ma"]["10"] or 0)},
        {"name": "MACD 金叉", "ok": bool(extra["macd"]["cross_up"]),
         "detail": "DIF %.3f / DEA %.3f" % (extra["macd"]["dif"], extra["macd"]["dea"])},
        {"name": "主力资金转正", "ok": bool(extra["flow"]["d3"] > 0),
         "detail": "近 3 日 %.0f 万" % (extra["flow"]["d3"] / 1e4)},
    ]
    ok_count = sum(1 for c in checklist if c["ok"])

    # 持仓评估
    position = None
    if pos_cfg and pos_cfg.get("shares"):
        shares = int(pos_cfg["shares"])
        cost = float(pos_cfg["cost"])
        pnl = (price - cost) * shares
        pnl_pct = (price / cost - 1) * 100
        risk_amt = (price - levels["stop"]) * shares
        total_cap = float(acct.get("total_capital") or 0)
        pos_pct = (price * shares / total_cap * 100) if total_cap else None
        max_shares = None
        if total_cap and levels["stop"] < price:
            budget = total_cap * float(acct.get("risk_budget_pct", 2)) / 100
            by_risk = budget / (price - levels["stop"])
            by_cap = total_cap * float(acct.get("max_position_pct", 45)) / 100 / price
            max_shares = int(min(by_risk, by_cap) // 100 * 100)
        position = {
            "shares": shares, "cost": cost, "market_value": round(price * shares, 2),
            "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2),
            "risk_to_stop": round(risk_amt, 2),
            "risk_pct_of_capital": round(risk_amt / total_cap * 100, 2) if total_cap else None,
            "position_pct": round(pos_pct, 1) if pos_pct is not None else None,
            "max_shares_by_risk": max_shares,
            "stop_hit": price <= levels["stop"],
        }

    # 本周/本日形态补充
    bar = daily[-1]
    today = datetime.now().date().isoformat()
    bar_closed = bar["date"] != today or datetime.now().time() > dtime(15, 0)

    return {
        "code": code,
        "name": quote.get("name"),
        "price": price,
        "pct": quote.get("pct"),
        "open": quote.get("open"), "high": quote.get("high"), "low": quote.get("low"),
        "prev_close": quote.get("prev_close"),
        "volume": quote.get("volume"), "amount": quote.get("amount"),
        "turnover": quote.get("turnover"), "vol_ratio": quote.get("vol_ratio"),
        "pe": quote.get("pe"), "pb": quote.get("pb"),
        "mktcap_yi": round((quote.get("mktcap") or 0) / 1e8, 1),
        "bar_date": bar["date"], "bar_closed": bar_closed,
        "bar": bar,
        "scores": scores, "notes": notes, "extra": extra,
        "levels": levels,
        "verdict": verdict,
        "checklist": checklist, "checklist_ok": ok_count,
        "position": position,
        "weekly_trend": _weekly_trend(weekly),
        "indices": indices,
        "note": sym_cfg.get("note"),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _weekly_trend(weekly):
    if len(weekly) < 12:
        return None
    closes = [b["close"] for b in weekly]
    ma5 = ind.sma(closes, 5)[-1]
    ma10 = ind.sma(closes, 10)[-1]
    last = closes[-1]
    if ma5 and ma10:
        if last > ma5 > ma10:
            state = "周线多头"
        elif last < ma5 < ma10:
            state = "周线空头"
        else:
            state = "周线震荡"
        return {"state": state, "ma5": round(ma5, 2), "ma10": round(ma10, 2),
                "close": round(last, 2)}
    return None
