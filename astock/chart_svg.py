# -*- coding: utf-8 -*-
"""报告用 K 线图：纯 SVG 手绘（零依赖），可直接嵌进 Markdown / HTML。

为什么不强制用 matplotlib：本项目主张"克隆下来就能跑"，SVG 不需要任何第三方库，
在浏览器、GitHub 预览、Word/PDF 转换里都能正常显示。
（若安装了 matplotlib，可用 --png 另存一份位图，见 chart_svg.render_png）
"""
from __future__ import annotations

import os
from datetime import datetime

from . import indicators as ind

UP, DOWN, GRID, TXT = "#d9534f", "#2e9e63", "#e8ecf3", "#8a94a6"


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def kline_svg(bars, ma=None, markers=None, width=980, height=430,
              title="", sub="", dark=False):
    """生成 K 线 SVG 字符串。

    bars: [{date,open,high,low,close,vol}]（升序）
    ma:   {"ma5": [...], "ma10": [...], "ma20": [...], "ma60": [...]}
    markers: [{"date","type":"buy|sell|plan","price","text"}]
    """
    if not bars:
        return "<svg></svg>"
    n = len(bars)
    L, R, T = 8, 62, 30
    main_h, gap, vol_h, axis_h = 300, 14, 70, 20
    H = T + main_h + gap + vol_h + axis_h
    W = width
    plot_w = W - L - R
    step = plot_w / n
    bw = max(1.2, min(9, step * 0.62))

    hi = max(b["high"] for b in bars)
    lo = min(b["low"] for b in bars)
    for key, arr in (ma or {}).items():
        for v in arr or []:
            if v:
                hi = max(hi, v)
                lo = min(lo, v)
    pad = (hi - lo) * 0.06 or 0.05
    hi += pad
    lo -= pad
    X = lambda i: L + i * step + step / 2
    Y = lambda v: T + (hi - v) / (hi - lo) * main_h
    vmax = max(1, max(b["vol"] for b in bars))
    vol_base = T + main_h + gap + vol_h

    p = []
    bg = "#0b0f16" if dark else "#ffffff"
    fg = "#dfe7f3" if dark else "#222831"
    p.append('<rect x="0" y="0" width="%d" height="%d" fill="%s"/>' % (W, H, bg))
    if title:
        p.append('<text x="%d" y="18" font-size="13" font-weight="700" fill="%s">%s</text>'
                 % (L, fg, _esc(title)))
    if sub:
        p.append('<text x="%d" y="18" font-size="11" fill="%s" text-anchor="end">%s</text>'
                 % (W - R, "#8a94a6", _esc(sub)))

    # 网格与价格轴
    for i in range(5):
        v = hi - (hi - lo) * i / 4
        y = Y(v)
        p.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1"/>'
                 % (L, y, W - R, y, GRID))
        p.append('<text x="%d" y="%.1f" font-size="10" fill="%s">%.2f</text>'
                 % (W - R + 5, y + 3, TXT, v))

    # 蜡烛 + 量
    for i, b in enumerate(bars):
        x = X(i)
        up = b["close"] >= b["open"]
        color = UP if up else DOWN
        p.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="1"/>'
                 % (x, Y(b["high"]), x, Y(b["low"]), color))
        y1, y2 = Y(max(b["open"], b["close"])), Y(min(b["open"], b["close"]))
        p.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s"/>'
                 % (x - bw / 2, y1, bw, max(1, y2 - y1), color))
        vh = b["vol"] / vmax * vol_h
        p.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" opacity="0.45"/>'
                 % (x - bw / 2, vol_base - vh, bw, max(0.5, vh), color))

    # 均线
    ma_colors = {"ma5": "#e8a33d", "ma10": "#3fa7d6", "ma20": "#b06ad9", "ma60": "#8a94a6"}
    for key, arr in (ma or {}).items():
        if not arr:
            continue
        d, started = "", False
        for i, v in enumerate(arr[:n]):
            if not v:
                started = False
                continue
            d += ("L" if started else "M") + "%.1f %.1f " % (X(i), Y(v))
            started = True
        if d:
            p.append('<path d="%s" fill="none" stroke="%s" stroke-width="1.2" opacity="0.9"/>'
                     % (d.strip(), ma_colors.get(key, "#999")))

    # 标记（买点/卖点/计划单）
    idx_by_date = {b["date"]: i for i, b in enumerate(bars)}
    for m in (markers or []):
        i = idx_by_date.get(m.get("date"))
        if i is None:
            continue
        x = X(i)
        if m["type"] == "buy":
            y = Y(bars[i]["low"]) + 10
            p.append('<path d="M%.1f %.1f L%.1f %.1f L%.1f %.1f Z" fill="%s">'
                     '<title>%s</title></path>'
                     % (x - 4, y + 8, x + 4, y + 8, x, y, UP, _esc(m.get("text", ""))))
        elif m["type"] == "sell":
            y = Y(bars[i]["high"]) - 10
            p.append('<path d="M%.1f %.1f L%.1f %.1f L%.1f %.1f Z" fill="%s">'
                     '<title>%s</title></path>'
                     % (x - 4, y - 8, x + 4, y - 8, x, y, DOWN, _esc(m.get("text", ""))))
        else:                                        # plan
            p.append('<circle cx="%.1f" cy="%.1f" r="6" fill="none" stroke="#e8a33d" '
                     'stroke-width="1.4" stroke-dasharray="3 2"><title>%s</title></circle>'
                     % (x, Y(m.get("price") or bars[i]["close"]), _esc(m.get("text", ""))))

    # 日期轴
    every = max(1, n // 6)
    for i, b in enumerate(bars):
        if i % every and i != n - 1:
            continue
        p.append('<text x="%.1f" y="%d" font-size="10" fill="%s" text-anchor="middle">%s</text>'
                 % (X(i), H - 4, TXT, _esc(str(b["date"])[5:])))

    # 图例
    lx = L
    for key in ("ma5", "ma10", "ma20", "ma60"):
        if (ma or {}).get(key):
            p.append('<rect x="%d" y="%d" width="10" height="3" fill="%s"/>' % (lx, 26, ma_colors[key]))
            p.append('<text x="%d" y="%d" font-size="10" fill="%s">%s</text>'
                     % (lx + 13, 30, TXT, key.upper()))
            lx += 52
    p.append('<text x="%d" y="30" font-size="10" fill="%s">▲买 ▼卖 ○计划单</text>'
             % (lx + 6, TXT))

    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" '
            'font-family="Segoe UI,Microsoft YaHei,sans-serif">%s</svg>'
            % (W, H, W, H, "".join(p)))


def build_for_code(code, name="", days=120, trades=None, plans=None, dark=False):
    """抓数据 → 生成 SVG 字符串（供报告使用）。"""
    from . import feed
    bars = feed.get_kline(code, 101, limit=days + 70, adjust=1)[-days:]
    closes = [b["close"] for b in feed.get_kline(code, 101, limit=days + 70, adjust=1)]
    ma = {}
    for n_ in (5, 10, 20, 60):
        series = ind.sma(closes, n_)
        ma["ma%d" % n_] = series[-days:]
    markers = []
    for t in (trades or []):
        if t.get("code") and t["code"] != code:
            continue
        markers.append({"date": (t.get("ts") or "")[:10],
                        "type": "buy" if t.get("side") == "BUY" else "sell",
                        "text": "%s %s股 @%s" % (t.get("ts", ""), t.get("shares"),
                                                t.get("price"))})
    for pl in (plans or []):
        if pl.get("code") and pl["code"] != code:
            continue
        markers.append({"date": pl.get("signal_date"), "type": "plan",
                        "price": pl.get("signal_price"),
                        "text": "计划单 %s %s股 @%s" % (pl.get("action"), pl.get("shares"),
                                                     pl.get("signal_price"))})
    last = bars[-1]
    chg = (last["close"] / bars[-2]["close"] - 1) * 100 if len(bars) > 1 else 0
    sub = "%.2f  %+.2f%%  %s" % (last["close"], chg, datetime.now().strftime("%Y-%m-%d"))
    return kline_svg(bars, ma, markers, title="%s %s" % (name or "", code), sub=sub, dark=dark)


def save_svg(code, name="", path=None, **kw):
    svg = build_for_code(code, name, **kw)
    path = path or os.path.join("data", "reports", "%s_kline.svg" % code)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(svg)
    return path


def render_png(svg_path, png_path=None):
    """可选：装了 matplotlib 就把数据另存一份 PNG（不依赖 cairosvg）。"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return None
    return None          # 需要时再扩展；SVG 已足够浏览器/GitHub 显示


if __name__ == "__main__":
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "600015"
    print(save_svg(code))
