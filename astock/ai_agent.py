# -*- coding: utf-8 -*-
"""AI 自主操盘手（LLM 决策 + 代码硬风控 + 模拟盘成交 + 自我复盘进化）。

三个角色循环：
    ① 选股会（盘前/收盘后）：规则初筛 60 只 → AI 从中挑出今日观察池
    ② 操盘（盘中）：给 AI 喂「K线 + 指标 + 持仓 + 关键位」，AI 输出买卖指令
    ③ 复盘（收盘后）：AI 看自己的成交记录与绩效 → 写教训 → 重写策略手册

关键安全设计：
    · AI 只能"建议"，所有指令必须通过 RiskRails 校验才会成交；
    · AI 不能修改风控参数（写在 config.json，由人控制）；
    · AI 只能在 持仓 ∪ 观察池 ∪ 候选池 内交易，杜绝编造代码；
    · 每次决策的提示词、原始输出、校验结果全部落盘，可完整审计。
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime

from . import (altdata, feed, holdings, indicators as ind, llm as llm_mod, mailer,
               market, memory, monitor, notify, paper, screener as scr)
from .notify import color

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AI_DIR = os.path.join(BASE, "data", "ai")
STATE_PATH = os.path.join(AI_DIR, "paper_state.json")
WATCHLIST_PATH = os.path.join(AI_DIR, "watchlist.json")
REPORT_DIR = os.path.join(AI_DIR, "reports")
FAILED_DIR = os.path.join(AI_DIR, "failed")
PLANS_DIR = os.path.join(AI_DIR, "plans")
PROMPT_LOG = os.path.join(AI_DIR, "prompts")
USAGE_LOG = os.path.join(AI_DIR, "llm_usage.jsonl")

RAILS_DEFAULT = {
    "max_position_pct": 40.0,        # 单只股票占总资金上限
    "max_total_position_pct": 80.0,  # 总仓位上限（始终留 20% 现金）
    "max_trades_per_day": 4,
    "max_risk_per_trade_pct": 1.5,   # 单笔（成本-止损）最大亏损占总资金
    "daily_loss_limit_pct": 3.0,     # 当日亏损超过即当日禁止开新仓
    "min_price": 2.0,
    "max_price": 40.0,
    "price_band_pct": 2.5,           # 委托价与最新价最大偏离
    "forbid_buy_pct": 8.0,           # 当日涨幅超过该值禁止买入（不追涨停）
    "forbid_sell_pct": -8.0,         # 当日跌幅超过该值不卖（避免恐慌盘割在地板）
    "lot": 100,
    "min_shares": 200,               # 最小开仓股数（少于此手续费不经济）
    # 组合级风控（AI 自己在复盘里提出的：单笔合规不等于组合合规）
    "max_total_risk_pct": 2.0,       # 全部持仓「到止损的距离」之和占总资金上限
}

# 下单执行策略
DEFAULT_EXEC = {
    "defer_outside_session": True,   # 非交易时段只生成计划单，不用收盘价事后成交
    "cancel_if_gap_pct": 1.5,        # 次日高开超过该幅度则取消买入计划
}

SYSTEM_STRATEGIST = """你是一位在 A 股市场用真金白银交易的操盘手，风格务实、重视赔率与纪律。你的判断必须建立在给定数据（K线、指标、资金）之上，禁止编造成交量、价格或消息。
你必须严格遵守系统给出的硬性风控约束；任何越界指令都会被系统拒绝并记录为失误。
你只输出 JSON，不输出任何其它文字。"""

SYSTEM_REVIEWER = """你是一位极其诚实的交易复盘员，会用数据审判自己的每一笔交易。
你痛恨自我欺骗：亏损就是亏损，运气好也要点出来，不给自己找借口。
你的复盘要能改变明天的行为，而不是写正确的废话。
你只输出一个 JSON 对象，不输出任何其它文字。"""

SYSTEM_PLAYBOOK = """你是同一位交易者，现在要把今天的复盘结论沉淀成自己的《交易手册》。
你写的是给自己下次开盘照做的操作细则，必须具体、可机械执行、可验证。
硬性格式要求：
- 第一行必须是一级标题，形如 `# 交易手册 vN`，之前不许有任何一句话（不要写"已读取数据""结论是"之类前言）；
- 全文纯 Markdown，不要代码围栏，不要结尾寒暄；
- 结构固定：一、市场观；二、开仓条件（逐条可判定）；三、卖出条件；四、仓位与风控纪律；五、本期观察问题；六、变更记录（写清这次改了什么、为什么）；
- 保留上一版仍有效的条目，只在有理由时修改；总长度 800~1800 字。"""


def clean_playbook(text):
    """把手册正文规范化：去掉首个 # 标题之前的前言/闲聊，剥掉代码围栏。"""
    if not text:
        return ""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    if t.endswith("```"):
        t = t[:-3].rstrip()
    i = t.find("\n# ")
    if not t.startswith("#"):
        if i >= 0:
            t = t[i + 1:]          # 丢掉标题前的前言
        else:
            t = "# 交易手册\n\n" + t
    return t.strip()


# --------------------------------------------------------------------------- #
# 硬风控
# --------------------------------------------------------------------------- #
class RiskRails:
    def __init__(self, cfg, capital):
        self.cfg = {**RAILS_DEFAULT, **(cfg or {})}
        self.capital = float(capital)

    def describe(self):
        c = self.cfg
        return ("单股≤%.0f%%｜总仓≤%.0f%%｜每日最多%d笔｜单笔风险≤%.1f%%｜组合风险≤%.1f%%｜"
                "当日亏损>%.0f%%即停止开仓｜价格区间 %.1f~%.1f 元｜委托价偏离≤%.1f%%"
                % (c["max_position_pct"], c["max_total_position_pct"],
                   c["max_trades_per_day"], c["max_risk_per_trade_pct"],
                   c.get("max_total_risk_pct", 2.0),
                   c["daily_loss_limit_pct"], c["min_price"], c["max_price"],
                   c["price_band_pct"]))

    def check(self, order, snap, quote, day_state, cap_override=None):
        """返回 (ok, reason, shares)。order 为 AI 给出的原始指令。"""
        c = self.cfg
        code = order.get("code")
        act = (order.get("action") or "").upper()
        if act not in ("BUY", "SELL", "REDUCE", "HOLD"):
            return False, "未知动作 %s" % act, 0
        if act == "HOLD":
            return True, "持有", 0
        px = quote.get("price")
        limit = order.get("limit_price") or px
        if not px:
            return False, "无有效行情（可能停牌）", 0
        if not (c["min_price"] <= px <= c["max_price"]):
            return False, "价格 %.2f 超出可交易区间" % px, 0
        dev = abs(limit / px - 1) * 100
        if dev > c["price_band_pct"]:
            return False, "委托价 %.3f 偏离最新价 %.2f%%（上限 %.1f%%）" % (
                limit, dev, c["price_band_pct"]), 0

        shares = int(order.get("shares") or 0)
        pos = next((p for p in snap["positions"] if p["code"] == code), None)

        if act == "BUY":
            if day_state["loss_limit_hit"]:
                return False, "当日亏损已达熔断线，禁止开新仓", 0
            if day_state["trades"] >= c["max_trades_per_day"]:
                return False, "已达当日最大交易笔数 %d" % c["max_trades_per_day"], 0
            if (quote.get("pct") or 0) >= c["forbid_buy_pct"]:
                return False, "当日已涨 %.1f%%，禁止追高买入" % (quote.get("pct") or 0), 0
            if shares <= 0:
                return False, "买入股数为 0", 0
            shares = int(shares // c["lot"] * c["lot"])
            # 单股仓位上限
            cur_val = (pos["market_value"] if pos else 0.0)
            cap_pct = min(c["max_position_pct"],
                          float(cap_override) if cap_override else 999)
            max_val = self.capital * cap_pct / 100
            add_val = shares * px
            if cur_val + add_val > max_val + 1:
                shares = int(((max_val - cur_val) / px) // c["lot"] * c["lot"])
                if shares <= 0:
                    return False, "已达单股仓位上限 %.0f%%（含大盘温度收紧）" % cap_pct, 0
            # 总仓位上限
            total_val = snap["market_value"]
            max_total = self.capital * c["max_total_position_pct"] / 100
            if total_val + shares * px > max_total + 1:
                shares = int(((max_total - total_val) / px) // c["lot"] * c["lot"])
                if shares <= 0:
                    return False, "已达总仓位上限 %.0f%%" % c["max_total_position_pct"], 0
            # 风险预算
            stop = order.get("stop") or (quote.get("low") or px)
            if stop >= px:
                return False, "止损价 %.2f 不低于买入价 %.2f，指令无效" % (stop, px), 0
            budget = self.capital * c["max_risk_per_trade_pct"] / 100
            max_by_risk = int((budget / (px - stop)) // c["lot"] * c["lot"])
            if max_by_risk <= 0:
                return False, "止损距离过宽，单笔风险超预算", 0
            if shares > max_by_risk:
                shares = max_by_risk
            # 现金
            cash = snap["cash"]
            if shares * px * 1.001 > cash:
                shares = int((cash / (px * 1.001)) // c["lot"] * c["lot"])
                if shares <= 0:
                    return False, "现金不足", 0
            if shares < int(c.get("min_shares", 200)):
                return False, "算下来只能买 %d 股（< %d 股），手续费不经济，放弃本次开仓" % (
                    shares, int(c.get("min_shares", 200))), 0
            # 组合级风险预算：已有持仓「到止损的距离」+ 本次新增，不得超过上限
            total_budget = self.capital * float(c.get("max_total_risk_pct", 2.0)) / 100
            used = 0.0
            for p in snap["positions"]:
                sp = p.get("stop") or 0
                if sp and p.get("price") and p["price"] > sp:
                    used += (p["price"] - sp) * p.get("shares", 0)
            new_risk = (px - stop) * shares
            if used + new_risk > total_budget + 1e-6:
                room = total_budget - used
                if room <= 0:
                    return False, ("组合止损风险已用 %.0f/%.0f 元（达上限 %.1f%%），本次不开仓"
                                   % (used, total_budget, c.get("max_total_risk_pct", 2.0))), 0
                shares = int((room / (px - stop)) // c["lot"] * c["lot"])
                if shares < int(c.get("min_shares", 200)):
                    return False, ("组合风险预算只剩 %.0f 元，只能买 %d 股，不经济，放弃"
                                   % (room, shares)), 0
            return True, "过风控（%d 股，本笔风险 %.0f 元 / 组合 %.0f+%.0f=%.0f 元，上限 %.0f）" % (
                shares, new_risk, used, new_risk, used + new_risk, total_budget), shares

        # SELL / REDUCE
        if not pos:
            return False, "无该股持仓，无法卖出", 0
        avail = pos.get("available", 0)
        if avail <= 0:
            return False, "T+1 限制，今日无可卖股份", 0
        if (quote.get("pct") or 0) <= c["forbid_sell_pct"]:
            return False, "当日跌幅 %.1f%% 过大，暂缓卖出（次日处理）" % (quote.get("pct") or 0), 0
        if act == "SELL" or shares <= 0 or shares >= pos["shares"]:
            shares = avail
        else:
            shares = min(int(shares // c["lot"] * c["lot"]), avail)
        if shares <= 0:
            return False, "可卖股数为 0", 0
        return True, "通过风控（卖出 %d 股）" % shares, shares


# --------------------------------------------------------------------------- #
# AI 操盘手
# --------------------------------------------------------------------------- #
class AITrader:
    def __init__(self, cfg):
        self.cfg = cfg
        ai = cfg.get("ai") or {}
        self.ai_cfg = ai
        self.total_capital = float((cfg.get("account") or {}).get("total_capital", 15000))
        os.makedirs(AI_DIR, exist_ok=True)
        os.makedirs(REPORT_DIR, exist_ok=True)
        os.makedirs(PROMPT_LOG, exist_ok=True)
        self.llm = llm_mod.make_llm(ai)
        self.broker = paper.PaperBroker(STATE_PATH, capital=self.total_capital)
        self.rails = RiskRails(ai.get("rails"), self.total_capital)
        self.max_bars = int(ai.get("bars_in_prompt", 15))
        self.watchlist = self._load_watchlist()

    # ---------------- 状态 ----------------
    def _load_watchlist(self):
        if os.path.exists(WATCHLIST_PATH):
            try:
                with open(WATCHLIST_PATH, encoding="utf-8-sig") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return [c for c in (data.get("codes") or []) if c]
                return [c for c in data if c]
            except Exception:
                return []
        return []

    def _save_watchlist(self, codes, meta=None):
        os.makedirs(AI_DIR, exist_ok=True)
        with open(WATCHLIST_PATH, "w", encoding="utf-8") as f:
            json.dump({"codes": codes, "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
                       "meta": meta or {}}, f, ensure_ascii=False, indent=1)
        self.watchlist = codes

    def _day_state(self, snap):
        today = datetime.now().strftime("%Y-%m-%d")
        trades_today = [t for t in self.broker.state["trades"] if t["ts"].startswith(today)]
        open_eq = None
        for e in self.broker.state["equity_history"]:
            if e["date"] == today:
                open_eq = e["equity"]
                break
        ref = open_eq if open_eq else self.broker.state["capital"]
        loss_pct = (ref - snap["equity"]) / ref * 100 if ref else 0
        return {"trades": len(trades_today),
                "loss_pct": round(loss_pct, 2),
                "loss_limit_hit": loss_pct >= self.rails.cfg["daily_loss_limit_pct"]}

    # ---------------- 上下文 ----------------
    def _bars_brief(self, code, n=None):
        n = n or self.max_bars
        bars = feed.get_kline(code, 101, limit=max(n + 60, 80), adjust=1)
        closes = [b["close"] for b in bars]
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]
        atr = ind.atr(highs, lows, closes, 14)[-1] or closes[-1] * 0.02
        ma = {k: ind.sma(closes, v)[-1] for k, v in
              (("5", 5), ("10", 10), ("20", 20), ("60", 60))}
        dif, dea, hist = ind.macd(closes)
        rsi6 = ind.rsi(closes, 6)[-1]
        k, d, j = ind.kdj(highs, lows, closes)
        pctpos, lo60, hi60 = ind.price_position(closes, 60)
        vols = [b["vol"] for b in bars]
        avg5 = sum(vols[-6:-1]) / 5 if len(vols) >= 6 else sum(vols) / len(vols)
        recent = [[b["date"][5:], b["open"], b["high"], b["low"], b["close"],
                   round(b["vol"] / 1e4, 1)] for b in bars[-n:]]
        return {
            "最近K线[日期,开,高,低,收,量万手]": recent,
            "均线": {"MA5": round(ma["5"], 2) if ma["5"] else None,
                     "MA10": round(ma["10"], 2) if ma["10"] else None,
                     "MA20": round(ma["20"], 2) if ma["20"] else None,
                     "MA60": round(ma["60"], 2) if ma["60"] else None},
            "MACD": {"DIF": round(dif[-1], 3), "DEA": round(dea[-1], 3),
                     "柱": round(hist[-1], 3),
                     "前柱": round(hist[-2], 3) if len(hist) > 1 else None},
            "RSI6": round(rsi6, 1) if rsi6 else None,
            "KDJ": [round(k[-1], 1) if k[-1] else None,
                    round(d[-1], 1) if d[-1] else None,
                    round(j[-1], 1) if j[-1] else None],
            "ATR": round(atr, 3),
            "60日区间分位%": pctpos,
            "60日低/高": [round(lo60, 2), round(hi60, 2)],
            "量能倍数": round(vols[-1] / avg5, 2) if avg5 else None,
            "近20日低点": round(min(lows[-20:]), 2) if len(lows) >= 20 else None,
            "近20日高点": round(max(highs[-20:]), 2) if len(highs) >= 20 else None,
        }

    def _position_brief(self, p):
        q = feed.get_quote(p["code"])
        brief = {"code": p["code"], "name": p["name"] or q.get("name"),
                 "现价": q.get("price"), "今日涨跌%": q.get("pct"),
                 "持仓股数": p["shares"], "可卖股数": p.get("available", 0),
                 "成本": p["cost"], "浮动盈亏%": p["pnl_pct"],
                 "入场日": p.get("entry_date"), "我的止损": p.get("stop"),
                 "我的目标": p.get("target"), "当初理由": p.get("note") or ""}
        if p.get("stop") and q.get("price") and q["price"] <= p["stop"]:
            brief["⚠"] = "现价已跌破我设定的止损"
        brief.update(self._bars_brief(p["code"]))
        return brief

    def _market_brief(self):
        try:
            idx = feed.get_indices()
        except Exception:
            idx = []
        return {"大盘": [{"名称": i["name"], "点位": i["price"], "涨跌%": i["pct"]} for i in idx],
                "时间": datetime.now().strftime("%Y-%m-%d %H:%M")}

    def _snapshot(self):
        prices = {}
        for code in list(self.broker.positions.keys()):
            try:
                prices[code] = feed.get_quote(code).get("price")
            except Exception:
                pass
        return self.broker.mark(prices)

    # ---------------- ① 选股会 ----------------
    def phase_pick(self, verbose=True):
        if verbose:
            print(color("\n【① 选股会】规则初筛中…", "bold"))
        ai_cfg = self.cfg.get("ai") or {}
        feats = scr.screen({**(ai_cfg.get("screen") or {}), "_ai_cfg": ai_cfg})
        if not feats:
            print(color("  候选池为空（可能非交易时段或接口异常）", "yellow"))
            return None
        briefs = scr.brief_for_ai(feats)
        snap = self._snapshot()
        payload = {
            "task": "pick",
            "date": datetime.now().strftime("%Y-%m-%d"),
            "market": self._market_brief(),
            "我的策略手册": memory.read_playbook()[:2500],
            "近期日记": memory.recent_journal(3, 1200),
            "当前持仓": [{"code": p["code"], "浮动盈亏%": p["pnl_pct"]} for p in snap["positions"]],
            "候选池(规则初筛后)": briefs,
            "要求": {
                "输出JSON": {"market_view": "一句话概括今日市场",
                          "watchlist": ["6位股票代码，最多5只"],
                          "reason": "为什么选它们（结合手册与形态）"},
                "原则": "只选你愿意在明确止损位下买入的票；宁缺毋滥；可只选1-2只或空仓。",
            },
        }
        user = json.dumps(payload, ensure_ascii=False)
        self._log_prompt("pick", user)
        text, usage = self.llm.chat(SYSTEM_STRATEGIST, user,
                                    max_tokens=self._budget("max_tokens_pick", 2000))
        data = llm_mod.extract_json(text) or {}
        self._log_output("pick", text, usage)
        if not data:
            self._capture_failure("pick", text, user)
            print(color("  AI 输出无法解析为 JSON，本次不更新观察池（原文已存 data/ai/failed/）",
                        "yellow"))
            return None
        codes = [c for c in (data.get("watchlist") or []) if str(c).isdigit() and len(str(c)) == 6]
        allowed = {f["code"] for f in feats}
        codes = [c for c in codes if c in allowed][:5]
        self._log_output("pick", text, usage)
        if not codes:
            print(color("  AI 今日选择空仓/未给出标的：%s" % (data.get("market_view") or ""), "yellow"))
            self._save_watchlist([], {"market_view": data.get("market_view")})
        else:
            self._save_watchlist(codes, {"market_view": data.get("market_view"),
                                         "reason": data.get("reason")})
            print(color("  AI 今日观察池：%s" % "、".join(codes), "bold"))
            print("  理由：%s" % (data.get("reason") or ""))
        memory.append_journal("【选股会】%s\n观察池：%s\n理由：%s"
                              % (data.get("market_view", ""), codes, data.get("reason", "")),
                              phase="选股")
        return data

    # ---------------- ② 操盘决策 ----------------
    def phase_decide(self, verbose=True, force_plan=False):
        if verbose:
            print(color("\n【② 操盘决策】收集盘面数据…", "bold"))
        snap = self._snapshot()
        day = self._day_state(snap)
        positions = []
        for p in snap["positions"]:
            try:
                positions.append(self._position_brief(p))
            except Exception:
                continue
        watch = []
        for code in self.watchlist:
            try:
                q = feed.get_quote(code)
                item = {"code": code, "name": q.get("name"), "现价": q.get("price"),
                        "今日涨跌%": q.get("pct"), "成交额亿": round((q.get("amount") or 0) / 1e8, 2)}
                item.update(self._bars_brief(code))
                try:
                    weak, sname, spct = market.is_weak(
                        code, market.sector_rules(self.ai_cfg)["weak_pct"])
                    item["所属板块"] = "%s(%+.2f%%)" % (sname, spct if spct is not None else 0)
                    if weak:
                        item["⚠板块"] = "所属板块弱势，按规则不看"
                        print(color("  ⊘ 观察池剔除 %s：板块 %s %.2f%% 偏弱"
                                    % (code, sname, spct or 0), "yellow"))
                        continue
                except Exception:
                    pass
                watch.append(item)
            except Exception:
                continue

        try:
            temp_info = market.market_temp()
            sector_rows = [{"板块": s["name"], "涨幅%": s["pct"],
                            "主力净流入万": round(s["main_in"] / 1e4, 0)}
                           for s in market.sector_board(8)]
            gate_info = market.gate(temp_info)
        except Exception as exc:                       # noqa: BLE001
            temp_info, sector_rows, gate_info = None, [], None
            print(color("  （大盘温度获取失败：%s，按常规风控）" % exc, "grey"))
        # 持仓与观察池的所属板块（只用前几只，控制 token）
        try:
            for item in (positions[:3] + watch[:3]):
                secs = market.stock_sectors(item.get("code") or "")
                if secs:
                    item["所属板块"] = "%s(%+.2f%%) / %s(%+.2f%%)" % (
                        secs[0]["name"], secs[0]["pct"],
                        secs[1]["name"] if len(secs) > 1 else "-",
                        secs[1]["pct"] if len(secs) > 1 else 0)
        except Exception:
            pass
        # 可选扩展数据：财报 / 股东 / 龙虎榜（未安装 akshare/tushare 时自动跳过）
        try:
            use_alt = bool((self.ai_cfg or {}).get("use_altdata", True))
            token = (self.ai_cfg or {}).get("tushare_token", "")
            for item in (positions[:3] + watch[:3]):
                if not use_alt:
                    break
                c = item.get("code")
                if not c:
                    continue
                txt = altdata.compact(c, token=token, use=use_alt)
                if txt:
                    item["基本面"] = txt
        except Exception:
            pass
        # 我的真实持仓（只读参考，AI 的模拟盘照旧独立运作）
        my_holds = []
        try:
            hd = holdings.load()
            for h in (hd.get("items") or [])[:6]:
                try:
                    q = feed.get_quote(h["code"]) or {}
                except Exception:
                    q = {}
                px = q.get("price")
                my_holds.append({
                    "code": h["code"], "name": h.get("name") or "",
                    "股数": h["shares"], "成本": h["cost"],
                    "现价": px,
                    "浮动盈亏%": round((px / h["cost"] - 1) * 100, 2) if px and h["cost"] else None,
                    "买入日期": h.get("date") or "", "备注": h.get("note") or "",
                })
            if my_holds:
                print(color("  我的持仓 %d 只已并入决策参考" % len(my_holds), "grey"))
        except Exception:
            my_holds = []
        day["gate"] = gate_info
        day["force_plan"] = bool(force_plan)      # 收盘后复盘用：只出次日计划单，不成交
        payload = {
            "task": "decide",
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "market": self._market_brief(),
            "账户": {"总资金": self.broker.state["capital"], "现金": snap["cash"],
                    "持仓市值": snap["market_value"], "总权益": snap["equity"],
                    "累计收益%": snap["total_return_pct"],
                    "今日已交易笔数": day["trades"], "今日浮亏%": day["loss_pct"],
                    "今日是否触发亏损熔断": day["loss_limit_hit"]},
            "硬性风控(不可违反)": self.rails.describe(),
            "我的策略手册": memory.read_playbook()[:2500],
            "最近教训": [x["lesson"] for x in memory.read_lessons(8)],
            "大盘温度": temp_info,
            "行业板块强度(涨幅前列)": sector_rows,
            "风控收紧(代码已生效，不可违反)": gate_info,
            "板块强度过滤规则": market.sector_rules(self.ai_cfg),
            "我的持仓(真实账户，AI 不能下单，只给建议)": my_holds,
            "额外要求": ("若「我的持仓(真实账户)」非空，必须在 JSON 里额外输出 "
                     "\"我的持仓操作建议\" 数组，每只一条："
                     '{"code":"6位代码","name":"名称","建议":"持有|减仓|清仓|加仓",'
                     '"触发位":"如跌破2.53清仓/站上2.74加仓","理由":"一句话具体理由"}。'
                     "建议要务实可执行、必须有触发价，不许说\"视情况而定\"。"
                     "这些是对真人账户的建议，不是模拟盘下单，不要和 actions 混在一起。"),
            "待执行计划单(昨日收盘时定下，今日开盘按规则执行)": self._load_plans(),
            "持仓": positions,
            "观察池": watch,
            "要求": {
                "输出JSON": {
                    "market_view": "一句话",
                    "actions": [{"code": "6位代码",
                                 "action": "BUY|SELL|REDUCE|HOLD",
                                 "shares": "整数，100 的倍数；SELL 可填 0 表示清仓",
                                 "limit_price": "委托价，须接近最新价",
                                 "stop": "买入必填止损价",
                                 "target": "目标价（可选）",
                                 "reason": "为什么（≤60字，说明依据的K线/指标/资金）"}],
                    "journal": "我今天的判断与心态（≤200字）"},
                "原则": ["没有明确止损位的开仓一律不要给",
                       "优先处理持仓（止损/止盈），再考虑新开仓",
                       "不确定就 HOLD，空仓也是决策",
                       "禁止编造未在数据中出现的代码或价格"],
            },
        }
        user = json.dumps(payload, ensure_ascii=False)
        self._log_prompt("decide", user)
        text, usage = self.llm.chat(SYSTEM_STRATEGIST, user,
                                    max_tokens=self._budget("max_tokens_decide", 2000))
        data = llm_mod.extract_json(text) or {}
        self._log_output("decide", text, usage)
        if not data:
            self._capture_failure("decide", text, user)
            print(color("  AI 输出无法解析为 JSON，本次不交易（原文已存 data/ai/failed/）", "yellow"))
            memory.append_journal("【操盘】模型输出解析失败，未交易。", phase="操盘")
            return None

        view = data.get("market_view") or ""
        print(color("  AI 市场判断：%s" % view, "cyan"))
        # 我的持仓操作建议落盘（前端仪表盘与邮件都会用）
        try:
            adv = data.get("我的持仓操作建议") or []
            if adv:
                path = os.path.join(BASE, "data", "holdings_advice.json")
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
                               "items": adv,
                               "market_view": data.get("market_view") or ""},
                              f, ensure_ascii=False, indent=1)
                print(color("  已生成 %d 只真实持仓的操作建议" % len(adv), "grey"))
        except Exception as exc:
            print(color("  （持仓建议保存失败：%s）" % exc, "yellow"))
        results = self._execute(data.get("actions") or [], snap, watch, day)
        memory.append_journal(
            "【操盘】%s\n%s\n执行：%s" % (view, data.get("journal", ""),
                                    "\n".join(results) if results else "无操作"),
            phase="操盘")
        self._write_report("decide", payload, data, results, usage)
        self.broker.record_equity(self._snapshot())
        return data

    def _execute(self, actions, snap, watch, day):
        allowed_codes = {p["code"] for p in snap["positions"]} | set(self.watchlist) \
            | {w["code"] for w in watch}
        quote_cache = {}
        results = []
        exec_cfg = {**DEFAULT_EXEC, **((self.ai_cfg or {}).get("execution") or {})}
        session_open = monitor.in_session(include_auction=False)
        deferred = bool(day.get("force_plan")) or \
            (not session_open and exec_cfg.get("defer_outside_session", True))
        for a in actions:
            code = str(a.get("code") or "")
            act = (a.get("action") or "").upper()
            reason = (a.get("reason") or "")[:120]
            if code not in allowed_codes:
                msg = "拒绝 %s：标的不在持仓/观察池内（防幻觉）" % code
                print(color("  ✘ " + msg, "green"))
                results.append(msg)
                continue
            if code not in quote_cache:
                try:
                    quote_cache[code] = feed.get_quote(code)
                except Exception as exc:
                    results.append("拒绝 %s：行情获取失败 %s" % (code, exc))
                    continue
            q = quote_cache[code]
            gate_info = day.get("gate") or {}
            if act == "BUY":
                try:
                    rules = market.sector_rules(self.ai_cfg)
                    if rules["enabled"]:
                        weak, sname, spct = market.is_weak(code, rules["weak_pct"])
                        if weak:
                            msg = ("拒绝买入 %s：所属板块「%s」当日 %.2f%%，"
                                   "低于阈值 %.1f%%，弱势板块不做"
                                   % (code, sname, spct or 0, rules["weak_pct"]))
                            print(color("  ✘ " + msg, "green"))
                            results.append(msg)
                            continue
                except Exception:
                    pass
            if act == "BUY" and gate_info and gate_info.get("allow_new") is False:
                msg = "拒绝买入 %s：%s" % (code, gate_info.get("note", "大盘弱势，禁止开新仓"))
                print(color("  ✘ " + msg, "green"))
                results.append(msg)
                continue
            ok, why, shares = self.rails.check(
                a, snap, q, day,
                cap_override=(gate_info or {}).get("max_position_pct"))
            name = q.get("name") or ""
            if not ok:
                msg = "拒绝 %s %s：%s" % (code, act, why)
                print(color("  ✘ " + msg, "green"))
                results.append(msg)
                continue
            if act == "HOLD":
                held = code in {p["code"] for p in snap["positions"]}
                msg = ("持有 %s：%s" if held else "观望 %s（无持仓）：%s") % (code, reason)
                print(color("  · " + msg, "grey"))
                results.append(msg)
                continue
            # 非交易时段：不下单，转为「次日计划单」（避免用收盘价事后成交）
            if deferred:
                self._add_plan(code, name, act, shares, q.get("price"),
                               a.get("stop"), a.get("target"), reason)
                msg = ("计划 %s %s %d 股（信号价 %.3f）→ 已存入次日计划单，"
                       "开盘按规则执行" % (code, act, shares, q.get("price") or 0))
                print(color("  ⏳ " + msg, "yellow"))
                results.append(msg)
                continue
            px = float(a.get("limit_price") or q.get("price"))
            px = max(min(px, q.get("high") or px), q.get("low") or px)   # 成交价夹在当日区间
            if act == "BUY":
                ok2, msg = self.broker.buy(code, px, shares, name=name,
                                           stop=a.get("stop"), target=a.get("target"),
                                           note=reason)
                snap2 = self._snapshot()
                if ok2:
                    day["trades"] += 1
                    snap.update(snap2)
            else:
                ok2, msg = self.broker.sell(code, px, shares, note=reason)
                if ok2:
                    day["trades"] += 1
                    snap.update(self._snapshot())
            print(color("  %s %s %s" % ("✔" if ok2 else "✘", code, msg),
                        "red" if act == "BUY" else "cyan"))
            results.append(("✔ " if ok2 else "✘ ") + "%s %s → %s（%s）" % (code, act, msg, reason))
            if ok2:
                notify.beep("warn")
                try:
                    mailer.send_trade(act, code, name, shares, px, px * shares,
                                      reason, account=snap, cfg=self.cfg)
                except Exception:
                    pass
        return results

    # ---------------- 次日计划单 ----------------
    def _plans_path(self):
        os.makedirs(PLANS_DIR, exist_ok=True)
        return os.path.join(PLANS_DIR, "pending_plans.json")

    def _load_plans(self):
        p = self._plans_path()
        if not os.path.exists(p):
            return []
        try:
            with open(p, encoding="utf-8-sig") as f:
                return json.load(f) or []
        except Exception:
            return []

    def _save_plans(self, plans):
        with open(self._plans_path(), "w", encoding="utf-8") as f:
            json.dump(plans, f, ensure_ascii=False, indent=1)

    def _add_plan(self, code, name, action, shares, signal_price, stop, target, reason):
        plans = self._load_plans()
        plans.append({"signal_date": datetime.now().strftime("%Y-%m-%d"),
                      "signal_time": datetime.now().strftime("%H:%M:%S"),
                      "code": code, "name": name, "action": action, "shares": shares,
                      "signal_price": signal_price, "stop": stop, "target": target,
                      "reason": reason})
        self._save_plans(plans)

    def phase_execute_plans(self, verbose=True):
        """开盘后执行上一交易日遗留的计划单（含隔夜跳空取消规则）。"""
        plans = self._load_plans()
        if not plans:
            if verbose:
                print(color("\n【执行计划】无待执行计划单。", "grey"))
            return []
        exec_cfg = {**DEFAULT_EXEC, **((self.ai_cfg or {}).get("execution") or {})}
        cancel_pct = float(exec_cfg.get("cancel_if_gap_pct", 1.5))
        today = datetime.now().strftime("%Y-%m-%d")
        if verbose:
            print(color("\n【执行计划】待执行 %d 条（开盘价偏离 >%.1f%% 或跳空跌破止损则取消）"
                        % (len(plans), cancel_pct), "bold"))
        snap = self._snapshot()
        day = {"trades": 0, "loss_pct": 0.0, "loss_limit_hit": False}
        remaining, results = [], []
        for pl in plans:
            if pl["signal_date"] >= today:           # 当天的计划等次日
                remaining.append(pl)
                continue
            code = pl["code"]
            try:
                q = feed.get_quote(code)
            except Exception as exc:
                results.append("跳过 %s：行情失败 %s" % (code, exc))
                remaining.append(pl)
                continue
            open_px = q.get("open") or q.get("price")
            sig = pl.get("signal_price") or open_px
            if not open_px:
                remaining.append(pl)
                continue
            gap = (open_px / sig - 1) * 100 if sig else 0
            if pl["action"] == "BUY" and gap > cancel_pct:
                msg = "取消买入 %s：高开 %.2f%%（>%.1f%%），不追" % (code, gap, cancel_pct)
                print(color("  ⊘ " + msg, "yellow"))
                results.append(msg)
                continue
            if pl["action"] == "BUY" and pl.get("stop") and open_px <= pl["stop"]:
                msg = "取消买入 %s：开盘已跌破止损 %.2f" % (code, pl["stop"])
                print(color("  ⊘ " + msg, "yellow"))
                results.append(msg)
                continue
            order = {"code": code, "action": pl["action"], "shares": pl["shares"],
                     "limit_price": open_px, "stop": pl.get("stop"),
                     "target": pl.get("target")}
            ok, why, shares = self.rails.check(order, snap, q, day)
            if not ok:
                msg = "取消 %s：%s" % (code, why)
                print(color("  ⊘ " + msg, "yellow"))
                results.append(msg)
                continue
            if pl["action"] == "BUY":
                ok2, msg = self.broker.buy(code, open_px, shares, name=pl.get("name", ""),
                                           stop=pl.get("stop"), target=pl.get("target"),
                                           note="计划单执行：" + (pl.get("reason") or "")[:60])
            else:
                ok2, msg = self.broker.sell(code, open_px, shares,
                                            note="计划单执行：" + (pl.get("reason") or "")[:60])
            print(color("  %s %s %s（开盘 %.3f，信号 %.3f，缺口 %+.2f%%）"
                        % ("✔" if ok2 else "✘", code,
                           msg, open_px, sig, gap), "red" if ok2 else "green"))
            results.append(("%s " % ("✔" if ok2 else "✘")) + "%s %s" % (code, msg))
            if ok2:
                day["trades"] += 1
                snap = self._snapshot()
                notify.beep("warn")
        self._save_plans(remaining)
        if results:
            memory.append_journal("【执行计划】\n" + "\n".join(results), phase="执行")
        return results

    # ---------------- ③ 收盘复盘 ----------------
    def phase_review(self, verbose=True):
        if verbose:
            print(color("\n【③ 收盘复盘】AI 正在审判自己…", "bold"))
        snap = self._snapshot()
        stats = self.broker.stats()
        today = datetime.now().strftime("%Y-%m-%d")
        today_trades = [t for t in self.broker.state["trades"] if t["ts"].startswith(today)]
        payload = {
            "task": "review",
            "date": today,
            "账户与绩效": {"总权益": snap["equity"], "累计收益%": snap["total_return_pct"],
                       **{k: v for k, v in stats.items() if k != "equity"}},
            "今日成交": today_trades,
            "当前持仓": [{k: p[k] for k in ("code", "name", "shares", "cost", "price",
                                          "pnl_pct", "entry_date", "stop", "note")}
                       for p in snap["positions"]],
            "最近决策记录": [{"date": d.get("date"), "view": d.get("market_view"),
                         "actions": [{k: a.get(k) for k in ("code", "action", "shares", "reason")}
                                     for a in (d.get("actions") or [])]}
                        for d in memory.decision_history(2)][-6:],
            "今日日记": memory.recent_journal(1, 1200),
            "我的现用手册": memory.read_playbook()[:2500],
            "要求": {
                "输出JSON": {
                    "critique": "对今天操作的严厉评价（哪些是纪律，哪些是情绪/运气），150字内",
                    "lessons": ["≤3 条，具体到下次开盘能机械执行的规则"],
                },
                "标准": ["必须指出至少一个自己的问题（哪怕是仓位偏小机会成本）",
                       "教训必须能在下次开盘时被机械执行",
                       "禁止写正确的废话"],
            },
        }
        user = json.dumps(payload, ensure_ascii=False)
        self._log_prompt("review", user)
        text, usage = self.llm.chat(SYSTEM_REVIEWER, user, temperature=0.3,
                                    max_tokens=self._budget("max_tokens_review", 6000))
        self._log_output("review", text, usage)
        data = llm_mod.extract_json(text)
        if not data:
            data, _pb = llm_mod.split_sections(text)      # 兼容两段式输出
        if not data:
            self._capture_failure("review", text, user)
            print(color("  复盘输出解析失败：模型的 JSON 段不可解析。", "yellow"))
            print(color("  原文已保存到 data/ai/failed/，可在 Web「AI 决策」页查看诊断。", "grey"))
            if getattr(self.llm, "last_truncated", False):
                print(color("  原因很可能是输出被截断（finish_reason=length），"
                            "请把「设置 → max_tokens_review」调大到 8000。", "yellow"))
            return None

        print(color("  自我批评：%s" % (data.get("critique") or ""), "yellow"))
        for i, l in enumerate(data.get("lessons") or [], 1):
            print("   教训%d：%s" % (i, l))
        memory.add_lessons(data.get("lessons") or [])

        # 第二次调用：单独写手册（纯 Markdown，不塞 JSON，避免转义/截断）
        pb = None
        try:
            pb_prompt = json.dumps({
                "date": today,
                "今日复盘结论": data,
                "账户与绩效": {"总权益": snap["equity"], "累计收益%": snap["total_return_pct"],
                           "胜率%": stats["win_rate_pct"], "盈亏比": stats["profit_factor"],
                           "最大回撤%": stats["max_drawdown_pct"],
                           "已平仓笔数": stats["closed_trades"]},
                "今日成交": today_trades,
                "当前持仓": [{k: p[k] for k in ("code", "name", "shares", "cost",
                                              "price", "pnl_pct", "stop")}
                           for p in snap["positions"]],
                "上一版手册": memory.read_playbook()[:2500],
            }, ensure_ascii=False)
            self._log_prompt("playbook", pb_prompt)
            pb_text, pb_usage = self.llm.chat(
                SYSTEM_PLAYBOOK, pb_prompt, json_mode=False, temperature=0.35,
                max_tokens=self._budget("max_tokens_playbook", 4000))
            self._log_output("playbook", pb_text, pb_usage)
            cand = clean_playbook(pb_text or "")
            if len(cand) > 200:
                pb = cand
        except Exception as exc:                            # noqa: BLE001
            print(color("  手册重写调用失败（本次保留旧版手册）：%s" % exc, "yellow"))

        if pb:
            memory.save_playbook(pb, tag="review")
            print(color("  策略手册已更新（新版 %d 字），旧版已归档" % len(pb), "bold"))
        else:
            print(color("  本次未更新手册（模型未给出有效正文）", "grey"))
        memory.append_journal("【复盘】%s\n教训：%s" % (data.get("critique", ""),
                                                "；".join(data.get("lessons") or [])),
                              phase="复盘")
        note = ("策略手册已更新（%d 字）" % len(pb)) if (pb and len(pb) > 80) \
            else "手册未更新"
        self._write_report("review", payload, data, [note], usage)
        return data

    # ---------------- 运行调度 ----------------
    def run_once(self, phase="auto", verbose=True):
        now = datetime.now()
        if phase == "auto":
            t = now.time()
            from datetime import time as dtime
            if verbose:
                notify.banner("AI 操盘手  %s（%s）" % (now.strftime("%Y-%m-%d %H:%M"), self.llm.name))
                print(color("风控：" + self.rails.describe(), "grey"))
                print(color("本金 %.0f 元｜现金 %.2f 元｜持仓 %d 只"
                            % (self.broker.state["capital"], self.broker.cash,
                               len(self.broker.positions)), "grey"))
            self.broker.new_day()
            if dtime(15, 0) <= t <= dtime(16, 30):
                self.phase_review(verbose)
            elif dtime(15, 30) <= t <= dtime(23, 59) or dtime(0, 0) <= t <= dtime(8, 30):
                self.phase_pick(verbose)
            else:
                # 开盘后先执行昨日计划单，再考虑新决策
                self.phase_execute_plans(verbose)
                if not self.watchlist:
                    self.phase_pick(verbose)
                self.phase_decide(verbose)
        else:
            self.broker.new_day()
            {"pick": self.phase_pick, "decide": self.phase_decide,
             "review": self.phase_review,
             "execute": self.phase_execute_plans}.get(phase, self.phase_decide)(verbose)

    def status(self):
        snap = self._snapshot()
        stats = self.broker.stats()
        notify.banner("AI 操盘手状态  %s" % datetime.now().strftime("%Y-%m-%d %H:%M"))
        print("  模型：%s（本进程累计 %d 次调用，%d+%d tokens）"
              % (self.llm.name, self.llm.usage["calls"],
                 self.llm.usage["prompt_tokens"], self.llm.usage["completion_tokens"]))
        print("  本金 %.2f  现金 %.2f  持仓市值 %.2f  权益 %.2f  累计 %+.2f%%"
              % (self.broker.state["capital"], snap["cash"], snap["market_value"],
                 snap["equity"], snap["total_return_pct"]))
        print("  已平仓 %d 笔  胜率 %.1f%%  盈亏比 %s  最大回撤 %.2f%%  累计手续费 %.2f"
              % (stats["closed_trades"], stats["win_rate_pct"], stats["profit_factor"],
                 stats["max_drawdown_pct"], stats["total_fee"]))
        for p in snap["positions"]:
            print("    %s %s %d股 成本%.3f 现价%.2f 浮盈%+.2f%% 止损%s 目标%s"
                  % (p["code"], p["name"], p["shares"], p["cost"], p["price"],
                     p["pnl_pct"], p["stop"], p["target"]))
        if self.watchlist:
            print("  观察池：%s" % "、".join(self.watchlist))
        print("  手册字数：%d｜教训条数：%d"
              % (len(memory.read_playbook()), len(memory.read_lessons(100))))
        return snap

    # ---------------- 审计落盘 ----------------
    def _budget(self, key, default):
        return int((self.ai_cfg or {}).get(key) or default)

    def _capture_failure(self, kind, text, prompt):
        """模型输出不可解析时，把原文与提示词留档，便于事后诊断。"""
        try:
            os.makedirs(FAILED_DIR, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(FAILED_DIR, "%s_%s.txt" % (stamp, kind))
            with open(path, "w", encoding="utf-8") as f:
                f.write("=== finish_reason: %s ===\n"
                        % getattr(self.llm, "last_finish_reason", "?"))
                f.write("=== 原始输出 ===\n%s\n\n=== 提示词 ===\n%s\n" % (text or "", prompt))
            print(color("  失败原文：%s" % path, "grey"))
            return path
        except Exception:
            return None

    def _log_prompt(self, kind, user):
        path = os.path.join(PROMPT_LOG, "%s_%s_%s.txt" % (
            datetime.now().strftime("%Y%m%d_%H%M%S"), kind, self.llm.name))
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(user)
        except Exception:
            pass

    def _log_output(self, kind, text, usage):
        try:
            with open(USAGE_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    "kind": kind, "model": self.llm.name,
                                    "usage": usage, "out_chars": len(text or "")},
                                   ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _write_report(self, kind, payload, data, results, usage):
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        path = os.path.join(REPORT_DIR, "%s_%s.md" % (stamp, kind))
        lines = ["# AI %s 记录  %s" % (kind, datetime.now().strftime("%Y-%m-%d %H:%M:%S")), ""]
        lines.append("## 模型输出")
        lines.append("```json")
        lines.append(json.dumps(data, ensure_ascii=False, indent=1)[:4000])
        lines.append("```")
        if results:
            lines.append("## 风控执行结果")
            lines.extend("- " + r for r in results)
        lines.append("")
        lines.append("## 输入快照（审计用）")
        lines.append("```json")
        lines.append(json.dumps(payload, ensure_ascii=False)[:6000])
        lines.append("```")
        lines.append("")
        lines.append("_token 用量：%s_" % json.dumps(usage, ensure_ascii=False))
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        memory.log_decision({"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                             "kind": kind, "output": data, "results": results,
                             "market_view": data.get("market_view"),
                             "actions": data.get("actions")})
        return path
