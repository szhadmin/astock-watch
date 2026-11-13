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
import os
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


def _get(url: str, timeout: float = 6.0, retries: int = 2) -> str:
    """东财请求。超时/重试故意压短：上游被限流时不能让调用方卡几十秒。"""
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
            time.sleep(0.3 * (attempt + 1))
    raise FeedError("请求失败(%s): %s" % (last_err, url))


# --------------------------------------------------------------------------- #
# 备用数据源（腾讯 / 新浪）
#   2026-11-13：东财 push2 / push2his 对服务器 IP 直接断连（0.1s 即 Remote end
#   closed），而 www 首页、腾讯、新浪都正常 → 属于接口级封禁/限流。
#   这里给「行情 / K线 / 指数」加备用源，东财失败自动切换，避免 AI 盲决策。
# --------------------------------------------------------------------------- #
_TENCENT_HEADERS = {"User-Agent": UA, "Referer": "https://gu.qq.com/"}
_DATA_CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "ai", "cache")


def _tc_symbol(code: str) -> str:
    """6 位代码 → 腾讯代码（sh/sz/bj）。"""
    code = str(code).strip()
    if code.startswith(("6", "9", "5")):
        return "sh" + code
    if code.startswith(("4", "8")):
        return "bj" + code
    return "sz" + code


def _tc_get(url: str, timeout: float = 6.0) -> str:
    req = Request(url, headers=_TENCENT_HEADERS)
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("gbk", "replace")


def _quote_tencent(code: str) -> dict:
    """腾讯实时行情（东财挂掉时的备用源）。"""
    txt = _tc_get("https://qt.gtimg.cn/q=%s" % _tc_symbol(code))
    seg = txt.split('="', 1)[-1].split('"')[0]
    f = seg.split("~")
    if len(f) < 40:
        raise FeedError("腾讯行情无数据：%s" % code)

    def num(i, scale=1.0):
        try:
            v = f[i]
            if v in ("", "-", "0.00") and i in (39, 46):
                return None
            return round(float(v) / scale, 6) if v not in ("", "-") else None
        except (IndexError, ValueError):
            return None

    amt = num(37)
    fc, mc = num(44), num(45)
    return {
        "code": (f[2] or str(code)).strip(),
        "name": (f[1] or "").strip(),
        "price": num(3),
        "prev_close": num(4),
        "open": num(5),
        "high": num(33),
        "low": num(34),
        "volume": num(36),                       # 手
        "amount": amt * 10000 if amt is not None else None,   # 万元 → 元
        "pct": num(32),
        "chg": num(31),
        "turnover": num(38),
        "pe": num(39),
        "amplitude": num(43),
        "float_cap": fc * 1e8 if fc else None,
        "mktcap": mc * 1e8 if mc else None,
        "pb": num(46),
        "vol_ratio": num(49),
        "ts": f[30] if len(f) > 30 else None,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "tencent",
    }


def _kline_tencent(code: str, klt: int = 101, limit: int = 300, adjust: int = 1) -> list:
    """腾讯 K 线（备用）。返回与东财同结构，amount/pct 由价格推算。"""
    period = {1: "m1", 5: "m5", 15: "m15", 30: "m30", 60: "m60",
              101: "day", 102: "week", 103: "month"}.get(int(klt), "day")
    fq = "qfq" if adjust == 1 else ("hfq" if adjust == 2 else "")
    sym = _tc_symbol(code)
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,%s,,,%d,%s"
           % (sym, period, max(10, min(limit, 800)), fq))
    payload = json.loads(_tc_get(url))
    node = ((payload.get("data") or {}).get(sym) or {})
    bars = node.get(fq + period) or node.get(period) or []
    out = []
    prev_close = None
    for b in bars:
        try:
            o, c, h, l, v = (float(b[1]), float(b[2]), float(b[3]),
                             float(b[4]), float(b[5]))
        except (IndexError, TypeError, ValueError):
            continue
        pct = round((c / prev_close - 1) * 100, 4) if prev_close else None
        chg = round(c - prev_close, 4) if prev_close else None
        out.append({"date": b[0], "open": o, "close": c, "high": h, "low": l,
                    "vol": v, "amount": 0.0,
                    "amp": round((h - l) / prev_close * 100, 4) if prev_close else None,
                    "pct": pct, "chg": chg, "turnover": None})
        prev_close = c
    if not out:
        raise FeedError("腾讯 K 线为空：%s" % code)
    return out[-limit:]


_TC_INDICES = (("上证指数", "sh000001"), ("深证成指", "sz399001"),
               ("创业板指", "sz399006"), ("沪深300", "sh000300"))


def _indices_tencent() -> list:
    txt = _tc_get("https://qt.gtimg.cn/q=" + ",".join(s for _, s in _TC_INDICES))
    out = []
    for seg in txt.strip().split(";"):
        if "~" not in seg:
            continue
        f = seg.split("~")
        if len(f) < 33:
            continue
        try:
            out.append({"name": (f[1] or "").strip(), "code": (f[2] or "").strip(),
                        "price": float(f[3]), "pct": float(f[32])})
        except (TypeError, ValueError):
            continue
    if not out:
        raise FeedError("腾讯指数为空")
    return out


def _universe_cache_path() -> str:
    os.makedirs(_DATA_CACHE, exist_ok=True)
    return os.path.join(_DATA_CACHE, "universe.json")


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
    """返回实时快照字典。字段缺失时值为 None。

    东财（push2）不可用时自动切腾讯行情，保证持仓/决策不因单一数据源被封而瞎掉。
    """
    try:
        return _quote_eastmoney(code)
    except Exception:                              # noqa: BLE001
        return _quote_tencent(code)


def _quote_eastmoney(code: str) -> dict:
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
    东财不可用时自动切腾讯 K 线（amount/turnover 会缺，指标计算不受影响）。
    """
    try:
        return _kline_eastmoney(code, klt, limit, adjust)
    except Exception:                              # noqa: BLE001
        return _kline_tencent(code, klt, limit, adjust)


def _kline_eastmoney(code: str, klt: int = 101, limit: int = 300, adjust: int = 1) -> list:
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

    东财 clist 被限流/封 IP 时（2026-11-12、11-13 实际发生过），退回最近一次成功
    的磁盘快照并打印数据时间，避免「选股连续失败 → 观察池一直不更新」。
    """
    try:
        out = _universe_eastmoney(size, page_size)
    except Exception as exc:                       # noqa: BLE001
        print("  [feed] 东财全市场快照失败（%s）→ 改用新浪排行" % str(exc)[:50])
        try:
            out = _universe_sina(size, page_size)
        except Exception as exc2:                  # noqa: BLE001
            cached = _load_universe_cache(size)
            if cached:
                print("  [feed] 新浪也失败（%s）→ 使用缓存快照（%s，%d 只）"
                      % (str(exc2)[:50], cached[0], len(cached[1])))
                return cached[1]
            raise
    _save_universe_cache(out)
    return out


def _universe_sina(size: int = 300, page_size: int = 100) -> list:
    """新浪沪深A排行（按成交额降序）——东财 clist 被封时的备用全市场快照。"""
    out = []
    page = 1
    hdr = {"User-Agent": UA, "Referer": "https://finance.sina.com.cn"}
    while len(out) < size and page <= 20:
        url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               "Market_Center.getHQNodeData?page=%d&num=%d&sort=amount&asc=0&node=hs_a&symbol="
               % (page, page_size))
        req = Request(url, headers=hdr)
        with urlopen(req, timeout=8) as resp:
            txt = resp.read().decode("utf-8", "replace")
        try:
            rows = json.loads(txt) or []
        except Exception:                          # noqa: BLE001
            rows = []
        if not rows:
            break
        for d in rows:
            try:
                def fnum(k):
                    v = d.get(k)
                    return float(v) if v not in (None, "", "-") else None
                out.append({
                    "code": str(d.get("code") or "").strip(),
                    "name": d.get("name") or "",
                    "price": fnum("trade"),
                    "pct": fnum("changepercent") or 0.0,
                    "volume": (fnum("volume") or 0) / 100.0,      # 股 → 手
                    "amount": fnum("amount") or 0.0,
                    "turnover": fnum("turnoverratio") or 0.0,
                    "pe": fnum("per"),
                    "high": fnum("high") or 0.0,
                    "low": fnum("low") or 0.0,
                    "open": fnum("open") or 0.0,
                    "prev_close": fnum("settlement") or 0.0,
                    "mktcap": fnum("mktcap") or 0.0,
                    "float_cap": fnum("nmc") or 0.0,
                    "pb": fnum("pb"),
                    "main_in": 0.0,
                })
            except (TypeError, ValueError):
                continue
        if len(rows) < page_size:
            break
        page += 1
    out = [x for x in out if x["code"]]
    if not out:
        raise FeedError("新浪全市场快照为空")
    return out[:size]


def _load_universe_cache(size):
    p = _universe_cache_path()
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f) or {}
        items = d.get("items") or []
        if not items:
            return None
        return d.get("ts") or "未知时间", items[:size]
    except Exception:                              # noqa: BLE001
        return None


def _save_universe_cache(items):
    try:
        with open(_universe_cache_path(), "w", encoding="utf-8") as f:
            json.dump({"ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
                       "items": items}, f, ensure_ascii=False)
    except Exception:                              # noqa: BLE001
        pass


def _universe_eastmoney(size: int = 300, page_size: int = 100) -> list:
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
    try:
        url = ("https://push2.eastmoney.com/api/qt/ulist.np/get"
               "?secids=1.000001,0.399001,0.399006,1.000300"
               "&fields=f2,f3,f12,f13,f14")
        data = json.loads(_get(url, retries=1)).get("data") or {}
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
        if not out:
            raise FeedError("指数为空")
        return out
    except Exception:                              # noqa: BLE001
        return _indices_tencent()


# --------------------------------------------------------------------------- #
# 指数 / 板块（大盘温度、板块强度用）
# --------------------------------------------------------------------------- #
def search_stock(keyword, limit=10):
    """全市场搜索：代码 / 名称 / 拼音首字母（东财 suggest 接口）。

    返回 [{"code","name","pinyin","market"}]；失败时回退到本地股票池模糊匹配。
    东财返回的 SecurityTypeName 是「沪A/深A/京A/台股」等，只保留 A 股。
    """
    import urllib.parse
    import urllib.request

    kw = str(keyword or "").strip()
    if not kw:
        return []
    url = ("https://searchapi.eastmoney.com/api/suggest/get?input=%s&type=14"
           "&token=D43BF722C8E33BDC906FB84D85E326E8&count=%d"
           % (urllib.parse.quote(kw), max(1, int(limit)) * 2))
    A_TYPES = ("沪A", "深A", "京A", "科创板", "创业板")
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
            "Accept": "*/*",
            "Referer": "https://www.eastmoney.com/",
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        rows = ((data.get("QuotationCodeTable") or {}).get("Data") or [])
        out = []
        for x in rows:
            code = str(x.get("Code") or "")
            if not (code.isdigit() and len(code) == 6):
                continue
            if code[0] not in "0369":
                continue
            stype = str(x.get("SecurityTypeName") or "")
            classify = str(x.get("Classify") or "")
            if stype and stype not in A_TYPES and classify != "AStock":
                continue
            out.append({"code": code, "name": x.get("Name") or "",
                        "pinyin": str(x.get("PinYin") or "").upper(),
                        "market": str(x.get("MktNum") or "")})
            if len(out) >= limit:
                break
        if out:
            return out
    except Exception:
        pass
    out = []
    try:
        for it in get_universe(size=500):
            c, n = str(it.get("code") or ""), str(it.get("name") or "")
            if kw in c or kw in n:
                out.append({"code": c, "name": n, "pinyin": "", "market": ""})
            if len(out) >= limit:
                break
    except Exception:
        pass
    return out


def get_kline_secid(sid: str, klt: int = 101, limit: int = 60, adjust: int = 0) -> list:
    """按 secid 直接取 K 线（用于指数：1.000001 上证、0.399001 深成等）。"""
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           "?secid=%s&fields1=f1,f2,f3,f4,f5,f6&fields2=%s"
           "&klt=%d&fqt=%d&end=20500101&lmt=%d"
           % (sid, _KLINE_FIELDS2, klt, adjust, limit))
    data = json.loads(_get(url)).get("data") or {}
    out = []
    for line in data.get("klines") or []:
        q = line.split(",")
        if len(q) < 11:
            continue
        out.append({"date": q[0], "open": float(q[1]), "close": float(q[2]),
                    "high": float(q[3]), "low": float(q[4]), "vol": float(q[5]),
                    "amount": float(q[6]), "pct": float(q[8])})
    return out


def get_index_kline(secid: str = "1.000001", limit: int = 60) -> list:
    """上证指数日线（默认）。"""
    return get_kline_secid(secid, 101, limit)


def get_sector_board(size: int = 40) -> list:
    """行业板块排行：返回 [{code,name,pct,main_in,leader,up,down}]"""
    url = ("https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=%d&po=1&np=1"
           "&fltt=2&invt=2&fid=f3&fs=m:90+t:2&fields="
           "f2,f3,f12,f14,f62,f104,f105,f128" % size)
    data = json.loads(_get(url)).get("data") or {}
    diff = data.get("diff") or []
    if isinstance(diff, dict):
        diff = list(diff.values())
    out = []
    for d in diff:
        try:
            out.append({
                "code": str(d.get("f12")), "name": d.get("f14"),
                "pct": float(d.get("f3") or 0),
                "main_in": float(d.get("f62") or 0),
                "up": int(d.get("f104") or 0), "down": int(d.get("f105") or 0),
                "leader": d.get("f128") or "",
            })
        except (TypeError, ValueError):
            continue
    return out


def get_stock_sectors(code: str, size: int = 12) -> list:
    """个股所属板块（行业/概念/地域）及其当日涨跌。"""
    url = ("https://push2.eastmoney.com/api/qt/slist/get?spt=3&fltt=2&invt=2"
           "&fields=f12,f13,f14,f3,f62&secid=%s&pn=1&pz=%d&po=1&np=1"
           % (secid(code), size))
    data = json.loads(_get(url)).get("data") or {}
    diff = data.get("diff") or []
    if isinstance(diff, dict):
        diff = list(diff.values())
    out = []
    for d in diff:
        try:
            out.append({"code": str(d.get("f12")), "name": d.get("f14"),
                        "pct": float(d.get("f3") or 0),
                        "main_in": float(d.get("f62") or 0)})
        except (TypeError, ValueError):
            continue
    return out
