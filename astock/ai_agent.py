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
from datetime import datetime, time as dtime, timedelta

from . import (altdata, evolution, feed, holdmode, holdings, hypothesis,
               indicators as ind, llm as llm_mod, mailer, market, memory,
               monitor, notify, paper, priors, rules as rules_mod,
               screener as scr, sizing)
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
    "max_price": 60.0,
    "price_band_pct": 2.5,           # 委托价与最新价最大偏离
    "forbid_buy_pct": 8.0,           # 当日涨幅超过该值禁止买入（不追涨停）
    "forbid_sell_pct": -8.0,         # 当日跌幅超过该值不卖（避免恐慌盘割在地板）
    "lot": 100,
    "min_shares": 100,               # 最小开仓股数（少于此手续费不经济）
    # 组合级风控（AI 自己在复盘里提出的：单笔合规不等于组合合规）
    "max_total_risk_pct": 3.0,       # 全部持仓「到止损的距离」之和占总资金上限
}

# 下单执行策略
DEFAULT_EXEC = {
    "defer_outside_session": True,   # 非交易时段只生成计划单，不用收盘价事后成交
    "cancel_if_gap_pct": 1.5,        # 次日高开超过该幅度则取消买入计划
}

def _participation_brief(cfg=None):
    """用户设定的参与度：决定"信号不完美时要不要参与"。"""
    ai = ((cfg or {}).get("ai") or {}) if isinstance(cfg, dict) else {}
    level = str(ai.get("participation") or "balanced").lower()
    table = {
        "conservative": ("保守", "只在信号齐全（趋势+量能+板块+止损明确）时开仓；不完美就空仓，接受错过。"),
        "balanced": ("均衡", "允许在候选质量合格时建 1 只「试错仓」：单只 ≤ 总资金 15%，必须带止损；"
                          "**试错仓不要求「评分变化≥+5」这类苛刻条件**，只要满足：板块非弱势、"
                          "止损明确且距离 ≤8%、单笔风险 ≤1.5%、组合风险预算有余量，即可建仓；"
                          "不得以「大盘过热 / 追高性价比低 / 计划单为空」为由完全不参与。"
                          "试错仓就是用来在震荡或过热行情里小仓试探的，跌破止损立刻离场。"),
        "aggressive": ("积极", "允许最多 2 只试错仓，合计 ≤ 总资金 30%，每只必须带止损；"
                             "目标是尽量参与强势行情，容忍更高回撤。"),
    }
    name, desc = table.get(level, table["balanced"])
    return {"档位": name, "规则": desc,
            "提醒": "试错仓不是重仓：先小仓试错，方向对了再按评分加仓；方向错了止损离场，不许扛。"}


def _idle_state():
    """统计连续多少个交易日没有开新仓，并给出"别把空仓当纪律"的提醒。"""
    try:
        import json as _j
        with open(os.path.join(BASE, "data", "ai", "paper_state.json"), encoding="utf-8-sig") as f:
            st = _j.load(f)
        trades = st.get("trades") or []
        buys = [t for t in trades if (t.get("side") or "").upper() == "BUY"]
        if not buys:
            return {"连续未开仓交易日": None, "提示": "还没有买入记录"}
        last = (buys[-1].get("ts") or "")[:10]
        gap = (datetime.now() - datetime.strptime(last, "%Y-%m-%d")).days
        tip = ""
        if gap >= 1:
            tip = ("已连续 %d 个自然日没有开新仓。空仓本身不是纪律，"
                   "只有在没有合格标的时才是。若候选池里有符合手册的票，应当给出买入计划。" % gap)
        return {"上次买入": last, "连续未开仓交易日": gap, "提示": tip}
    except Exception as exc:                       # noqa: BLE001
        return {"提示": "读取失败：%s" % exc}


def _failures_brief():
    try:
        from . import failures
        return failures.brief_for_prompt()
    except Exception as exc:                          # noqa: BLE001
        return "【失败档案】读取失败：%s" % exc


def _missed_brief():
    try:
        from . import missed
        return missed.brief_for_review()
    except Exception as exc:                          # noqa: BLE001
        return "【错过清单】读取失败：%s" % exc


def _active_rules():
    """给决策用的生效规则（只包含通过统计验证的）。"""
    try:
        view = rules_mod.active_for_prompt(1200)
        return {"规则": view["text"], "生效条数": view["active"]}
    except Exception:
        return {"规则": "（规则库不可用，按基础纪律执行）"}


def _pending_rule_count():
    try:
        return rules_mod.active_for_prompt()["pending"]
    except Exception:
        return 0


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
                   c.get("max_total_risk_pct", 3.0),
                   c["daily_loss_limit_pct"], c["min_price"], c["max_price"],
                   c["price_band_pct"]))

    def check(self, order, snap, quote, day_state, cap_override=None, force_open=False):
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
            if shares < int(c.get("min_shares", 100)):
                return False, "算下来只能买 %d 股（< %d 股），手续费不经济，放弃本次开仓" % (
                    shares, int(c.get("min_shares", 100))), 0
            # 组合级风险预算：已有持仓「到止损的距离」+ 本次新增，不得超过上限
            risk_pct = float(c.get("max_total_risk_pct", 3.0))
            if force_open:                       # 用户手动授权：组合风险预算放宽
                risk_pct = max(risk_pct, float(c.get("max_total_risk_pct_manual", 3.0)))
                print(color("  ⓘ 手动授权开仓：组合风险预算按 %.1f%% 计（原 %.1f%%）"
                            % (risk_pct, float(c.get("max_total_risk_pct", 2.0))), "yellow"))
            total_budget = self.capital * risk_pct / 100
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
                if shares < int(c.get("min_shares", 100)):
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
        held = holdmode.trading_days_held(p.get("entry_date"))
        if held:
            cap = holdmode.max_hold_days(self.ai_cfg)
            brief["已持有交易日"] = held
            brief["持有上限"] = "%d 个交易日（%s模式）" % (cap, holdmode.label(self.ai_cfg))
            if held >= cap:
                brief["⚠"] = "已超持有上限（%d 天 ≥ %d 天），代码会强制卖出" % (held, cap)
            elif held >= cap - 1:
                brief["⚠"] = "明天就满 %d 天，必须排卖出计划单" % cap
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

    def _score_now(self, code):
        """当日综合评分与"5 日前的评分"，用于评分变化率（回放显示它比绝对值更有信息量）。"""
        try:
            from . import strategy as _st
            # 评分要用 MA60 / 60 日分位，至少给 160 根（原来只取 45 根，
            # 长周期指标算不出来 → 评分恒为 0，会导致「评分变化≥+5」闸门永远过不了）
            bars = feed.get_kline(code, 101, limit=160)
            if len(bars) < 70:
                return None, None
            now = _st.evaluate(bars, len(bars) - 1).get("score")
            prev = _st.evaluate(bars, len(bars) - 6).get("score")
            chg = round(now - prev, 1) if (now is not None and prev is not None) else None
            return now, chg
        except Exception:
            return None, None

    def _index_close(self):
        """上证指数最新收盘（用于扣 beta 记账，失败返回 None）。"""
        try:
            if not hasattr(self, "_idx_cache") or self._idx_cache is None:
                bars = feed.get_index_kline("1.000001", 5)
                self._idx_cache = bars[-1]["close"] if bars else None
            return self._idx_cache
        except Exception:
            return None

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
            "手动授权说明": ("若今日决策记录中出现「用户手动授权开仓」标记，"
                              "那是用户在网页上主动点的强制开仓，属于用户指令，"
                              "不得计入你自己的流程违规，也不要在教训里写成'情绪追涨'；"
                              "只记录客观结果与可复用条件。"),
            "我的策略手册": memory.read_playbook(),
            "近期日记": memory.recent_journal(5, 8000),
            "当前持仓": [{"code": p["code"], "浮动盈亏%": p["pnl_pct"]} for p in snap["positions"]],
            "候选池(规则初筛后)": briefs,
            "要求": {
                "输出JSON": {"market_view": "一句话概括今日市场",
                          "watchlist": ["6位股票代码，3~8 只，按吸引力排序"],
                          "picks": [{"code": "6位代码", "关注点": "一句话：形态/位置/量能",
                                     "参考止损": "价格", "优先度": "高|中|低"}],
                          "reason": "为什么是这几只（结合手册与形态）"},
                "原则": [
                    "「选股会」产出的是**观察池**，不是下单指令——下单在后面的操盘决策里决定。",
                    "因此你必须从候选池里挑 3~8 只最值得跟踪的票，而不是直接判断'今天买不买'。",
                    "「市场过热/追高性价比低」不能成为不给候选的理由：过热恰恰说明要跟踪谁最强。",
                    "只有在候选池里全部不符合任何形态（突破/回踩/超跌/放量）时才允许少于 3 只，并说明原因。",
                    "每只都要给出参考止损位，便于后续决策直接使用。",
                ],
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
        codes = [c for c in codes if c in allowed][:8]
        self._log_output("pick", text, usage)
        if not codes:
            # 兜底：AI 没给标的时，用规则候选的前若干只填池，保证决策阶段有票可看
            fallback = [f["code"] for f in feats[:10]]
            print(color("  AI 未给出标的（%s）→ 用规则候选兜底写入观察池 %d 只"
                        % (data.get("market_view") or "", len(fallback)), "yellow"))
            self._save_watchlist(fallback, {"market_view": data.get("market_view"),
                                            "fallback": "screener", "ai_pick": []})
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
    def phase_decide(self, verbose=True, force_plan=False, force_open=False):
        if verbose:
            print(color("\n【② 操盘决策】收集盘面数据…", "bold"))
        snap = self._snapshot()
        day = self._day_state(snap)
        if force_open:
            day["force_open"] = True
            day["force_plan"] = False          # 手动授权要立刻成交，不等次日
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
                           for s in market.sector_board(20)]
            gate_info = market.gate(temp_info)
            if force_open:                       # 用户手动授权：解除闸门冻结
                gate_info = dict(gate_info or {})
                gate_info.update({
                    "mode": "manual-open",
                    "allow_new": True,
                    "max_position_pct": max(float(gate_info.get("max_position_pct") or 20.0), 20.0),
                    "note": "【用户手动授权】本轮的「不开新仓」冻结已解除；硬性风控（价格区间/单票上限/单笔止损/禁买≥8%）仍然生效",
                })
        except Exception as exc:                       # noqa: BLE001
            temp_info, sector_rows, gate_info = None, [], None
            print(color("  （大盘温度获取失败：%s，按常规风控）" % exc, "grey"))
        # 持仓与观察池的所属板块（只用前几只，控制 token）
        try:
            for item in (positions + watch):
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
            for item in (positions + watch):
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
        def _hold_cap(code):
            sc, chg = self._score_now(code)
            return sizing.cap_pct(sc, chg, None, 40.0)
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
                    "建议单股上限%": sizing.cap_pct(
                        (self._score_now(h["code"]) or [None, None])[0],
                        (self._score_now(h["code"]) or [None, None])[1], None, 40.0),
                })
            if my_holds:
                print(color("  我的持仓 %d 只已并入决策参考" % len(my_holds), "grey"))
        except Exception:
            my_holds = []
        day["gate"] = gate_info
        # 连续加权：评分 + 评分变化 → 单股上限（替代"过/不过"的开关）
        sizing_map = {}
        try:
            gate_cap = (gate_info or {}).get("max_position_pct")
            rails_cap = float((self.rails.cfg or {}).get("max_position_pct") or 40)
            for item in watch + positions:
                code = item.get("code")
                if not code:
                    continue
                sc, chg = self._score_now(code)
                cap = sizing.cap_pct(sc, chg, gate_cap, rails_cap)
                sizing_map[code] = cap
                item["评分"] = sc
                item["评分变化"] = chg
                item["建议单股上限%"] = cap
                item["仓位说明"] = sizing.explain(sc, chg)
            if sizing_map and verbose:
                print(color("  连续加权仓位：%d 只已按评分/变化率给出上限" % len(sizing_map), "grey"))
        except Exception as exc:                       # noqa: BLE001
            print(color("  （连续加权计算失败：%s）" % exc, "yellow"))
        day["sizing"] = sizing_map
        day["force_plan"] = bool(force_plan)      # 收盘后复盘用：只出次日计划单，不成交
        # 今日计划单的「执行情况」：收盘后的计划阶段先把旧单归档清空，
        # 让本轮生成的就是唯一的「明日计划单」——否则前端/邮件会一直显示昨天的残留单。
        pending_before = self._load_plans()
        if force_plan:
            pending_before = self._reset_plans("pre-plan")
        # 明日开盘会先执行这些确定性卖单（时间止损）：腾出的现金和风险额度
        # 明天是可以用来买的，所以买入额度必须按「卖出后」的预计账户来算，
        # 否则满仓状态下永远判「无新仓额度」，卖完的钱就干躺着。
        time_stops = []
        if force_plan:
            try:
                time_stops = self._time_stop_actions(snap, force_plan=True)
            except Exception as exc:                   # noqa: BLE001
                print(color("  （时间止损预演失败：%s）" % exc, "grey"))
        projected = self._project_after_sells(snap, time_stops) if time_stops else None
        proj_summary, proj_snap = projected if projected else (None, None)
        # 计划单是明天开盘执行的：风控要按「卖单先成交」之后的账户校验，
        # 否则今天的满仓状态会把明天本可以做的买入全拒掉。
        day["projected_snap"] = proj_snap
        payload = {
            "task": "decide",
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "market": self._market_brief(),
            "账户": {"总资金": self.broker.state["capital"], "现金": snap["cash"],
                    "持仓市值": snap["market_value"], "总权益": snap["equity"],
                    "累计收益%": snap["total_return_pct"],
                    "今日已交易笔数": day["trades"], "今日浮亏%": day["loss_pct"],
                    "今日是否触发亏损熔断": day["loss_limit_hit"]},
            "明日开盘后预计账户(卖单先成交，买入额度按这个算)": proj_summary or
                "本次没有到期卖单，买入额度按上面「账户」的实际数字算",
            "硬性风控(不可违反)": self.rails.describe(),
            "我的策略手册": memory.read_playbook(),
            "我的生效规则(经统计验证，权重最高)": _active_rules(),
            "候选规则(样本不足，仅供参考、不许当依据)": _pending_rule_count(),
            "大盘温度": temp_info,
            "行业板块强度(涨幅前列)": sector_rows,
            "风控收紧(代码已生效，不可违反)": gate_info,
            "系统已验证先验(代码维护，优先级最高，不可协商)": priors.text(),
            "板块强度过滤规则": market.sector_rules(self.ai_cfg),
            "失败档案(代码统计，必须主动规避)": _failures_brief(),
            "错过清单(最近被规则漏掉的最强票，用于反思自己的选股偏差)": _missed_brief(),
            "仓位加权规则(代码已生效)": {
                "说明": "单股上限 = min(大盘闸门, 风控上限) × 评分系数；"
                        "评分0-40→0.5、40-50→0.7、60-70→0.9、70+→1.0~1.1；"
                        "评分变化≥+5 再加 0.15，≤−5 减 0.20（回放验证：变化率比绝对值更有信息量）",
                "你的活": "在「观察池/持仓」里会看到每只票的 评分、评分变化、建议单股上限%，"
                          "按这个尺度决定买多少，而不是只决定买不买",
            },
            "持有期指引(用户模式，硬约束)": {
                "模式": "%s｜单票最多持有 %d 个交易日"
                        % (holdmode.label(self.ai_cfg), holdmode.max_hold_days(self.ai_cfg)),
                "铁律": "持有到期由代码强制卖出，你输出 HOLD 也会被覆盖；"
                        "买入那一刻就要把「第几天退出」算清楚，"
                        "不许把短线拖成中线（这是用户明确要求，优先级高于任何回放证据）",
                "回放证据(只作参考，不得用来违抗模式)":
                    "扣费扣beta后的平均超额：5日+0.30%、10日+0.92%、20日+2.34%"
                    "——长持有期统计上更好，但用户当前要的是短线滚动，按模式执行",
                "止盈参考": holdmode.preset(self.ai_cfg)["target_hint"],
            },
            "我的持仓(真实账户，AI 不能下单，只给建议)": my_holds,
            "额外要求": ("若「我的持仓(真实账户)」非空，必须在 JSON 里额外输出 "
                     "\"我的持仓操作建议\" 数组，每只一条："
                     '{"code":"6位代码","name":"名称","建议":"持有|减仓|清仓|加仓",'
                     '"触发位":"如跌破2.53清仓/站上2.74加仓","理由":"一句话具体理由"}。'
                     "建议要务实可执行、必须有触发价，不许说\"视情况而定\"。"
                     "这些是对真人账户的建议，不是模拟盘下单，不要和 actions 混在一起。"),
            "今日计划单执行回顾(开盘已执行/已过期归档，仅供复盘，不是明日指令)": pending_before,
            "关于「计划单为空」": ("计划单为空只代表昨天没有触发信号，"
                             "绝不等于今天禁止开仓。只要有候选池/观察池里符合手册的标的，"
                             "就应当正常给出 BUY 并落为计划单。"
                             "禁止用「昨日计划单为空」作为不开仓的理由——那会形成"
                             "『不开仓→计划单空→更不开仓』的死循环，属于系统性错失。"),
            "开仓节奏(代码统计)": _idle_state(),
            "参与度设定(用户偏好，必须遵守)": _participation_brief(self.cfg),
            "持有模式(用户偏好，代码会强制)": holdmode.brief(self.ai_cfg),
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
                    "明日关注": [{"code": "6位代码",
                               "name": "名称",
                               "关注理由": "≤40字，为什么明天值得盯（板块/K线/资金）",
                               "触发条件": "明天盘中看到就能执行的买点或条件，"
                                           "如『站上12.80且量比>1.5』『回踩11.50不破』",
                               "优先级": "高|中|低"}],
                    "journal": "我今天的判断与心态（≤200字）"},
                "原则": ["没有明确止损位的开仓一律不要给",
                       "优先处理持仓（止损/止盈），再考虑新开仓",
                       "不确定就 HOLD，空仓也是决策",
                       "禁止编造未在数据中出现的代码或价格",
                       "「明日关注」最多 5 只，只能从观察池/持仓里选；"
                       "即使今天不买，也要把明天最看好的几只写清楚——这是给用户看的明日清单",
                       "卖出就是为了换仓：若「明日开盘后预计账户」显示有可用现金与仓位空间，"
                       "就必须在这些额度内给出买入计划，把钱放着不动等于白卖"],
            },
        }
        if force_open:
            payload["【用户手动授权·主动开仓】"] = (
                "本轮由用户在网页上手动点了「强制开仓」，大盘闸门的冻结已解除。"
                "你必须从「观察池 / 候选」中选出 1 只（最多 2 只）最符合手册与先验的标的，"
                "给出 BUY，并写明止损价、整手股数与具体依据；股数按「建议单股上限%」取值。"
                "硬性风控不可违反（价格区间、单票上限、单笔止损、禁买涨幅≥8%、最少 200 股）。"
                "如果确实一个都不合格，允许输出空 actions 并逐条说明为什么不合格。")
            try:
                payload["要求"]["原则"] = list(payload["要求"]["原则"]) + [
                    "本轮是用户手动授权的主动开仓：至少给 1 条 BUY（除非全部标的不合格并说明理由）"]
            except Exception:
                pass
        user = json.dumps(payload, ensure_ascii=False)
        self._log_prompt("decide", user)
        try:
            text, usage = self.llm.chat(SYSTEM_STRATEGIST, user,
                                        max_tokens=self._budget("max_tokens_decide", 2000))
        except Exception as exc:                       # noqa: BLE001
            print(color("  ✘ 模型调用失败：%s" % str(exc)[:160], "yellow"))
            self._salvage_time_stops(time_stops, snap, watch, day)
            raise
        data = llm_mod.extract_json(text) or {}
        self._log_output("decide", text, usage)
        if not data:
            self._capture_failure("decide", text, user)
            print(color("  AI 输出无法解析为 JSON，本次不交易（原文已存 data/ai/failed/）", "yellow"))
            self._salvage_time_stops(time_stops, snap, watch, day)
            memory.append_journal("【操盘】模型输出解析失败，未交易。", phase="操盘")
            return None

        view = data.get("market_view") or ""
        print(color("  AI 市场判断：%s" % view, "cyan"))
        # 我的持仓操作建议落盘（前端仪表盘与邮件都会用）
        try:
            adv = data.get("我的持仓操作建议") or []
            if adv:
                # 统一字段名 + 补全股票名（模型有时只给代码）
                try:
                    from . import holdings as _H
                    nmap = {}
                    for _it in (_H.load().get("items") or []):
                        if _it.get("code"):
                            nmap[_it["code"]] = _it.get("name") or ""
                    for _p in (snap.get("positions") or []):
                        if _p.get("code") and _p.get("name"):
                            nmap.setdefault(_p["code"], _p["name"])
                    for _a in adv:
                        _a["建议"] = _a.get("建议") or _a.get("advice") or ""
                        _a["触发位"] = _a.get("触发位") or _a.get("trigger") or ""
                        _a["理由"] = _a.get("理由") or _a.get("reason") or ""
                        if not _a.get("name"):
                            _c = _a.get("code")
                            _a["name"] = nmap.get(_c) or ""
                            if not _a["name"]:
                                try:
                                    _a["name"] = feed.get_quote(_c).get("name") or ""
                                except Exception:
                                    pass
                except Exception:
                    pass
                path = os.path.join(BASE, "data", "holdings_advice.json")
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
                               "items": adv,
                               "market_view": data.get("market_view") or ""},
                              f, ensure_ascii=False, indent=1)
                print(color("  已生成 %d 只真实持仓的操作建议" % len(adv), "grey"))
        except Exception as exc:
            print(color("  （持仓建议保存失败：%s）" % exc, "yellow"))
        acts = data.get("actions") or []
        # ① 时间止损（代码强制）：持有到期/超期的持仓必须卖出，模型说 HOLD 也不算
        try:
            forced = time_stops or self._time_stop_actions(snap, force_plan=bool(force_plan))
            have_sell = {str(x.get("code") or "") for x in acts
                         if (x.get("action") or "").upper() in ("SELL", "REDUCE")}
            forced = [a for a in forced if a["code"] not in have_sell]
            if forced:
                acts = forced + acts
                data["actions"] = acts
                print(color("  ⏰ 时间止损（%s模式最多 %d 个交易日）：%s 到期 → 强制卖出"
                            % (holdmode.label(self.ai_cfg),
                               holdmode.max_hold_days(self.ai_cfg),
                               "、".join("%s %d股（已持有%d日）"
                                        % (a["code"], a["shares"], a["held"])
                                        for a in forced)), "yellow"))
        except Exception as exc:                       # noqa: BLE001
            print(color("  （时间止损判定失败：%s）" % exc, "grey"))
        # ② 明日关注清单（用户就想知道「明天哪些票看好」）
        if force_plan:
            try:
                items = data.get("明日关注") or []
                if not items:
                    items = self._fallback_watch_items(watch, 3)
                    if items:
                        print(color("  ⓘ 模型没给「明日关注」，用观察池评分最高的 %d 只兜底"
                                    % len(items), "yellow"))
                self._save_tomorrow_watch(items, self._plan_for_date(True))
            except Exception as exc:                   # noqa: BLE001
                print(color("  （明日关注保存失败：%s）" % exc, "grey"))
        try:
            _ai = (self.cfg.get("ai") or {}) if isinstance(self.cfg, dict) else {}
            need = int(_ai.get("plan_target_count") or 3)
            have = [x for x in acts if (x.get("action") or "").upper() == "BUY"]
            if len(have) < need:
                # 额度按「卖完之后」的账户算：卖出的钱明天是可以再买的
                extra = self._trial_candidates(
                    watch, day, proj_snap or snap, need=need - len(have),
                    exclude={str(x.get("code") or "") for x in have}
                    | {a["code"] for a in time_stops})
                if extra:
                    acts.extend(extra)
                    data["actions"] = acts
                    print(color("  ⓘ 计划补足：AI 给了 %d 只买入，代码补 %d 只试错仓 → %s"
                                % (len(have), len(extra),
                                   "、".join("%s(%d股)" % (x["code"], x["shares"]) for x in extra)),
                                "yellow"))
        except Exception as exc:                       # noqa: BLE001
            print(color("  （试错仓判定失败：%s）" % exc, "grey"))
        # ③ 明日总交易笔数是硬上限（卖单也算）：计划里多出来的买单明天一定会被拒，
        #    不如现在就砍掉，免得邮件里出现执行不了的计划。
        if force_plan:
            try:
                cap_trades = int(float((self.rails.cfg or {}).get("max_trades_per_day") or 4))
                n_sell = sum(1 for a in acts
                             if (a.get("action") or "").upper() in ("SELL", "REDUCE"))
                room = max(0, cap_trades - n_sell)
                kept, dropped = 0, 0
                trimmed = []
                for a in acts:
                    if (a.get("action") or "").upper() == "BUY":
                        if kept >= room:
                            dropped += 1
                            continue
                        kept += 1
                    trimmed.append(a)
                if dropped:
                    acts = trimmed
                    data["actions"] = acts
                    print(color("  ⓘ 明日最多 %d 笔（卖单已占 %d 笔）→ 砍掉 %d 条执行不了的买入计划"
                                % (cap_trades, n_sell, dropped), "yellow"))
            except Exception as exc:                   # noqa: BLE001
                print(color("  （交易笔数裁剪失败：%s）" % exc, "grey"))
        results = self._execute(acts, snap, watch, day)
        if force_plan:                       # 计划单写完后按每日笔数上限做最终裁剪
            try:
                self._trim_plans_to_trade_cap(self._plan_for_date(True))
            except Exception as exc:                   # noqa: BLE001
                print(color("  （计划单笔数裁剪失败：%s）" % exc, "grey"))
        memory.append_journal(
            "【操盘】%s\n%s\n执行：%s" % (view, data.get("journal", ""),
                                    "\n".join(results) if results else "无操作"),
            phase="操盘")
        try:
            hypothesis.record_from_decision(data, verbose=True)
        except Exception as exc:
            print(color("  （假设记录失败：%s）" % exc, "yellow"))
        self._write_report("decide", payload, data, results, usage)
        self.broker.record_equity(self._snapshot(),
                                  extra={"index": self._index_close()})
        return data

    def _trial_candidates(self, watch, day, snap=None, need=3, exclude=None):
        """均衡/积极档强制参与：挑出 need 只满足硬条件的试错仓。

        准入（代码判定）：评分 ≥ trial_min_score、评分变化 ≥ trial_min_score_chg、
        板块不弱势、止损距离 ≤8%、单笔风险 ≤ 均分后的剩余预算、现金买得起一手。
        风险预算按「本次还需几只」均分，保证 3 只都能落地而不互相挤占。
        exclude：AI 已经给出 BUY 的代码，避免同一只票重复下两张计划单。
        """
        exclude = exclude or set()
        ai = (self.cfg.get("ai") or {}) if isinstance(self.cfg, dict) else {}
        level = str(ai.get("participation") or "balanced").lower()
        if level == "conservative":
            return []
        if level == "aggressive":
            need = max(need, int(ai.get("plan_target_count") or 3))
        c = self.rails.cfg or {}
        snap = snap or {}
        equity = float(snap.get("equity") or 0)
        cash = float(snap.get("cash") or 0)
        budget_total = equity * float(c.get("max_total_risk_pct") or 4.0) / 100
        used = 0.0
        for p_ in snap.get("positions") or []:
            st = p_.get("stop")
            px_ = p_.get("price") or p_.get("last_price") or 0
            if st and px_:
                used += max(0.0, (float(px_) - float(st))) * int(p_.get("shares") or 0)
        remain = max(0.0, budget_total - used)
        if remain <= 0 or need <= 0:
            return []
        cands = []
        for it in watch or []:
            code = it.get("code")
            px = it.get("现价") or it.get("price")
            sc = it.get("评分")
            chg = it.get("评分变化")
            if not code or not px:
                continue
            if str(code) in exclude:          # AI 已经买过这只，不再重复补计划
                continue
            if it.get("⚠板块"):
                continue
            if sc is None or float(sc) < float(ai.get("trial_min_score", 50)):
                continue
            if chg is not None and float(chg) < float(ai.get("trial_min_score_chg", -6)):
                continue
            cands.append((float(sc) * 10 + float(chg or 0), str(code), it))
        cands.sort(key=lambda x: -x[0])
        out = []
        for _k, code, it in cands:
            if len(out) >= need:
                break
            px = float(it.get("现价") or it.get("price") or 0)
            lot = 200 if code.startswith(("688", "689")) else 100
            room = remain / max(1, need - len(out))
            sup = it.get("近20日支撑") or it.get("support20")
            try:
                stop = float(sup) if sup else px * 0.955
            except Exception:
                stop = px * 0.955
            if stop >= px or (px - stop) / px > 0.08:
                stop = round(px * 0.955, 2)
            max_dist = min(0.08, max(0.015, room / (px * lot)))
            if (px - stop) / px > max_dist:
                stop = round(px * (1 - max_dist), 2)
            stop = round(float(stop), 2)          # 价格统一两位小数
            if stop >= px:
                continue
            risk_ps = px - stop
            max_by_risk = int(room / (risk_ps * lot)) * lot
            cash_budget = min(cash, equity * float(ai.get("trial_max_pct", 15)) / 100 * need)
            max_by_cash = int((cash_budget / px) // lot * lot) if px else 0
            shares = min(int(ai.get("trial_shares", 200)), max_by_risk, max_by_cash)
            shares = int(shares // lot * lot)
            if shares < lot:
                continue
            cash -= shares * px
            remain -= risk_ps * shares
            out.append({"code": code, "name": it.get("name") or "", "action": "BUY",
                        "shares": shares, "limit_price": round(px, 3), "stop": stop,
                        "reason": ("均衡档试错仓（代码强制参与）：评分 %s、变化 %s、"
                                   "止损 %.2f（%.1f%%）、%d 股"
                                   % (it.get("评分"), it.get("评分变化"), stop,
                                      (px - stop) / px * 100, shares))})
        return out

    def _execute(self, actions, snap, watch, day):
        allowed_codes = {p["code"] for p in snap["positions"]} | set(self.watchlist) \
            | {w["code"] for w in watch}
        quote_cache = {}
        results = []
        exec_cfg = {**DEFAULT_EXEC, **((self.ai_cfg or {}).get("execution") or {})}
        session_open = monitor.in_session(include_auction=False)
        deferred = bool(day.get("force_plan")) or \
            (not session_open and exec_cfg.get("defer_outside_session", True))
        # 计划单在「执行日开盘」成交，而那一天开盘会先卖掉到期的仓位：
        #   · 买单 → 用预计（卖出后）快照校验，否则今天的满仓会把明天本可以做的买入全拒掉；
        #   · 卖单 → 仍用实时快照校验（预计快照里这些票已经算卖掉了）。
        proj_snap = day.get("projected_snap") if deferred else None
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
            gate_cap = (gate_info or {}).get("max_position_pct")
            size_cap = (day.get("sizing") or {}).get(code)
            caps = [x for x in (gate_cap, size_cap) if x]
            check_snap = proj_snap if (act == "BUY" and proj_snap) else snap
            ok, why, shares = self.rails.check(
                a, check_snap, q, day, cap_override=(min(caps) if caps else None),
                force_open=bool(day.get("force_open")))
            name = q.get("name") or ""
            if not ok:
                msg = "拒绝 %s %s：%s" % (code, act, why)
                print(color("  ✘ " + msg, "green"))
                results.append(msg)
                continue
            if act == "BUY" and day.get("force_open"):
                reason = ("【用户手动授权开仓】" + (reason or ""))[:120]
                a["reason"] = reason
            if act == "HOLD":
                held = code in {p["code"] for p in snap["positions"]}
                msg = ("持有 %s：%s" if held else "观望 %s（无持仓）：%s") % (code, reason)
                print(color("  · " + msg, "grey"))
                results.append(msg)
                continue
            # 非交易时段：不下单，转为计划单（避免用收盘价事后成交）
            if deferred:
                for_date = self._plan_for_date(bool(day.get("force_plan")))
                self._add_plan(code, name, act, shares, q.get("price"),
                               a.get("stop"), a.get("target"), reason,
                               for_date=for_date)
                msg = ("计划 %s %s %d 股（信号价 %.3f）→ 已存入计划单，执行日 %s 开盘按规则执行"
                       % (code, act, shares, q.get("price") or 0, for_date))
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
                    snap_before = snap.copy()
                    snap.update(self._snapshot())
                    # 亏损平仓 → 自动归档到失败档案（错题本）
                    try:
                        from . import failures as _FM
                        _pos = next((x for x in (snap_before.get("positions") or [])
                                    if x.get("code") == code), None)
                        _cost = float((_pos or {}).get("cost") or 0)
                        _pnl = (px / _cost - 1) * 100 if _cost else 0.0
                        if _pnl < -0.5:
                            _hold = None
                            _ed = (_pos or {}).get("entry_date")
                            if _ed:
                                try:
                                    _hold = (datetime.now() - datetime.strptime(_ed, "%Y-%m-%d")).days
                                except Exception:
                                    _hold = None
                            _FM.log_failure({
                                "code": code, "name": name, "entry": round(_cost, 3),
                                "exit": px, "shares": shares, "pnl_pct": round(_pnl, 2),
                                "hold_days": _hold,
                                "score": (_pos or {}).get("score"),
                                "sector_pct": (q or {}).get("sector_pct"),
                                "reason": reason,
                            }, verbose=verbose)
                    except Exception as _exc:                # noqa: BLE001
                        print(color("  （失败归档跳过：%s）" % _exc, "grey"))
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

    # ---------------- 计划单（含执行日归属） ----------------
    def _plans_path(self):
        os.makedirs(PLANS_DIR, exist_ok=True)
        return os.path.join(PLANS_DIR, "pending_plans.json")

    def _next_trading_date(self, base=None):
        """下一个交易日（跳过周末）。"""
        d = (base or datetime.now()).date() + timedelta(days=1)
        for _ in range(14):
            if monitor.is_trading_day(datetime.combine(d, dtime(9, 30))):
                break
            d += timedelta(days=1)
        return d.strftime("%Y-%m-%d")

    def _plan_for_date(self, force_plan=False):
        """计划单的「执行日」。

        · 盘前（09:30 之前）决策 → 当天开盘执行（否则会拖到明天重复下单）；
        · 收盘后计划阶段（force_plan）→ 下一个交易日；
        · 其余非交易时段 → 下一个交易日。
        """
        now = datetime.now()
        if not force_plan and monitor.is_trading_day(now) and now.time() < dtime(9, 30):
            return now.strftime("%Y-%m-%d")
        return self._next_trading_date(now)

    def _plan_exec_date(self, pl):
        """计划单的执行日；兼容没有 for_date 的历史数据（退回 signal_date）。"""
        return str((pl or {}).get("for_date") or (pl or {}).get("signal_date") or "")

    # ---------------- 时间止损（持有模式强制） ----------------
    def _project_after_sells(self, snap, sells):
        """把已排定的卖单先结算一遍，得到「明天开盘卖出之后」的账户。

        用途：收盘后的计划阶段按这个账户算买入额度。否则仓位 79% 的时候
        永远判「无新仓额度」，卖出腾出来的现金就只能干躺着（等于白卖）。
        返回 (给提示词看的摘要, 供额度计算用的预计快照)。
        """
        positions = []
        for p in (snap or {}).get("positions") or []:
            positions.append(dict(p))
        by_code = {p["code"]: p for p in positions}
        cash = float((snap or {}).get("cash") or 0)
        freed, sold = 0.0, []
        for a in sells or []:
            p = by_code.get(a.get("code"))
            if not p:
                continue
            shares = min(int(a.get("shares") or 0), int(p.get("shares") or 0))
            px = float(p.get("price") or a.get("limit_price") or 0)
            if shares <= 0 or px <= 0:
                continue
            proceeds = shares * px * 0.999        # 粗略扣掉卖出费用
            cash += proceeds
            freed += proceeds
            p["shares"] = int(p["shares"]) - shares
            p["market_value"] = round(px * p["shares"], 2)
            if p["shares"] <= 0:
                by_code.pop(p["code"], None)
            sold.append("%s %d股≈%.0f元" % (p.get("name") or a.get("code"), shares, proceeds))
        rest = list(by_code.values())
        mv = sum(float(p.get("price") or 0) * int(p.get("shares") or 0) for p in rest)
        risk = sum(max(0.0, float(p.get("price") or 0) - float(p.get("stop") or 0))
                   * int(p.get("shares") or 0) for p in rest)
        equity = cash + mv
        cap = float((self.rails.cfg or {}).get("max_total_position_pct") or 80.0)
        risk_cap_pct = float((self.rails.cfg or {}).get("max_total_risk_pct") or 3.0)
        risk_room = max(0.0, equity * risk_cap_pct / 100 - risk)
        summary = {
            "动作": "开盘先卖出：" + "、".join(sold) if sold else "无",
            "预计现金": round(cash, 2),
            "预计可用买入金额(按总仓上限 %.0f%% 算)" % cap: round(
                max(0.0, equity * cap / 100 - mv), 2),
            "预计持仓市值": round(mv, 2),
            "预计总权益": round(equity, 2),
            "预计仓位%": round(mv / equity * 100, 2) if equity else 0.0,
            "预计组合风险额": round(risk, 2),
            "预计组合风险%": round(risk / equity * 100, 2) if equity else 0.0,
            "预计还可增加的风险额(硬上限 %.1f%%)" % risk_cap_pct: round(risk_room, 2),
            "明日剩余可交易笔数(卖单已占 %d 笔)" % len(sold): int(
                max(0, float((self.rails.cfg or {}).get("max_trades_per_day") or 4)
                    - len(sold))),
            "说明": "卖出的钱明天是可以再买的：买入计划按这里的可用金额与组合风险额度来定。"
                    "若你手册里的自设目标比硬上限更严，先用硬上限判断「到底有没有空间」，"
                    "有空间就在空间内按手册谨慎取小；只有确实没有空间才允许零买入。",
        }
        proj_snap = {"cash": round(cash, 2), "market_value": round(mv, 2),
                     "equity": round(equity, 2), "positions": rest,
                     "total_return_pct": self.broker.state.get("capital") and
                     round((equity / float(self.broker.state["capital"]) - 1) * 100, 2)}
        return summary, proj_snap

    def _time_stop_actions(self, snap, force_plan=False):
        """把「持有到期」的持仓变成强制卖出动作。

        盘中（force_plan=False）：持有已达上限 N 天 → 立刻卖；
        收盘计划阶段（force_plan=True）：持有已达 N-1 天 → 排明日开盘卖出的计划单，
        这样正好在第 N 个交易日了结，不会把短线拖成中线。
        """
        cap = holdmode.max_hold_days(self.ai_cfg)
        need = max(1, cap - 1) if force_plan else cap
        out = []
        for p in (snap or {}).get("positions") or []:
            code = str(p.get("code") or "")
            held = holdmode.trading_days_held(p.get("entry_date"))
            if not code or not held or held < need:
                continue
            shares = int(p.get("available") or 0)
            if shares <= 0:                     # T+1 不可卖，明天再说
                continue
            out.append({"code": code, "name": p.get("name") or "",
                        "action": "SELL", "shares": shares,
                        "limit_price": p.get("price"), "stop": None, "target": None,
                        "held": held,
                        "reason": "时间止损：%s模式最多持有 %d 个交易日，已持有 %d 日"
                                  % (holdmode.label(self.ai_cfg), cap, held)})
        return out

    # ---------------- 明日关注（待观察清单） ----------------
    def _watch_next_path(self):
        os.makedirs(PLANS_DIR, exist_ok=True)
        return os.path.join(PLANS_DIR, "tomorrow_watch.json")

    def _fallback_watch_items(self, watch, n=3):
        """AI 没给「明日关注」时，用观察池评分最高的几只兜底。

        用户要的是「明天哪些票看好」，这个清单不该因为模型这次话少就空着。
        """
        cands = []
        for it in watch or []:
            code = str(it.get("code") or "")
            if not code or it.get("⚠板块"):
                continue
            sc = it.get("评分")
            cands.append((float(sc) if sc is not None else -1.0, code, it))
        cands.sort(key=lambda x: -x[0])
        out = []
        for sc, code, it in cands:
            hi = it.get("近20日高点")
            out.append({"code": code,
                        "name": it.get("name") or "",
                        "关注理由": "代码兜底（模型未给清单）：评分 %s、变化 %s、%s"
                                    % (it.get("评分"), it.get("评分变化"),
                                       it.get("所属板块") or "板块未取到"),
                        "触发条件": ("放量站上前高 %.2f" % hi) if hi
                                    else "回踩不破 5 日线再介入",
                        "优先级": "高" if not out else "中"})
            if len(out) >= n:
                break
        return out

    def _save_tomorrow_watch(self, items, for_date):
        clean = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            code = str(it.get("code") or "").strip()
            if not code:
                continue
            clean.append({"code": code,
                          "name": it.get("name") or "",
                          "reason": str(it.get("关注理由") or it.get("reason") or "")[:200],
                          "trigger": str(it.get("触发条件") or it.get("trigger") or "")[:200],
                          "priority": str(it.get("优先级") or it.get("priority") or "中")})
            if len(clean) >= 5:
                break
        data = {"for_date": for_date,
                "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "mode": holdmode.name(self.ai_cfg),
                "items": clean}
        with open(self._watch_next_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        print(color("  👀 明日关注清单已保存 %d 只（执行日 %s）" % (len(clean), for_date), "grey"))
        return data

    def _load_tomorrow_watch(self):
        p = self._watch_next_path()
        if not os.path.exists(p):
            return {"for_date": "", "items": []}
        try:
            with open(p, encoding="utf-8-sig") as f:
                return json.load(f) or {"for_date": "", "items": []}
        except Exception:
            return {"for_date": "", "items": []}

    def _archive_plans(self, plans, tag="archive"):
        if not plans:
            return None
        d = os.path.join(PLANS_DIR, "history")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "%s_%s.json"
                            % (datetime.now().strftime("%Y%m%d_%H%M%S"), tag))
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(plans, f, ensure_ascii=False, indent=1)
            return path
        except Exception as exc:                       # noqa: BLE001
            print(color("  （计划单归档失败：%s）" % exc, "yellow"))
            return None

    def _reset_plans(self, tag="reset"):
        """归档并清掉「已过期」的旧计划单（收盘后的计划阶段用）。

        过期 = 执行日 ≤ 今天：要么今天开盘已经执行过，要么已被取消，留着只会让
        「明日计划单」继续显示昨天的内容。执行日在未来的计划单保留不动。
        清空后本轮生成什么就是什么；一条都没有＝明日不开新仓（这本身就是决策）。
        返回被清掉的旧单，供提示词里的「今日计划单回顾」使用。
        """
        old = self._load_plans()
        today = datetime.now().strftime("%Y-%m-%d")
        keep = [p for p in old if self._plan_exec_date(p) > today]
        drop = [p for p in old if self._plan_exec_date(p) <= today]
        if not drop:
            if keep:
                print(color("  ⓘ 保留 %d 条未到期计划单（执行日在未来）" % len(keep), "grey"))
            return []
        path = self._archive_plans(drop, tag)
        self._save_plans(keep)
        self._dedup_plans()
        print(color("  ⓘ 旧计划单 %d 条已归档并清空%s%s"
                    % (len(drop),
                       "（%s）" % os.path.basename(path) if path else "",
                       "，保留 %d 条未到期" % len(keep) if keep else ""), "grey"))
        return drop

    def _salvage_time_stops(self, sells, snap, watch, day):
        """模型失败时的兜底：到期卖出的纪律不能因为模型挂了就不执行。"""
        if not sells:
            return []
        print(color("  ⏰ 模型本轮失败，但时间止损照常执行：%s"
                    % "、".join("%s %d股" % (a["code"], a["shares"]) for a in sells), "yellow"))
        try:
            res = self._execute(sells, snap, watch, day)
            if day.get("force_plan"):
                try:
                    self._trim_plans_to_trade_cap(self._plan_for_date(True))
                except Exception:                      # noqa: BLE001
                    pass
            return res
        except Exception as exc:                       # noqa: BLE001
            print(color("  （时间止损兜底执行失败：%s）" % exc, "yellow"))
            return []

    def _trim_plans_to_trade_cap(self, for_date):
        """同一执行日的计划单不能超过「每日最大交易笔数」（卖单也占名额）。

        收盘计划阶段结束前对**最终文件**做一次裁剪——因为上一轮的未到期计划单
        会被保留下来，只按本轮新增去裁剪会漏算。
        """
        cap = int(float((self.rails.cfg or {}).get("max_trades_per_day") or 4))
        plans = self._load_plans()
        same = [p for p in plans if self._plan_exec_date(p) == for_date]
        if len(same) <= cap:
            return plans
        sells = [p for p in same if (p.get("action") or "").upper() != "BUY"]
        buys = [p for p in same if (p.get("action") or "").upper() == "BUY"]
        room = max(0, cap - len(sells))
        keep = sells + buys[:room]
        dropped = buys[room:]
        other = [p for p in plans if self._plan_exec_date(p) != for_date]
        self._archive_plans(dropped, "trade-cap-trim")
        self._save_plans(other + keep)
        if dropped:
            print(color("  ⓘ %s 计划共 %d 条超每日 %d 笔上限 → 砍掉买入 %s（明天执行不了，"
                        "不写进计划单）" % (for_date, len(same), cap,
                                           "、".join(p.get("code") or "?" for p in dropped)),
                        "yellow"))
        return other + keep

    def _dedup_plans(self):
        """同一代码+同一方向+同一执行日，只保留最早的一条（防重复下单）。"""
        plans = self._load_plans()
        seen = {}
        out = []
        for pl in plans:
            key = (pl.get("code"), (pl.get("action") or "").upper(),
                   self._plan_exec_date(pl))
            if key in seen:
                continue
            seen[key] = True
            out.append(pl)
        if len(out) != len(plans):
            self._save_plans(out)
            print(color("  ⓘ 计划单去重：%d → %d 条" % (len(plans), len(out)), "yellow"))
        return out

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

    def _add_plan(self, code, name, action, shares, signal_price, stop, target, reason,
                  for_date=None):
        plans = self._load_plans()
        plans.append({"for_date": for_date or self._plan_for_date(),
                      "signal_date": datetime.now().strftime("%Y-%m-%d"),
                      "signal_time": datetime.now().strftime("%H:%M:%S"),
                      "code": code, "name": name, "action": action, "shares": shares,
                      "signal_price": signal_price, "stop": stop, "target": target,
                      "reason": reason})
        self._save_plans(plans)
        self._dedup_plans()

    def phase_execute_plans(self, verbose=True):
        """开盘后执行到期计划单（含隔夜跳空取消规则）。

        执行日 for_date ≤ 今天 的计划单才会被执行：收盘后生成的明日计划单
        到了那个交易日才成交；未到期的留给后面。
        """
        plans = self._dedup_plans()      # 同一代码/方向/执行日只执行一条
        # 先卖后买：卖单先成交，腾出的现金/仓位才能支持后面的买单（同一份资金不重复算）
        plans.sort(key=lambda p: 1 if (p.get("action") or "").upper() == "BUY" else 0)
        if not plans:
            if verbose:
                print(color("\n【执行计划】无待执行计划单。", "grey"))
            return []
        exec_cfg = {**DEFAULT_EXEC, **((self.ai_cfg or {}).get("execution") or {})}
        cancel_pct = float(exec_cfg.get("cancel_if_gap_pct", 1.5))
        today = datetime.now().strftime("%Y-%m-%d")
        if verbose:
            print(color("\n【执行计划】待执行 %d 条（执行日≤%s；开盘价偏离 >%.1f%% 或跳空跌破止损则取消）"
                        % (len(plans), today, cancel_pct), "bold"))
        snap = self._snapshot()
        day = {"trades": 0, "loss_pct": 0.0, "loss_limit_hit": False}
        remaining, results = [], []
        for pl in plans:
            if self._plan_exec_date(pl) > today:     # 还没到执行日（明日计划单）
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
        try:                                   # 记录今日"错过清单"并回填历史
            from . import missed as _MS
            _MS.record_daily(verbose=verbose)
            _MS.backfill(verbose=verbose)
        except Exception as _exc:              # noqa: BLE001
            print(color("  （错过清单跳过：%s）" % _exc, "grey"))
        if verbose:
            print(color("\n【③ 收盘复盘】AI 正在审判自己…", "bold"))
        snap = self._snapshot()
        stats = self.broker.stats()
        today = datetime.now().strftime("%Y-%m-%d")
        today_trades = [t for t in self.broker.state["trades"] if t["ts"].startswith(today)]
        # 先回填我过去判断的结果，让复盘对着事实而不是对着记忆
        try:
            hypothesis.backfill(verbose=verbose)
            judgment_rows = hypothesis.brief_for_review(8)
            card = hypothesis.scorecard()
        except Exception as exc:                       # noqa: BLE001
            judgment_rows, card = [], {}
            print(color("  （回填失败：%s）" % exc, "yellow"))
        try:
            mistakes = evolution.pick_mistakes(3)
            ab = evolution.ab_report()
        except Exception:
            mistakes, ab = [], {}
        if verbose and judgment_rows:
            print(color("  已带 %d 条「我的判断 vs 实际结果」进入复盘"
                        % len(judgment_rows), "grey"))
        payload = {
            "task": "review",
            "手动授权说明": ("若今日决策/成交记录里出现「【用户手动授权开仓】」标记，"
                        "那是用户在网页上主动点的强制开仓，属于用户指令，"
                        "不得计入你的流程违规，也不得写成'情绪追涨'；"
                        "只记录客观结果与可复用条件。"),
            "date": today,
            "用户持有模式(不可协商，手册必须服从)": holdmode.brief(self.ai_cfg),
            "账户与绩效": {"总权益": snap["equity"], "累计收益%": snap["total_return_pct"],
                       "同期指数%": stats.get("index_return_pct"),
                       "相对指数超额%": stats.get("excess_pct"),
                       **{k: v for k, v in stats.items() if k != "equity"}},
            "今日成交": today_trades,
            "当前持仓": [{**{k: p[k] for k in ("code", "name", "shares", "cost", "price",
                                            "pnl_pct", "entry_date", "stop", "note")},
                        "可卖股数": p.get("available")}
                       for p in snap["positions"]],
            "最近决策记录": [{"date": d.get("date"), "view": d.get("market_view"),
                         "actions": [{k: a.get(k) for k in ("code", "action", "shares", "reason")}
                                     for a in (d.get("actions") or [])]}
                        for d in memory.decision_history(2)][-6:],
            "今日日记": memory.recent_journal(2, 8000),
            "我过去的判断与真实结果(务必据此改规则，别凭印象)": judgment_rows,
            "我的判断记分卡": card,
            "系统已验证先验(勿改写)": priors.text(),
            "我的错题本(最近判断失误，必须逐条归因)": mistakes,
            "策略手册版本A/B(不同版本生效期间的表现)": ab,
            "我的现用手册": memory.read_playbook(),
            "要求": {
                "输出JSON": {
                    "critique": "对今天操作的严厉评价（哪些是纪律，哪些是情绪/运气），150字内",
                    "lessons": ["≤3 条，具体到下次开盘能机械执行的规则"],
                    "错题归因": [{"id": "错题本里的 id", "归因": "方向错|时机错|失效条件设错|运气",
                              "说明": "一句话说清当时哪个前提错了"}],
                },
                "标准": ["必须指出至少一个自己的问题（哪怕是仓位偏小机会成本）",
                       "教训必须能在下次开盘时被机械执行",
                       "禁止写正确的废话",
                       "每条教训必须写成『当/若 + 可量化条件 + 具体动作』，"
                       "例：当大盘温度<30 时不追高开>2% 的票；写不出阈值的会被系统丢弃",
                       "若「我过去的判断与真实结果」里出现未命中的判断，教训必须点名"
                       "是哪一条、错在哪个前提（方向错/时机错/失效条件设错）",
                       "手册里凡是写「持有 10~20 日」这类与「用户持有模式」冲突的表述，"
                       "必须改写成服从模式上限的说法（短线上限 3 个交易日由代码强制执行）",
                       "样本不足 5 条的分组不许当规律用，只能标记为『观察中』"],
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
        lessons = data.get("lessons") or []
        memory.add_lessons(lessons)
        try:                                     # 教训 → 可证伪规则（不可检验的直接丢弃）
            res = rules_mod.upsert_from_lessons(lessons, source="review:" + today)
            up = rules_mod.link_from_hypotheses(verbose=False)
            if verbose:
                print(color("  规则库：新增 %d / 合并 %d / 丢弃不可证伪 %d%s"
                            % (len(res["added"]), len(res["merged"]),
                               len(res["rejected"]),
                               ("｜状态更新 %d 条" % len(up["changed"])) if up["changed"] else ""),
                            "grey"))
                for x in res["rejected"][:3]:
                    print(color("    ✘ 丢弃：%s（%s）" % (x["text"][:38], x["reason"]), "yellow"))
        except Exception as exc:                 # noqa: BLE001
            print(color("  （规则库更新失败：%s）" % exc, "yellow"))
        try:
            n = evolution.save_mistakes(data.get("错题归因"))
            if verbose and n:
                print(color("  错题本：记录 %d 条归因" % n, "grey"))
        except Exception as exc:                 # noqa: BLE001
            print(color("  （错题本保存失败：%s）" % exc, "yellow"))

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
                "上一版手册": memory.read_playbook(),
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
