# -*- coding: utf-8 -*-
"""大盘温度 与 板块强度 —— 用来在弱势市场自动收紧买入阈值。

大盘温度（0~100）由四个客观维度合成：
    ① 四大指数涨跌幅（权重最高）
    ② 全市场涨跌家数比（广度）
    ③ 涨停 / 跌停家数（情绪极值）
    ④ 上证指数相对 MA5 / MA20 的位置（趋势）
板块强度：行业板块涨幅排行 + 主力净流入排行，并可查个股所属板块及其强度。
"""
from __future__ import annotations

import json
from datetime import datetime

from . import feed

LEVELS = [(25, "冰点"), (40, "偏冷"), (60, "中性"), (75, "偏热"), (101, "过热")]


def _level(temp):
    for thr, name in LEVELS:
        if temp < thr:
            return name
    return "过热"


def market_temp(verbose=False):
    """返回 {temp, level, detail, indices, breadth, limit, trend}"""
    detail = {}
    score = 50.0

    # ① 指数涨跌
    try:
        idx = feed.get_indices()
    except Exception:
        idx = []
    pcts = [i["pct"] for i in idx if i.get("pct") is not None]
    idx_pct = sum(pcts) / len(pcts) if pcts else 0.0
    score += max(-15, min(15, idx_pct * 6))          # 平均涨 1% → +6
    detail["指数平均涨跌%"] = round(idx_pct, 2)

    # ② 涨跌家数（广度）
    up = down = limit_up = limit_down = 0
    try:
        uni = feed.get_universe(size=400)
        for q in uni:
            p = q.get("pct") or 0
            if p > 0:
                up += 1
            elif p < 0:
                down += 1
            if p >= 9.5:
                limit_up += 1
            elif p <= -9.5:
                limit_down += 1
    except Exception:
        pass
    total = max(1, up + down)
    breadth = up / total * 100
    score += max(-15, min(15, (breadth - 50) * 0.3))  # 70% 上涨 → +6
    detail["上涨家数占比%"] = round(breadth, 1)
    detail["涨/跌家数"] = "%d/%d" % (up, down)

    # ③ 涨停家数
    score += max(-8, min(8, (limit_up - limit_down) * 0.5))
    detail["涨停/跌停"] = "%d/%d" % (limit_up, limit_down)

    # ④ 上证趋势（相对 MA5 / MA20）
    trend_txt = "-"
    try:
        bars = feed.get_kline("000001", 101, limit=40, adjust=1) if False else None
    except Exception:
        bars = None
    try:
        from . import indicators as ind
        # 上证指数用 secid 1.000001，这里走指数专用接口拿日线
        kb = feed.get_index_kline("1.000001", 60)
        closes = [b["close"] for b in kb]
        ma5 = sum(closes[-5:]) / 5
        ma20 = sum(closes[-20:]) / 20
        c = closes[-1]
        if c > ma5 > ma20:
            score += 10
            trend_txt = "多头（站上MA5>MA20）"
        elif c > ma20:
            score += 4
            trend_txt = "震荡偏强（站上MA20）"
        elif c < ma5 < ma20:
            score -= 10
            trend_txt = "空头（跌破MA5<MA20）"
        else:
            score -= 4
            trend_txt = "震荡偏弱（跌破MA20）"
        detail["上证MA5/MA20"] = "%.1f / %.1f" % (ma5, ma20)
    except Exception:
        pass
    detail["上证趋势"] = trend_txt

    temp = int(max(0, min(100, round(score))))
    out = {"temp": temp, "level": _level(temp), "detail": detail,
           "indices": [{"name": i["name"], "pct": i["pct"]} for i in idx],
           "updated": datetime.now().strftime("%H:%M:%S")}
    if verbose:
        print("大盘温度 %d（%s）｜%s" % (temp, out["level"], detail))
    return out


def sector_board(top=10):
    """行业板块排行（涨幅 / 主力净流入），返回前 top 名。"""
    try:
        rows = feed.get_sector_board()
    except Exception:
        return []
    rows.sort(key=lambda x: -(x.get("pct") or 0))
    return rows[:top]


def stock_sectors(code, top=4):
    """个股所属板块及板块当日强度。"""
    try:
        rows = feed.get_stock_sectors(code)
    except Exception:
        return []
    rows.sort(key=lambda x: -(x.get("pct") or 0))
    return rows[:top]


def gate(temp_info=None):
    """把温度翻译成"收紧多少"的硬约束（供 ai_agent 直接执行）。"""
    t = (temp_info or market_temp())
    temp = t["temp"]
    if temp < 30:
        return {"mode": "freeze", "allow_new": False, "max_position_pct": 20.0,
                "note": "大盘温度 %d（%s）：禁止开新仓，只管理现有持仓" % (temp, t["level"])}
    if temp < 45:
        return {"mode": "tight", "allow_new": True, "max_position_pct": 25.0,
                "note": "大盘温度 %d（%s）：收紧开仓，单股上限降至 25%%" % (temp, t["level"])}
    if temp < 60:
        return {"mode": "normal", "allow_new": True, "max_position_pct": 35.0,
                "note": "大盘温度 %d（%s）：常规风控" % (temp, t["level"])}
    return {"mode": "loose", "allow_new": True, "max_position_pct": 40.0,
            "note": "大盘温度 %d（%s）：情绪偏热，按原上限执行但更要注意追高风险"
                    % (temp, t["level"])}


if __name__ == "__main__":
    print(json.dumps(market_temp(verbose=True), ensure_ascii=False, indent=1))
    print("gate:", json.dumps(gate(), ensure_ascii=False))
    print("板块:", json.dumps(sector_board(6), ensure_ascii=False))
