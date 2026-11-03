# -*- coding: utf-8 -*-
"""连续加权仓位：把"评分 / 评分变化率"映射成单股上限，替代"过/不过"的开关式判断。

依据（29,059 条回放样本，扣 beta、扣费后 5 日超额）：
    评分   0~40 → +0.11%｜40~50 → +0.38%｜60~70 → +0.54%｜70+ → +0.65%
    变化率 <−5  → +0.01%｜0~5   → +0.37%｜>+5  → +0.53%
即：信号越强、且正在变强，才配更多仓位；弱信号与小仓位挂钩，而不是一刀切禁止。
"""
from __future__ import annotations

# 评分档 → 基础系数
SCORE_BANDS = [(40, 0.50), (50, 0.70), (60, 0.90), (70, 1.00), (999, 1.10)]
# 评分变化率 → 修正项
CHG_UP = 5.0
CHG_DOWN = -5.0
ADJ_UP = 0.15
ADJ_DOWN = -0.20

FLOOR = 0.40
CEIL = 1.25


def factor(score=None, score_chg=None):
    """返回 0.4~1.25 的仓位系数（1.0 = 按上限满配）。"""
    if score is None:
        base = 0.80
    else:
        base = 1.10
        for th, f in SCORE_BANDS:
            if score < th:
                base = f
                break
    adj = 0.0
    if score_chg is not None:
        if score_chg >= CHG_UP:
            adj = ADJ_UP
        elif score_chg <= CHG_DOWN:
            adj = ADJ_DOWN
    return round(max(FLOOR, min(CEIL, base + adj)), 2)


def cap_pct(score=None, score_chg=None, gate_cap=None, rails_cap=40.0):
    """单股上限(%) = min(闸门上限, 风控上限) × 系数。"""
    base = min(float(gate_cap or rails_cap), float(rails_cap))
    return round(base * factor(score, score_chg), 1)


def explain(score=None, score_chg=None):
    """给 AI 与日志看的一句话解释。"""
    f = factor(score, score_chg)
    tone = ("强信号" if f >= 1.0 else ("中性" if f >= 0.8 else "弱信号"))
    extra = ""
    if score_chg is not None:
        extra = "，评分变化 %+.1f（%s）" % (
            score_chg, "正在转强" if score_chg >= CHG_UP
            else ("正在走弱" if score_chg <= CHG_DOWN else "基本持平"))
    return "评分%s%s → 仓位系数 %.2f（%s）" % (
        ("%.0f" % score) if score is not None else "未知", extra, f, tone)
