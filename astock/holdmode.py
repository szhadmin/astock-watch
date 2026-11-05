# -*- coding: utf-8 -*-
"""持有模式（短线 / 中线 / 长线）：用户的策略偏好，由**代码强制**执行。

为什么放在代码里而不只写进提示词：
    · 「持有多久」是回合纪律，不能靠模型每天的心情；
    · 到期必须了结——由代码直接生成卖出计划单，模型输出 HOLD 也会被覆盖；
    · 模式是用户偏好（网页可切换、无需重启），默认短线（小资金快买快卖滚动）。

口径（重要）：
    持有交易日数 = **含买入日在内**、到今天为止的交易日数（买入当天 = 1）。
    上限 N 天时：第 N-1 天收盘生成卖出计划单 → 第 N 天开盘了结，
    实际持有不超过 N 个交易日。周末不计入交易日。

模式差异：
    short 短线：3 个交易日，目标 3%~6%，快进快出，不参与横盘磨；
    mid   中线：10 个交易日，目标 8%~15%；
    long  长线：20 个交易日，目标 15%~30%（回放证据里扣费后超额最高的一档）。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_PATH = os.path.join(BASE, "data", "ai", "hold_mode.json")

DEFAULT = "short"

PRESETS = {
    "short": {
        "label": "短线",
        "max_hold_days": 3,
        "horizon_days": 3,
        "target_hint": "3%~6%",
        "note": "快买快卖：第 3 个交易日之前必须了结，不参与横盘磨，不许把短线拖成中线",
    },
    "mid": {
        "label": "中线",
        "max_hold_days": 10,
        "horizon_days": 10,
        "target_hint": "8%~15%",
        "note": "波段为主：允许一次正常回踩，但不超过 2 周",
    },
    "long": {
        "label": "长线",
        "max_hold_days": 20,
        "horizon_days": 20,
        "target_hint": "15%~30%",
        "note": "趋势跟随：回放证据里扣费后超额最高的档位，但资金占用时间也最长",
    },
}

ORDER = ("short", "mid", "long")


_CACHE = {"ts": None, "mode": None}


def _from_settings():
    """网页切换写的 data/ai/hold_mode.json 优先（config.json 在容器里可能是只读挂载）。"""
    try:
        if not os.path.exists(SETTINGS_PATH):
            return None
        mt = os.path.getmtime(SETTINGS_PATH)
        if _CACHE["ts"] == mt:
            return _CACHE["mode"]
        with open(SETTINGS_PATH, encoding="utf-8-sig") as f:
            m = str((json.load(f) or {}).get("mode") or "").lower()
        m = m if m in PRESETS else None
        _CACHE.update({"ts": mt, "mode": m})
        return m
    except Exception:
        return None


def set_mode(mode):
    """写入当前模式（原子写，落在可写的数据卷里），返回写入后的模式键。"""
    m = str(mode or "").strip().lower()
    if m not in PRESETS:
        raise ValueError("未知模式 %s（可选：%s）" % (mode, "/".join(ORDER)))
    d = os.path.dirname(SETTINGS_PATH)
    os.makedirs(d, exist_ok=True)
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"mode": m,
                   "label": PRESETS[m]["label"],
                   "max_hold_days": PRESETS[m]["max_hold_days"],
                   "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                  f, ensure_ascii=False, indent=1)
    os.replace(tmp, SETTINGS_PATH)
    _CACHE.update({"ts": None, "mode": None})
    return m


def name(cfg=None):
    """当前模式键：settings 文件 > config.json > 默认（短线）。"""
    m = _from_settings()
    if m:
        return m
    ai = (cfg or {}).get("ai") if isinstance(cfg, dict) and "ai" in cfg else (cfg or {})
    m = str((ai or {}).get("hold_mode") or DEFAULT).lower()
    return m if m in PRESETS else DEFAULT


def preset(cfg=None):
    p = dict(PRESETS[name(cfg)])
    p["key"] = name(cfg)
    return p


def label(cfg=None):
    return preset(cfg)["label"]


def max_hold_days(cfg=None):
    return int(preset(cfg)["max_hold_days"])


def options():
    """给前端做切换按钮用。"""
    return [{"key": k, "label": PRESETS[k]["label"],
             "max_hold_days": PRESETS[k]["max_hold_days"],
             "target_hint": PRESETS[k]["target_hint"],
             "note": PRESETS[k]["note"]} for k in ORDER]


def trading_days_held(entry_date, today=None):
    """含买入日在内的持有交易日数（周末与非法日期安全处理）。

    返回 None 表示没有可用的入场日；返回 0 表示入场日在未来（脏数据）。
    """
    raw = str(entry_date or "")[:10]
    if not raw:
        return None
    try:
        d0 = datetime.strptime(raw, "%Y-%m-%d").date()
    except Exception:
        return None
    d1 = (today or datetime.now()).date()
    if d1 < d0:
        return 0
    n = 0
    d = d0
    while d <= d1:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def exit_due(cfg=None, entry_date=None, today=None):
    """是否该排「卖出计划单」：持有已到 N-1 天（次日开盘正好满 N 天）。"""
    held = trading_days_held(entry_date, today)
    if not held:
        return False
    return held >= max(1, max_hold_days(cfg) - 1)


def overdue(cfg=None, entry_date=None, today=None):
    """是否已超期：持有已达 N 天，盘中任何决策都必须卖出。"""
    held = trading_days_held(entry_date, today)
    if not held:
        return False
    return held >= max_hold_days(cfg)


def brief(cfg=None):
    """给提示词用的一段话。"""
    p = preset(cfg)
    return ("【用户持有模式】%s｜单票最多持有 %d 个交易日（%s）。"
            "持有到期由代码强制卖出，你输出 HOLD 也会被覆盖；"
            "买入那一刻就要把「第几天退出」算清楚。止盈参考 %s。"
            % (p["label"], p["max_hold_days"], p["note"], p["target_hint"]))


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    for o in options():
        print(o)
    print(brief())
    for d in ("2026-11-03", "2026-11-04", "2026-11-05"):
        print(d, "→ 持有交易日", trading_days_held(d))
