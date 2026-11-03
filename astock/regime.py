# -*- coding: utf-8 -*-
"""市场状态细分：把"震荡市"拆开——83% 的时间不可能全是垃圾时间。

为什么不用板块涨幅：历史板块行情拿不到（东财只给当下），
所以这里用**指数自身的结构特征**做代理，全部可从日线算出，历史/实时通用：

    idx20   20 日涨跌幅(%)            → 方向
    amp20   20 日振幅(%)              → 波动幅度
    vol20   20 日收益标准差(%)        → 波动稳定性
    ma_dev  指数相对 MA20 偏离(%)     → 位置
    slope   MA5 相对 MA20 偏离(%)     → 斜率

状态：牛市(>+5%) / 熊市(<-5%) / 震荡(|idx20|<=5%)
震荡再细分：
    窄幅缩量   amp20<6  且 vol20<1.0   → 真正没有机会，最该收手
    宽幅剧震   amp20>=10 或 vol20>=2.0 → 有波段可做，允许小仓试
    常规震荡   其余                     → 要求高评分
"""
from __future__ import annotations

import math
from datetime import datetime

from . import feed


def _stats(closes, highs, lows, i, n=20):
    if i < n:
        return None
    seg_c = closes[i - n + 1:i + 1]
    seg_h = highs[i - n + 1:i + 1]
    seg_l = lows[i - n + 1:i + 1]
    hi, lo = max(seg_h), min(seg_l)
    amp = (hi - lo) / (sum(seg_c) / len(seg_c)) * 100 if seg_c else 0
    rets = [(seg_c[k] / seg_c[k - 1] - 1) * 100 for k in range(1, len(seg_c))]
    mean = sum(rets) / len(rets) if rets else 0
    vol = math.sqrt(sum((x - mean) ** 2 for x in rets) / len(rets)) if rets else 0
    ma20 = sum(seg_c) / len(seg_c)
    ma5 = sum(closes[i - 4:i + 1]) / 5
    return {
        "amp20": round(amp, 2),
        "vol20": round(vol, 2),
        "ma_dev": round((closes[i] / ma20 - 1) * 100, 2) if ma20 else 0,
        "slope": round((ma5 / ma20 - 1) * 100, 2) if ma20 else 0,
    }


def classify(i, closes, highs, lows):
    """返回 {state, sub, idx20, amp20, vol20, ma_dev, slope}"""
    if i < 21:
        return {"state": "未知", "sub": "未知", "idx20": None}
    idx20 = round((closes[i] / closes[i - 20] - 1) * 100, 2)
    st = _stats(closes, highs, lows, i) or {"amp20": 0, "vol20": 0,
                                            "ma_dev": 0, "slope": 0}
    if idx20 > 5:
        state, sub = "牛市", "牛市"
    elif idx20 < -5:
        state, sub = "熊市", "熊市"
    else:
        state = "震荡"
        if st["amp20"] < 6 and st["vol20"] < 1.0:
            sub = "窄幅缩量"
        elif st["amp20"] >= 10 or st["vol20"] >= 2.0:
            sub = "宽幅剧震"
        else:
            sub = "常规震荡"
    return {"state": state, "sub": sub, "idx20": idx20, **st}


def hist_map(index_bars=None, limit=900):
    """历史查表：{日期: 分类结果}，供回放时按日期取（一次算完，几乎零成本）。"""
    if index_bars is None:
        try:
            index_bars = feed.get_index_kline("1.000001", limit)
        except Exception:
            return {}
    closes = [b["close"] for b in index_bars]
    highs = [b["high"] for b in index_bars]
    lows = [b["low"] for b in index_bars]
    out = {}
    for i, b in enumerate(index_bars):
        c = classify(i, closes, highs, lows)
        c["date"] = b["date"]
        out[b["date"]] = c
    return out


def now(verbose=False):
    """实时市场状态细分（用最近 60 根日线）。"""
    try:
        bars = feed.get_index_kline("1.000001", 60)
    except Exception as exc:                          # noqa: BLE001
        return {"state": "未知", "sub": "未知", "error": str(exc)}
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    c = classify(len(bars) - 1, closes, highs, lows)
    c["updated"] = datetime.now().strftime("%H:%M:%S")
    if verbose:
        print("市场状态：%s / %s｜指数20日 %+.2f%%｜振幅 %.2f%%｜波动 %.2f"
              % (c["state"], c["sub"], c["idx20"] or 0, c.get("amp20") or 0,
                 c.get("vol20") or 0))
    return c


def policy(regime=None):
    """把市场状态细分翻译成硬约束（供 gate 使用）。"""
    r = regime or now()
    sub = r.get("sub") or "未知"
    table = {
        "牛市": {"max_position_pct": 40.0, "min_score": 55, "note": "牛市：超额最高，可正常做"},
        "熊市": {"max_position_pct": 22.0, "min_score": 70,
                 "note": "熊市：只做抗跌龙头，小仓、高分"},
        "窄幅缩量": {"max_position_pct": 18.0, "min_score": 75,
                     "note": "震荡·窄幅缩量：没有机会，最该收手"},
        "宽幅剧震": {"max_position_pct": 30.0, "min_score": 60,
                     "note": "震荡·宽幅剧震：有波段可做，小仓试错"},
        "常规震荡": {"max_position_pct": 30.0, "min_score": 62,
                     "note": "震荡·常规：回放验证这里做多有效（超额 +0.5pp），可正常参与但要求评分≥62"},
    }
    out = dict(table.get(sub, {"max_position_pct": 25.0, "min_score": 65,
                               "note": "市场状态未知：保守"}))
    out.update({"state": r.get("state"), "sub": sub,
                "idx20": r.get("idx20"), "amp20": r.get("amp20"),
                "vol20": r.get("vol20")})
    return out


if __name__ == "__main__":
    import json
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(json.dumps(now(verbose=True), ensure_ascii=False, indent=1))
    print(json.dumps(policy(), ensure_ascii=False, indent=1))
    hm = hist_map(limit=400)
    from collections import Counter
    print("近 400 日状态分布：", dict(Counter(v["sub"] for v in hm.values())))
