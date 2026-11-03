# -*- coding: utf-8 -*-
"""规律发现器：让系统自己从回放样本里找出"什么条件下一类交易有正期望"。

做法（有意做得保守，防止数据挖掘自欺）：
    1. 把样本按日期切分：前 70% 做「发现集」，后 30% 做「验证集」（样本外）；
    2. 遍历条件模板（温度 / 评分 / 板块涨幅 × 小于 / 大于 × 多档阈值 × 方向）；
    3. 发现集上要求：样本≥MIN_TRAIN、命中率≥55%、扣费后净期望≥0.5%；
    4. 验证集上要求：样本≥MIN_TEST 且方向一致（净期望同号），
       通过 → 规则状态「生效」；只过发现集 → 「候选」；
    5. 同时检查反向：如果"跟随"为负而"反向"为正，就产出一条反向规则
       （例如"高评分的票 5 日内平均下跌 → 不追高"）。

产出的规则进 rules.json，与复盘产出的规则同场竞技，同样受升格/淘汰约束。
"""
from __future__ import annotations

import json
import os
from datetime import datetime

from . import rules as rules_mod

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(BASE, "data", "ai", "hypotheses.jsonl")

COST = 0.3
MIN_TRAIN = 60
MIN_TEST = 20
MIN_STOCKS = 25            # 验证通过还要求覆盖足够多股票，防止只靠一两只票
MIN_HIT = 55.0
MIN_NET = 0.4
MIN_EDGE_TRAIN = 0.4       # 相对"全体样本基准"的超额（训练集）
MIN_EDGE_TEST = 0.2        # 相对基准的超额（验证集，必须同向）
MIN_HIT_EDGE = 3.0         # 命中率相对基准至少高 3 个百分点

FIELDS = {
    "temp": ("大盘温度", [15, 20, 25, 30, 40, 50, 60, 70, 80]),
    "score": ("综合评分", [35, 40, 45, 50, 55, 60, 65, 70, 75]),
}


def _load():
    out = []
    if not os.path.exists(PATH):
        return out
    with open(PATH, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def _closed():
    hyps = [h for h in _load() if h.get("result") and (h.get("ctx") or {})]
    hyps.sort(key=lambda h: (h.get("date") or "", h.get("id") or ""))
    return hyps


def _stat(samples):
    if not samples:
        return None
    n = len(samples)
    hits = sum(1 for x in samples if x["result"].get("hit"))
    rets = [x["result"].get("end_ret") or 0 for x in samples]
    avg = sum(rets) / n
    return {"n": n, "hit": round(hits / n * 100, 1), "avg": round(avg, 2),
            "net": round(avg - COST, 2), "stocks": len({x["code"] for x in samples})}


def _match(x, field, op, th, direction=None):
    v = (x.get("ctx") or {}).get(field)
    if v is None:
        return False
    if direction and (x.get("direction") or "") != direction:
        return False
    try:
        v = float(v)
    except (TypeError, ValueError):
        return False
    return v < th if op == "<" else v > th


def discover(verbose=True, dry_run=False):
    hyps = _closed()
    if len(hyps) < MIN_TRAIN + MIN_TEST:
        return {"error": "样本太少（%d），先跑 python main.py replay" % len(hyps)}
    cut = int(len(hyps) * 0.7)
    train, test = hyps[:cut], hyps[cut:]
    base_tr = _stat(train) or {"net": 0, "hit": 0}
    base_te = _stat(test) or {"net": 0, "hit": 0}
    # 方向对齐基准：看涨只能跟"全体看涨"比，看跌只能跟"全体看跌"比
    base_dir = {}
    for d in ("bullish", "bearish"):
        base_dir[("tr", d)] = _stat([x for x in train if (x.get("direction") or "") == d])
        base_dir[("te", d)] = _stat([x for x in test if (x.get("direction") or "") == d])
    found, candidates, scanned = [], [], 0
    for field, (fname, ths) in FIELDS.items():
        for op in ("<", ">"):
            for th in ths:
                for direction, dname in (("bullish", "看涨"), ("bearish", "看跌")):
                    scanned += 1
                    tr = [x for x in train if _match(x, field, op, th, direction)]
                    st_tr = _stat(tr)
                    if not st_tr or st_tr["n"] < MIN_TRAIN:
                        continue
                    te = [x for x in test if _match(x, field, op, th, direction)]
                    st_te = _stat(te)
                    sign = "<" if op == "<" else ">"
                    cond_txt = "当%s%s%s 时按%s方向交易" % (fname, sign, th, dname)
                    b_tr = base_dir.get(("tr", direction)) or {"net": 0, "hit": 0, "n": 1}
                    b_te = base_dir.get(("te", direction)) or {"net": 0, "hit": 0, "n": 1}
                    # 选择性：条件必须真的"筛掉大部分样本"，否则只是换了个名字的基准
                    if b_tr["n"] and st_tr["n"] > 0.5 * b_tr["n"]:
                        continue
                    edge_f_tr = round(st_tr["net"] - b_tr["net"], 2)
                    edge_f_te = (round(st_te["net"] - b_te["net"], 2) if st_te else None)
                    edge_c_tr = round(-st_tr["net"] + b_tr["net"], 2)
                    edge_c_te = (round(-st_te["net"] + b_te["net"], 2) if st_te else None)
                    follow_pos = (edge_f_tr >= MIN_EDGE_TRAIN
                                  and st_tr["hit"] - b_tr["hit"] >= MIN_HIT_EDGE)
                    contra_pos = (edge_c_tr >= MIN_EDGE_TRAIN
                                  and b_tr["hit"] - st_tr["hit"] >= MIN_HIT_EDGE)
                    if follow_pos:
                        validated = bool(st_te and st_te["n"] >= MIN_TEST
                                         and st_te["stocks"] >= MIN_STOCKS
                                         and (edge_f_te or 0) >= MIN_EDGE_TEST)
                        rec = _mk(cond_txt, "买入" if direction == "bullish" else "卖出",
                                  field, op, th, direction, st_tr, st_te, validated)
                        rec["超额%"] = edge_f_tr
                        rec["验证超额%"] = edge_f_te
                        (found if validated else candidates).append(rec)
                    elif contra_pos:
                        ct = "当%s%s%s 时不做%s（反向：该条件下%s 5日内平均%+.2f%%）" % (
                            fname, sign, th, dname, dname, st_tr["avg"])
                        validated = bool(st_te and st_te["n"] >= MIN_TEST
                                         and st_te["stocks"] >= MIN_STOCKS
                                         and (edge_c_te or 0) >= MIN_EDGE_TEST)
                        rec = _mk(ct, "不追" if direction == "bullish" else "空仓",
                                  field, op, th, direction, st_tr, st_te, validated,
                                  inverted=True)
                        rec["超额%"] = edge_c_tr
                        rec["验证超额%"] = edge_c_te
                        (found if validated else candidates).append(rec)
    # 组合条件：温度 × 评分（只测少数关键阈值，避免组合爆炸/过拟合）
    for t_th in (25, 40, 60):
        for s_th in (45, 60, 70):
            for direction, dname in (("bullish", "看涨"), ("bearish", "看跌")):
                scanned += 1
                def _both(x):
                    ctx = x.get("ctx") or {}
                    tv = ctx.get("temp")
                    sv = ctx.get("score")
                    if tv is None or sv is None:
                        return False
                    if (x.get("direction") or "") != direction:
                        return False
                    return float(tv) < t_th and float(sv) >= s_th
                tr = [x for x in train if _both(x)]
                st_tr = _stat(tr)
                if not st_tr or st_tr["n"] < MIN_TRAIN or st_tr["stocks"] < 8:
                    continue
                te = [x for x in test if _both(x)]
                st_te = _stat(te)
                b_tr = base_dir.get(("tr", direction)) or {"net": 0, "hit": 0, "n": 1}
                b_te = base_dir.get(("te", direction)) or {"net": 0, "hit": 0, "n": 1}
                if b_tr["n"] and st_tr["n"] > 0.5 * b_tr["n"]:
                    continue
                e_tr = round(st_tr["net"] - b_tr["net"], 2)
                e_te = round((st_te or {}).get("net", 0) - b_te["net"], 2) if st_te else None
                if e_tr >= MIN_EDGE_TRAIN and st_tr["hit"] - b_tr["hit"] >= MIN_HIT_EDGE:
                    txt = "当大盘温度<%d 且 综合评分≥%d 时按%s方向交易" % (t_th, s_th, dname)
                    ok = bool(st_te and st_te["n"] >= MIN_TEST and st_te["stocks"] >= 10
                              and (e_te or 0) >= MIN_EDGE_TEST)
                    rec = _mk(txt, "买入" if direction == "bullish" else "卖出",
                              "temp", "<", t_th, direction, st_tr, st_te, ok)
                    rec["id"] = "c_t%d_s%d_%s" % (t_th, s_th,
                                                  "bull" if direction == "bullish" else "bear")
                    rec["组合"] = {"temp<": t_th, "score>=": s_th}
                    rec["超额%"] = e_tr
                    rec["验证超额%"] = e_te
                    (found if ok else candidates).append(rec)
    # —— 诊断：把"净期望最强但没通过"的也列出来，便于判断是噪声还是边缘 ——
    diag = []
    for field, (fname, ths) in FIELDS.items():
        for op in ("<", ">"):
            for th in ths:
                for direction, dname in (("bullish", "看涨"), ("bearish", "看跌")):
                    tr = [x for x in train if _match(x, field, op, th, direction)]
                    st = _stat(tr)
                    if not st or st["n"] < 80:
                        continue
                    te = _stat([x for x in test if _match(x, field, op, th, direction)])
                    bd = base_dir.get(("tr", direction)) or {"net": 0, "hit": 0, "n": 1}
                    if bd["n"] and st["n"] > 0.6 * bd["n"]:
                        continue
                    diag.append({"条件": "%s%s%s %s" % (fname, "<" if op == "<" else ">", th, dname),
                                 "超额": round(st["net"] - bd["net"], 2),
                                 "n": st["n"], "命中率": st["hit"], "净": st["net"],
                                 "股票": st["stocks"],
                                 "验证净": (te or {}).get("net"),
                                 "验证n": (te or {}).get("n"),
                                 "验证股票": (te or {}).get("stocks")})
    diag.sort(key=lambda x: -x.get("超额", 0))
    globals()["_LAST_DIAG"] = diag[:12]

    if not dry_run:
        _persist(found + candidates)
    if verbose:
        print("  扫描条件组合 %d 个｜发现集 %d 条 / 验证集 %d 条" %
              (scanned, len(train), len(test)))
        print("  基准（全体样本）：发现集 命中%.1f%% 净%+.2f%%｜验证集 命中%.1f%% 净%+.2f%%"
              % (base_tr["hit"], base_tr["net"], base_te["hit"], base_te["net"]))
        bt = base_dir.get(("tr", "bullish")) or {}
        bb = base_dir.get(("tr", "bearish")) or {}
        vt = base_dir.get(("te", "bullish")) or {}
        vb = base_dir.get(("te", "bearish")) or {}
        print("  方向基准（发现集）：看涨 n=%s 净%+.2f%%｜看跌 n=%s 净%+.2f%%"
              % (bt.get("n"), bt.get("net", 0), bb.get("n"), bb.get("net", 0)))
        print("  方向基准（验证集）：看涨 n=%s 净%+.2f%%｜看跌 n=%s 净%+.2f%%"
              % (vt.get("n"), vt.get("net", 0), vb.get("n"), vb.get("net", 0)))
        print("  → 条件必须与同方向基准比，且必须真的筛掉大部分样本")
        print("\n  通过样本外验证（可升「生效」）：%d 条" % len(found))
        for r in sorted(found, key=lambda x: -(x["净期望%"] or 0))[:10]:
            print("    %s｜发现集 命中%.0f%% 净%+.2f%%（n=%d）｜验证集 命中%.0f%% 净%+.2f%%（n=%d）"
                  % (r["条件"][:44], r["发现集"]["hit"], r["发现集"]["net"], r["发现集"]["n"],
                     (r["验证集"] or {}).get("hit", 0), (r["验证集"] or {}).get("net", 0),
                     (r["验证集"] or {}).get("n", 0)))
        print("\n  仅发现集通过（列「候选」等待更多样本）：%d 条" % len(candidates))
        for r in sorted(candidates, key=lambda x: -(x["净期望%"] or 0))[:6]:
            print("    %s｜命中%.0f%% 净%+.2f%%（n=%d）"
                  % (r["条件"][:50], r["发现集"]["hit"], r["发现集"]["net"], r["发现集"]["n"]))
    dg = globals().get("_LAST_DIAG") or []
    if verbose and dg:
        print("\n  诊断：净期望绝对值最大的条件（含未通过项，用来判断是噪声还是边缘）")
        for x in dg[:8]:
            print("    %-22s n=%-5d 股票%-3d 命中%5.1f%% 超额%+6.2f%%｜验证 n=%-5s 股票%-4s 净%s"
                  % (x["条件"], x["n"], x["股票"], x["命中率"], x["超额"],
                     x["验证n"], x["验证股票"],
                     ("%+.2f%%" % x["验证净"]) if x["验证净"] is not None else "--"))
    return {"scanned": scanned, "found": len(found), "candidates": len(candidates),
            "train": len(train), "test": len(test), "diag": dg[:12]}


def _mk(cond_txt, action, field, op, th, direction, st_tr, st_te, validated, inverted=False):
    return {
        "id": "d_%s%s%s_%s%s" % (field, "lt" if op == "<" else "gt", th,
                                  "bull" if direction == "bullish" else "bear",
                                  "x" if inverted else ""),
        "条件": cond_txt,
        "动作": action,
        "类别": "entry",
        "cond": {"field": field, "op": op, "value": th},
        "可机读": True,
        "方向": direction,
        "反向": inverted,
        "样本数": st_tr["n"] + ((st_te or {}).get("n") or 0),
        "命中": None,
        "命中率": st_tr["hit"],
        "期望收益%": st_tr["avg"],
        "净期望%": st_tr["net"],
        "覆盖股票数": st_tr["stocks"],
        "发现集": st_tr, "验证集": st_te,
        "状态": "生效" if validated else "候选",
        "检验方法": "history_replay",
        "来源": "discover:" + datetime.now().strftime("%Y-%m-%d"),
        "首次出现": datetime.now().strftime("%Y-%m-%d"),
        "最近出现": datetime.now().strftime("%Y-%m-%d"),
        "最近验证": datetime.now().strftime("%Y-%m-%d"),
        "出现次数": 1,
    }


def _persist(recs):
    """写入规则库（同 id 覆盖，保留状态机字段）。"""
    d = rules_mod.load()
    by_id = {r["id"]: i for i, r in enumerate(d["rules"])}
    added = updated = 0
    for rec in recs:
        if rec["id"] in by_id:
            old = d["rules"][by_id[rec["id"]]]
            keep_state = old.get("状态")
            rec["状态"] = "生效" if (rec["状态"] == "生效" or keep_state == "生效") else rec["状态"]
            rec["出现次数"] = int(old.get("出现次数") or 1) + 1
            d["rules"][by_id[rec["id"]]] = rec
            updated += 1
        else:
            d["rules"].append(rec)
            added += 1
    rules_mod.save(d)
    print("\n  规则库：新增 %d 条，更新 %d 条" % (added, updated))


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(json.dumps(discover(), ensure_ascii=False, indent=1))
