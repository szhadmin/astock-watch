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
import time
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


_CACHE = {"ts": 0.0, "key": None, "res": None}
CACHE_TTL = 60.0


def _key(d):
    return (d.get("capital"), tuple(sorted((i["code"], i["shares"], i["cost"])
                                           for i in d["items"])))


def analyze(cfg=None, verbose=False, use_cache=True):
    """返回 {capital, summary, items:[...]}，每只含现价/盈亏/评分/止损/目标/建议。"""
    d = load()
    k = _key(d)
    if use_cache and _CACHE["res"] and _CACHE["key"] == k \
            and time.time() - _CACHE["ts"] < CACHE_TTL:
        return _CACHE["res"]
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
    res = {"capital": capital, "summary": summary, "items": rows,
           "raw": d["items"], "updated": d.get("updated")}
    _CACHE.update({"ts": time.time(), "key": k, "res": res})
    return res


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


# --------------------------------------------------------------------------- #
# 实盘成交流水（我自己真实的买卖），用于算真实持仓、胜率、盈亏比
#   存储：data/real_trades.json  {"items":[{id,date,code,name,side,shares,price,fee,note}]}
#   口径：加权平均成本；每次卖出结算一笔已实现盈亏
# --------------------------------------------------------------------------- #
TRADES_PATH = os.path.join(BASE, "data", "real_trades.json")


def load_trades():
    import uuid
    try:
        with open(TRADES_PATH, encoding="utf-8-sig") as f:
            d = json.load(f)
        items = d.get("items") or []
    except Exception:
        items = []
    return items


def _save_trades(items):
    os.makedirs(os.path.dirname(TRADES_PATH), exist_ok=True)
    with open(TRADES_PATH, "w", encoding="utf-8") as f:
        json.dump({"items": items,
                   "updated": datetime.now().strftime("%Y-%m-%d %H:%M")},
                  f, ensure_ascii=False, indent=1)
    return items


def add_trade(rec):
    """追加一笔成交（date/code/side/shares/price/fee/note）。"""
    import uuid
    items = load_trades()
    side = str(rec.get("side") or "BUY").upper()
    if side not in ("BUY", "SELL"):
        side = "BUY"
    code = str(rec.get("code") or "").strip()
    if not code.isdigit() or len(code) != 6:
        raise ValueError("股票代码必须是 6 位数字")
    shares = int(float(rec.get("shares") or 0))
    price = float(rec.get("price") or 0)
    if shares <= 0 or price <= 0:
        raise ValueError("股数与价格必须大于 0")
    name = (rec.get("name") or "").strip()
    if not name:
        try:
            q = feed.get_quote(code)
            name = q.get("name") or ""
        except Exception:
            name = ""
    items.append({
        "id": uuid.uuid4().hex[:12],
        "date": (rec.get("date") or datetime.now().strftime("%Y-%m-%d")).strip(),
        "code": code, "name": name, "side": side, "shares": shares,
        "price": round(price, 4),
        "fee": round(float(rec.get("fee") or 0), 2),
        "note": (rec.get("note") or "").strip(),
    })
    items.sort(key=lambda x: (x["date"], x["id"]))
    _save_trades(items)
    return items[-1]


def del_trade(tid):
    items = [x for x in load_trades() if x.get("id") != tid]
    _save_trades(items)
    return len(items)


def replay():
    """按时间顺序回放流水，返回 (持仓字典, 已平仓列表, 汇总)。

    持仓：{code: {code,name,shares,cost(摊薄),cost_basis,buys,sells}}
    平仓：每次卖出一条 {code,name,date,shares,price,pnl,fee}
    """
    items = sorted(load_trades(), key=lambda x: (x.get("date") or "", x.get("id") or ""))
    pos, closed = {}, []
    for t in items:
        code, side, sh = t["code"], t["side"], float(t["shares"])
        price, fee = float(t["price"]), float(t.get("fee") or 0)
        p = pos.setdefault(code, {"code": code, "name": t.get("name") or "",
                                  "shares": 0.0, "cost_basis": 0.0,
                                  "buys": 0, "sells": 0})
        if t.get("name") and not p["name"]:
            p["name"] = t["name"]
        if side == "BUY":
            p["shares"] += sh
            p["cost_basis"] += sh * price + fee
            p["buys"] += 1
        else:
            held = p["shares"]
            sell_sh = min(sh, held) if held else sh        # 允许先卖后买（不计负仓）
            avg = (p["cost_basis"] / held) if held else 0.0
            pnl = (price - avg) * sell_sh - fee if held else 0.0
            closed.append({"code": code, "name": p["name"], "date": t["date"],
                           "shares": int(sell_sh), "price": price,
                           "pnl": round(pnl, 2), "fee": fee})
            if held:
                p["cost_basis"] -= avg * sell_sh
                p["shares"] -= sell_sh
            p["sells"] += 1
    for code, p in list(pos.items()):
        if p["shares"] <= 0:
            pos.pop(code)
        else:
            p["cost"] = round(p["cost_basis"] / p["shares"], 4)
            p["shares"] = int(round(p["shares"]))
    wins = [c for c in closed if c["pnl"] > 0]
    losses = [c for c in closed if c["pnl"] < 0]
    gross_win = sum(c["pnl"] for c in wins)
    gross_loss = abs(sum(c["pnl"] for c in losses))
    stats = {
        "closed": len(closed), "wins": len(wins), "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 2) if closed else 0.0,
        "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "realized": round(sum(c["pnl"] for c in closed), 2),
        "fee_total": round(sum(float(t.get("fee") or 0) for t in items), 2),
        "buys": len([t for t in items if t["side"] == "BUY"]),
        "sells": len([t for t in items if t["side"] == "SELL"]),
        "trades": len(items),
    }
    return pos, closed, stats


def trades_overview():
    """给前端/邮件用：流水 + 推导持仓 + 绩效。"""
    pos, closed, stats = replay()
    items = load_trades()
    for t in items:                                  # 卖出记录带上已实现盈亏
        t["realized"] = None
    by_code_date = {}
    for c in closed:
        by_code_date.setdefault((c["code"], c["date"]), []).append(c["pnl"])
    for t in items:
        if t["side"] == "SELL":
            lst = by_code_date.get((t["code"], t["date"]))
            if lst:
                t["realized"] = round(lst.pop(0), 2)
    return {"items": items, "stats": stats,
            "derived": [{"code": p["code"], "name": p["name"], "shares": p["shares"],
                         "cost": p["cost"], "buys": p["buys"], "sells": p["sells"]}
                        for p in pos.values()],
            "closed": closed}


def rebuild_holdings_from_trades(force=False):
    """用流水推导的持仓覆盖 holdings.json 的 items（资金量保留）。

    安全阀：流水推导不出任何持仓、而当前持仓表非空时，默认**拒绝**，
    避免"流水没记全"就把手工录入的持仓清空。确认要清空请传 force=True。
    """
    pos, _closed, _stats = replay()
    cur = load()
    if not pos and (cur.get("items") or []) and not force:
        raise ValueError("成交流水推导不出任何持仓，为避免误删已取消。"
                         "先把买卖流水补全，或确认要清空时再操作。")
    items = [{"code": p["code"], "name": p["name"], "shares": p["shares"],
              "cost": p["cost"], "date": "", "note": "由成交流水自动同步"}
             for p in pos.values()]
    save({"capital": cur.get("capital"), "items": items})
    return len(items)
