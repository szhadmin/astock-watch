# -*- coding: utf-8 -*-
"""每日报告：自动生成 HTML（内嵌 SVG 图）+ Markdown，并归档到按日期分目录。

    python main.py daily                 # 手动生成
    Web「手动操作 → 生成日报」            # 同上
    调度器 schedule.report_at（默认 15:20）# 每天自动生成并归档

产物：
    data/reports/daily_YYYY-MM-DD.md / .html            ← 最新一份
    data/reports/archive/YYYY-MM-DD/                    ← 归档（含各票 SVG）
保留天数由 config.json → report.keep_days 控制（默认 60 天）。
"""
from __future__ import annotations

import os
import shutil
from datetime import datetime

from . import chart_svg, market, memory, paper, signals

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT_DIR = os.path.join(BASE, "data", "reports")
ARCHIVE_DIR = os.path.join(REPORT_DIR, "archive")
STATE_PATH = os.path.join(BASE, "data", "ai", "paper_state.json")
WATCHLIST_PATH = os.path.join(BASE, "data", "ai", "watchlist.json")

CSS = """
body{background:#0b0f16;color:#dfe7f3;font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif;
     margin:0;padding:26px 20px;line-height:1.6}
.wrap{max-width:1080px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px}
h2{font-size:16px;margin:26px 0 8px;padding-bottom:6px;border-bottom:1px solid #1e2839;color:#cfe0f5}
.dim{color:#8a99b0}
.tag{display:inline-block;padding:1px 8px;border-radius:999px;background:#1b2436;
     border:1px solid #1e2839;font-size:12px;margin-right:6px}
.up{color:#e04a4a}.down{color:#3fa66a}
table{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0}
th,td{padding:6px 9px;border-bottom:1px solid #1e2839;text-align:right}
th:first-child,td:first-child{text-align:left}
th{color:#8a99b0;font-weight:500;font-size:12px}
.chart{margin:12px 0 4px;overflow-x:auto}
.foot{margin-top:30px;color:#5c6a80;font-size:12px;border-top:1px solid #1e2839;padding-top:10px}
.kpi{display:flex;gap:26px;flex-wrap:wrap;margin:10px 0 4px}
.kpi div span{color:#8a99b0;font-size:12px;display:block}
.kpi b{font-family:ui-monospace,Consolas,monospace;font-size:18px}
"""


def _load_json(path, default):
    import json
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return default


def _codes(cfg, codes=None):
    out = list(codes or [])
    wl = _load_json(WATCHLIST_PATH, {}) or {}
    out += list(wl.get("codes") or [])
    try:
        b = paper.PaperBroker(STATE_PATH, capital=float(
            (cfg.get("account") or {}).get("total_capital", 15000)))
        out += list(b.positions.keys())
    except Exception:
        pass
    out += list(cfg.get("watchlist") or [])
    seen, uniq = set(), []
    for c in out:
        c = str(c).strip()
        if c and c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq[:6]


def build(codes=None, cfg=None, verbose=True, keep_days=None):
    """生成当日报告（md + html + 归档），返回产物路径与摘要。"""
    cfg = cfg or {}
    today = datetime.now().strftime("%Y-%m-%d")
    os.makedirs(REPORT_DIR, exist_ok=True)
    arch = os.path.join(ARCHIVE_DIR, today)
    os.makedirs(arch, exist_ok=True)

    codes = _codes(cfg, codes)
    if verbose:
        print("生成日报：%s｜标的 %s" % (today, "、".join(codes) or "（无）"))

    # 大盘温度
    try:
        temp = market.market_temp()
        gate = market.gate(temp)
    except Exception as exc:                          # noqa: BLE001
        temp, gate = None, None
        if verbose:
            print("  （大盘温度获取失败：%s）" % exc)
    try:
        sectors = market.sector_board(6)
    except Exception:
        sectors = []

    # 账户
    acct = None
    try:
        b = paper.PaperBroker(STATE_PATH, capital=float(
            (cfg.get("account") or {}).get("total_capital", 15000)))
        snap = b.mark({})
        acct = {"equity": snap["equity"], "cash": snap["cash"],
                "mv": snap["market_value"], "ret": snap["total_return_pct"],
                "positions": snap["positions"], "stats": b.stats()}
    except Exception:
        pass

    blocks, md_parts, codes_ok = [], [], []
    md_parts.append("# 收盘报告 %s\n" % today)
    if temp:
        md_parts.append("大盘温度 **%d（%s）** ｜ %s\n"
                        % (temp["temp"], temp["level"], gate["note"] if gate else ""))
    for code in codes:
        try:
            r = signals.build_report(code, cfg)
        except Exception as exc:                      # noqa: BLE001
            if verbose:
                print("  [%s] 分析失败：%s" % (code, exc))
            md_parts.append("\n## %s\n\n数据获取失败：%s\n" % (code, exc))
            continue
        codes_ok.append(code)
        svg_path = os.path.join(arch, "%s_kline.svg" % code)
        try:
            svg = chart_svg.build_for_code(code, r.get("name") or "")
            with open(svg_path, "w", encoding="utf-8") as f:
                f.write(svg)
        except Exception as exc:                      # noqa: BLE001
            svg = ""
            if verbose:
                print("  [%s] 画图失败：%s" % (code, exc))
        lv = r["levels"]
        v = r["verdict"]
        md_parts.append(
            "\n## %s %s　%s 元　%+.2f%%\n\n"
            "- 结论：**%s**（%s）｜动作：`%s`\n"
            "- 评分：综合 %d（趋势 %d/30 · 位置 %d/20 · 动能 %d/20 · 量能 %d/15 · 资金 %d/15）\n"
            "- 右侧 %s ｜ 左侧 %s ｜ 确认清单 %d/4\n"
            "- 支撑 %s｜阻力 %s｜止损 %s｜目标 %s\n"
            % (r["name"], code, r["price"], r["pct"] or 0, lv and v["level"], v["text"],
               v["action"], r["scores"]["total"], r["scores"]["trend"], r["scores"]["position"],
               r["scores"]["momentum"], r["scores"]["volume"], r["scores"]["capital"],
               v.get("right"), v.get("left"), r["checklist_ok"],
               "/".join("%.2f" % x for x in lv["support"]),
               "/".join("%.2f" % x for x in lv["resistance"]),
               "%.2f" % lv["stop"],
               " → ".join("%.2f" % x for x in lv["targets"])))
        if svg:
            md_parts.append("![%s K线](%s_kline.svg)\n" % (code, code))
        # HTML 区块
        cls = "up" if (r["pct"] or 0) > 0 else ("down" if (r["pct"] or 0) < 0 else "dim")
        rows = "".join(
            "<tr><td>%s</td><td>%.2f</td><td>%.2f</td><td>%.2f</td><td class='%s'>%+.2f%%</td></tr>"
            % (x["d"], x["o"], x["h"], x["l"], "up" if x["c"] >= x["o"] else "down",
               (x["c"] / x["o"] - 1) * 100 if x["o"] else 0) for x in
            [{"d": b_["date"], "o": b_["open"], "h": b_["high"],
              "l": b_["low"], "c": b_["close"]} for b_ in []])   # 占位，不需明细表
        blocks.append("""
        <h2>%s <span class="dim">%s</span>
          <span class="%s" style="font-size:16px">%.2f　%+.2f%%</span></h2>
        <div><span class="tag">%s</span><span class="tag">%s</span>
          <span class="tag">综合 %d</span><span class="tag">右侧 %s / 左侧 %s</span>
          <span class="tag">确认 %d/4</span></div>
        <div class="chart">%s</div>
        <table><tr><th>止损</th><th>第一目标</th><th>支撑</th><th>阻力</th></tr>
          <tr><td class="dim">%.2f</td><td>%s</td><td class="dim">%s</td>
              <td class="dim">%s</td></tr></table>
        """ % (r["name"], code, cls, r["price"], r["pct"] or 0,
               v["level"], v["text"][:60], r["scores"]["total"],
               v.get("right"), v.get("left"), r["checklist_ok"], svg or "",
               lv["stop"], "%.2f" % lv["targets"][0] if lv["targets"] else "--",
               " / ".join("%.2f" % x for x in lv["support"]),
               " / ".join("%.2f" % x for x in lv["resistance"])))

    # 账户与温度区块
    head_blocks = []
    if acct:
        stats = acct["stats"]
        head_blocks.append("""
        <div class="kpi">
          <div><span>AI 模拟盘权益</span><b class="%s">%.2f</b></div>
          <div><span>累计收益</span><b class="%s">%+.2f%%</b></div>
          <div><span>现金 / 持仓市值</span><b>%.0f / %.0f</b></div>
          <div><span>已平仓 / 胜率 / 盈亏比</span><b>%d / %.1f%% / %s</b></div>
        </div>""" % ("up" if acct["ret"] > 0 else "down", acct["equity"],
                     "up" if acct["ret"] > 0 else "down", acct["ret"],
                     acct["cash"], acct["mv"], stats["closed_trades"],
                     stats["win_rate_pct"], stats["profit_factor"] or "--"))
        if acct["positions"]:
            rows = "".join(
                "<tr><td>%s <span class='dim'>%s</span></td><td>%d</td><td>%.3f</td>"
                "<td>%.2f</td><td class='%s'>%+.2f%%</td></tr>"
                % (p.get("name") or "", p["code"], p["shares"], p["cost"], p["price"],
                   "up" if p["pnl_pct"] > 0 else "down", p["pnl_pct"])
                for p in acct["positions"])
            head_blocks.append("<table><tr><th>持仓</th><th>股数</th><th>成本</th>"
                               "<th>现价</th><th>浮盈</th></tr>%s</table>" % rows)
    if temp:
        d = temp.get("detail") or {}
        head_blocks.append(
            '<div><span class="tag">大盘温度 %d（%s）</span>'
            '<span class="tag">指数平均 %+.2f%%</span>'
            '<span class="tag">上涨占比 %.1f%%</span>'
            '<span class="tag">涨停/跌停 %s</span>'
            '<span class="tag">%s</span></div>'
            % (temp["temp"], temp["level"], d.get("指数平均涨跌%", 0),
               d.get("上涨家数占比%", 0), d.get("涨停/跌停", "-"), d.get("上证趋势", "")))
    if sectors:
        head_blocks.append(
            '<div style="margin-top:6px">%s</div>' % "".join(
                '<span class="tag">%s <span class="%s">%+.2f%%</span></span>'
                % (s["name"], "up" if s["pct"] > 0 else "down", s["pct"])
                for s in sectors))

    html = ("<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
            "<title>收盘报告 %s</title><style>%s</style><div class='wrap'>"
            "<h1>收盘报告　%s</h1>"
            "<div class='dim'>数据源：东方财富公开接口 · 生成于 %s</div>"
            "%s%s<div class='foot'>本报告由 astock-watch 自动生成，仅用于技术研究，不构成投资建议。</div>"
            "</div></html>"
            % (today, CSS, today, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "".join(head_blocks), "".join(blocks)))

    md_path = os.path.join(REPORT_DIR, "daily_%s.md" % today)
    html_path = os.path.join(REPORT_DIR, "daily_%s.html" % today)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md_parts))
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    # 归档
    shutil.copy2(md_path, os.path.join(arch, os.path.basename(md_path)))
    shutil.copy2(html_path, os.path.join(arch, os.path.basename(html_path)))

    keep = int(keep_days or ((cfg.get("report") or {}).get("keep_days") or 60))
    removed = _prune(keep)
    if verbose:
        print("  MD  : %s" % md_path)
        print("  HTML: %s" % html_path)
        print("  归档: %s（保留 %d 天，本次清理 %d 个旧目录）" % (arch, keep, removed))
    return {"date": today, "md": md_path, "html": html_path, "archive": arch,
            "codes": codes_ok, "pruned": removed, "temp": temp}


def _prune(keep_days):
    if not os.path.isdir(ARCHIVE_DIR):
        return 0
    dirs = sorted(d for d in os.listdir(ARCHIVE_DIR)
                  if os.path.isdir(os.path.join(ARCHIVE_DIR, d)))
    removed = 0
    for d in dirs[:-keep_days] if keep_days > 0 else []:
        try:
            shutil.rmtree(os.path.join(ARCHIVE_DIR, d), ignore_errors=True)
            removed += 1
        except Exception:
            pass
    return removed


def archive_list(limit=30):
    if not os.path.isdir(ARCHIVE_DIR):
        return []
    out = []
    for d in sorted(os.listdir(ARCHIVE_DIR), reverse=True)[:limit]:
        p = os.path.join(ARCHIVE_DIR, d)
        if not os.path.isdir(p):
            continue
        html = os.path.join(p, "daily_%s.html" % d)
        out.append({"date": d, "html": html if os.path.exists(html) else None,
                    "files": len(os.listdir(p))})
    return out
