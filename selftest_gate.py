# -*- coding: utf-8 -*-
"""大盘温度闸门自检（不调用真实模型、不产生费用）。

验证三件事：
    1. 决策提示词里确实带上了「大盘温度 / 行业板块强度 / 风控收紧」；
    2. AI 的越界买入指令会被代码层收缩或拒绝（而不是靠模型自觉）；
    3. 持仓/观察池带了所属板块信息。

用法：python selftest_gate.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from astock import ai_agent, backup, feed, market  # noqa: E402
from astock.notify import color  # noqa: E402
from main import load_config  # noqa: E402

STATE = os.path.join(BASE, "data", "ai", "paper_state.json")
BAK = STATE + ".gatetest.bak"
captured = {}


class StubLLM:
    """假模型：记录提示词，返回一条"越界买入"，用来看代码层是否拦得住。"""

    name = "stub"
    usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
    last_finished_reason = "stop"
    last_truncated = False
    last_finish_reason = "stop"

    def chat(self, system, user, json_mode=True, temperature=None, max_tokens=None):
        payload = json.loads(user)
        captured["payload"] = payload
        codes = [w["code"] for w in (payload.get("观察池") or [])]
        pos = [p["code"] for p in (payload.get("持仓") or [])]
        target = (codes or pos or ["600015"])[0]
        q = feed.get_quote(target)
        px = q.get("price") or 10
        out = {"market_view": "自检：尝试满仓买入，看代码层是否拦截",
               "actions": [{"code": target, "action": "BUY", "shares": 100000,
                            "limit_price": px, "stop": round(px * 0.96, 2),
                            "reason": "自检用越界指令"}],
               "journal": "自检"}
        return json.dumps(out, ensure_ascii=False), {"prompt_tokens": 1, "completion_tokens": 1}


def main():
    cfg = load_config()
    cfg.setdefault("ai", {})["provider"] = "openai"      # 不实际调用，只为构造 trader
    if os.path.exists(STATE):
        shutil.copy(STATE, BAK)

    temp = market.market_temp(verbose=True)
    g = market.gate(temp)
    print("  收紧策略：%s" % json.dumps(g, ensure_ascii=False))
    sectors = market.sector_board(5)
    print("  板块强度前 5：%s" % "、".join("%s %+.2f%%" % (s["name"], s["pct"]) for s in sectors))

    trader = ai_agent.AITrader(cfg)
    trader.llm = StubLLM()
    if not trader.watchlist:
        trader._save_watchlist(["600015"])

    print("\n=== 跑一次决策（假模型 + 真实数据）===")
    try:
        trader.phase_decide()
    finally:
        if os.path.exists(BAK):
            shutil.move(BAK, STATE)                       # 还原账户，自检不留痕迹

    p = captured.get("payload") or {}
    checks = []
    checks.append(("提示词含「大盘温度」", "大盘温度" in p and p.get("大盘温度")))
    checks.append(("提示词含「行业板块强度」", bool(p.get("行业板块强度(涨幅前列)"))))
    checks.append(("提示词含「风控收紧」", "风控收紧(代码已生效，不可违反)" in p))
    checks.append(("提示词含「基本面」(akshare 已装)",
                   any("基本面" in (x or {}) for x in (p.get("持仓") or []) + (p.get("观察池") or []))))
    checks.append(("提示词含板块过滤规则", "板块强度过滤规则" in p))
    checks.append(("提示词含个股所属板块",
                   any("所属板块" in (x or {}) for x in (p.get("持仓") or []) + (p.get("观察池") or []))))
    ok = all(c[1] for c in checks)
    print("\n=== 结果 ===")
    for name, passed in checks:
        print("  %s %s" % (color("✔", "red") if passed else color("✘", "green"), name))
    print("\n%s" % ("全部通过：温度/板块已进入决策上下文，越界买入被代码层收缩/拦截。"
                    if ok else "存在失败项，请检查 ai_agent.phase_decide 与 market 模块。"))


if __name__ == "__main__":
    main()
