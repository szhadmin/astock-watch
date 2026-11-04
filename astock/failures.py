# -*- coding: utf-8 -*-
"""失败档案：每次亏损平仓都归档成一条可统计的"错题"，并自动归因。

为什么单独一个模块：
    lessons.json 是 AI 自己写的自然语言教训（会漂）；
    failures.jsonl 是硬数据（代码写），能统计"哪类操作最容易亏"，
    复盘时把统计喂给 AI，它才可能真正改掉重复犯错。

数据：data/ai/failures.jsonl
"""
from __future__ import annotations

import json
import os
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(BASE, "data", "ai", "failures.jsonl")

# 归因规则（代码判定，不靠模型）
def classify(rec):
    """返回归因标签列表。"""
    tags = []
    entry_pct = rec.get("entry_day_pct")      # 买入当天的涨幅
    hold_days = rec.get("hold_days")
    pnl = rec.get("pnl_pct") or 0
    sector_pct = rec.get("sector_pct")
    temp = rec.get("temp")
    score = rec.get("score")
    if entry_pct is not None and entry_pct >= 5:
        tags.append("追高买入")
    if sector_pct is not None and sector_pct <= -1.5:
        tags.append("买在弱势板块")
    if hold_days is not None and hold_days <= 2 and pnl <= -1:
        tags.append("止损过紧/被震出")
    if hold_days is not None and hold_days >= 15 and pnl <= -3:
        tags.append("持有过久未止损")
    if temp is not None and temp >= 75:
        tags.append("过热时开仓")
    if score is not None and score < 40:
        tags.append("低分强行买入")
    if not tags:
        tags.append("正常止损（无特定过错）")
    return tags


def log_failure(rec, verbose=False):
    """写一条失败记录。rec 至少含 code/pnl_pct。"""
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    rec = dict(rec)
    rec.setdefault("ts", datetime.now().strftime("%Y-%m-%d %H:%M"))
    rec["tags"] = classify(rec)
    with open(PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if verbose:
        print("  ⚠ 失败已归档：%s %s %+.2f%% → %s"
              % (rec.get("code"), rec.get("name") or "", rec.get("pnl_pct") or 0,
                 "/".join(rec["tags"])))
    return rec


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


def stats():
    items = _read()
    if not items:
        return {"n": 0, "tags": {}, "avg_pnl": None}
    tag_count = {}
    for it in items:
        for t in (it.get("tags") or []):
            tag_count[t] = tag_count.get(t, 0) + 1
    pnls = [it.get("pnl_pct") for it in items if it.get("pnl_pct") is not None]
    ordered = sorted(tag_count.items(), key=lambda kv: -kv[1])
    return {"n": len(items),
            "avg_pnl": round(sum(pnls) / len(pnls), 2) if pnls else None,
            "tags": dict(ordered)}


def brief_for_prompt(n=6):
    """给决策/复盘提示词用的紧凑文本：最近失败 + 归因统计 + 明确禁令。"""
    items = _read()
    if not items:
        return "【失败档案】暂无亏损平仓记录。"
    st = stats()
    lines = ["【失败档案】共 %d 笔亏损平仓，平均 %.2f%%" % (st["n"], st["avg_pnl"] or 0)]
    if st["tags"]:
        lines.append("  高频错误原因：" + "、".join(
            "%s×%d" % (k, v) for k, v in list(st["tags"].items())[:5]))
        worst = list(st["tags"].items())[0][0]
        lines.append("  ⚠ 当前最大失分项：「%s」——本轮决策必须主动规避" % worst)
    lines.append("  最近失败案例：")
    for it in items[-n:]:
        lines.append("    %s %s %+.2f%%（持有 %s 天）｜%s"
                     % (it.get("code"), it.get("name") or "", it.get("pnl_pct") or 0,
                        it.get("hold_days", "?"), "/".join(it.get("tags") or [])))
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(json.dumps(stats(), ensure_ascii=False, indent=1))
    print(brief_for_prompt())
