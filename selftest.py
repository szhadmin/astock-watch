# -*- coding: utf-8 -*-
"""告警规则自检：用「注入假价格」的方式验证每一条盯盘规则是否触发。

用法：python selftest.py
不写日志、不响铃、不推送，只在控制台打印结果。
"""
from __future__ import annotations

import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from astock import monitor, notify, store  # noqa: E402
from main import load_config  # noqa: E402

# 静默副作用
store.append_alert = lambda *a, **k: ""
store.append_snapshot = lambda *a, **k: ""
notify.beep = lambda *a, **k: ""
notify.push_webhook = lambda *a, **k: False

HITS = []


def scenario(w, code, price, pct, label):
    st = w.states[code]
    st.price = price
    st.pct = pct
    st.ticks.append((__import__("time").time() - 400, price * 0.99))
    st.ticks.append((__import__("time").time(), price))
    before = w.alert_count
    print("\n--- %s：注入价格 %.2f（%+.2f%%） ---" % (label, price, pct))
    w._check(st)
    fired = [(k, v) for k, v in st.cooldown.items()]
    HITS.append((label, w.alert_count - before, len(fired)))
    st.cooldown.clear()


def main():
    cfg = load_config()
    codes = cfg.get("watchlist") or ["000620"]
    cfg["alerts"]["cooldown_sec"] = 0
    w = monitor.Watcher(cfg)

    for code in codes:
        r = w._refresh_report(code)
        if not r:
            print("跳过 %s：分析失败" % code)
            continue
        lv = r["levels"]
        st = w.states[code]
        print("标的 %s(%s) 现价 %.2f | 支撑 %s | 阻力 %s | 止损 %.2f | 加仓触发 %s"
              % (r["name"], code, r["price"], lv["support"], lv["resistance"],
                 lv["stop"], lv.get("add_trigger")))

        st.stop_alerts = True
        scenarios = [
            (round(lv["support"][0], 2), None, "回踩第一支撑"),
            (round(lv["stop"] - 0.01, 2), -6.0, "跌破止损"),
            (round(lv["resistance"][0] + 0.01, 2), 2.0, "突破第一阻力"),
            (float(lv.get("add_trigger") or lv["resistance"][-1]) + 0.01, 3.0, "触及加仓触发价"),
            (round(lv["targets"][0] + 0.01, 2), 4.0, "到达第一目标位"),
        ]
        for price, pct, label in scenarios:
            scenario(w, code, price, pct if pct is not None else 0.0, label)

    print("\n================ 汇总 ================")
    ok = True
    for label, fired, total in HITS:
        flag = "✔" if fired > 0 else "✘"
        if fired == 0:
            ok = False
        print("  %s %-14s 触发 %d 条告警" % (flag, label, fired))
    print("\n自检%s：告警引擎按预期工作。" % ("通过" if ok else "未完全通过（检查规则）"))


if __name__ == "__main__":
    main()
