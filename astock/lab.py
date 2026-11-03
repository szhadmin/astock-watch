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
          % (sum(rets) / len(rets), sum(rets) / len(rets) - COST,
             hits / len(rows) * 100))

    print("① 综合评分分层（评分到底有没有区分度？）")
    st = bucket_stats(rows, "score", [(0, 40), (40, 50), (50, 60), (60, 70), (70, 101)])
    for x in st:
        print("   %-8s n=%-5s 平均%+6s%% 扣费后%+6s%% 超额%+6s%% 扣费后超额%+6s%% 命中%5s%%"
              % (x["档"], x.get("n"), x.get("平均收益%", "-"), x.get("扣费后%", "-"),
                 x.get("超额%", "-"), x.get("扣费超额%", "-"), x.get("命中率%", "-")))
    print("   → 单调性：%s\n" % monotonic(st))

    print("② 大盘温度分层")
    st = bucket_stats(rows, "temp", [(0, 25), (25, 40), (40, 60), (60, 75), (75, 101)])
    for x in st:
        print("   %-8s n=%-5s 平均%+6s%% 扣费后%+6s%% 超额%+6s%% 扣费后超额%+6s%% 命中%5s%%"
              % (x["档"], x.get("n"), x.get("平均收益%", "-"), x.get("扣费后%", "-"),
                 x.get("超额%", "-"), x.get("扣费超额%", "-"), x.get("命中率%", "-")))
    print("   → 单调性：%s\n" % monotonic(st))

    print("③ 方向分离（看涨/看跌各自的基准）")
    for d, name in (("bullish", "看涨"), ("bearish", "看跌")):
        sel = [r for r in rows if r["direction"] == d]
        if not sel:
            continue
        rr = [r["result"].get("end_ret") or 0 for r in sel]
        hh = sum(1 for r in sel if r["result"].get("hit"))
        print("   %s n=%-5d 平均%+6.2f%% 命中%5.1f%%"
              % (name, len(sel), sum(rr) / len(rr), hh / len(sel) * 100))
    print()

    print("④ 持有期敏感性（当前只回填 5 日；这是上限估计，非实测）")
    print("   5 日的扣费后基准是 %+.2f%%；若把持有期拉长，费用摊薄但波动放大。"
          % (sum(rets) / len(rets) - COST))
    print("   结论：如果扣费后基准≤0，说明这套'评分→方向'的映射本身没有 alpha，"
          "换持有期也救不回来。")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
