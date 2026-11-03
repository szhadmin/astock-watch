# -*- coding: utf-8 -*-
"""经验规则库（L2）：把 AI 的"教训"变成可证伪、带统计、能升格/淘汰的规则。

设计原则：**LLM 负责提炼，代码负责验证。**
    · AI 在复盘里写的每条教训，都要先过"可证伪性体检"；
    · 只有 `样本≥20 且 跨≥10 只股票 且 命中率>55% 且 扣费后期望>0.3%` 的规则
      才能升格为「生效」，才会进入决策提示词；
    · 期望为负且样本足够的规则 → 「淘汰」，永久不再进提示词（但保留可审计）；
    · 其余一律「候选/观察中」，只记录、不指导交易。

存储：data/ai/rules.json
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AI_DIR = os.path.join(BASE, "data", "ai")
PATH = os.path.join(AI_DIR, "rules.json")

# 升格门槛（硬编码，AI 不能改）
MIN_SAMPLES = 20
MIN_STOCKS = 10
MIN_HIT_RATE = 55.0
MIN_NET_EXPECT = 0.3          # 扣掉 0.3% 来回成本后仍要为正
COST_PCT = 0.3

ACTIONS = ("买入", "卖出", "减仓", "清仓", "加仓", "持有", "不追", "止损", "离场",
           "开仓", "不买", "减半", "退出", "止盈", "空仓", "观望")
VAGUE = ("重视", "注意", "保持", "心态", "纪律", "敬畏", "耐心", "冷静", "理性",
         "严格", "认真", "多做", "少做")
COND_WORDS = ("当", "若", "如果", "在", "一旦", "只要", "除非", "时", "则")

# 可机读字段：温度 / 板块涨幅 / 高开 / RSI / 评分 / 涨幅
FIELD_PATTERNS = [
    ("temp", r"(?:大盘)?温度\s*(?:<=|>=|<|>|≤|≥)\s*(-?\d+(?:\.\d+)?)"),
    ("sector_pct", r"板块(?:涨幅)?\s*(?:<=|>=|<|>|≤|≥)\s*(-?\d+(?:\.\d+)?)\s*%?"),
    ("gap_pct", r"高开\s*(?:<=|>=|<|>|≤|≥)\s*(-?\d+(?:\.\d+)?)\s*%?"),
    ("rsi6", r"RSI\s*6?\s*(?:<=|>=|<|>|≤|≥)\s*(-?\d+(?:\.\d+)?)"),
    ("score", r"评分\s*(?:<=|>=|<|>|≤|≥)\s*(-?\d+(?:\.\d+)?)"),
    ("chg_pct", r"(?:当日)?涨幅\s*(?:<=|>=|<|>|≤|≥)\s*(-?\d+(?:\.\d+)?)\s*%?"),
]
KW_MAP = {"冰点": ("temp", "<", 25), "偏冷": ("temp", "<", 45),
          "过热": ("temp", ">=", 75), "强势板块": ("sector_pct", ">", 1), "弱势板块": ("sector_pct", "<", -1.5)}


def _ensure():
    os.makedirs(AI_DIR, exist_ok=True)


def load():
    _ensure()
    try:
        with open(PATH, encoding="utf-8-sig") as f:
            d = json.load(f)
    except Exception:
        d = {}
    d.setdefault("rules", [])
    d.setdefault("version", 1)
    d.setdefault("updated", "")
    return d


def save(d):
    _ensure()
    d["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    return d


def _norm(text):
    t = re.sub(r"[\s，。；、（）：【】\[\]!！?？,.;:]+", "", str(text or ""))
    return t.lower()


# --------------------------------------------------------------------------- #
# 1) 可证伪性体检
# --------------------------------------------------------------------------- #
def check_verifiable(text):
    """返回 {ok, reason, cond, cond_text}。

    可证伪 = 有数字阈值 + 有条件 + 有可执行动作。只有态度没有阈值的，一律判死。
    """
    t = str(text or "").strip()
    if len(t) < 6:
        return {"ok": False, "reason": "太短，无法判断", "cond": None, "cond_text": ""}
    has_action = any(a in t for a in ACTIONS)
    has_cond_word = any(w in t for w in COND_WORDS)
    num = re.findall(r"-?\d+(?:\.\d+)?\s*%?", t)
    has_num = bool(num)
    vague_only = any(w in t for w in VAGUE) and not has_num
    if not has_action:
        return {"ok": False, "reason": "没有可执行动作（买/卖/减仓/止损…）",
                "cond": None, "cond_text": ""}
    if not has_num:
        return {"ok": False, "reason": "没有数字阈值（多少点/多少%/几天），无法检验",
                "cond": None, "cond_text": ""}
    if vague_only:
        return {"ok": False, "reason": "只有态度词没有量化条件", "cond": None, "cond_text": ""}
    if not has_cond_word:
        return {"ok": False, "reason": "没有触发条件（当/若/一旦…）", "cond": None, "cond_text": ""}
    cond = None
    for field, pat in FIELD_PATTERNS:
        m = re.search(pat, t)
        if m:
            op = "<" if ("<" in m.group(0) or "≤" in m.group(0)) else ">"
            if "≥" in m.group(0) or ">" in m.group(0):
                op = ">"
            try:
                cond = {"field": field, "op": op, "value": float(m.group(1))}
            except Exception:
                cond = None
            break
    if not cond:
        for kw, (field, op, val) in KW_MAP.items():
            if kw in t:
                cond = {"field": field, "op": op, "value": val}
                break
    return {"ok": True, "reason": "可证伪" + ("（可机读）" if cond else "（需人工/回测检验）"),
            "cond": cond, "cond_text": cond and "%s %s %s" % (cond["field"], cond["op"], cond["value"]) or ""}


def _action_of(text):
    for a in ACTIONS:
        if a in str(text):
            return a
    return ""


def _category(text):
    t = str(text)
    if any(k in t for k in ("止损", "离场", "清仓", "减仓", "退出", "止盈")):
        return "exit"
    if any(k in t for k in ("追", "高开", "涨停", "开仓", "买入", "加仓")):
        return "entry"
    if any(k in t for k in ("仓位", "风险", "集中", "满仓", "半仓")):
        return "risk"
    if any(k in t for k in ("时间", "收盘", "开盘", "尾盘", "早盘")):
        return "timing"
    return "other"


# --------------------------------------------------------------------------- #
# 2) 从复盘教训入库（去重合并）
# --------------------------------------------------------------------------- #
def upsert_from_lessons(lessons, source="review"):
    """把 AI 写的教训转成规则；不可证伪的直接丢弃并记录原因。"""
    d = load()
    rules = d["rules"]
    added, rejected, merged = [], [], []
    for raw in (lessons or []):
        if isinstance(raw, dict):
            text = (raw.get("教训") or raw.get("lesson")
                    or raw.get("text") or raw.get("rule") or "")
        else:
            text = raw
        text = str(text or "").strip()
        if not text:
            continue
        chk = check_verifiable(text)
        if not chk["ok"]:
            rejected.append({"text": text[:80], "reason": chk["reason"]})
            continue
        nt = _norm(text)
        dup = None
        for r in rules:
            if _norm(r["条件"]) == nt:
                dup = r
                break
        if dup:
            dup["出现次数"] = int(dup.get("出现次数") or 1) + 1
            dup["最近出现"] = datetime.now().strftime("%Y-%m-%d")
            merged.append(dup["id"])
            continue
        rule = {
            "id": "r_" + uuid.uuid4().hex[:8],
            "条件": text,
            "动作": _action_of(text),
            "类别": _category(text),
            "cond": chk["cond"],
            "可机读": bool(chk["cond"]),
            "样本数": 0, "命中": 0, "命中率": None, "期望收益%": None,
            "净期望%": None, "覆盖股票数": 0,
            "状态": "候选",
            "检验方法": ("hypothesis" if chk["cond"] else "backtest"),
            "来源": source,
            "首次出现": datetime.now().strftime("%Y-%m-%d"),
            "最近出现": datetime.now().strftime("%Y-%m-%d"),
            "最近验证": None,
            "出现次数": 1,
        }
        rules.append(rule)
        added.append(rule["id"])
    save(d)
    return {"added": added, "merged": merged, "rejected": rejected,
            "rules_total": len(rules)}


# --------------------------------------------------------------------------- #
# 3) 用已回填的判断统计规则表现
# --------------------------------------------------------------------------- #
def _match(cond, ctx):
    if not cond or not ctx:
        return False
    v = ctx.get(cond["field"])
    if v is None:
        return False
    try:
        v = float(v)
    except (TypeError, ValueError):
        return False
    return v < cond["value"] if cond["op"] == "<" else v > cond["value"]


def link_from_hypotheses(hypotheses=None, verbose=False):
    """拿已结算的判断样本，算每条可机读规则的 样本/命中率/期望，并升格或淘汰。"""
    from . import hypothesis as hypo
    items = hypotheses if hypotheses is not None else hypo.load_all()
    closed = [x for x in items if x.get("result")]
    d = load()
    changed = []
    for r in d["rules"]:
        if not r.get("cond"):
            continue
        matched = [x for x in closed if _match(r["cond"], (x.get("ctx") or {}))]
        if not matched:
            continue
        hits = sum(1 for x in matched if x["result"].get("hit"))
        rets = [x["result"].get("end_ret") or 0 for x in matched]
        avg = round(sum(rets) / len(rets), 2)
        # 「禁止/不追/空仓」类规则：跟随后应规避亏损，因此命中与收益两个方向都要反转
        forbid = any(k in r["动作"] for k in ("不买", "不追", "禁止", "空仓", "观望"))
        sign = -1.0 if forbid else 1.0
        hits_eff = (len(matched) - hits) if forbid else hits
        ret_eff = round(avg * sign, 2)
        r["样本数"] = len(matched)
        r["命中"] = hits_eff
        r["命中率"] = round(hits_eff / len(matched) * 100, 1)
        r["期望收益%"] = ret_eff
        r["净期望%"] = round(ret_eff - COST_PCT, 2)
        r["覆盖股票数"] = len({x["code"] for x in matched})
        r["最近验证"] = datetime.now().strftime("%Y-%m-%d")
        old = r["状态"]
        if r["样本数"] >= MIN_SAMPLES and r["覆盖股票数"] >= MIN_STOCKS \
                and r["命中率"] >= MIN_HIT_RATE and r["净期望%"] >= MIN_NET_EXPECT:
            r["状态"] = "生效"
        elif r["样本数"] >= MIN_SAMPLES and (r["净期望%"] or 0) < 0:
            r["状态"] = "淘汰"
        elif r["状态"] in ("生效", "淘汰"):
            r["状态"] = "候选"          # 证据不足时降级，避免旧结论长期挂着
        if old != r["状态"]:
            changed.append({"id": r["id"], "from": old, "to": r["状态"],
                            "条件": r["条件"][:50]})
    save(d)
    if verbose:
        for c in changed:
            print("  规则状态变更 %s → %s：%s" % (c["from"], c["to"], c["条件"]))
        print("  统计完成：可机读规则 %d 条，已结算样本 %d 条"
              % (len([x for x in d["rules"] if x.get("cond")]), len(closed)))
    return {"changed": changed, "closed": len(closed)}


# --------------------------------------------------------------------------- #
# 4) 给提示词用的精简视图
# --------------------------------------------------------------------------- #
def active_for_prompt(max_chars=1200):
    """只把「生效」规则给 AI；候选只报数量，不进正文。"""
    d = load()
    active = [r for r in d["rules"] if r["状态"] == "生效"]
    pending = [r for r in d["rules"] if r["状态"] == "候选"]
    lines = []
    used = 0
    for r in active:
        line = "· [%s] %s（样本%d 命中%.0f%% 净期望%+.2f%%）" % (
            r["类别"], r["条件"], r["样本数"], r["命中率"] or 0, r["净期望%"] or 0)
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
    text = "\\n".join(lines) if lines else "（暂无经统计验证的规则，按基础纪律执行）"
    return {"text": text, "active": len(active), "pending": len(pending),
            "note": "候选规则样本不足，不进提示词，避免过拟合"}


def summary():
    d = load()
    rules = d["rules"]
    by = {}
    for r in rules:
        by.setdefault(r["状态"], []).append(r)
    for k in by:
        by[k].sort(key=lambda x: (-(x.get("样本数") or 0), -(x.get("命中率") or 0)))
    return {"version": d["version"], "updated": d.get("updated"),
            "total": len(rules),
            "counts": {k: len(v) for k, v in by.items()},
            "thresholds": {"样本数≥": MIN_SAMPLES, "覆盖股票≥": MIN_STOCKS,
                           "命中率≥": MIN_HIT_RATE, "净期望≥": MIN_NET_EXPECT,
                           "成本假设%": COST_PCT},
            "生效": by.get("生效", []), "候选": by.get("候选", []),
            "淘汰": by.get("淘汰", [])}


def migrate_lessons(lessons, verbose=True):
    """把现有 lessons.json 做一次可证伪性体检并入库。"""
    res = upsert_from_lessons(lessons, source="migrate:lessons.json")
    if verbose:
        print("  入库 %d 条 / 合并 %d 条 / 丢弃 %d 条" %
              (len(res["added"]), len(res["merged"]), len(res["rejected"])))
        for x in res["rejected"]:
            print("    ✘ 丢弃：%s ← %s" % (x["text"][:56], x["reason"]))
    return res


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    s = summary()
    print(json.dumps({k: s[k] for k in ("version", "updated", "total", "counts",
                                        "thresholds")}, ensure_ascii=False, indent=1))
    for st in ("生效", "候选", "淘汰"):
        print("\n[%s] %d 条" % (st, len(s[st])))
        for r in s[st][:12]:
            print("  %s｜样本%s 命中%s%% 净期望%s%%｜%s" %
                  (r["id"], r["样本数"], r.get("命中率"), r.get("净期望%"), r["条件"][:64]))
    print("\n给 AI 的规则（进提示词的部分）：")
    print(active_for_prompt()["text"])
