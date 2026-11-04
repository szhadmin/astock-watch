# -*- coding: utf-8 -*-
"""错过清单：记录「当天最强的票里，哪些没进候选池」，并在持有期后回填真实收益。

目的：AI 现在只知道自己买了什么，不知道自己**没看见什么**。
      把"错过"也变成可检验的样本，复盘时一起看，才能发现选股规则的系统性偏差。

数据：data/ai/missed.jsonl
字段：{date, code, name, pct, price, amount_yi, boards, in_pool, pool_channel,
       result: {h5, h10, h20, excess, filled_at}}
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

from . import feed

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(BASE, "data", "ai", "missed.jsonl")
HORIZON = 10
TOP_MISSED = 20


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def _read():
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


def _write(items):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    with open(PATH, "w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")


def _gainers(n=TOP_MISSED):
    """按涨幅降序取全市场（东财 clist，fid=f3）。"""
    import urllib.request
    url = ("https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=%d&po=1&np=1&fltt=2&invt=2"
           "&fid=f3&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
           "&fields=f12,f13,f14,f2,f3,f6" % (n * 2))
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
        "Referer": "https://quote.eastmoney.com/",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    out = []
    for x in ((d.get("data") or {}).get("diff") or []):
        code = str(x.get("f12") or "")
        name = x.get("f14") or ""
        if not (code.isdigit() and len(code) == 6):
            continue
        if any(t in name for t in ("ST", "退", "N ", "C ")):
            continue
        out.append({"code": code, "name": name,
                    "price": x.get("f2"), "pct": x.get("f3"),
                    "amount_yi": round((x.get("f6") or 0) / 1e8, 2)})
        if len(out) >= n:
            break
    return out


def record_daily(pool_codes=None, verbose=True):
    """收盘后记录：今日涨幅前 N，标记是否进过当天候选池。返回新增条数。"""
    today = _today()
    items = _read()
    if any(x.get("date") == today for x in items):
        if verbose:
            print("  错过清单：今日已记录，跳过")
        return 0
    try:
        rows = _gainers(TOP_MISSED)
    except Exception as exc:
        if verbose:
            print("  错过清单：涨幅榜获取失败 %s" % exc)
        return 0
    pool = {str(c) for c in (pool_codes or [])}
    if not pool:
        try:                                  # 兜底：读观察池
            wl = json.load(open(os.path.join(BASE, "data", "ai", "watchlist.json"),
                                encoding="utf-8-sig"))
            pool = {str(c) for c in (wl.get("codes") or [])}
        except Exception:
            pool = set()
    new = []
    for r in rows:
        new.append({"date": today, "code": r["code"], "name": r["name"],
                    "pct": r["pct"], "price": r["price"],
                    "amount_yi": r["amount_yi"],
                    "in_pool": r["code"] in pool,
                    "result": None})
    items.extend(new)
    _write(items)
    if verbose:
        missed = [x for x in new if not x["in_pool"]]
        print("  错过清单：今日涨幅前 %d 名，其中 %d 只没进候选池"
              % (len(new), len(missed)))
        for x in missed[:5]:
            print("    · %s %s %+.1f%%（未入池）" % (x["code"], x["name"], x["pct"] or 0))
    return len(new)


def backfill(verbose=True):
    """对记录满 HORIZON 个交易日的条目，回填真实收益（扣 beta 用上证指数）。"""
    items = _read()
    idx = {}
    try:
        bars = feed.get_index_kline("1.000001", 260)
        idx = {b["date"]: b["close"] for b in bars}
    except Exception:
        pass
    filled = 0
    cutoff = (datetime.now() - timedelta(days=HORIZON * 2)).strftime("%Y-%m-%d")
    for it in items:
        if it.get("result"):
            continue
        if (it.get("date") or "") > cutoff:
            continue
        code = it.get("code")
        try:
            bars = feed.get_kline(code, 101, limit=40, adjust=1)
        except Exception:
            continue
        if not bars:
            continue
        dates = [b["date"] for b in bars]
        d0 = it.get("date")
        if d0 not in dates:
            continue
        i0 = dates.index(d0)
        base = bars[i0]["close"]
        res = {}
        for h in (5, 10, 20):
            if i0 + h < len(bars) and base:
                r = (bars[i0 + h]["close"] / base - 1) * 100
                res["h%d" % h] = round(r, 2)
                d_end = bars[i0 + h]["date"]
                if idx.get(d0) and idx.get(d_end):
                    ex = ((bars[i0 + h]["close"] / base) - (idx[d_end] / idx[d0])) * 100
                    res["exc%d" % h] = round(ex, 2)
        if res:
            it["result"] = {**res, "filled_at": _today()}
            filled += 1
    if filled:
        _write(items)
    if verbose:
        print("  错过清单回填：%d 条已补齐真实收益" % filled)
    return filled


def stats():
    items = _read()
    closed = [x for x in items if x.get("result")]
    def avg(vals):
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    out = {"recorded": len(items), "closed": len(closed)}
    for label, flag in (("入池", True), ("未入池", False)):
        grp = [x for x in closed if bool(x.get("in_pool")) is flag]
        out[label] = {
            "n": len(grp),
            "5日超额%": avg([(x["result"] or {}).get("exc5") for x in grp]),
            "10日超额%": avg([(x["result"] or {}).get("exc10") for x in grp]),
            "20日超额%": avg([(x["result"] or {}).get("exc20") for x in grp]),
        }
    return out


def brief_for_review(n=8):
    """给复盘用的文字：最近被错过的强票 + 入池/未入池的实际表现对比。"""
    items = _read()
    recent = [x for x in items if not x.get("in_pool")][-n:]
    s = stats()
    lines = ["【错过清单】最近被规则漏掉的最强票："]
    if not recent:
        lines.append("  （暂无记录）")
    for x in reversed(recent):
        r = x.get("result") or {}
        extra = ""
        if r.get("exc10") is not None:
            extra = "｜10日超额 %+.2f%%" % r["exc10"]
        lines.append("  %s %s 当日 %+.1f%%%s" % (x["code"], x["name"], x.get("pct") or 0, extra))
    a, b = s.get("入池") or {}, s.get("未入池") or {}
    if a.get("n") and b.get("n"):
        lines.append("  统计：入池股票 10 日超额 %s%%（%d 只）｜未入池 %s%%（%d 只）"
                     % (a.get("10日超额%"), a["n"], b.get("10日超额%"), b["n"]))
        if (b.get("10日超额%") or 0) > (a.get("10日超额%") or 0) + 0.3:
            lines.append("  ⚠ 结论：被漏掉的票反而更强 → 选股规则偏保守，应放宽强势通道")
        elif (a.get("10日超额%") or 0) > (b.get("10日超额%") or 0) + 0.3:
            lines.append("  ✓ 结论：现有筛选有效，被漏掉的票确实更差")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if "--record" in sys.argv:
        record_daily()
    if "--backfill" in sys.argv:
        backfill()
    print(json.dumps(stats(), ensure_ascii=False, indent=1))
    print(brief_for_review())
