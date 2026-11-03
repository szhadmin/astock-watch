# -*- coding: utf-8 -*-
"""硬风控自检：验证 AI 的越界指令是否会被代码层拒绝/裁剪。

用法：python selftest_ai.py
不调用大模型、不下单、不写交易记录，仅在控制台输出判定结果。
"""
from __future__ import annotations

import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from astock import ai_agent, feed  # noqa: E402
from main import load_config  # noqa: E402

CASES = []


def case(name, expect_ok, fn):
    try:
        ok, why, shares = fn()
    except Exception as exc:                      # noqa: BLE001
        CASES.append((name, False, "异常：%s" % exc))
        return
    CASES.append((name, ok == expect_ok, "%s｜股数 %s" % (why, shares)))


def main():
    cfg = load_config()
    ai = cfg.get("ai") or {}
    cap = float((cfg.get("account") or {}).get("total_capital", 15000))
    rails = ai_agent.RiskRails(ai.get("rails"), cap)

    # 用一个空账户快照（不读取真实模拟盘，避免干扰）
    snap = {"cash": cap, "market_value": 0.0, "equity": cap,
            "positions": [], "total_return_pct": 0.0}
    day = {"trades": 0, "loss_pct": 0.0, "loss_limit_hit": False}

    code = (cfg.get("watchlist") or ["000620"])[0]
    try:
        q = feed.get_quote(code)
    except Exception as exc:                      # noqa: BLE001
        print("行情获取失败：%s" % exc)
        return
    px = q["price"]
    print("基准标的 %s %s 现价 %.2f｜本金 %.0f｜单股上限 %.0f%%｜总仓上限 %.0f%%"
          % (code, q["name"], px, cap, rails.cfg["max_position_pct"],
             rails.cfg["max_total_position_pct"]))

    case("正常买入", True,
         lambda: rails.check({"code": code, "action": "BUY", "limit_price": px,
                              "shares": 500, "stop": round(px * 0.96, 2)}, snap, q, day))
    case("超单股上限（要求满仓）", True,
         lambda: rails.check({"code": code, "action": "BUY", "limit_price": px,
                              "shares": int(cap / px), "stop": round(px * 0.96, 2)},
                             snap, q, day))
    case("委托价偏离 10%", False,
         lambda: rails.check({"code": code, "action": "BUY",
                              "limit_price": round(px * 1.10, 2),
                              "shares": 300, "stop": round(px * 0.95, 2)}, snap, q, day))
    case("无有效止损", False,
         lambda: rails.check({"code": code, "action": "BUY", "limit_price": px,
                              "shares": 300, "stop": px}, snap, q, day))
    case("止损过宽(-30%)→缩到最小仓", True,
         lambda: rails.check({"code": code, "action": "BUY", "limit_price": px,
                              "shares": 2000, "stop": round(px * 0.70, 2)}, snap, q, day))
    case("止损极宽(-60%)→拒绝", False,
         lambda: rails.check({"code": code, "action": "BUY", "limit_price": px,
                              "shares": 4000, "stop": round(px * 0.40, 2)}, snap, q, day))
    q_hot = dict(q)
    q_hot["pct"] = 9.2
    case("当日已涨 9.2%（禁止追高）", False,
         lambda: rails.check({"code": code, "action": "BUY", "limit_price": px,
                              "shares": 300, "stop": round(px * 0.96, 2)}, snap, q_hot, day))
    case("当日亏损熔断后开新仓", False,
         lambda: rails.check({"code": code, "action": "BUY", "limit_price": px,
                              "shares": 300, "stop": round(px * 0.96, 2)},
                             snap, q, {"trades": 0, "loss_pct": 5.0, "loss_limit_hit": True}))
    case("超过当日最大笔数", False,
         lambda: rails.check({"code": code, "action": "BUY", "limit_price": px,
                              "shares": 300, "stop": round(px * 0.96, 2)},
                             snap, q, {"trades": 99, "loss_pct": 0, "loss_limit_hit": False}))
    case("卖出无持仓标的", False,
         lambda: rails.check({"code": "000001", "action": "SELL", "shares": 0},
                             snap, q, day))
    pos = {"code": code, "shares": 500, "available": 0, "cost": px,
           "market_value": px * 500, "price": px, "pnl_pct": 0.0}
    snap_t1 = dict(snap)
    snap_t1["positions"] = [pos]
    snap_t1["market_value"] = px * 500
    snap_t1["cash"] = cap - px * 500
    case("T+1 当日买入试图卖出", False,
         lambda: rails.check({"code": code, "action": "SELL", "shares": 0},
                             snap_t1, q, day))

    print("\n================ 硬风控自检 ================")
    ok_all = True
    for name, passed, detail in CASES:
        if not passed:
            ok_all = False
        print("  %s %-24s %s" % ("✔" if passed else "✘", name, detail))
    print("\n结论：%s" % ("全部通过——AI 的越界指令都会被拦截或裁剪。"
                    if ok_all else "存在未按预期拦截的用例，请检查 RiskRails。"))


if __name__ == "__main__":
    main()
