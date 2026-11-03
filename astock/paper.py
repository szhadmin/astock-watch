# -*- coding: utf-8 -*-
"""模拟盘账户：真实费用、T+1、整手、按均价法记账、每日权益曲线。

AI 的所有交易指令都经过这里，再加一层风控（ai_agent.RiskRails）才真正成交。
"""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime

FEE = {"commission": 0.00025, "commission_min": 5.0,
       "stamp_sell": 0.0005, "transfer": 0.00002}


def _buy_fee(amount, fee):
    return max(amount * fee["commission"], fee["commission_min"]) + amount * fee["transfer"]


def _sell_fee(amount, fee):
    return (max(amount * fee["commission"], fee["commission_min"])
            + amount * fee["transfer"] + amount * fee["stamp_sell"])


class PaperBroker:
    def __init__(self, state_path, capital=15000.0, fee=None):
        self.path = state_path
        self.fee = {**FEE, **(fee or {})}
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        if os.path.exists(state_path):
            with open(state_path, encoding="utf-8-sig") as f:
                self.state = json.load(f)
        else:
            self.state = {
                "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "capital": float(capital), "cash": float(capital),
                "positions": {}, "trades": [], "equity_history": seed_equity(capital),
                "trade_date": datetime.now().strftime("%Y-%m-%d"),
            }
            self.save()

    # ---------------- 基础 ----------------
    @property
    def cash(self):
        return float(self.state["cash"])

    @property
    def positions(self):
        return self.state["positions"]

    def new_day(self, today=None):
        """跨交易日解锁 T+1 冻结的股数。"""
        today = today or datetime.now().strftime("%Y-%m-%d")
        if self.state.get("trade_date") != today:
            self.state["trade_date"] = today
            for p in self.state["positions"].values():
                p["available"] = p["shares"]
            self.save()

    def mark(self, prices):
        """按最新价估算市值与权益。prices: {code: price}"""
        mv = 0.0
        detail = []
        for code, p in self.state["positions"].items():
            px = prices.get(code) or p.get("last_price") or p["cost"]
            p["last_price"] = px
            val = p["shares"] * px
            mv += val
            detail.append({"code": code, "name": p.get("name", ""), "shares": p["shares"],
                           "available": p.get("available", p["shares"]),
                           "cost": p["cost"], "price": px,
                           "market_value": round(val, 2),
                           "pnl": round((px - p["cost"]) * p["shares"], 2),
                           "pnl_pct": round((px / p["cost"] - 1) * 100, 2) if p["cost"] else 0,
                           "entry_date": p.get("entry_date"), "stop": p.get("stop"),
                           "target": p.get("target"), "high_since": p.get("high_since"),
                           "note": p.get("note", "")})
        eq = self.cash + mv
        return {"cash": round(self.cash, 2), "market_value": round(mv, 2),
                "equity": round(eq, 2),
                "total_return_pct": round((eq / self.state["capital"] - 1) * 100, 2),
                "positions": detail}

    # ---------------- 成交 ----------------
    def buy(self, code, price, shares, name="", stop=None, target=None, note=""):
        shares = int(shares // 100 * 100)
        if shares <= 0:
            return False, "股数不足 1 手"
        amount = price * shares
        fee = _buy_fee(amount, self.fee)
        if amount + fee > self.cash + 1e-6:
            return False, "现金不足（需 %.2f，可用 %.2f）" % (amount + fee, self.cash)
        self.state["cash"] = round(self.cash - amount - fee, 4)
        pos = self.state["positions"].get(code)
        if pos:
            total = pos["shares"] + shares
            pos["cost"] = round((pos["cost"] * pos["shares"] + amount) / total, 4)
            pos["shares"] = total
            pos["stop"] = stop or pos.get("stop")
            pos["target"] = target or pos.get("target")
            pos["note"] = note or pos.get("note", "")
        else:
            self.state["positions"][code] = {
                "shares": shares, "available": 0, "cost": round(price, 4),
                "entry_date": datetime.now().strftime("%Y-%m-%d"),
                "high_since": price, "last_price": price, "name": name,
                "stop": stop, "target": target, "note": note,
            }
        self._log_trade("BUY", code, name, price, shares, fee, note)
        self.save()
        return True, "买入 %d 股 @ %.3f（费用 %.2f）" % (shares, price, fee)

    def sell(self, code, price, shares=None, note=""):
        pos = self.state["positions"].get(code)
        if not pos:
            return False, "无持仓"
        avail = pos.get("available", pos["shares"])
        if avail <= 0:
            return False, "T+1 限制：今日买入不可卖出"
        shares = pos["shares"] if shares is None else min(int(shares // 100 * 100), avail)
        if shares <= 0:
            return False, "可卖股数不足 1 手"
        amount = price * shares
        fee = _sell_fee(amount, self.fee)
        pnl = (price - pos["cost"]) * shares - fee
        self.state["cash"] = round(self.cash + amount - fee, 4)
        pos["shares"] -= shares
        pos["available"] = avail - shares
        if pos["shares"] <= 0:
            self.state["positions"].pop(code, None)
        self._log_trade("SELL", code, pos.get("name", ""), price, shares, fee, note,
                        pnl=pnl, cost=pos["cost"])
        self.save()
        return True, "卖出 %d 股 @ %.3f，盈亏 %+.2f 元" % (shares, price, pnl)

    def _log_trade(self, side, code, name, price, shares, fee, note, pnl=None, cost=None):
        self.state["trades"].append({
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "side": side, "code": code, "name": name,
            "price": round(price, 3), "shares": shares, "fee": round(fee, 2),
            "amount": round(price * shares, 2),
            "pnl": None if pnl is None else round(pnl, 2),
            "pct": None if cost in (None, 0) else round((price / cost - 1) * 100, 2),
            "note": note,
        })

    # ---------------- 落盘 / 报表 ----------------
    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def record_equity(self, snapshot, extra=None):
        row = {"date": datetime.now().strftime("%Y-%m-%d"),
               "time": datetime.now().strftime("%H:%M:%S"),
               "equity": snapshot["equity"], "cash": snapshot["cash"],
               "market_value": snapshot["market_value"]}
        if extra:
            row.update(extra)
        self.state["equity_history"].append(row)
        self.save()

    def stats(self):
        trades = [t for t in self.state["trades"] if t["side"] == "SELL"]
        wins = [t for t in trades if (t.get("pnl") or 0) > 0]
        losses = [t for t in trades if (t.get("pnl") or 0) <= 0]
        eqs = [e["equity"] for e in self.state["equity_history"]] or [self.state["capital"]]
        peak, mdd = -1e18, 0.0
        for e in eqs:
            peak = max(peak, e)
            mdd = max(mdd, (peak - e) / peak * 100 if peak > 0 else 0)
        gross_w = sum(t["pnl"] for t in wins)
        gross_l = abs(sum(t["pnl"] for t in losses))
        return {
            "start_capital": self.state["capital"],
            "equity": eqs[-1],
            "total_return_pct": round((eqs[-1] / self.state["capital"] - 1) * 100, 2),
            "closed_trades": len(trades),
            "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
            "avg_win": round(gross_w / len(wins), 2) if wins else 0.0,
            "avg_loss": round(-gross_l / len(losses), 2) if losses else 0.0,
            "profit_factor": round(gross_w / gross_l, 2) if gross_l else None,
            "max_drawdown_pct": round(mdd, 2),
            "total_fee": round(sum(t.get("fee") or 0 for t in self.state["trades"]), 2),
        }

    def export_trades_csv(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rows = self.state["trades"]
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["ts", "side", "code", "name", "price",
                                              "shares", "amount", "fee", "pnl", "pct", "note"])
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k) for k in w.fieldnames})
        return path


def seed_equity(capital):
    return [{"date": datetime.now().strftime("%Y-%m-%d"),
             "time": datetime.now().strftime("%H:%M:%S"),
             "equity": float(capital), "cash": float(capital), "market_value": 0.0}]
