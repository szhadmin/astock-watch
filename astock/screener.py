# -*- coding: utf-8 -*-
"""选股器：先用确定性规则把 5000 只票缩到 ~60 只「有看头」的候选，
再交给 AI 去读懂它们的形态并挑选。

这样做的原因：把全市场 K 线塞给模型既贵又不可靠；
但让模型在「已经过滤掉垃圾股」的小池子里做选择，才是它的强项。
"""
from __future__ import annotations

import json
import os

from . import feed, indicators as ind, market

DEFAULT_SCREEN = {
    "universe_size": 300,       # 按成交额取前 N 只作为视野
    "min_amount_yi": 2.0,       # 成交额下限（亿）
    "min_price": 2.0,
    "max_price": 40.0,          # 固定上限（max_price_auto=True 时按资金动态算）
    "max_price_auto": True,     # 价格上限按资金规模反推（避免系统性排除高价强势股）
    "max_price_cap": 300.0,     # 动态算时的绝对上限
    "single_cap_pct": 40.0,     # 单股仓位上限（用于反推得起的股价）
    "lot": 100,                 # 最小交易单位
    "min_mktcap_yi": 20.0,
    "max_mktcap_yi": 1500.0,
    "min_turnover": 0.8,
    "max_turnover": 20.0,
    "max_abs_pct": 9.5,         # 剔除接近涨停（9.8%+）
    "max_scan": 60,             # 逐只下载 K 线的上限（控制耗时）
    "top": 20,                  # 最终交给 AI 的候选数

    # —— 三通道开关 ——
    "enabled_channels": ["dip", "breakout", "rotation"],
    "dip_min_pctpos": 30,       # 低吸通道：60 日位置区间
    "dip_max_pctpos": 75,
    "breakout_pct_min": 3.0,    # 突破通道：当日涨幅区间
    "breakout_pct_max": 9.5,
    "breakout_vol_ratio": 1.2,  # 突破通道：量能倍数下限
    "breakout_pos_min": 70,     # 突破通道：60 日位置下限（越强越好）
    "breakout_max_scan": 25,    # 突破通道最多读多少只 K 线
    "rotation_boards": 5,       # 板块轮动：取强度榜前 N 个板块
    "rotation_per_board": 3,    # 每个板块取几只
    "rotation_max_scan": 15,    # 轮动通道最多给多少只补 K 线特征（其余只给行情）
    "min_per_channel": {"breakout": 5, "rotation": 5},   # 各通道保底名额（防被低吸通道挤掉）
    # 各通道的仓位系数（喂给 sizing / 风控）
    "pos_factor": {"dip": 1.0, "breakout": 0.5, "rotation": 0.7},
}


def _capital(ai_cfg=None):
    """当前 AI 账户本金（用于反推买得起的股价）。"""
    import json as _json
    for p in (os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "ai", "paper_state.json"),):
        try:
            with open(p, encoding="utf-8-sig") as f:
                d = _json.load(f)
            return float(d.get("capital") or 0)
        except Exception:
            continue
    return 0.0


def price_cap(sc, ai_cfg=None):
    """价格上限：按 资金×单股上限% ÷ 最小手数 反推，取 min(绝对上限)。"""
    if not sc.get("max_price_auto"):
        return float(sc.get("max_price") or 40)
    cap = _capital(ai_cfg)
    lot = int(sc.get("lot") or 100)
    if cap <= 0:
        return float(sc.get("max_price") or 40)
    allow = cap * float(sc.get("single_cap_pct") or 40) / 100 / lot
    return float(min(sc.get("max_price_cap") or 300, max(sc.get("min_price") or 2, allow)))


def _board_top(n=5):
    """板块强度榜前 N（东财板块列表按涨幅降序），返回 [{code,name,pct}]。"""
    import json as _json
    import urllib.request
    url = ("https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=%d&po=1&np=1&fltt=2&invt=2"
           "&fid=f3&fs=m:90+t:2&fields=f12,f14,f3" % max(1, int(n)))
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
        "Referer": "https://quote.eastmoney.com/",
    })
    with urllib.request.urlopen(req, timeout=12) as r:
        d = _json.loads(r.read().decode("utf-8", "replace"))
    out = []
    for x in ((d.get("data") or {}).get("diff") or []):
        out.append({"code": str(x.get("f12") or ""), "name": x.get("f14") or "",
                    "pct": x.get("f3")})
    return out[:n]


def _board_members(board_code, limit=30):
    """某板块的成分股（按成交额降序）。"""
    import json as _json
    import urllib.request
    url = ("https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=%d&po=1&np=1&fltt=2&invt=2"
           "&fid=f6&fs=b:%s&fields=f12,f14,f2,f3,f6,f8,f20" % (limit, board_code))
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
        "Referer": "https://quote.eastmoney.com/",
    })
    with urllib.request.urlopen(req, timeout=12) as r:
        d = _json.loads(r.read().decode("utf-8", "replace"))
    out = []
    for x in ((d.get("data") or {}).get("diff") or []):
        code = str(x.get("f12") or "")
        if not (code.isdigit() and len(code) == 6):
            continue
        out.append({"code": code, "name": x.get("f14") or "", "price": x.get("f2"),
                    "pct": x.get("f3"), "amount": (x.get("f6") or 0),
                    "turnover": x.get("f8"), "mktcap": (x.get("f20") or 0) * 1})
    return out


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


def _quote_feat(q, channel, sector=None, sector_pct=None, pos_factor=1.0):
    """把行情 dict 压成候选条目（板块轮动通道用，不读 K 线，省时间/token）。"""
    return {
        "code": q.get("code"), "name": q.get("name"), "price": round(float(q.get("price") or 0), 2),
        "pct": q.get("pct"), "amount_yi": round((q.get("amount") or 0) / 1e8, 2),
        "turnover": q.get("turnover"), "mktcap_yi": round((q.get("mktcap") or 0) / 1e8, 1),
        "ma5": None, "ma10": None, "ma20": None, "ma60": None, "dev_ma20": None,
        "pct_pos60": None, "rsi6": None, "macd_hist": None, "macd_up": None,
        "vol_ratio5": None, "atr": None, "atr_pct": None, "support20": None,
        "low60": None, "high20_prev": None, "chg5": None,
        "category": "板块强势", "channel": channel,
        "pos_factor": pos_factor, "screen_score": 12.0,
        "sector": sector, "sector_pct": sector_pct,
    }


def screen(cfg_screen=None, verbose=True):
    """三通道候选：低吸(dip) + 突破(breakout) + 板块轮动(rotation)。

    - dip      ：原逻辑，位置 30~75 分位、涨幅适中（低吸）
    - breakout ：当日 +3%~+9.5%、量比≥1.5、60 日位置≥80（追趋势，仓位减半）
    - rotation ：强势板块前 5 的成分股（保证热点不整块缺席）
    """
    sc = {**DEFAULT_SCREEN, **(cfg_screen or {})}
    channels = [c for c in (sc.get("enabled_channels") or ["dip"]) if c]
    pcap = price_cap(sc)
    uni = feed.get_universe(size=int(sc["universe_size"]))
    if verbose:
        print("  全市场快照 %d 只 → 规则初筛…" % len(uni))

    pre = []
    for q in uni:
        name = q.get("name") or ""
        if any(t in name for t in ("ST", "*ST", "退", "N ", "C ")):
            continue
        px = q.get("price")
        if not px or not (sc["min_price"] <= px <= pcap):
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

    _bp_min = float(sc["breakout_pct_min"])
    _bp_pos = float(sc["breakout_pos_min"])
    for f in feats:
        _pct = f.get("pct") or 0
        _pos = f.get("pct_pos60") or 0
        if _pct >= _bp_min and _pos >= _bp_pos:      # 形态上属于"强势突破"
            f["channel"] = "breakout"
            f["pos_factor"] = float((sc.get("pos_factor") or {}).get("breakout", 0.5))
            f["category"] = (f.get("category") or "") + "｜强势突破"
        else:
            f["channel"] = "dip"
            f["pos_factor"] = float((sc.get("pos_factor") or {}).get("dip", 1.0))
    feats = feats[:int(sc["top"])]

    # ---------- 突破通道：追强势但有纪律 ----------
    if "breakout" in channels:
        try:
            bk = []
            bmin, bmax = float(sc["breakout_pct_min"]), float(sc["breakout_pct_max"])
            for q in uni:
                name = q.get("name") or ""
                if any(t in name for t in ("ST", "*ST", "退", "N ", "C ")):
                    continue
                px, pct = q.get("price"), q.get("pct") or 0
                if not px or not (sc["min_price"] <= px <= pcap):
                    continue
                if not (bmin <= pct <= bmax):
                    continue
                if (q.get("amount") or 0) / 1e8 < sc["min_amount_yi"]:
                    continue
                bk.append(q)
                if len(bk) >= int(sc["breakout_max_scan"]):
                    break
            got = 0
            for q in bk:
                try:
                    bars = feed.get_kline(q["code"], 101, limit=90, adjust=1)
                except Exception:
                    continue
                if len(bars) < 70:
                    continue
                f = features(q["code"], q["name"], q, bars)
                strong = (f.get("pct_pos60") or 0) >= float(sc["breakout_pos_min"]) or f.get("breakout")
                vr_ok = (f.get("vol_ratio5") or 0) >= float(sc["breakout_vol_ratio"])
                if not (strong and vr_ok):
                    continue
                f["channel"] = "breakout"
                f["pos_factor"] = float((sc.get("pos_factor") or {}).get("breakout", 0.5))
                f["screen_score"] = round((f.get("screen_score") or 0) + 15, 1)
                f["category"] = (f.get("category") or "") + "｜强势突破"
                feats.append(f)
                got += 1
            if verbose:
                print("  突破通道：%d 只入选（当日 +%.1f~%.1f%%，量比≥%.1f）"
                      % (got, bmin, bmax, float(sc["breakout_vol_ratio"])))
        except Exception as exc:                       # noqa: BLE001
            if verbose:
                print("  （突破通道跳过：%s）" % exc)

    # ---------- 板块轮动通道：强势板块成分股 ----------
    if "rotation" in channels:
        try:
            nboards = int(sc["rotation_boards"])
            per = int(sc["rotation_per_board"])
            boards = _board_top(nboards)
            got = 0
            _budget = [int(sc.get("rotation_max_scan") or 15)]
            for b in boards:
                if not b.get("code"):
                    continue
                try:
                    members = _board_members(b["code"], 30)
                except Exception:
                    continue
                picked = 0
                for m in members:
                    px, pct = m.get("price"), m.get("pct") or 0
                    if not px or not (sc["min_price"] <= px <= pcap):
                        continue
                    if abs(pct) >= float(sc["max_abs_pct"]):
                        continue
                    if (m.get("amount") or 0) / 1e8 < sc["min_amount_yi"]:
                        continue
                    _pf = float((sc.get("pos_factor") or {}).get("rotation", 0.7))
                    if _budget[0] > 0:              # 优先补 K 线：AI 需要均线/止损位才能下单
                        _bars = None
                        try:
                            _bars = feed.get_kline(m["code"], 101, limit=90, adjust=1)
                        except Exception:
                            _bars = None
                        if _bars and len(_bars) >= 70:
                            _budget[0] -= 1
                            f = features(m["code"], m["name"], m, _bars)
                            f["channel"] = "rotation"
                            f["pos_factor"] = _pf
                            f["category"] = (f.get("category") or "") + "｜板块强势(%s)" % b["name"]
                            f["sector"] = b["name"]
                            f["sector_pct"] = b.get("pct")
                            f["screen_score"] = round((f.get("screen_score") or 0) + 8, 1)
                            feats.append(f)
                            got += 1
                            picked += 1
                            if picked >= per:
                                break
                            continue
                    feats.append(_quote_feat(m, "rotation", sector=b["name"],
                                             sector_pct=b.get("pct"), pos_factor=_pf))
                    got += 1
                    picked += 1
                    if picked >= per:
                        break
            if verbose:
                print("  板块轮动通道：%d 只入选（强势板块前 %d 名各取 %d 只）"
                      % (got, nboards, per))
        except Exception as exc:                       # noqa: BLE001
            if verbose:
                print("  （板块轮动通道跳过：%s）" % exc)

    # 去重（同一只票保留分数最高的那条）
    seen = {}
    for f in feats:
        c = f.get("code")
        if c not in seen or (f.get("screen_score") or 0) > (seen[c].get("screen_score") or 0):
            seen[c] = f
    feats = sorted(seen.values(), key=lambda x: -(x.get("screen_score") or 0))
    _top = int(sc["top"])
    _quota = sc.get("min_per_channel") or {}
    final = []
    for _ch, _need in _quota.items():               # 先按通道保底
        picked = [f for f in feats if f.get("channel") == _ch][:int(_need)]
        final.extend(picked)
    for f in feats:                                 # 再按分数填满
        if len(final) >= _top:
            break
        if f not in final:
            final.append(f)
    feats = sorted(final[:_top], key=lambda x: -(x.get("screen_score") or 0))
    # 板块强度过滤：弱势板块的票直接不看（可在 config.ai.sector_filter 调整）
    try:
        feats = market.annotate_and_filter(feats, (cfg_screen or {}).get("_ai_cfg"), verbose=verbose)
    except Exception as exc:                          # noqa: BLE001
        if verbose:
            print("  （板块强度过滤跳过：%s）" % exc)
    return feats


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
            "入选通道": f.get("channel") or "dip",
            "建议仓位系数": f.get("pos_factor", 1.0),
            "所属板块": f.get("sector"), "板块涨幅%": f.get("sector_pct"),
        })
    return out
