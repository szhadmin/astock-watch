# -*- coding: utf-8 -*-
"""经验引擎（第一层）：把 AI 的每次判断变成"可证伪的假设"，并自动回填结果。

为什么需要它：
    复盘阶段原来只能看到「我当时的理由」和「我的成交」，
    **看不到"我这个判断后来对没对"** —— 于是所谓经验全是自说自话，
    lessons.json 里写的多是"正确的废话"。

数据流：
    decide  → hypothesis.record_from_decision()  记下假设（含失效条件、期限）
    每天复盘 → hypothesis.backfill()              用真实日线回填 T+1..T+5 结果
    复盘提示词 → 带上「我过去的判断与结果」         让模型对着事实学
    记分卡   → hypothesis.scorecard()             按温度/评分/动作分组统计命中率

存储：data/ai/hypotheses.jsonl（一行一条，append-only，便于审计）
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta

from . import feed

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AI_DIR = os.path.join(BASE, "data", "ai")
PATH = os.path.join(AI_DIR, "hypotheses.jsonl")
HORIZON = 5          # 观察 5 个交易日
MOVE_PCT = 1.0       # 涨/跌 1% 以上算"方向判断成立"


def _ensure():
    os.makedirs(AI_DIR, exist_ok=True)


def load_all():
    _ensure()
    out = []
    if not os.path.exists(PATH):
        return out
    try:
        with open(PATH, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return out


def _rewrite(items):
    _ensure()
    with open(PATH, "w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")


def _norm_dir(action, advice=None):
    """把动作归一成 bullish / bearish / hold 三种可检验的方向。"""
    a = str(action or "").upper()
    if a in ("BUY", "ADD"):
        return "bullish"
    if a in ("SELL", "REDUCE", "CLEAR"):
        return "bearish"
    adv = str(advice or "")
    if "加仓" in adv or "买入" in adv:
        return "bullish"
    if any(k in adv for k in ("减仓", "清仓", "止损", "卖出")):
        return "bearish"
    return "hold"


def record_from_decision(data, cfg=None, verbose=False):
    """从一次决策的输出里抽出假设并落盘（只记有明确方向的判断）。"""
    if not data:
        return []
    today = datetime.now().strftime("%Y-%m-%d")
    # 决策当时的市场上下文（规则统计要按条件切片）
    ctx_base = {}
    try:
        from . import market as _mk
        ctx_base["temp"] = (_mk.market_temp() or {}).get("temp")
    except Exception:
        ctx_base["temp"] = None
    existing = load_all()
    open_keys = {(x["date"], x["code"]) for x in existing if x.get("status") == "open"}
    out = []

    def _add(code, name, direction, ref, stop, target, reason, source):
        if not code or (today, code) in open_keys:
            return
        rec = {
            "id": uuid.uuid4().hex[:10],
            "date": today, "code": code, "name": name or "",
            "direction": direction, "source": source,
            "ref_price": round(float(ref), 4) if ref else None,
            "invalidate": round(float(stop), 4) if stop else None,
            "target": round(float(target), 4) if target else None,
            "horizon": HORIZON, "move_pct": MOVE_PCT,
            "reason": (reason or "")[:200],
            "ctx": dict(ctx_base),
            "status": "open", "result": None,
        }
        try:                                    # 所属板块当日涨幅
            from . import market as _mk
            info = _mk.sector_of(code) or {}
            rec["ctx"]["sector_pct"] = info.get("pct")
        except Exception:
            rec["ctx"]["sector_pct"] = None
        out.append(rec)

    quotes = {}
    for a in (data.get("actions") or []):
        code = a.get("code")
        if not code:
            continue
        try:
            quotes[code] = quotes.get(code) or feed.get_quote(code)
        except Exception:
            quotes[code] = {}
        q = quotes[code] or {}
        _add(code, q.get("name"), _norm_dir(a.get("action")),
             a.get("limit_price") or q.get("price"), a.get("stop"), a.get("target"),
             a.get("reason"), "action:" + str(a.get("action")))
    for x in (data.get("我的持仓操作建议") or []):
        code = x.get("code")
        if not code:
            continue
        try:
            quotes[code] = quotes.get(code) or feed.get_quote(code)
        except Exception:
            quotes[code] = {}
        q = quotes[code] or {}
        trig = None
        for seg in str(x.get("触发位") or "").replace("；", ";").split(";"):
            for tok in seg.replace("，", ",").split(","):
                t = tok.strip()
                if t.startswith("跌破") or t.startswith("站上"):
                    try:
                        trig = float("".join(ch for ch in t if ch.isdigit() or ch == ".")[:6])
                    except Exception:
                        trig = None
        _add(code, q.get("name") or x.get("name"), _norm_dir(None, x.get("建议")),
             q.get("price"), trig, None, x.get("理由"), "advice:" + str(x.get("建议")))
    if out:
        existing.extend(out)
        _rewrite(existing)
        if verbose:
            print("  假设已记录 %d 条（可证伪，%d 交易日后回填）" % (len(out), HORIZON))
    return out


def backfill(cfg=None, verbose=False):
    """对 open 的假设用真实日线回填结果（到期或已失效的才结算）。"""
    items = load_all()
    todo = [x for x in items if x.get("status") == "open"]
    if not todo:
        return {"checked": 0, "filled": 0}
    filled = 0
    today = datetime.now().strftime("%Y-%m-%d")
    for it in todo:
        try:
            bars = feed.get_kline(it["code"], 101, limit=60)
        except Exception:
            continue
        after = [b for b in bars if b["date"] > it["date"]]
        if not after:
            continue
        window = after[:it.get("horizon", HORIZON)]
        ref = it.get("ref_price") or window[0]["open"]
        if not ref:
            continue
        rets = [round((b["close"] / ref - 1) * 100, 2) for b in window]
        max_hi = max((b["high"] / ref - 1) * 100 for b in window)
        max_lo = min((b["low"] / ref - 1) * 100 for b in window)
        inv = it.get("invalidate")
        broke = None
        if inv:
            for b in window:                                  # 先破失效条件？
                if it["direction"] == "bullish" and b["low"] <= inv:
                    broke = b["date"]
                    break
                if it["direction"] == "bearish" and b["high"] >= inv:
                    broke = b["date"]
                    break
        done = len(window) >= it.get("horizon", HORIZON) or broke is not None
        if not done:
            continue
        end_ret = rets[-1]
        d = it["direction"]
        if d == "bullish":
            hit = (not broke) and end_ret >= it["move_pct"]
        elif d == "bearish":
            hit = (not broke) and end_ret <= -it["move_pct"]
        else:                                                # hold：不破位即算对
            hit = broke is None
        it["result"] = {
            "filled_at": today, "days": len(window),
            "rets": rets, "end_ret": end_ret,
            "max_gain": round(max_hi, 2), "max_drawdown": round(max_lo, 2),
            "invalidated": broke, "hit": bool(hit),
            "verdict": ("命中" if hit else ("先破失效条件" if broke else "方向未走出来")),
        }
        it["status"] = "closed"
        filled += 1
    if filled:
        _rewrite(items)
    if verbose:
        print("  回填完成：结算 %d 条（未到期 %d 条）"
              % (filled, len([x for x in items if x.get("status") == "open"])))
    return {"checked": len(todo), "filled": filled}


def recent(n=10, only_closed=True):
    items = [x for x in load_all() if x.get("result")] if only_closed else load_all()
    items.sort(key=lambda x: (x.get("date") or "", x.get("id") or ""), reverse=True)
    return items[:n]


def brief_for_review(n=8):
    """给复盘提示词用的紧凑版：判断 + 结果（让模型对着事实学）。"""
    rows = []
    for x in recent(n):
        r = x["result"]
        rows.append({
            "日期": x["date"], "标的": "%s %s" % (x["code"], x.get("name") or ""),
            "我的判断": {"bullish": "看涨", "bearish": "看跌", "hold": "持有/不破位"}[x["direction"]],
            "依据": (x.get("reason") or "")[:60],
            "结果": "%s（%d日 %+.2f%%，期间最高 %+.2f%% / 最低 %+.2f%%）"
                    % (r["verdict"], r["days"], r["end_ret"], r["max_gain"], r["max_drawdown"]),
            "是否命中": "命中" if r["hit"] else "未命中",
        })
    return rows


def scorecard(min_samples=5):
    """按方向/温度/评分分组统计判断质量。"""
    items = [x for x in load_all() if x.get("result")]
    groups = {}
    for x in items:
        key = x["direction"]
        g = groups.setdefault(key, {"n": 0, "hit": 0, "rets": []})
        g["n"] += 1
        g["hit"] += 1 if x["result"]["hit"] else 0
        g["rets"].append(x["result"]["end_ret"])
    out = []
    for k, g in groups.items():
        out.append({"group": {"bullish": "看涨", "bearish": "看跌", "hold": "持有"}[k],
                    "samples": g["n"],
                    "hit_rate_pct": round(g["hit"] / g["n"] * 100, 1) if g["n"] else 0,
                    "avg_ret_pct": round(sum(g["rets"]) / len(g["rets"]), 2) if g["rets"] else 0,
                    "enough": g["n"] >= min_samples})
    open_n = len([x for x in load_all() if x.get("status") == "open"])
    return {"closed": len(items), "open": open_n, "groups": out,
            "note": ("样本 < %d 条时统计意义不足，只作观察、不进提示词" % min_samples)}


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if "--backfill" in sys.argv:
        print(backfill(verbose=True))
    print(json.dumps(scorecard(), ensure_ascii=False, indent=1))
    for r in brief_for_review(5):
        print(r)
