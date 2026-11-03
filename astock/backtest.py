# -*- coding: utf-8 -*-
"""事件驱动回测：日线级、无未来函数、含 T+1 与真实费用。

撮合规则（保守）：
    · 第 i 根 K 线「收盘后」生成决策 → 第 i+1 根「开盘价」成交（天然满足 T+1）
    · 盘中止损/止盈用当日 low/high 判断，成交价取 (开盘价, 触发价) 中更不利者
    · 买入按 100 股整手，资金不足则按手数下调
    · 费用：佣金（默认万1.5，最低 5 元，双边）+ 印花税（卖出 0.05%）+ 过户费（双边 0.002%）
    · 滑点：默认 0.1%
"""
from __future__ import annotations

import math
from datetime import datetime

from .strategy import DEFAULT_PARAMS, evaluate

FEE = {"commission": 0.00015, "commission_min": 5.0,
       "stamp_sell": 0.0005, "transfer": 0.00002, "slippage": 0.001}


def _buy_fee(amount, fee):
    return max(amount * fee["commission"], fee["commission_min"]) + amount * fee["transfer"]


def _sell_fee(amount, fee):
    return (max(amount * fee["commission"], fee["commission_min"])
            + amount * fee["transfer"] + amount * fee["stamp_sell"])


def run(bars, flows=None, params=None, capital=15000.0, fee=None,
        start_index=None, max_position_pct=100.0):
    """返回回测结果 dict。"""
    p = {**DEFAULT_PARAMS, **(params or {})}
    f = {**FEE, **(fee or {})}
    n = len(bars)
    start = start_index if start_index is not None else max(p["ma_long"], 30) + 1
    if start >= n:
        raise ValueError("数据不足以回测（需要 > %d 根 K 线）" % start)

    cash = float(capital)
    shares = 0
    avg_cost = 0.0
    entry_i = None
    high_since = 0.0
    stop = 0.0
    target1 = 0.0
    tp1_done = False
    pending = None
    last_atr = 0.0
    last_stop_i = None
    trades, equity, decisions = [], [], []

    def equity_at(price):
        return cash + shares * price

    for i in range(start, n):
        bar = bars[i]
        o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]

        # ---------- 1. 执行上一根收盘生成的订单（按今开） ----------
        if pending:
            tgt_shares, note, p_atr, p_sup = pending
            pending = None
            if tgt_shares > shares:
                price = o * (1 + f["slippage"])
                want = tgt_shares - shares
                lots = int(want // 100)
                while lots > 0:
                    qty = lots * 100
                    amount = qty * price
                    if amount + _buy_fee(amount, f) <= cash:
                        break
                    lots -= 1
                if lots > 0:
                    qty = lots * 100
                    amount = qty * price
                    expense = amount + _buy_fee(amount, f)
                    cash -= expense
                    if shares == 0:
                        avg_cost = price
                        entry_i = i
                        high_since = h
                        atr_v = p_atr or price * 0.02
                        stop = price - p["stop_atr"] * atr_v
                        if p_sup:                       # 止损贴近支撑下方，但不高于成本
                            stop = max(stop, p_sup * 0.99)
                        stop = min(stop, price * 0.995)
                        target1 = price + p["tp1_atr"] * atr_v
                        tp1_done = False
                    else:
                        avg_cost = (avg_cost * shares + price * qty) / (shares + qty)
                    shares += qty
            elif tgt_shares < shares:
                price = o * (1 - f["slippage"])
                qty = min(shares, (shares - tgt_shares) // 100 * 100)
                if shares - qty < 100:            # 清仓余数
                    qty = shares
                if qty > 0:
                    amount = qty * price
                    cash += amount - _sell_fee(amount, f)
                    pnl = (price - avg_cost) * qty - _sell_fee(amount, f)
                    trades.append({"entry": bars[entry_i]["date"] if entry_i is not None else "",
                                   "exit": bar["date"], "entry_price": round(avg_cost, 3),
                                   "exit_price": round(price, 3), "shares": qty,
                                   "pnl": round(pnl, 2),
                                   "pct": round((price / avg_cost - 1) * 100, 2) if avg_cost else 0,
                                   "days": i - entry_i if entry_i is not None else 0,
                                   "reason": note})
                    shares -= qty
                    if shares == 0:
                        avg_cost = 0.0
                        entry_i = None

        # ---------- 2. 盘中止损 / 止盈（T+1：买入当日不能卖出） ----------
        if shares > 0 and entry_i is not None and i > entry_i:
            high_since = max(high_since, h)
            if l <= stop:
                price = min(o, stop) * (1 - f["slippage"])
                amount = shares * price
                cash += amount - _sell_fee(amount, f)
                pnl = (price - avg_cost) * shares - _sell_fee(amount, f)
                trades.append({"entry": bars[entry_i]["date"], "exit": bar["date"],
                               "entry_price": round(avg_cost, 3), "exit_price": round(price, 3),
                               "shares": shares, "pnl": round(pnl, 2),
                               "pct": round((price / avg_cost - 1) * 100, 2),
                               "days": i - entry_i, "reason": "止损"})
                shares = 0
                avg_cost = 0.0
                entry_i = None
                last_stop_i = i
            elif (not tp1_done) and target1 and h >= target1:
                qty = int(shares * p["tp1_ratio"] // 100 * 100)
                if 0 < qty < shares:
                    price = max(o, target1) * (1 - f["slippage"])
                    amount = qty * price
                    cash += amount - _sell_fee(amount, f)
                    pnl = (price - avg_cost) * qty - _sell_fee(amount, f)
                    trades.append({"entry": bars[entry_i]["date"], "exit": bar["date"],
                                   "entry_price": round(avg_cost, 3), "exit_price": round(price, 3),
                                   "shares": qty, "pnl": round(pnl, 2),
                                   "pct": round((price / avg_cost - 1) * 100, 2),
                                   "days": i - entry_i, "reason": "第一目标减半"})
                    shares -= qty
                    tp1_done = True
            # 吊灯止损上移
            if shares > 0:
                stop = max(stop, high_since - p["trail_atr"] * (last_atr or c * 0.02))

        # ---------- 3. 收盘决策 ----------
        d = evaluate(bars, i, flows, p)
        atr_v = d["levels"].get("atr")
        sup_v = (d["levels"].get("support") or [None])[0]
        last_atr = atr_v or last_atr
        target_pct = min(d["target_pct"], max_position_pct)
        eq = equity_at(c)
        target_shares = int((eq * target_pct / 100.0) // (c * 100)) * 100 if c > 0 else 0
        # 只有偏离足够大才调仓，抑制过度交易
        cur_pct = (shares * c / eq * 100) if eq else 0
        min_hold = int(p.get("min_hold_days", 2))
        can_reduce = (entry_i is None) or (i - entry_i >= min_hold)
        cooling = (last_stop_i is not None
                   and (i - last_stop_i) <= int(p.get("stop_cooldown", 1)))
        if target_pct >= cur_pct + 15 and not cooling:
            pending = (target_shares, "评分 %d → 目标仓位 %d%%" % (d["score"], target_pct),
                       atr_v, sup_v)
        elif target_pct <= cur_pct - 20 and can_reduce:
            pending = (target_shares, "评分 %d → 目标仓位降至 %d%%" % (d["score"], target_pct),
                       atr_v, sup_v)
        decisions.append({"date": bar["date"], "score": d["score"],
                          "target_pct": target_pct, "signal": d["signal"]})
        equity.append({"date": bar["date"], "equity": round(eq, 2), "price": c,
                       "position_pct": round(cur_pct, 1)})

    end_price = bars[-1]["close"]
    final_equity = equity[-1]["equity"] if equity else capital
    if shares:                                   # 期末按收盘价估值，不计入已平仓交易
        open_pos = {"shares": shares, "avg_cost": round(avg_cost, 3),
                    "value": round(shares * end_price, 2),
                    "pnl": round((end_price - avg_cost) * shares, 2)}
    else:
        open_pos = None

    # ---------- 绩效统计 ----------
    peak, mdd, mdd_date = -1e18, 0.0, ""
    for e in equity:
        peak = max(peak, e["equity"])
        dd = (peak - e["equity"]) / peak * 100 if peak > 0 else 0
        if dd > mdd:
            mdd, mdd_date = dd, e["date"]
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    days = len(equity)
    total_ret = (final_equity / capital - 1) * 100
    ann = ((final_equity / capital) ** (250.0 / days) - 1) * 100 if days > 0 else 0
    # 买入持有基准
    bh = (bars[-1]["close"] / bars[start]["close"] - 1) * 100
    bh_max_dd = 0.0
    bh_peak = -1e18
    for b in bars[start:]:
        bh_peak = max(bh_peak, b["close"])
        bh_max_dd = max(bh_max_dd, (bh_peak - b["close"]) / bh_peak * 100)

    return {
        "code": bars[-1].get("code", ""),
        "start": bars[start]["date"], "end": bars[-1]["date"], "days": days,
        "capital": capital, "final_equity": round(final_equity, 2),
        "total_return_pct": round(total_ret, 2),
        "annual_pct": round(ann, 2),
        "max_drawdown_pct": round(mdd, 2), "max_dd_date": mdd_date,
        "trades": trades, "trade_count": len(trades),
        "win_count": len(wins), "loss_count": len(losses),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
        "avg_win_pct": round(sum(t["pct"] for t in wins) / len(wins), 2) if wins else 0.0,
        "avg_loss_pct": round(sum(t["pct"] for t in losses) / len(losses), 2) if losses else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "avg_hold_days": round(sum(t["days"] for t in trades) / len(trades), 1) if trades else 0.0,
        "exposure_pct": round(sum(1 for e in equity if e["position_pct"] > 0) / days * 100, 1) if days else 0,
        "open_position": open_pos,
        "buy_hold_return_pct": round(bh, 2),
        "buy_hold_max_dd_pct": round(bh_max_dd, 2),
        "equity_curve": equity, "decisions": decisions, "params": p,
    }


def format_result(r):
    L = []
    L.append("═" * 74)
    L.append("  回测结果  %s ~ %s（%d 个交易日）" % (r["start"], r["end"], r["days"]))
    L.append("═" * 74)
    L.append("  初始资金 %-10.0f  期末权益 %-10.2f  总收益 %+.2f%%（年化 %+.2f%%）"
             % (r["capital"], r["final_equity"], r["total_return_pct"], r["annual_pct"]))
    L.append("  最大回撤 %.2f%%（%s）  持仓天数占比 %.1f%%  平均持仓 %.1f 天"
             % (r["max_drawdown_pct"], r["max_dd_date"], r["exposure_pct"], r["avg_hold_days"]))
    L.append("  交易 %d 笔 | 胜率 %.1f%% | 平均盈 %.2f%% / 平均亏 %.2f%% | 盈亏比 %s"
             % (r["trade_count"], r["win_rate_pct"], r["avg_win_pct"],
                r["avg_loss_pct"], r["profit_factor"]))
    L.append("  基准对比（买入持有）：收益 %+.2f%% / 最大回撤 %.2f%%   →  超额 %+.2f%%"
             % (r["buy_hold_return_pct"], r["buy_hold_max_dd_pct"],
                r["total_return_pct"] - r["buy_hold_return_pct"]))
    if r["open_position"]:
        o = r["open_position"]
        L.append("  期末持仓 %d 股 @ %.3f，浮盈 %+.2f 元"
                 % (o["shares"], o["avg_cost"], o["pnl"]))
    L.append("-" * 74)
    L.append("  交易明细：")
    for t in r["trades"][-15:]:
        L.append("    %s → %s  %5d股  %.2f→%.2f  %+6.2f%%  %+7.2f元  %s"
                 % (t["entry"], t["exit"], t["shares"], t["entry_price"],
                    t["exit_price"], t["pct"], t["pnl"], t["reason"]))
    if len(r["trades"]) > 15:
        L.append("    …（仅显示最近 15 笔，共 %d 笔）" % r["trade_count"])
    L.append("═" * 74)
    return "\n".join(L)

