# -*- coding: utf-8 -*-
"""短线模式 / 时间止损 / 明日关注 自检（离线，不联行情、不调模型）。"""
import io, json, os, shutil, sys, tempfile
from datetime import datetime, timedelta
sys.path.insert(0, ".")
from astock import ai_agent, holdmode

ok = True
def check(name, cond, extra=""):
    global ok
    ok = ok and bool(cond)
    print("%s %-46s %s" % ("✔" if cond else "✘", name, extra))

cfg = {"ai": {"hold_mode": "short"}}

print("=== 1) 持有交易日数（含买入日，周末不计数）===")
# 2026-11-02 是周一
check("周一买、周一当天 = 1 天", holdmode.trading_days_held("2026-11-02", datetime(2026, 11, 2)) == 1)
check("周一买、周三 = 3 天", holdmode.trading_days_held("2026-11-02", datetime(2026, 11, 4)) == 3)
check("周五买、下周一是第 2 天",
      holdmode.trading_days_held("2026-10-30", datetime(2026, 11, 2)) == 2,
      "10-30 周五 → 11-02 周一")
check("脏数据安全返回", holdmode.trading_days_held("", None) is None and
      holdmode.trading_days_held("2026-13-99", None) is None)

print("\n=== 2) 到期判定（短线 3 天：第 2 天排卖出、第 3 天必须卖）===")
def date_with_held(n):
    """找出「到目前恰好持有 n 个交易日」的入场日，避免用例写死日期后过期。"""
    d = datetime.now().date()
    for _ in range(40):
        if holdmode.trading_days_held(d.strftime("%Y-%m-%d")) == n:
            return d.strftime("%Y-%m-%d")
        d -= timedelta(days=1)
    return None

d1, d2, d3 = date_with_held(1), date_with_held(2), date_with_held(3)
check("持有 1 天 → 不排", not holdmode.exit_due(cfg, d1), "入场 %s" % d1)
check("持有 2 天 → 该排卖出计划", holdmode.exit_due(cfg, d2), "入场 %s" % d2)
check("持有 3 天 → 已超期", holdmode.overdue(cfg, d3), "入场 %s" % d3)
check("中线 10 天：持有 3 天不排", not holdmode.exit_due({"ai": {"hold_mode": "mid"}}, d3))

print("\n=== 3) 时间止损 → 强制卖出动作 ===")
tmp = tempfile.mkdtemp()
ai_agent.PLANS_DIR = tmp
t = object.__new__(ai_agent.AITrader)
t.cfg = cfg
t.ai_cfg = cfg["ai"]
today = datetime.now().strftime("%Y-%m-%d")
snap = {"positions": [
    {"code": "600015", "name": "华夏银行", "shares": 800, "available": 800,
     "entry_date": d3, "price": 6.51},
    {"code": "601012", "name": "隆基绿能", "shares": 200, "available": 200,
     "entry_date": d2, "price": 12.47},
    {"code": "002202", "name": "金风科技", "shares": 100, "available": 0,
     "entry_date": today, "price": 15.42},
]}
force = t._time_stop_actions(snap, force_plan=True)
codes = {a["code"]: a for a in force}
check("收盘计划阶段：持有 3 天的排卖出", "600015" in codes,
      codes.get("600015", {}).get("reason", ""))
check("收盘计划阶段：持有 2 天的排卖出", "601012" in codes)
check("今日买入的不动", "002202" not in codes)
check("卖出股数 = 可卖股数", codes.get("600015", {}).get("shares") == 800)
intraday = t._time_stop_actions(snap, force_plan=False)
check("盘中：只强平已超期（3天）的那只",
      [a["code"] for a in intraday] == ["600015"])
snap_t1 = {"positions": [{"code": "600015", "name": "华夏银行", "shares": 800,
                          "available": 0, "entry_date": d3, "price": 6.51}]}
check("T+1 不可卖时不强卖", t._time_stop_actions(snap_t1, force_plan=True) == [])

print("\n=== 4) 明日关注清单落盘 ===")
data = t._save_tomorrow_watch([
    {"code": "603806", "name": "福斯特", "关注理由": "光伏辅材领涨",
     "触发条件": "站上 42.5 且量比>1.5", "优先级": "高"},
    {"code": "300017", "name": "网宿科技", "reason": "RSI 不高", "trigger": "回踩 8.9 不破"},
    {"code": "bad"}, {"name": "没有代码"},
    {"code": "A1"}, {"code": "A2"}, {"code": "A3"}, {"code": "A4"}, {"code": "A5"},
], "2026-11-06")
back = t._load_tomorrow_watch()
check("只保留有代码的条目", back["items"][0]["code"] == "603806")
check("中文/英文键都兼容", back["items"][1]["trigger"] == "回踩 8.9 不破")
check("最多 5 只", len(back["items"]) <= 5, "实际 %d 条" % len(back["items"]))
check("带执行日与模式", back["for_date"] == "2026-11-06" and back["mode"] == "short")

t._save_plans([])
shutil.rmtree(tmp)

print("\n=== 5) 卖出后预计账户（卖出的钱能不能马上用来买）===")
class _Rails:
    cfg = {"max_total_position_pct": 80.0, "max_total_risk_pct": 4.0}
class _Broker:
    state = {"capital": 15000.0}
t.rails = _Rails()
t.broker = _Broker()
snap2 = {"cash": 3130.76, "market_value": 12034.0, "equity": 15164.76, "positions": [
    {"code": "600015", "name": "华夏银行", "shares": 800, "available": 800,
     "entry_date": d3, "price": 6.51, "stop": 6.33},
    {"code": "601012", "name": "隆基绿能", "shares": 200, "available": 200,
     "entry_date": d2, "price": 12.47, "stop": 11.55},
    {"code": "002202", "name": "金风科技", "shares": 100, "available": 0,
     "entry_date": today, "price": 15.42, "stop": 14.35},
    {"code": "300433", "name": "蓝思科技", "shares": 100, "available": 0,
     "entry_date": today, "price": 27.90, "stop": 25.88},
]}
sells = t._time_stop_actions(snap2, force_plan=True)
summary, proj = t._project_after_sells(snap2, sells)
cash_key = [k for k in summary if k.startswith("预计现金")][0]
buy_key = [k for k in summary if k.startswith("预计可用买入金额")][0]
risk_key = [k for k in summary if k.startswith("预计还可增加的风险额")][0]
print("  卖单:", summary["动作"])
print("  预计现金 %.2f｜可用买入 %.2f｜仓位 %.2f%%｜组合风险 %.2f%%｜可增加风险 %.2f 元"
      % (summary[cash_key], summary[buy_key], summary["预计仓位%"],
         summary["预计组合风险%"], summary[risk_key]))
check("卖出后现金变多（≈10825）", abs(summary[cash_key] - 10825.06) < 2, "%.2f" % summary[cash_key])
check("腾出可用买入额度（≈7793）", abs(summary[buy_key] - 7793.65) < 3, "%.2f" % summary[buy_key])
check("仓位从 79% 降到 ~28.6%", abs(summary["预计仓位%"] - 28.58) < 0.2,
      "%.2f%%" % summary["预计仓位%"])
check("组合风险仍有额度可加（>0）", summary[risk_key] > 0, "%.2f 元" % summary[risk_key])
check("预计快照里两只到期票已消失",
      sorted(p["code"] for p in proj["positions"]) == ["002202", "300433"])
check("预计快照现金可用于额度计算", abs(proj["cash"] - summary[cash_key]) < 0.01)
check("没有卖单时不吃掉任何额度",
      t._project_after_sells(snap2, [])[0][cash_key] == 3130.76)

print("\n=== 6) 计划阶段的风控要用「卖出后」账户（否则满仓永远买不进）===")
rails = ai_agent.RiskRails({"max_total_position_pct": 80.0, "max_total_risk_pct": 4.0}, 15000)
lay = {"trades": 2, "loss_pct": -1.03, "loss_limit_hit": False, "sizing": {}}
order = {"code": "603806", "action": "BUY", "shares": 100, "limit_price": 13.37, "stop": 12.77}
quote = {"code": "603806", "name": "福斯特", "price": 13.37, "pct": 1.0,
         "low": 13.10, "high": 13.60}
ok_live, why_live, _ = rails.check(order, snap2, quote, lay)
ok_proj, why_proj, sh = rails.check(order, proj, quote, lay)
print("  用今天的满仓快照:", "通过" if ok_live else "拒绝", "→", why_live)
print("  用卖出后预计快照:", "通过" if ok_proj else "拒绝", "→", why_proj)
check("满仓快照下买单会被拒（复现老问题）", not ok_live and "总仓位上限" in why_live)
check("卖出后预计快照下买单能通过", ok_proj, "%d 股" % sh)
check("批准的股数是整手", ok_proj and sh % 100 == 0)

print("\n=== 7) 同一执行日的计划单不超过每日交易笔数上限（卖单也占名额）===")
tmp2 = tempfile.mkdtemp()
ai_agent.PLANS_DIR = tmp2
t2 = object.__new__(ai_agent.AITrader)
class _R2:
    cfg = {"max_trades_per_day": 4}
t2.rails = _R2()
t2.cfg = {"ai": {}}
t2.ai_cfg = {}
t2._save_plans([
    {"for_date": "2026-11-06", "code": "600015", "action": "SELL", "shares": 800},
    {"for_date": "2026-11-06", "code": "601012", "action": "SELL", "shares": 200},
    {"for_date": "2026-11-06", "code": "600732", "action": "BUY", "shares": 100},
    {"for_date": "2026-11-06", "code": "603396", "action": "BUY", "shares": 100},
    {"for_date": "2026-11-06", "code": "603806", "action": "BUY", "shares": 100},
    {"for_date": "2026-11-09", "code": "000001", "action": "BUY", "shares": 100},
])
out = t2._trim_plans_to_trade_cap("2026-11-06")
same = [p for p in out if p["for_date"] == "2026-11-06"]
check("裁剪到 4 笔（含 2 笔卖单）", len(same) == 4,
      "卖 %d / 买 %d" % (sum(1 for p in same if p["action"] != "BUY"),
                         sum(1 for p in same if p["action"] == "BUY")))
check("卖单一条都不能少", sum(1 for p in same if p["action"] != "BUY") == 2)
check("其他执行日不受影响", any(p["for_date"] == "2026-11-09" for p in out))
t2._save_plans([])
shutil.rmtree(tmp2)

print("\n=== 8) 每日交易笔数 = 0 → 不限制 ===")
tmp3 = tempfile.mkdtemp()
ai_agent.PLANS_DIR = tmp3
t3 = object.__new__(ai_agent.AITrader)
class _R3:
    cfg = {"max_trades_per_day": 0}
t3.rails = _R3()
t3.cfg = {"ai": {}}
t3.ai_cfg = {}
many = [{"for_date": "2026-11-06", "code": "00000%d" % i, "action": "BUY", "shares": 100}
        for i in range(6)]
t3._save_plans(many)
out3 = t3._trim_plans_to_trade_cap("2026-11-06")
check("0=不限：6 条计划全部保留", len(out3) == 6, "%d 条" % len(out3))
t3._save_plans([])
shutil.rmtree(tmp3)

rails0 = ai_agent.RiskRails({"max_trades_per_day": 0}, 15000)
day_many = {"trades": 99, "loss_pct": 0.0, "loss_limit_hit": False, "sizing": {}}
snap8 = {"cash": 12000.0, "market_value": 0.0, "equity": 15000.0, "positions": []}
ok0, why0, sh0 = rails0.check(
    {"code": "603806", "action": "BUY", "shares": 500, "limit_price": 13.37, "stop": 12.77},
    snap8, quote, day_many)
check("已成交 99 笔仍允许继续买", ok0, (why0 or "")[:40] + " %d 股" % sh0)
check("风控文案写明「不限笔数」", "不限" in rails0.describe())
rails4 = ai_agent.RiskRails({"max_trades_per_day": 4}, 15000)
ok4, why4, _ = rails4.check(
    {"code": "603806", "action": "BUY", "shares": 100, "limit_price": 13.37, "stop": 12.77},
    proj, quote, day_many)
check("设回 4 笔时仍然拦得住", (not ok4) and "最大交易笔数" in why4, why4[:30])

print("\n=== 9) 成本与节奏控制（2026-11 复盘的四项修复）===")
rails_c = ai_agent.RiskRails({}, 15000)
snap_empty = {"cash": 12000.0, "market_value": 0.0, "equity": 15000.0, "positions": []}
q = {"code": "002506", "name": "协鑫集成", "price": 2.53, "pct": 0.5,
     "low": 2.50, "high": 2.56}
base_day = {"trades": 0, "loss_pct": 0.0, "loss_limit_hit": False}
BUY = {"code": "002506", "action": "BUY", "shares": 100, "limit_price": 2.53, "stop": 2.42}

# ① 同一标的当日只建仓一次
d1 = {**base_day, "bought_today": {"002506": 1}, "add_trigger": {}}
ok9, why9, _ = rails_c.check(dict(BUY), snap_empty, q, d1)
check("同标的当日已买过 → 拒绝再买", (not ok9) and "只建仓一次" in why9, why9[:34])

# ② 有加仓触发价且已站上 → 放行（但仍受其它上限约束）
d2 = {**base_day, "bought_today": {"002506": 1}, "add_trigger": {"002506": 2.50}}
ok9b, why9b, sh9b = rails_c.check(dict(BUY), snap_empty, q, d2)
check("站上加仓触发价 → 允许加仓一次", ok9b, (why9b or "")[:34] + " %d股" % sh9b)

# ③ 每日新建仓标的数上限（默认 3 只）
d3 = {**base_day, "bought_today": {"600050": 1, "600988": 1, "600089": 1}, "add_trigger": {}}
ok9c, why9c, _ = rails_c.check(dict(BUY), snap_empty, q, d3)
check("今日新建仓已 3 只 → 拒绝第 4 只", (not ok9c) and "新建仓标的已达" in why9c, why9c[:34])

# ④ 单笔金额下限：250 元的小单 → 提到 ≥5000 元，而不是照单执行
ok9d, why9d, sh9d = rails_c.check(dict(BUY), snap_empty, q, base_day)
check("小额单被提到 ≥5000 元（不是 250 元的小单）",
      ok9d and sh9d * 2.53 >= 5000, "%d股 = %.0f元" % (sh9d, sh9d * 2.53))
# 额度不够时必须放弃
snap_tiny = {"cash": 300.0, "market_value": 0.0, "equity": 300.0, "positions": []}
ok9e, why9e, _ = rails_c.check(dict(BUY), snap_tiny, q, base_day)
check("额度不够凑到下限 → 放弃", (not ok9e) and "下限" in why9e, why9e[:40])

# ⑤ 目标价按 2R 修正
t9 = object.__new__(ai_agent.AITrader)
t9.rails = rails_c
t9.cfg = {"ai": {}}
t9.ai_cfg = {}
fixed = t9._fix_reward_risk({"code": "002506", "stop": 2.42, "target": 2.60}, 2.53)
check("目标价被拉到 ≥2R", abs(fixed["target"] - (2.53 + 2 * (2.53 - 2.42))) < 0.02,
      "target=%.2f" % fixed["target"])

# ⑥ 移动止损：浮盈 1R 移保本，1.5R 后跟踪
class _B:
    state = {"positions": {
        "A": {"code": "A", "name": "保本档", "cost": 10.0, "stop": 9.5, "last_price": 10.6,
              "high_since": 10.6, "shares": 100},
        "B": {"code": "B", "name": "跟踪档", "cost": 10.0, "stop": 9.5, "last_price": 11.8,
              "high_since": 11.8, "shares": 100},
        "C": {"code": "C", "name": "未触发", "cost": 10.0, "stop": 9.5, "last_price": 10.2,
              "high_since": 10.2, "shares": 100},
    }}
    def save(self):
        pass
t9.broker = _B()
t9._apply_trailing_stops(verbose=False)
pA, pB, pC = _B.state["positions"]["A"], _B.state["positions"]["B"], _B.state["positions"]["C"]
check("浮盈 1R → 止损移到成本（保本）", abs(pA["stop"] - 10.0) < 1e-6, "%.2f" % pA["stop"])
check("浮盈 1.5R → 止损按最高价−1R 跟踪", abs(pB["stop"] - 11.3) < 1e-6, "%.2f" % pB["stop"])
check("未到 1R → 止损不动", abs(pC["stop"] - 9.5) < 1e-6, "%.2f" % pC["stop"])

# ⑦ 跌破止损 → 代码强制卖出
snap_stop = {"positions": [{"code": "002506", "name": "协鑫集成", "shares": 1600,
                            "available": 1600, "entry_date": today,
                            "price": 2.40, "stop": 2.42}]}
forced = t9._time_stop_actions(snap_stop, force_plan=False)
check("跌破止损 → 强制卖出", bool(forced) and forced[0]["action"] == "SELL" and
      "跌破止损" in forced[0]["reason"], (forced[0]["reason"][:30] if forced else "无"))

print("\n结论：%s" % ("全部通过" if ok else "有用例未通过"))
sys.exit(0 if ok else 1)
