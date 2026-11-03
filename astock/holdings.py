# -*- coding: utf-8 -*-
"""我的持仓（真实账户）：资金量、手工录入的股票与成本，以及逐只分析建议。

与 AI 模拟盘（data/ai/paper_state.json）**完全分开**：
    · 这里记的是你自己的钱和成本，AI 不动它；
    · 分析复用 signals.build_report（评分/止损/目标/右侧确认），不产生模型费用。

存储：data/holdings.json
    {"capital": 15000, "updated": "...", "items": [
        {"code": "000620", "name": "", "shares": 1000, "cost": 2.605,
         "date": "2026-11-02", "note": ""}]}
"""
from __future__ import annotations

import json
import os
from datetime import datetime

from . import feed, signals

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(BASE, "data", "holdings.json")


def load():
    try:
        with open(PATH, encoding="utf-8-sig") as f:
            d = json.load(f)
    except Exception:
        d = {}
    d.setdefault("capital", 15000.0)
    d.setdefault("items", [])
    d.setdefault("updated", "")
    return d


def save(data):
    d = {"capital": float(data.get("capital") or 0),
         "items": [], "updated": datetime.now().strftime("%Y-%m-%d %H:%M")}
    for it in (data.get("items") or []):
        code = str(it.get("code") or "").strip()
        if not code.isdigit() or len(code) != 6:
            continue
        try:
            shares = int(float(it.get("shares") or 0))
            cost = float(it.get("cost") or 0)
        except (TypeError, ValueError):
            continue
        if shares <= 0 or cost <= 0:
            continue
        d["items"].append({"code": code, "name": (it.get("name") or "").strip(),
                           "shares": shares, "cost": round(cost, 4),
                           "date": (it.get("date") or "").strip(),
                           "note": (it.get("note") or "").strip()})
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    return d


def _advise(rep, pnl_pct):
    """把技术评分翻译成人话建议（不调用模型）。"""
    v = rep["verdict"]
    lv = rep["levels"]
    price = rep["price"]
    right = v.get("right") or 0
    stop = lv.get("stop") or 0
    if stop and price <= stop:
        return "已跌破止损 %.2f，按纪律离场" % stop, "danger"
    if rep["scores"]["total"] < 40:
        return "弱势下行（%s），反弹到关键阻力位减仓，严守止损 %.2f" % (v["level"], stop), "warn"
    if right < 2:
        return "位置不高但右侧未确认（确认 %d/4），持有观察，不补仓" % rep["checklist_ok"], "warn"
    if pnl_pct > 8 and right >= 3:
        return "趋势已确认（右侧 %d/4），可上移止损保利润，目标 %s" % (
            right, "%.2f" % (lv["targets"][0] if lv["targets"] else 0)), "ok"
    return "持有，破 %.2f 走人，上方看 %s" % (
        stop, "/".join("%.2f" % x for x in lv["targets"][:2]) or "--"), "ok"


def analyze(cfg=None, verbose=False):
    """返回 {capital, summary, items:[...]}，每只含现价/盈亏/评分/止损/目标/建议。"""
    d = load()
    rows = []
    total_cost = total_mv = 0.0
    for it in d["items"]:
        code = it["code"]
        rep = None
        try:
            rep = signals.build_report(code, cfg or {})
        except Exception as exc:                      # noqa: BLE001
            if verbose:
                print("  [%s] 分析失败：%s" % (code, exc))
        if not rep:
            rows.append({**it, "ok": False, "error": "行情获取失败"})
            continue
        price = rep["price"]
        mv = price * it["shares"]
        pnl = (price - it["cost"]) * it["shares"]
        pnl_pct = (price / it["cost"] - 1) * 100 if it["cost"] else 0
        advice, tone = _advise(rep, pnl_pct)
        lv = rep["levels"]
        rows.append({
            "code": code, "name": rep.get("name") or it.get("name") or "",
            "shares": it["shares"], "cost": it["cost"], "price": price,
            "pct": rep.get("pct") or 0, "mv": round(mv, 2),
            "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2),
            "score": rep["scores"]["total"], "level": rep["verdict"]["level"],
            "action": rep["verdict"]["action"],
            "right": rep["verdict"].get("right"), "left": rep["verdict"].get("left"),
            "checklist": rep["checklist_ok"],
            "stop": lv.get("stop"), "targets": lv.get("targets") or [],
            "support": lv.get("support") or [], "resistance": lv.get("resistance") or [],
            "dist_stop_pct": round((price / lv["stop"] - 1) * 100, 2) if lv.get("stop") else None,
            "advice": advice, "tone": tone,
            "date": it.get("date"), "note": it.get("note"), "ok": True,
        })
        total_cost += it["cost"] * it["shares"]
        total_mv += mv
    capital = float(d.get("capital") or 0)
    cash = capital - total_cost
    summary = {
        "capital": round(capital, 2),
        "total_cost": round(total_cost, 2),
        "total_mv": round(total_mv, 2),
        "cash": round(cash, 2),
        "pnl": round(total_mv - total_cost, 2),
        "pnl_pct": round((total_mv / total_cost - 1) * 100, 2) if total_cost else 0,
        "position_pct": round(total_cost / capital * 100, 1) if capital else 0,
        "count": len([r for r in rows if r.get("ok")]),
        "updated": d.get("updated"),
    }
    for r in rows:                                    # 仓位占比
        if r.get("ok") and total_mv:
            r["weight_pct"] = round(r["mv"] / total_mv * 100, 1)
    return {"capital": capital, "summary": summary, "items": rows,
            "raw": d["items"], "updated": d.get("updated")}


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    res = analyze({}, verbose=True)
    print(json.dumps(res["summary"], ensure_ascii=False, indent=1))
    for r in res["items"]:
        if r.get("ok"):
            print("  %s %-6s %d股 成本%.3f 现价%.2f 浮盈%+.2f%% 评分%d → %s"
                  % (r["code"], r["name"], r["shares"], r["cost"], r["price"],
                     r["pnl_pct"], r["score"], r["advice"]))
