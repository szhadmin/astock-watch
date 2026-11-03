# -*- coding: utf-8 -*-
"""L4 手册版本 A/B + L5 错题本。

L4：每次 AI 重写策略手册都留一版；按"该版本生效期间产生的判断表现"给版本打分，
    新版明显差于旧版时给出回滚建议（并可一键回滚）。

L5：从已回填的判断里挑出"最典型的错"，做成错题本喂给复盘，
    要求 AI 归因到：方向错 / 时机错 / 失效条件设错 / 运气。错题本的归因结论落盘累积。
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AI_DIR = os.path.join(BASE, "data", "ai")
VER_DIR = os.path.join(AI_DIR, "playbook_versions")
VER_INDEX = os.path.join(AI_DIR, "playbook_versions.json")
MISTAKES = os.path.join(AI_DIR, "mistakes.json")
HYPO = os.path.join(AI_DIR, "hypotheses.jsonl")

MIN_SAMPLES_AB = 15            # 评价一个版本至少需要的判断样本
ROLLBACK_GAP = 0.5             # 新版净期望低于旧版这么多（%）就建议回滚


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


# --------------------------------------------------------------------------- #
# L4：手册版本
# --------------------------------------------------------------------------- #
def snapshot_playbook(text, tag="update"):
    """记录一版手册（内容完全相同则不重复记录）。返回版本信息。"""
    text = (text or "").strip()
    if not text:
        return None
    os.makedirs(VER_DIR, exist_ok=True)
    idx = _load_json(VER_INDEX, {"versions": []})
    digest = hashlib.md5(text.encode("utf-8")).hexdigest()[:10]
    for v in idx["versions"]:
        if v.get("hash") == digest and v.get("active", True):
            return v
    no = len(idx["versions"]) + 1
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    fn = "v%03d_%s.md" % (no, ts)
    with open(os.path.join(VER_DIR, fn), "w", encoding="utf-8") as f:
        f.write(text)
    for v in idx["versions"]:
        v["active"] = False
    rec = {"version": "v%03d" % no, "tag": tag, "hash": digest,
           "chars": len(text), "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
           "since": datetime.now().strftime("%Y-%m-%d"), "file": fn, "active": True}
    idx["versions"].append(rec)
    _save_json(VER_INDEX, idx)
    return rec


def _hypotheses():
    out = []
    if not os.path.exists(HYPO):
        return out
    try:
        with open(HYPO, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        continue
    except Exception:
        pass
    return out


def ab_report(min_samples=MIN_SAMPLES_AB):
    """按版本生效区间统判断表现，给出 A/B 对比与回滚建议。"""
    idx = _load_json(VER_INDEX, {"versions": []})
    versions = idx.get("versions") or []
    hyps = [h for h in _hypotheses() if h.get("result")]
    rows = []
    for i, v in enumerate(versions):
        start = v.get("since") or ""
        end = versions[i + 1].get("since") if i + 1 < len(versions) else None
        seg = [h for h in hyps if (h.get("date") or "") >= start
               and (end is None or (h.get("date") or "") < end)]
        real = [h for h in seg if (h.get("source") or "decide") == "decide"]
        if not seg:
            rows.append({**v, "samples": 0, "verdict": "无样本"})
            continue
        hits = sum(1 for h in seg if h["result"].get("hit"))
        rets = [h["result"].get("end_ret") or 0 for h in seg]
        avg = round(sum(rets) / len(rets), 2)
        rows.append({**v, "samples": len(seg), "real_samples": len(real),
                     "hit_rate_pct": round(hits / len(seg) * 100, 1),
                     "avg_ret_pct": avg, "net_pct": round(avg - 0.3, 2),
                     "verdict": "有效" if (len(seg) >= min_samples and avg - 0.3 > 0)
                                else ("样本不足" if len(seg) < min_samples else "表现不佳")})
    suggestion = {"action": "keep", "reason": "样本不足，继续观察"}
    scored = [r for r in rows if r.get("samples", 0) >= min_samples]
    if len(scored) >= 2:
        newest, prev = scored[-1], scored[-2]
        if (newest.get("net_pct") or 0) < (prev.get("net_pct") or 0) - ROLLBACK_GAP:
            suggestion = {"action": "rollback", "target": prev["version"],
                          "reason": "新版(%s)净期望 %+.2f%%，明显低于上一版(%s) %+.2f%%，建议回滚"
                                    % (newest["version"], newest.get("net_pct") or 0,
                                       prev["version"], prev.get("net_pct") or 0)}
        else:
            suggestion = {"action": "keep",
                          "reason": "新版(%s)净期望 %+.2f%%，不差于上一版，保留"
                                    % (newest["version"], newest.get("net_pct") or 0)}
    return {"versions": rows, "suggestion": suggestion, "min_samples": min_samples,
            "active": next((v["version"] for v in versions if v.get("active")), None)}


def rollback(version=None, verbose=True):
    """回滚到指定版本（缺省=回滚到上一版）。"""
    idx = _load_json(VER_INDEX, {"versions": []})
    versions = idx.get("versions") or []
    if len(versions) < 2:
        return False, "只有一个版本，无需回滚"
    target = None
    if version:
        target = next((v for v in versions if v["version"] == version), None)
    else:
        act = next((i for i, v in enumerate(versions) if v.get("active")), len(versions) - 1)
        target = versions[act - 1] if act > 0 else None
    if not target:
        return False, "找不到目标版本"
    p = os.path.join(VER_DIR, target["file"])
    if not os.path.isfile(p):
        return False, "版本文件缺失：%s" % target["file"]
    with open(p, encoding="utf-8") as f:
        text = f.read()
    from . import memory
    memory.save_playbook(text, tag="rollback-to-" + target["version"])
    snapshot_playbook(text, tag="rollback-from-manual")
    if verbose:
        print("已回滚到 %s（%s，%d 字）" % (target["version"], target["saved_at"], target["chars"]))
    return True, "已回滚到 %s" % target["version"]


# --------------------------------------------------------------------------- #
# L5：错题本
# --------------------------------------------------------------------------- #
CAUSES = ("方向错", "时机错", "失效条件设错", "运气")


def pick_mistakes(limit=3, include_replay=False, exclude_done=True):
    """挑最典型的错误判断：先破失效条件的优先，其次亏损幅度大的。"""
    done = set()
    if exclude_done:
        for m in (_load_json(MISTAKES, {}).get("items") or []):
            done.add(m.get("id"))
    fails = [h for h in _hypotheses()
             if h.get("result") and not h["result"].get("hit")
             and (include_replay or (h.get("source") or "decide") == "decide")]
    fails = [h for h in fails if h.get("id") not in done]
    fails.sort(key=lambda h: (h["result"].get("invalidated") is None,
                              h["result"].get("end_ret") or 0))
    out = []
    for h in fails[:limit]:
        r = h["result"]
        out.append({
            "id": h.get("id"), "date": h.get("date"),
            "标的": "%s %s" % (h.get("code"), h.get("name") or ""),
            "我的判断": {"bullish": "看涨", "bearish": "看跌", "hold": "持有"}.get(
                h.get("direction"), h.get("direction")),
            "当时依据": (h.get("reason") or "")[:90],
            "失效价": h.get("invalidate"),
            "实际结果": "%s｜%d日 %+.2f%%（期间最高 %+.2f%% / 最低 %+.2f%%）"
                        % (r.get("verdict"), r.get("days") or 0, r.get("end_ret") or 0,
                           r.get("max_gain") or 0, r.get("max_drawdown") or 0),
            "归因(需AI回答)": "四选一：方向错 / 时机错 / 失效条件设错 / 运气",
        })
    return out


def save_mistakes(causes=None, verbose=False):
    """落盘错题本归因（causes: [{id, 归因, 说明}]）。"""
    if not causes:
        return 0
    d = _load_json(MISTAKES, {"items": []})
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    n = 0
    for c in causes:
        cid = c.get("id") or c.get("编号")
        if not cid:
            continue
        rec = {"id": cid, "ts": ts, "归因": c.get("归因") or c.get("cause") or "",
               "说明": c.get("说明") or c.get("reason") or ""}
        if rec["归因"] not in CAUSES:
            rec["归因"] = rec["归因"] or "未分类"
        d["items"].append(rec)
        n += 1
    d["updated"] = ts
    _save_json(MISTAKES, d)
    if verbose:
        print("  错题本已记录 %d 条归因" % n)
    return n


def mistakes_summary():
    d = _load_json(MISTAKES, {"items": []})
    items = d.get("items") or []
    by = {}
    for m in items:
        by[m.get("归因") or "未分类"] = by.get(m.get("归因") or "未分类", 0) + 1
    return {"total": len(items), "by_cause": by, "updated": d.get("updated"),
            "recent": items[-8:], "pending": pick_mistakes(3)}


def summary():
    ab = ab_report()
    ms = mistakes_summary()
    return {"playbook_ab": ab, "mistakes": ms}
