# -*- coding: utf-8 -*-
"""把信号引擎的结果渲染成中文分析报告（控制台 / Markdown 通用）。"""
from __future__ import annotations

import math

from . import notify
from .notify import color, pct_color


def _f(v, nd=2, dash="--"):
    if v is None:
        return dash
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return dash
    return ("%." + str(nd) + "f") % v


def _bar(score, full=100, width=20):
    filled = int(round(width * score / full))
    return "█" * filled + "░" * (width - filled)


def render_report(r, plain=False):
    """返回字符串；plain=True 时用于 Markdown（无 ANSI 颜色）。"""
    def c(t, name):
        return str(t) if plain else color(t, name)

    L = []
    lv = r["levels"]
    ex = r["extra"]
    sc = r["scores"]
    pos = r["position"]
    pct = r["pct"]
    pct_txt = ("%+.2f%%" % pct) if pct is not None else "--"

    L.append("")
    L.append(c("═" * 74, "grey"))
    title = "  %s (%s)   %s 元   %s" % (r["name"], r["code"], _f(r["price"]), pct_txt)
    L.append(c(title, "bold"))
    sub = "  K线日期 %s%s | 换手 %s%% | 量比 %s | 成交额 %s 亿 | 市值 %s 亿 | PE %s PB %s" % (
        r["bar_date"],
        "" if r["bar_closed"] else "（未收盘）",
        _f(r["turnover"], 2), _f(r["vol_ratio"], 2),
        _f((r["amount"] or 0) / 1e8, 2), _f(r["mktcap_yi"], 1),
        _f(r["pe"], 1), _f(r["pb"], 2))
    L.append(c(sub, "grey"))
    if r.get("indices"):
        env = "  ".join("%s %+.2f%%" % (i["name"], i["pct"]) for i in r["indices"])
        L.append(c("  大盘环境 " + env, "grey"))
    L.append(c("═" * 74, "grey"))

    # ---------- 结论 ----------
    v = r["verdict"]
    from .signals import ACTION_LABEL
    lv_color = {"强势": "red", "偏多": "red", "中性偏弱": "yellow",
                "弱势筑底": "yellow", "偏弱": "green", "弱势": "green",
                "破位": "green"}.get(v["level"], "grey")
    L.append("")
    L.append(c("【%s】 %s" % (v["level"], v["text"]), lv_color))
    L.append(c("        动作指令：%s" % ACTION_LABEL.get(v["action"], v["action"]), "bold"))
    L.append("        综合强度 %d/100  %s" % (sc["total"], _bar(sc["total"])))
    L.append("        分项：趋势 %d/30 | 位置 %d/20 | 动能 %d/20 | 量能 %d/15 | 资金 %d/15"
             % (sc["trend"], sc["position"], sc["momentum"], sc["volume"], sc["capital"]))
    if v.get("right") is not None:
        L.append("        右侧分（能否加仓）%3d  %s" % (v["right"], _bar(v["right"], 100, 16)))
        L.append("        左侧分（值不值得守）%3d  %s" % (v["left"], _bar(v["left"], 100, 16)))

    # ---------- 指标 ----------
    L.append("")
    L.append(c("【技术面】", "bold"))
    ma = lv["ma"]
    L.append("  均线   MA5 %-7s MA10 %-7s MA20 %-7s MA60 %-7s  ATR %s"
             % (_f(ma["5"]), _f(ma["10"]), _f(ma["20"]), _f(ma["60"]), _f(lv["atr"], 3)))
    above = sum(1 for k in ("5", "10", "20", "60") if ma[k] and r["price"] > ma[k])
    L.append("  位置   股价在 %d/4 条均线之上 | 距MA20 %s%% | 近60日回撤 %s%% | 区间分位 %s%%"
             % (above, _f(ex["dev_ma20"], 1), _f(ex["drawdown60"], 1), _f(ex["pct_pos60"], 0)))
    m = ex["macd"]
    L.append("  MACD   DIF %s  DEA %s  柱 %s（前值 %s）%s"
             % (_f(m["dif"], 3), _f(m["dea"], 3), _f(m["hist"], 3), _f(m["prev_hist"], 3),
                c(" 已金叉", "red") if m["cross_up"] else c(" 未金叉", "green")))
    k, d, j = ex["kdj"]
    L.append("  摆动   RSI6 %s  RSI12 %s  KDJ(%s/%s/%s)"
             % (_f(ex["rsi6"], 1), _f(ex["rsi12"], 1), _f(k, 1), _f(d, 1), _f(j, 1)))
    sh = ex["shape"]
    L.append("  形态   实体 %s / 上影 %s / 下影 %s   %s%s%s"
             % (_f(sh["body"], 2), _f(sh["upper"], 2), _f(sh["lower"], 2),
                "阳线" if sh["is_yang"] else "阴线",
                " 十字星" if sh["is_doji"] else "",
                " 长下影" if sh["is_hammer"] else ""))
    if r["weekly_trend"]:
        w = r["weekly_trend"]
        L.append("  周线   %s（周收盘 %s，周MA5 %s，周MA10 %s）"
                 % (w["state"], _f(w["close"]), _f(w["ma5"]), _f(w["ma10"])))

    # ---------- 资金 ----------
    fl = ex["flow"]
    L.append("")
    L.append(c("【资金面】", "bold"))
    L.append("  主力净额  今日 %s 万 | 3日 %s 万 | 5日 %s 万 | 10日 %s 万"
             % (_f(fl["today"] / 1e4, 0), _f(fl["d3"] / 1e4, 0),
                _f(fl["d5"] / 1e4, 0), _f(fl["d10"] / 1e4, 0)))
    L.append("  量能      今日 %s 万手 = 5日均量的 %s 倍"
             % (_f((r["volume"] or 0) / 1e4, 1), _f(ex["vol_ratio5"], 2)))

    # ---------- 价位 ----------
    L.append("")
    L.append(c("【关键价位】", "bold"))
    sup = " / ".join("%.2f" % x for x in lv["support"]) or "--"
    res = " / ".join("%.2f" % x for x in lv["resistance"]) or "--"
    tgt = " → ".join("%.2f" % x for x in lv["targets"]) or "--"
    L.append("  支撑   %s   （近20日区间 %.2f ~ %.2f）" % (sup, lv["low20"], lv["high20"]))
    L.append("  阻力   %s" % res)
    L.append("  止损   %s %s" % (c("%.2f" % lv["stop"], "yellow"),
                                 "（现价已跌破！）" if r["price"] <= lv["stop"] else
                                 "（距现价 %.1f%%）" % ((r["price"] / lv["stop"] - 1) * 100)))
    L.append("  目标   %s" % tgt)
    if lv.get("add_trigger"):
        gap = (float(lv["add_trigger"]) / r["price"] - 1) * 100
        L.append("  加仓触发价  %s（需上涨 %.1f%% 触及）" % (_f(lv["add_trigger"]), gap))

    # ---------- 确认清单 ----------
    L.append("")
    L.append(c("【右侧确认清单】 %d/4 项满足" % r["checklist_ok"], "bold"))
    for item in r["checklist"]:
        mark = c("✔", "red") if item["ok"] else c("✘", "green")
        L.append("  %s %-10s %s" % (mark, item["name"], item["detail"]))

    # ---------- 持仓 ----------
    if pos:
        L.append("")
        L.append(c("【你的持仓】", "bold"))
        L.append("  持仓 %d 股，成本 %.3f，市值 %.2f 元（占总资金 %s%%）"
                 % (pos["shares"], pos["cost"], pos["market_value"], _f(pos["position_pct"], 1)))
        pnl_c = "red" if pos["pnl"] > 0 else "green"
        L.append("  浮动盈亏 %s 元（%s%%）"
                 % (c("%+.2f" % pos["pnl"], pnl_c), c("%+.2f" % pos["pnl_pct"], pnl_c)))
        L.append("  距止损 %.2f：最大亏损 %s 元（占总资金 %s%%）"
                 % (lv["stop"], _f(pos["risk_to_stop"], 0), _f(pos["risk_pct_of_capital"], 2)))
        if pos["max_shares_by_risk"]:
            room = pos["max_shares_by_risk"] - pos["shares"]
            L.append("  风险预算允许的最大持仓 %d 股 → %s"
                     % (pos["max_shares_by_risk"],
                        "还可加 %d 股" % room if room > 0 else "已达上限，禁止加仓"))
        if pos["stop_hit"]:
            L.append(c("  ⚠ 现价已触及止损线，按纪律执行离场", "yellow"))

    # ---------- 判断依据 ----------
    L.append("")
    L.append(c("【判断依据】", "bold"))
    for tag, text in r["notes"]:
        L.append("  · [%s] %s" % (tag, text))

    if r.get("note"):
        L.append("")
        L.append(c("【人工备注】 " + r["note"], "grey"))
    L.append("")
    L.append(c("  数据源：东方财富公开接口 | 生成时间 %s | 仅供研究参考，不构成投资建议"
               % r["generated_at"], "grey"))
    return "\n".join(L)


def render_brief(reports):
    """自选股批量一览表。"""
    rows = ["", "%-8s %-8s %8s %8s %6s %6s %-6s %s" %
            ("代码", "名称", "现价", "涨跌%", "评分", "RSI6", "评级", "动作")]
    rows.append("-" * 74)
    for r in reports:
        if r.get("error"):
            rows.append("%-8s %-8s  数据获取失败: %s" % (r["code"], "", r["error"]))
            continue
        rows.append("%-8s %-8s %8.2f %+8.2f %6d %6s %-8s %s" % (
            r["code"], r["name"], r["price"], r["pct"] or 0,
            r["scores"]["total"], _f(r["extra"]["rsi6"], 1),
            r["verdict"]["level"],
            {"BUY": "加仓/建仓", "BUY_CAUTION": "可建仓勿追高",
             "HOLD_ADD": "持有可加", "HOLD": "持有观察",
             "HOLD_PROBE": "试探仓死守止损", "TRIM": "反弹减仓",
             "EXIT": "离场"}.get(r["verdict"]["action"], "")))
    return "\n".join(rows)


def render_markdown(report):
    """把报告转成 Markdown（用于收盘报告文件）。"""
    plain = render_report(report, plain=True)
    return "# %s (%s) 分析报告\n\n```\n%s\n```\n" % (report["name"], report["code"], plain)


def print_report(code, cfg):
    from . import signals
    r = signals.build_report(code, cfg)
    print(render_report(r))
    try:
        from . import store
        store.append_snapshot(r)
    except Exception:
        pass
    if cfg.get("notify", {}).get("console_beep"):
        notify.beep("info" if r["verdict"]["action"] in ("HOLD", "HOLD_ADD") else "warn")
    return r
