# -*- coding: utf-8 -*-
"""可选扩展数据：财报 / 股东户数 / 龙虎榜（akshare 或 tushare）。

实测可用的 akshare 接口（v1.20+）：
    stock_financial_abstract(symbol)      财务摘要：行=指标，列=报告期
    stock_zh_a_gdhs_detail_em(symbol)     股东户数详情（按截止日，需取最近几期）
    stock_lhb_detail_em(start, end)       龙虎榜明细（整段时间全市场，按代码过滤；带进程内缓存）

设计原则：**不装也能跑**，未安装时返回 None，系统照常运行。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE, "data", "altdata")
TTL_SEC = 6 * 3600
_LHB_CACHE = {"ts": 0.0, "df": None, "span": None}


def _try_import(name):
    try:
        return __import__(name)
    except Exception:
        return None


def status():
    ak = _try_import("akshare")
    ts = _try_import("tushare")
    ver = ""
    if ak:
        try:
            ver = " v" + ak.__version__
        except Exception:
            ver = ""
    note = ("已安装：" + "、".join(n for n, v in (("akshare", ak), ("tushare", ts)) if v)
            + "（已自动启用，数据缓存 6 小时）") if (ak or ts) else \
        "未安装扩展数据源（可选）。需要财报/股东/龙虎榜时：pip install akshare"
    return {"akshare": bool(ak), "tushare": bool(ts), "version": ver, "note": note}


# --------------------------------------------------------------------------- #
# 缓存
# --------------------------------------------------------------------------- #
def _cache_path(code):
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, "%s.json" % code)


def _read_cache(code):
    p = _cache_path(code)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8-sig") as f:
            d = json.load(f)
        return None if time.time() - d.get("_ts", 0) > TTL_SEC else d
    except Exception:
        return None


def _write_cache(code, data):
    try:
        data["_ts"] = time.time()
        with open(_cache_path(code), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# akshare：财务摘要（实测列结构：选项 / 指标 / 20260930 / 20260630 …）
# --------------------------------------------------------------------------- #
FIN_KEYS = ["营业总收入", "归母净利润", "净利润", "扣非净利润",
            "基本每股收益", "净资产收益率", "销售毛利率", "资产负债率"]


def _ak_financial(ak, code, periods=2):
    try:
        df = ak.stock_financial_abstract(symbol=str(code))
    except Exception:
        return None
    if df is None or df.empty:
        return None
    date_cols = [c for c in df.columns if str(c).isdigit() and len(str(c)) == 8][:periods]
    if not date_cols:
        return None
    rows = []
    try:
        for _, r in df.iterrows():
            name = str(r.get("指标") or "")
            hit = next((k for k in FIN_KEYS if k in name), None)
            if not hit:
                continue
            # 金额类指标换算成 亿/万，比率类（每股收益/ROE/毛利率/负债率）保留原值
            money_like = ("净利润", "营业总收入", "收入", "利润")
            is_money = any(k in name for k in money_like)
            vals = []
            for c in date_cols:
                v = r.get(c)
                s = str(v)
                if s in ("nan", "None", "--", ""):
                    continue
                try:
                    num = float(v)
                    if is_money:
                        if abs(num) >= 1e8:
                            s = "%.2f 亿" % (num / 1e8)
                        elif abs(num) >= 1e4:
                            s = "%.1f 万" % (num / 1e4)
                        else:
                            s = "%.2f" % num
                    else:
                        s = "%.4f" % num
                except (TypeError, ValueError):
                    pass
                vals.append({"报告期": str(c), "值": s})
            if vals:
                rows.append({"项": name.replace("(元)", "").strip(), "期": vals})
            if len(rows) >= 6:
                break
    except Exception:
        return None
    return rows or None


# --------------------------------------------------------------------------- #
# akshare：股东户数（按截止日升序，取最近 3 期）
# --------------------------------------------------------------------------- #
def _ak_holders(ak, code, periods=3):
    try:
        df = ak.stock_zh_a_gdhs_detail_em(symbol=str(code))
    except Exception:
        return None
    if df is None or df.empty:
        return None
    try:
        df = df.sort_values("股东户数统计截止日")
        rows = []
        for _, r in df.tail(periods).iloc[::-1].iterrows():
            rows.append({"截止日": str(r.get("股东户数统计截止日")),
                         "股东户数": str(r.get("股东户数-本次")),
                         "较上期%": str(round(float(r.get("股东户数-增减比例") or 0), 2)),
                         "户均持股市值万": str(round(float(r.get("户均持股市值") or 0) / 1e4, 1))})
        return rows or None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# akshare：龙虎榜（全市场一次拉取，进程内缓存 30 分钟）
# --------------------------------------------------------------------------- #
def _lhb_df(ak, days=30):
    now = time.time()
    if _LHB_CACHE["df"] is not None and now - _LHB_CACHE["ts"] < 1800 \
            and _LHB_CACHE["span"] == days:
        return _LHB_CACHE["df"]
    try:
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
        df = ak.stock_lhb_detail_em(start_date=start, end_date=end)
        _LHB_CACHE.update({"ts": now, "df": df, "span": days})
        return df
    except Exception:
        return None


def _ak_lhb(ak, code, days=30, limit=3):
    df = _lhb_df(ak, days)
    if df is None or df.empty:
        return None
    try:
        sub = df[df["代码"].astype(str).str.zfill(6) == str(code).zfill(6)]
        if sub.empty:
            return None
        sub = sub.sort_values("上榜日").iloc[::-1].head(limit)
        rows = []
        for _, r in sub.iterrows():
            net = float(r.get("龙虎榜净买额") or 0) / 1e4
            rows.append({"日期": str(r.get("上榜日")),
                         "解读": str(r.get("解读") or ""),
                         "净买额万": round(net, 1),
                         "上榜原因": str(r.get("上榜原因") or "")[:30],
                         "涨跌幅%": round(float(r.get("涨跌幅") or 0), 2)})
        return rows or None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# tushare（可选）
# --------------------------------------------------------------------------- #
def _ts_client(token):
    ts = _try_import("tushare")
    if not ts or not token:
        return None
    try:
        ts.set_token(token)
        return ts.pro_api()
    except Exception:
        return None


def _ts_financial(pro, code):
    try:
        suffix = "SH" if str(code).startswith(("6", "9")) else "SZ"
        df = pro.fina_indicator(ts_code="%s.%s" % (code, suffix),
                                fields="end_date,roe,netprofit_yoy,or_yoy,grossprofit_margin",
                                limit=4)
        return [{"项": "ROE", "期": [{"报告期": r["end_date"], "值": str(r.get("roe"))}]}
                for _, r in df.iterrows()] or None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #
def brief(code, token="", use=True, force=False):
    if not use:
        return None
    if not force:
        cached = _read_cache(code)
        if cached:
            cached.pop("_ts", None)
            return cached
    ak = _try_import("akshare")
    data = {"source": None, "financial": None, "holders": None, "lhb": None,
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M")}
    if ak:
        data["source"] = "akshare"
        data["financial"] = _ak_financial(ak, code)
        data["holders"] = _ak_holders(ak, code)
        data["lhb"] = _ak_lhb(ak, code)
    else:
        pro = _ts_client(token)
        if pro:
            data["source"] = "tushare"
            data["financial"] = _ts_financial(pro, code)
    if not data["source"]:
        return None
    _write_cache(code, data)
    data.pop("_ts", None)
    return data


def compact(code, token="", use=True):
    """压成 3~6 行中文摘要，直接进 AI 提示词。"""
    b = brief(code, token, use)
    if not b:
        return None
    parts = []
    for row in (b.get("financial") or [])[:3]:
        per = "、".join("%s:%s" % (p["报告期"], p["值"]) for p in (row.get("期") or [])[:2])
        if per:
            parts.append("%s %s" % (row["项"], per))
    if b.get("holders"):
        h = b["holders"][0]
        parts.append("股东户数 %s（%s，环比 %s%%）"
                     % (h.get("股东户数"), h.get("截止日"), h.get("较上期%")))
    if b.get("lhb"):
        l = b["lhb"][0]
        parts.append("近30日龙虎榜 %s：%s，净买 %.1f 万"
                     % (l.get("日期"), (l.get("解读") or "")[:24], l.get("净买额万") or 0))
    return "；".join(parts) if parts else None


def ingest(codes, token="", force=True, verbose=True):
    """把一批股票的扩展数据抓取入库（data/altdata/<code>.json）。"""
    out = {}
    for c in codes:
        try:
            b = brief(c, token=token, use=True, force=force)
            out[c] = "ok" if b else "empty"
            if verbose:
                print("  %s %s" % (c, "✔ " + compact(c) if b else "· 无数据"))
        except Exception as exc:                      # noqa: BLE001
            out[c] = "fail: %s" % exc
            if verbose:
                print("  %s ✘ %s" % (c, exc))
    return out


def lhb_recent(days=30, limit=80, code=None, min_net_wan=None):
    """全市场龙虎榜明细（近 days 天，按上榜日倒序）。code 可过滤单只股票。"""
    ak = _try_import("akshare")
    if not ak:
        return None, "未安装 akshare：pip install akshare"
    df = _lhb_df(ak, days)
    if df is None or df.empty:
        return None, "龙虎榜数据获取失败（可能非交易日或接口限频）"
    try:
        d = df.copy()
        d["代码"] = d["代码"].astype(str).str.zfill(6)
        if code:
            d = d[d["代码"] == str(code).zfill(6)]
        d["_net"] = d["龙虎榜净买额"].astype(float) / 1e4      # 万元
        if min_net_wan is not None:
            d = d[d["_net"] >= float(min_net_wan)]
        d = d.sort_values(["上榜日", "_net"], ascending=[False, False]).head(limit)
        rows = []
        for _, r in d.iterrows():
            rows.append({"date": str(r.get("上榜日")), "code": r["代码"],
                         "name": str(r.get("名称") or ""),
                         "pct": round(float(r.get("涨跌幅") or 0), 2),
                         "net_wan": round(float(r.get("_net") or 0), 1),
                         "buy_wan": round(float(r.get("龙虎榜买入额") or 0) / 1e4, 1),
                         "sell_wan": round(float(r.get("龙虎榜卖出额") or 0) / 1e4, 1),
                         "reason": str(r.get("上榜原因") or "")[:40],
                         "read": str(r.get("解读") or "")})
        return rows, None
    except Exception as exc:                          # noqa: BLE001
        return None, "解析失败：%s" % exc


if __name__ == "__main__":
    import sys
    print(json.dumps(status(), ensure_ascii=False))
    codes = sys.argv[1:] or ["600015"]
    print(json.dumps(ingest(codes), ensure_ascii=False, indent=1))
