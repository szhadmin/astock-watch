# -*- coding: utf-8 -*-
"""特征分层实验室：判断"评分/温度"这些特征到底有没有区分度。

用法：
    python -m astock.lab            # 看评分五分位、温度分档、持有期敏感性
结论会直接告诉你：现在的特征有没有 alpha，还是纯随机。
"""
from __future__ import annotations

import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATHS = [os.path.join(BASE, "data", "ai", "hypotheses.jsonl"),
         os.path.join(BASE, "data", "ai", "hypotheses_replay.jsonl")]
COST = 0.3


def load():
    out = []
    for p in PATHS:
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    x = json.loads(line)
                except Exception:
                    continue
                if x.get("result") and (x.get("ctx") or {}):
                    out.append(x)
    return out


def bucket_stats(rows, key, buckets):
    """按 key 分档统计平均收益与命中率。"""
    out = []
    for lo, hi in buckets:
        sel = [r for r in rows
               if (r["ctx"].get(key) is not None and lo <= float(r["ctx"][key]) < hi)]
        if not sel:
            out.append({"档": "%s~%s" % (lo, hi), "n": 0})
            continue
        rets = [r["result"].get("end_ret") or 0 for r in sel]
        excs = [(r["result"].get("excess")
                 if r["result"].get("excess") is not None else (r["result"].get("end_ret") or 0))
                for r in sel]
        hits = sum(1 for r in sel if r["result"].get("hit"))
        out.append({"档": "%s~%s" % (lo, hi), "n": len(sel),
                    "平均收益%": round(sum(rets) / len(rets), 2),
                    "扣费后%": round(sum(rets) / len(rets) - COST, 2),
                    "超额%": round(sum(excs) / len(excs), 2),
                    "扣费超额%": round(sum(excs) / len(excs) - COST, 2),
                    "命中率%": round(hits / len(sel) * 100, 1),
                    "股票数": len({r["code"] for r in sel})})
    return out


def monotonic(stats):
    """按档位看平均收益是否单调 —— 单调才有排序能力。"""
    vals = [s.get("平均收益%") for s in stats if s.get("n")]
    if len(vals) < 3:
        return "样本不足"
    inc = all(vals[i] <= vals[i + 1] for i in range(len(vals) - 1))
    dec = all(vals[i] >= vals[i + 1] for i in range(len(vals) - 1))
    if inc:
        return "单调递增（特征有效）"
    if dec:
        return "单调递减（特征反向有效）"
    return "非单调（没有排序能力）"


def horizon_table(rows):
    """持有期敏感性：同一批样本在 5/10/20 日上的表现。"""
    out = []
    for h in ("h5", "h10", "h20"):
        sel = [r for r in rows if isinstance(r["result"].get("rets"), dict)
               and r["result"]["rets"].get(h) is not None]
        if not sel:
            continue
        raw = [r["result"]["rets"][h] for r in sel]
        exc = [(r["result"].get("excess_map") or {}).get(h) for r in sel]
        exc = [x for x in exc if x is not None] or raw
        out.append({"期限": h.upper(), "n": len(sel),
                    "原始%": round(sum(raw) / len(raw), 2),
                    "超额%": round(sum(exc) / len(exc), 2),
                    "扣费后超额%": round(sum(exc) / len(exc) - COST, 2)})
    return out


def state_stats(rows):
    """分市场状态（用当时指数 20 日涨跌）：熊市 / 震荡 / 牛市。"""
    buckets = [("熊市(指数20日<-5%)", -999, -5), ("震荡(-5%~5%)", -5, 5),
               ("牛市(>5%)", 5, 999)]
    out = []
    for name, lo, hi in buckets:
        sel = [r for r in rows if r["ctx"].get("idx20") is not None
               and lo <= r["ctx"]["idx20"] < hi]
        if not sel:
            continue
        exc = [(r["result"].get("excess")
                if r["result"].get("excess") is not None else r["result"].get("end_ret") or 0)
               for r in sel]
        hits = sum(1 for r in sel if r["result"].get("hit"))
        out.append({"状态": name, "n": len(sel),
                    "超额%": round(sum(exc) / len(exc), 2),
                    "扣费后超额%": round(sum(exc) / len(exc) - COST, 2),
                    "命中率%": round(hits / len(sel) * 100, 1)})
    return out


def regime_stats(rows):
    """按市场状态细分（牛/熊/窄幅缩量/宽幅剧震/常规震荡）分桶。"""
    keys = ["牛市", "熊市", "窄幅缩量", "宽幅剧震", "常规震荡"]
    out = []
    for k in keys:
        sel = [r for r in rows if (r["ctx"].get("regime") == k)]
        if not sel:
            continue
        exc = [(r["result"].get("excess")
                if r["result"].get("excess") is not None else r["result"].get("end_ret") or 0)
               for r in sel]
        hits = sum(1 for r in sel if r["result"].get("hit"))
        out.append({"状态细分": k, "n": len(sel),
                    "超额%": round(sum(exc) / len(exc), 2),
                    "扣费后超额%": round(sum(exc) / len(exc) - COST, 2),
                    "命中率%": round(hits / len(sel) * 100, 1)})
    return out


def feature_stats(rows):
    """新特征分层：评分变化率 / 量能倍数 / 距MA20偏离。"""
    feats = [("score_chg", "评分变化率", [(-999, -5), (-5, 0), (0, 5), (5, 999)]),
             ("vol_ratio", "量能倍数", [(0, 1), (1, 1.5), (1.5, 2.5), (2.5, 999)]),
             ("ma20_dev", "距MA20偏离%", [(-999, -8), (-8, -2), (-2, 2), (2, 8), (8, 999)])]
    out = {}
    for key, label, buckets in feats:
        out[label] = bucket_stats(rows, key, buckets)
    return out


def main():
    rows = load()
    if not rows:
        print("没有样本，先跑：python main.py replay --max 60 --days 600")
        return
    print("样本总数：%d｜覆盖股票 %d 只｜区间 %s ~ %s"
          % (len(rows), len({r["code"] for r in rows}),
             min(r["date"] for r in rows), max(r["date"] for r in rows)))
    rets = [r["result"].get("end_ret") or 0 for r in rows]
    hits = sum(1 for r in rows if r["result"].get("hit"))
    print("全体基准：平均收益 %+.2f%%｜扣费后 %+.2f%%｜命中率 %.1f%%\n"
          % (sum(rets) / len(rets), sum(rets) / len(rets) - COST, hits / len(rows) * 100))

    print("① 综合评分分层")
    st = bucket_stats(rows, "score", [(0, 40), (40, 50), (50, 60), (60, 70), (70, 101)])
    for x in st:
        print("   %-8s n=%-6s 超额%+6s%% 扣费后超额%+6s%% 命中%5s%%"
              % (x["档"], x.get("n"), x.get("超额%", "-"), x.get("扣费超额%", "-"),
                 x.get("命中率%", "-")))
    print("   → %s\n" % monotonic([{**x, "平均收益%": x.get("超额%")} for x in st]))

    print("② 大盘温度分层")
    st = bucket_stats(rows, "temp", [(0, 25), (25, 40), (40, 60), (60, 75), (75, 101)])
    for x in st:
        print("   %-8s n=%-6s 超额%+6s%% 扣费后超额%+6s%% 命中%5s%%"
              % (x["档"], x.get("n"), x.get("超额%", "-"), x.get("扣费超额%", "-"),
                 x.get("命中率%", "-")))
    print("   → %s\n" % monotonic([{**x, "平均收益%": x.get("超额%")} for x in st]))

    print("③ 持有期敏感性（同一批样本，5/10/20 日）")
    for x in horizon_table(rows):
        print("   %-4s n=%-6d 原始%+6.2f%% 超额%+6.2f%% 扣费后超额%+6.2f%%"
              % (x["期限"], x["n"], x["原始%"], x["超额%"], x["扣费后超额%"]))
    print()

    print("④ 市场状态细分（震荡市再拆三层）")
    for x in regime_stats(rows):
        print("   %-10s n=%-6d 超额%+6.2f%% 扣费后超额%+6.2f%% 命中%5.1f%%"
              % (x["状态细分"], x["n"], x["超额%"], x["扣费后超额%"], x["命中率%"]))
    print()
    print("④b 分市场状态（当时指数 20 日涨跌）")
    for x in state_stats(rows):
        print("   %-18s n=%-6d 超额%+6.2f%% 扣费后超额%+6.2f%% 命中%5.1f%%"
              % (x["状态"], x["n"], x["超额%"], x["扣费后超额%"], x["命中率%"]))
    print()

    print("⑤ 新特征分层（扣 beta 超额）")
    for label, stats in feature_stats(rows).items():
        print("   %s：" % label)
        for x in stats:
            if not x.get("n"):
                continue
            print("      %-12s n=%-6s 超额%+6s%% 扣费后%+6s%% 命中%5s%%"
                  % (x["档"], x["n"], x.get("超额%"), x.get("扣费超额%"), x.get("命中率%")))
        print("      → %s" % monotonic([{**x, "平均收益%": x.get("超额%")} for x in stats]))

    print("\n⑥ 方向分离")
    for d, name in (("bullish", "看涨"), ("bearish", "看跌")):
        sel = [r for r in rows if r["direction"] == d]
        if not sel:
            continue
        rr = [r["result"].get("end_ret") or 0 for r in sel]
        hh = sum(1 for r in sel if r["result"].get("hit"))
        print("   %s n=%-6d 平均%+6.2f%% 命中%5.1f%%"
              % (name, len(sel), sum(rr) / len(rr), hh / len(sel) * 100))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
