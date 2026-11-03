# -*- coding: utf-8 -*-
"""行情数据源：东方财富公开接口封装（仅依赖 Python 标准库）。

对外接口：
    get_quote(code)                  实时快照（盘后为收盘快照）
    get_kline(code, klt, limit)      K 线：klt 1/5/15/30/60 分钟、101 日、102 周、103 月
    get_fundflow(code, limit)        主力资金流向（按日）
    get_trends(code)                 当日分时
    get_indices()                    大盘指数快照

注意：接口为公开免费接口，可能限频或调整字段；本模块内置重试与容错。
"""
from __future__ import annotations

import gzip
import json
import time
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "*/*",
}


class FeedError(RuntimeError):
    """行情获取失败。"""


def _get(url: str, timeout: float = 10.0, retries: int = 3) -> str:
    last_err = None
    for attempt in range(retries):
        try:
            req = Request(url, headers=HEADERS)
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            if raw[:2] == b"\x1f\x8b":          # 兼容 gzip
                raw = gzip.decompress(raw)
            return raw.decode("utf-8", "replace")
        except (URLError, HTTPError, TimeoutError, OSError) as exc:
            last_err = exc
            time.sleep(0.5 * (attempt + 1))
    raise FeedError("请求失败(%s): %s" % (last_err, url))


def secid(code: str) -> str:
    """把 6 位代码转成东财 secid。6/9 开头为沪市(1)，其余为深市/北交所(0)。"""
    code = str(code).strip()
    if code.startswith(("6", "9", "5")):
        return "1." + code
    return "0." + code


# --------------------------------------------------------------------------- #
# 实时快照
# --------------------------------------------------------------------------- #
_QUOTE_FIELDS = ("f43,f44,f45,f46,f47,f48,f50,f57,f58,f59,f60,f116,f117,"
                 "f162,f167,f168,f169,f170,f171,f124")


def get_quote(code: str) -> dict:
    """返回实时快照字典。字段缺失时值为 None。"""
    url = ("https://push2.eastmoney.com/api/qt/stock/get?secid=%s&fields=%s"
           % (secid(code), _QUOTE_FIELDS))
    data = json.loads(_get(url)).get("data")
    if not data:
        raise FeedError("无行情数据（代码是否存在？）：%s" % code)

    dp = int(data.get("f59") or 2)          # 小数位数
    div = 10 ** dp

    def num(key, scale=1.0):
        v = data.get(key)
        if v in (None, "-", ""):
            return None
        return round(float(v) / scale, 6)

    return {
        "code": data.get("f57") or str(code),
        "name": data.get("f58") or "",
        "price": num("f43", div),
        "high": num("f44", div),
        "low": num("f45", div),
        "open": num("f46", div),
        "prev_close": num("f60", div),
        "volume": num("f47"),              # 成交量（手）
        "amount": num("f48"),              # 成交额（元）
        "vol_ratio": num("f50", 100),      # 量比
        "pct": num("f170", 100),           # 涨跌幅 %
        "chg": num("f169", div),           # 涨跌额
        "amplitude": num("f171", 100),     # 振幅 %
        "turnover": num("f168", 100),      # 换手率 %
        "pe": num("f162", 100),            # 市盈率(动)
        "pb": num("f167", 100),            # 市净率
        "mktcap": num("f116"),             # 总市值（元）
        "float_cap": num("f117"),          # 流通市值（元）
        "ts": data.get("f124"),
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# --------------------------------------------------------------------------- #
# K 线
# --------------------------------------------------------------------------- #
_KLINE_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"


def get_kline(code: str, klt: int = 101, limit: int = 300, adjust: int = 1) -> list:
    """返回 K 线列表，每项 dict：date/open/close/high/low/vol/amount/amp/pct/chg/turnover。

    klt: 1/5/15/30/60 分钟；101 日；102 周；103 月。
    adjust: 0 不复权、1 前复权、2 后复权。
    """
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           "?secid=%s&fields1=f1,f2,f3,f4,f5,f6&fields2=%s"
           "&klt=%d&fqt=%d&end=20500101&lmt=%d"
           % (secid(code), _KLINE_FIELDS2, klt, adjust, limit))
    payload = json.loads(_get(url))
    data = payload.get("data") or {}
    out = []
    for line in data.get("klines") or []:
        p = line.split(",")
        if len(p) < 11:
            continue
        out.append({
            "date": p[0],
            "open": float(p[1]),
            "close": float(p[2]),
            "high": float(p[3]),
            "low": float(p[4]),
            "vol": float(p[5]),         # 手
            "amount": float(p[6]),      # 元
            "amp": float(p[7]),         # 振幅 %
            "pct": float(p[8]),         # 涨跌幅 %
            "chg": float(p[9]),
            "turnover": float(p[10]),   # 换手 %
        })
    if not out:
        raise FeedError("K 线为空：%s" % code)
    return out


def get_fundflow(code: str, limit: int = 10) -> list:
    """主力资金流向（日频）。金额单位：元。返回按时间升序。"""
    url = ("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
           "?secid=%s&fields1=f1,f2,f3,f7"
           "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
           "&lmt=%d" % (secid(code), limit))
    payload = json.loads(_get(url))
    data = payload.get("data") or {}
    out = []
    for line in data.get("klines") or []:
        p = line.split(",")
        if len(p) < 6:
            continue
        # f51 日期, f52 主力净额, f53 小单, f54 中单, f55 大单, f56 超大单
        out.append({
            "date": p[0],
            "main": float(p[1]),
            "small": float(p[2]),
            "medium": float(p[3]),
            "large": float(p[4]),
            "super": float(p[5]),
        })
    return out


def get_trends(code: str) -> list:
    """当日分时数据。返回 [{time, price, vol, avg}]。"""
    url = ("https://push2his.eastmoney.com/api/qt/stock/trends2/get?secid=%s"
           "&fields1=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13"
           "&fields2=f51,f52,f53,f54,f55,f56,f57,f58&iscr=0&ndays=1"
           % secid(code))
    payload = json.loads(_get(url))
    data = payload.get("data") or {}
    out = []
    for line in data.get("trends") or []:
        p = line.split(",")
        if len(p) < 8:
            continue
        out.append({
            "time": p[0],
            "price": float(p[2]),
            "vol": float(p[5]),
            "avg": float(p[7]) if p[7] not in ("-", "") else None,
        })
    return out


_UNIVERSE_FIELDS = ("f2,f3,f5,f6,f8,f9,f12,f14,f15,f16,f17,f18,f20,f21,f23,f62")
_A_SHARE_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"


def get_universe(size: int = 300, page_size: int = 100) -> list:
    """全市场快照（按成交额降序取前 size 只），用于选股初筛。

    返回 [{code,name,price,pct,amount,volume,turnover,mktcap,float_cap,pe,pb,main_in}]
    """
    out = []
    page = 1
    while len(out) < size:
        url = ("https://push2.eastmoney.com/api/qt/clist/get?pn=%d&pz=%d&po=1&np=1"
               "&fltt=2&invt=2&fid=f6&fs=%s&fields=%s"
               % (page, page_size, _A_SHARE_FS, _UNIVERSE_FIELDS))
        data = json.loads(_get(url)).get("data") or {}
        diff = data.get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        if not diff:
            break
        for d in diff:
            try:
                out.append({
                    "code": str(d.get("f12")),
                    "name": d.get("f14") or "",
                    "price": float(d.get("f2")) if d.get("f2") not in (None, "-") else None,
                    "pct": float(d.get("f3")) if d.get("f3") not in (None, "-") else 0.0,
                    "volume": float(d.get("f5") or 0),
                    "amount": float(d.get("f6") or 0),
                    "turnover": float(d.get("f8") or 0),
                    "pe": float(d.get("f9")) if d.get("f9") not in (None, "-") else None,
                    "high": float(d.get("f15") or 0),
                    "low": float(d.get("f16") or 0),
                    "open": float(d.get("f17") or 0),
                    "prev_close": float(d.get("f18") or 0),
                    "mktcap": float(d.get("f20") or 0),
                    "float_cap": float(d.get("f21") or 0),
                    "pb": float(d.get("f23")) if d.get("f23") not in (None, "-") else None,
                    "main_in": float(d.get("f62") or 0),
                })
            except (TypeError, ValueError):
                continue
        if len(diff) < page_size:
            break
        page += 1
        if page > 60:
            break
    return out[:size]


def get_indices() -> list:
    """大盘环境：上证指数、深证成指、创业板指、沪深300（单请求批量获取）。"""
    url = ("https://push2.eastmoney.com/api/qt/ulist.np/get"
           "?secids=1.000001,0.399001,0.399006,1.000300"
           "&fields=f2,f3,f12,f13,f14")
    data = json.loads(_get(url, retries=2)).get("data") or {}
    diff = data.get("diff") or []
    if isinstance(diff, dict):                  # 兼容 dict 形态
        diff = list(diff.values())
    out = []
    for d in diff:
        try:
            out.append({
                "name": d.get("f14"),
                "code": str(d.get("f12")),
                "price": round(float(d.get("f2")) / 100, 2),
                "pct": round(float(d.get("f3")) / 100, 2),
            })
        except (TypeError, ValueError):
            continue
    return out
