# -*- coding: utf-8 -*-
"""AI 的长期记忆：策略手册（playbook）+ 交易日记（journal）+ 教训库（lessons）+ 决策流水。

「AI 慢慢养成自己的交易风格」的实现方式：
    每天收盘后，AI 读自己的日记与绩效 → 重写 playbook.md → 第二天开盘就按新版手册行动。
    手册有版本号与变更记录，可以回看它是怎么一步步改变风格的。
"""
from __future__ import annotations

import json
import os
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AI_DIR = os.path.join(BASE, "data", "ai")
PLAYBOOK = os.path.join(AI_DIR, "playbook.md")
PLAYBOOK_HISTORY = os.path.join(AI_DIR, "playbook_history")
JOURNAL_DIR = os.path.join(AI_DIR, "journal")
LESSONS = os.path.join(AI_DIR, "lessons.json")
DECISIONS = os.path.join(AI_DIR, "decisions")

SEED_PLAYBOOK = """# 我的交易手册 v1

> 这是我（AI）自己维护的手册。我会在每次收盘复盘后改写它，记录真正奏效与不奏效的做法。

## 一、我相信的市场观（初始版本，待我用数据修正）
1. A 股是政策与情绪驱动的市场，趋势一旦形成会延续，但假突破极多。
2. 散户最常犯的错是「跌了不卖、涨了追高」，所以我要用机械纪律对抗手感。
3. 我宁可错过，不可做错——空仓也是一种仓位。

## 二、我的开仓条件（必须同时满足）
1. 日线站上 MA20，且 MA5 在 MA10 上方（趋势向上）。
2. 有量能配合：当日成交量 ≥ 5 日均量的 1.2 倍，且收阳。
3. 位置不差：距 MA20 偏离 ≤ 5%，近 60 日区间分位 ≤ 80%。
4. 有明确止损位：支撑位下方或 -2.5 ATR，取更近者。

## 三、我的卖出条件（任一触发即执行）
1. 跌破止损：无条件走，不找理由。
2. 趋势破坏：收盘跌破 MA20 且 MACD 绿柱放大。
3. 达到目标：涨幅 ≥ 3 ATR 或到达前高，先减一半。

## 四、我的仓位纪律
1. 单只股票不超过总资金 40%。
2. 单笔最大亏损不超过总资金 1.5%。
3. 连续 3 笔亏损后，仓位减半直到重新盈利。

## 五、我会持续观察的问题
- 我的止损是否太紧，导致被正常波动洗出？
- 我是否在弱势市场里仓位过重？
- 我是否对同一只票重复犯错？

## 六、变更记录
- v1（初始化）：以上为初始框架，尚未经过实盘验证。
"""


def ensure_dirs():
    for d in (AI_DIR, JOURNAL_DIR, DECISIONS, PLAYBOOK_HISTORY):
        os.makedirs(d, exist_ok=True)


def read_playbook():
    ensure_dirs()
    if not os.path.exists(PLAYBOOK):
        with open(PLAYBOOK, "w", encoding="utf-8") as f:
            f.write(SEED_PLAYBOOK)
        _snapshot_playbook(SEED_PLAYBOOK, "init")
        return SEED_PLAYBOOK
    with open(PLAYBOOK, encoding="utf-8-sig") as f:
        return f.read()


def save_playbook(text, tag="update"):
    """AI 重写手册：旧版自动归档，便于回看风格演化。"""
    ensure_dirs()
    if not text or len(text.strip()) < 40:
        return False
    old = None
    if os.path.exists(PLAYBOOK):
        with open(PLAYBOOK, encoding="utf-8-sig") as f:
            old = f.read()
    if old:
        _snapshot_playbook(old, "before_" + tag)
    with open(PLAYBOOK, "w", encoding="utf-8") as f:
        f.write(text)
    _snapshot_playbook(text, tag)
    return True


def _snapshot_playbook(text, tag):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(PLAYBOOK_HISTORY, "%s_%s.md" % (stamp, tag))
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def playbook_versions():
    ensure_dirs()
    return sorted(os.listdir(PLAYBOOK_HISTORY))


def append_journal(text, phase="", date=None):
    ensure_dirs()
    date = date or datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(JOURNAL_DIR, "%s.md" % date)
    header = "" if os.path.exists(path) else "# %s 交易日记\n\n" % date
    with open(path, "a", encoding="utf-8") as f:
        f.write(header)
        f.write("\n## [%s] %s\n\n%s\n" % (datetime.now().strftime("%H:%M"), phase, text.strip()))


def recent_journal(days=5, limit_chars=3000):
    ensure_dirs()
    files = sorted([f for f in os.listdir(JOURNAL_DIR) if f.endswith(".md")])[-days:]
    chunks = []
    for fn in files:
        with open(os.path.join(JOURNAL_DIR, fn), encoding="utf-8-sig") as f:
            chunks.append(f.read()[-1500:])
    return ("\n\n".join(chunks))[-limit_chars:]


def add_lessons(items):
    ensure_dirs()
    data = read_lessons()
    for it in items or []:
        if not it:
            continue
        data.append({"ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
                     "lesson": str(it)[:500]})
    data = data[-200:]
    with open(LESSONS, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    return data


def read_lessons(n=15):
    ensure_dirs()
    if not os.path.exists(LESSONS):
        return []
    try:
        with open(LESSONS, encoding="utf-8-sig") as f:
            data = json.load(f)
        return data[-n:]
    except Exception:
        return []


def log_decision(payload, date=None):
    ensure_dirs()
    date = date or datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(DECISIONS, "%s.jsonl" % date)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return path


def decision_history(days=3):
    ensure_dirs()
    files = sorted([f for f in os.listdir(DECISIONS) if f.endswith(".jsonl")])[-days:]
    out = []
    for fn in files:
        with open(os.path.join(DECISIONS, fn), encoding="utf-8-sig") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    return out
