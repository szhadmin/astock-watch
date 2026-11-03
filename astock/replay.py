# -*- coding: utf-8 -*-
"""L3 历史回放加速器：用历史日线"回放决策"，几分钟造出成百上千条带结果的判断样本。

为什么需要：
    真实判断要等 5 个交易日回填，一天最多攒 1~6 条 —— 攒到 20 条样本要 1~2 周。
    历史回放可以在几分钟内生成几百条同口径样本，让"经验规则"立刻具备统计意义。

严格防未来函数：
    第 i 天的判断只用 bars[:i+1] 的数据；结果用其后 5 根 K 线结算。

样本来源标记：source="replay"（与真实决策 source="decide" 区分，可分别统计）。

温度：历史当时的"大盘温度"无法完整复原（缺涨跌家数广度），
      这里用「指数涨跌 + 上证相对 MA5/MA20」做一个近似代理，字段记在 ctx.temp_est，
      并在统计时标注；板块历史涨幅不可得，ctx.sector_pct 记 None。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime

from . import feed, indicators as ind, strategy

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(BASE, "data", "ai", "hypotheses_replay.jsonl")
HORIZON = 5
MOVE_PCT = 1.0
COST_PCT = 0.3


# --------------------------------------------------------------------------- #
# 历史温度代理
# --------------------------------------------------------------------------- #
def hist_temp_map(index_bars=None, limit=400):
    """用上证指数日线估算历史"大盘温度"（近似值，缺广度数据）。"""
    if index_bars is None:
        try:
            index_bars = feed.get_index_kline("1.000001", limit)
        except Exception:
            return {}
    closes = [b["close"] for b in index_bars]
    out = {}
    for i, b in enumerate(index_bars):
        if i < 20:
            continue
        pct = b.get("pct") or 0
        ma5 = sum(closes[i - 4:i + 1]) / 5
        ma20 = sum(closes[i - 19:i + 1]) / 20
        c = b["close"]
        score = 50 + max(-15, min(15, pct * 6))
        if c > ma5 > ma20:
            score += 10
        elif c > ma20:
            score += 4
        elif c < ma5 < ma20:
            score -= 10
        else:
            score -= 4
        out[b["date"]] = int(max(0, min(100, round(score))))
    return out


# --------------------------------------------------------------------------- #
# 单只股票回放
# --------------------------------------------------------------------------- #
def replay_code(code, bars, temp_map=None, min_score=60, max_score_bear=40,
                verbose=False, idx_map=None):
    """回放一只股票的历史判断，返回 (记录列表, 统计)。

    idx_map: {日期: 指数收盘}，用于计算"扣 beta"的超额收益（个股收益 − 指数收益）。
    """
    temp_map = temp_map or {}
    idx_map = idx_map or {}
    out = []
    n = len(bars)
    if n < 80:
        return out, {"code": code, "skipped": "K线不足"}
    for i in range(60, n - HORIZON):
        window = bars[:i + 1]                       # 只看当天及之前（防未来函数）
        try:
            ev = strategy.evaluate(window, i)
        except Exception:
            continue
        score = ev.get("score")
        if score is None:
            continue
        if score >= min_score:
            direction = "bullish"
        elif score <= max_score_bear:
            direction = "bearish"
        else:
            continue
        ref = bars[i]["close"]
        if not ref:
            continue
        levels = ev.get("levels") or {}
        stop = levels.get("stop")
        targets = levels.get("targets") or []
        resist = levels.get("resistance") or []
        # 失效价要按方向取：看涨用下方止损，看跌用上方阻力（否则看跌样本必然"先破失效"）
        if direction == "bearish":
            stop = (resist[0] if resist else round(ref * 1.03, 3))
        fwd = bars[i + 1:i + 1 + HORIZON]
        if not fwd:
            continue
        rets = [round((b["close"] / ref - 1) * 100, 2) for b in fwd]
        max_hi = round(max((b["high"] / ref - 1) * 100 for b in fwd), 2)
        max_lo = round(min((b["low"] / ref - 1) * 100 for b in fwd), 2)
        broke = None
        if stop:
            for b in fwd:
                if direction == "bullish" and b["low"] <= stop:
                    broke = b["date"]
                    break
                if direction == "bearish" and b["high"] >= stop:
                    broke = b["date"]
                    break
        end_ret = rets[-1]
        # 扣 beta：同期指数收益，超额 = 个股 − 指数
        idx_ret = None
        d0 = bars[i]["date"]
        d1 = fwd[-1]["date"]
        if idx_map.get(d0) and idx_map.get(d1):
            idx_ret = round((idx_map[d1] / idx_map[d0] - 1) * 100, 2)
        excess = round(end_ret - idx_ret, 2) if idx_ret is not None else None
        if direction == "bullish":
            hit = (not broke) and end_ret >= MOVE_PCT
        else:
            hit = (not broke) and end_ret <= -MOVE_PCT
        rec = {
            "id": "rp_" + uuid.uuid4().hex[:8],
            "date": bars[i]["date"], "code": code,
            "name": ev.get("name") or "",
            "direction": direction, "source": "replay",
            "ref_price": round(ref, 4),
            "invalidate": round(stop, 4) if stop else None,
            "target": round(targets[0], 4) if targets else None,
            "horizon": HORIZON, "move_pct": MOVE_PCT,
            "reason": "回放：评分%d %s（%s，失效价%.2f）"
                      % (score, ev.get("verdict", {}).get("level") or "",
                         "看涨" if direction == "bullish" else "看跌", stop or 0),
            "ctx": {"temp": temp_map.get(bars[i]["date"]), "temp_est": True,
                    "sector_pct": None, "score": score},
            "status": "closed",
            "result": {"filled_at": datetime.now().strftime("%Y-%m-%d"),
                       "days": len(fwd), "end_ret": end_ret,
                       "max_gain": max_hi, "max_drawdown": max_lo,
                       "idx_ret": idx_ret, "excess": excess,
                       "invalidated": broke, "hit": bool(hit),
                       "verdict": "命中" if hit else ("先破失效条件" if broke else "方向未走出来")},
        }
        out.append(rec)
    if verbose:
        print("  %s %-6s 回放样本 %d 条" % (code, (out[0]["name"] if out else ""), len(out)))
    return out, {"code": code, "samples": len(out)}


# --------------------------------------------------------------------------- #
# 批量回放并落盘
# --------------------------------------------------------------------------- #
def _existing_keys():
    """已存在的 (source, code, date)，避免重复回放。"""
    keys = set()
    if not os.path.exists(PATH):
        return keys
    try:
        with open(PATH, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    x = json.loads(line)
                except Exception:
                    continue
                keys.add((x.get("source") or "decide", x.get("code"), x.get("date")))
    except Exception:
        pass
    return keys


def run(codes=None, days=250, max_codes=30, verbose=True, cfg=None):
    """批量回放：默认取候选池/观察池的股票。"""
    if not codes:
        codes = _default_universe(max_codes, cfg)
    codes = [c for c in codes][:max_codes]
    temp_map = hist_temp_map()
    idx_map = {}
    try:
        ibars = feed.get_index_kline("1.000001", max(1000, days + 260))
        idx_map = {b["date"]: b["close"] for b in ibars}
    except Exception as exc:                          # noqa: BLE001
        if verbose:
            print("  指数取数失败（超额收益将为空）：%s" % exc)
    if verbose:
        print("历史温度代理：%d 个交易日｜指数基准：%d 个交易日"
              % (len(temp_map), len(idx_map)))
    keys = _existing_keys()
    done_codes = {c for (src, c, _d) in keys if src == "replay"}
    if done_codes:
        before = len(codes)
        codes = [c for c in codes if c not in done_codes]
        if verbose and before != len(codes):
            print("续跑模式：跳过已回放的 %d 只，本轮处理 %d 只"
                  % (before - len(codes), len(codes)))
    all_recs, stats = [], []
    t0 = time.time()
    for idx, code in enumerate(codes, 1):
        bars = None
        for attempt in range(3):                     # 节流 + 重试，避免被限频
            try:
                bars = feed.get_kline(code, 101, limit=days + HORIZON + 5)
                break
            except Exception as exc:                 # noqa: BLE001
                if attempt == 2 and verbose:
                    print("  %s 取数失败：%s" % (code, exc))
                time.sleep(1.2 * (attempt + 1))
        if not bars:
            continue
        time.sleep(0.15)                             # 礼貌间隔
        recs, st = replay_code(code, bars, temp_map, verbose=verbose, idx_map=idx_map)
        recs = [r for r in recs if ("replay", r["code"], r["date"]) not in keys]
        all_recs.extend(recs)
        stats.append(st)
        if verbose and (idx % 10 == 0 or idx == len(codes)):
            used = time.time() - t0
            eta = used / idx * (len(codes) - idx)
            print("  进度 %d/%d｜样本 %d｜已用 %.0fs｜预计剩余 %.0fs"
                  % (idx, len(codes), len(all_recs), used, eta))
        if len(all_recs) >= 60000:                   # 单次上限，防内存爆炸
            if verbose:
                print("  已达单次上限 60000 条，停止本轮（可再跑下一批）")
            break
    if all_recs:
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        with open(PATH, "a", encoding="utf-8") as f:
            for r in all_recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    if verbose:
        bull = len([r for r in all_recs if r["direction"] == "bullish"])
        bear = len(all_recs) - bull
        hits = len([r for r in all_recs if r["result"]["hit"]])
        excs = [r["result"].get("excess") for r in all_recs
                if r["result"].get("excess") is not None]
        msg = ("\n回放完成：%d 只股票 → 新增样本 %d 条（看涨 %d / 看跌 %d），命中 %d 条（%.1f%%）"
               % (len(stats), len(all_recs), bull, bear, hits,
                  (hits / len(all_recs) * 100) if all_recs else 0))
        if excs:
            msg += "\n  扣 beta 后：平均超额 %+.2f%%（相对上证指数）" % (sum(excs) / len(excs))
        print(msg)
    return {"codes": len(stats), "new_samples": len(all_recs), "stats": stats}


def _top_by_amount(n):
    """按成交额取前 n 只（覆盖全市场，避免只看候选池）。"""
    out = []
    try:
        for q in feed.get_universe(size=int(n * 1.2)):
            if q.get("code") and str(q["code"]).isdigit():
                out.append(str(q["code"]))
    except Exception:
        pass
    return out[:n]


def _default_universe(max_codes, cfg=None):
    """优先用观察池 + 我的持仓，其次候选池，最后用成交额前列。"""
    codes = []
    try:
        from . import holdings, memory
        codes += [x["code"] for x in (holdings.load().get("items") or [])]
        codes += [x["code"] for x in (memory.read_watchlist() or [])]
    except Exception:
        pass
    if len(codes) < max_codes:
        try:
            from . import screener
            for f in screener.screen({"top": max_codes * 2}, verbose=False):
                codes.append(f["code"])
        except Exception:
            pass
    if len(codes) < max_codes:
        try:
            for q in feed.get_universe(size=max_codes * 3):
                codes.append(q["code"])
        except Exception:
            pass
    seen, out = set(), []
    for c in codes:
        if c and c not in seen and len(str(c)) == 6:
            seen.add(c)
            out.append(c)
    return out[:max_codes]


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    print(run(max_codes=n))
