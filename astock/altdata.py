# -*- coding: utf-8 -*-
"""可选扩展数据：财报 / 股东 / 龙虎榜（akshare 或 tushare）。

设计原则：**不装也能跑**。
    · 两个库都是可选的，未安装时本模块返回 None，全系统照常运行；
    · 装了以后自动启用，数据带缓存（默认 6 小时）落到 data/altdata/<code>.json；
    · 给 AI 的只是"紧凑摘要"（几行字），不会把整张财报塞进提示词。

安装（任选其一）：
    pip install akshare                # 免费、免 token
    pip install tushare                # 需要在 config.json → ai.tushare_token 填 token
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE, "data", "altdata")
TTL_SEC = 6 * 3600


def _try_import(name):
    try:
        return __import__(name)
    except Exception:
        return None


def status():
    ak = _try_import("akshare")
    ts = _try_import("tushare")
    return {"akshare": bool(ak), "tushare": bool(ts),
            "note": ("已安装：" + "、".join([n for n, v in
                                           (("akshare", ak), ("tushare", ts)) if v])
                     if (ak or ts) else
                     "未安装扩展数据源（可选）。需要财报/股东/龙虎榜时：pip install akshare")}


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
        if time.time() - d.get("_ts", 0) > TTL_SEC:
            return None
        return d
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
# akshare 取数
# --------------------------------------------------------------------------- #
def _ak_financial(ak, code):
    """最近几期财务摘要：营收、净利、同比。"""
    try:
        df = ak.stock_financial_abstract(symbol=code)
    except Exception:
        return None
    try:
        cols = [c for c in df.columns]
        date_col = "选项" if "选项" in cols else cols[0]
        rows = []
        want = {"归母净利润": None, "营业总收入": None, "净利润": None}
        for _, r in df.head(24).iterrows():
            name = str(r[date_col])
            for k in want:
                if k in name and want[k] is None:
                    vals = [(c, r[c]) for c in cols[1:6] if str(r[c]) not in ("nan", "None")]
                    want[k] = vals
        for label, vals in want.items():
            if vals:
                rows.append({"项": label,
                             "期": [{"报告期": str(p), "值": v} for p, v in vals[:3]]})
        return rows or None
    except Exception:
        return None


def _ak_holders(ak, code):
    try:
        df = ak.stock_zh_a_gdhs_detail_em(symbol=code)
        out = []
        for _, r in df.head(3).iterrows():
            out.append({"截止日": str(r.get("股东户数统计截止日", "")),
                        "股东户数": str(r.get("股东户数-本次", "")),
                        "较上期%": str(r.get("股东户数-增减比例", ""))})
        return out or None
    except Exception:
        return None


def _ak_lhb(ak, code):
    """近 30 天龙虎榜。"""
    try:
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
        df = ak.stock_lhb_detail_em(start_date=start, end_date=end)
        df = df[df["代码"].astype(str).str.zfill(6) == str(code).zfill(6)]
        out = []
        for _, r in df.head(3).iterrows():
            out.append({"日期": str(r.get("上榜日", "")),
                        "解读": str(r.get("解读", "")),
                        "净买额万": str(r.get("龙虎榜净买额", ""))})
        return out or None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# tushare 取数
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
        return [{"报告期": r["end_date"], "ROE": r.get("roe"),
                 "净利同比%": r.get("netprofit_yoy"), "营收同比%": r.get("or_yoy"),
                 "毛利率%": r.get("grossprofit_margin")}
                for _, r in df.iterrows()] or None
    except Exception:
        return None


def _ts_lhb(pro, code):
    try:
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
        df = pro.top_list(ts_code=code, start_date=start, end_date=end)
        return [{"日期": r["trade_date"], "净买额万": r.get("net_amount"),
                 "解读": "龙虎榜"} for _, r in df.head(3).iterrows()] or None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #
def brief(code, token="", use=True):
    """返回 {source, financial, holders, lhb} 或 None（不可用/未启用）。"""
    if not use:
        return None
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
            data["lhb"] = _ts_lhb(pro, code)
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
    lines = []
    if b.get("financial"):
        for row in b["financial"][:2]:
            per = "、".join("%s:%s" % (p["报告期"], p["值"]) for p in (row.get("期") or [])[:2])
            if per:
                lines.append("%s %s" % (row.get("项"), per))
    if b.get("holders"):
        h = b["holders"][0]
        lines.append("股东户数 %s（截至 %s，较上期 %s%%）"
                     % (h.get("股东户数"), h.get("截止日"), h.get("较上期%")))
    if b.get("lhb"):
        l = b["lhb"][0]
        lines.append("近30日龙虎榜：%s %s 净买额%s万" % (l.get("日期"), l.get("解读", ""),
                                                l.get("净买额万")))
    return "；".join(lines) if lines else None


if __name__ == "__main__":
    print(json.dumps(status(), ensure_ascii=False))
    print(json.dumps(compact("600015"), ensure_ascii=False))
