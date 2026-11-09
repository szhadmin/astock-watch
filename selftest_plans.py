# -*- coding: utf-8 -*-
"""计划单执行逻辑自检（离线，不依赖行情/大模型）。"""
import io, json, os, shutil, sys
sys.path.insert(0, ".")
from datetime import datetime, timedelta
from astock import ai_agent, feed

CFG = json.load(io.open("config.json", encoding="utf-8-sig"))
STATE = os.path.join("data", "ai", "paper_state.json")
PLANS = os.path.join("data", "ai", "plans", "pending_plans.json")
BAK = STATE + ".bak"
BAK_PLANS = PLANS + ".bak"
yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

# 备份真实账户与真实计划单，并把账户**移开**（否则真实持仓会占用组合风险预算，
# 导致用例结果受真实仓位影响）；测试结束后原样还原，绝不弄丢数据。
if os.path.exists(STATE):
    shutil.move(STATE, BAK)
if os.path.exists(PLANS):
    shutil.copy(PLANS, BAK_PLANS)

def write_plan(stop=6.33, shares=700, price=6.54, for_date=None):
    os.makedirs(os.path.dirname(PLANS), exist_ok=True)
    pl = {"signal_date": yesterday, "signal_time": "15:00:00",
          "code": "600015", "name": "华夏银行", "action": "BUY",
          "shares": shares, "signal_price": price, "stop": stop,
          "target": None, "reason": "自检用计划单"}
    if for_date:
        pl["for_date"] = for_date
    json.dump([pl], io.open(PLANS, "w", encoding="utf-8"), ensure_ascii=False)

def fake_quote(open_px, price=None):
    def q(code):
        return {"code": code, "name": "华夏银行", "price": price or open_px,
                "open": open_px, "high": max(open_px, price or open_px) * 1.01,
                "low": min(open_px, price or open_px) * 0.99,
                "prev_close": 6.54, "pct": 1.0, "amount": 5e8, "turnover": 2.0,
                "mktcap": 1e11, "float_cap": 1e11, "pe": 6, "pb": 0.7,
                "vol_ratio": 1.2, "ts": 0, "fetched_at": ""}
    return q

orig = feed.get_quote

def run_case(name, open_px, stop=6.33, shares=800, price=6.54, expect="buy",
             for_date=None, fresh=False):
    # 每个用例都从空账户开始：否则「同一标的当日只建仓一次」会把后面的用例挡掉
    if os.path.exists(STATE):
        os.remove(STATE)
    write_plan(stop, shares, price, for_date=for_date)
    feed.get_quote = fake_quote(open_px)
    t = ai_agent.AITrader(CFG)
    before = len(t.broker.state["trades"])
    try:
        res = t.phase_execute_plans(verbose=False)
    finally:
        feed.get_quote = orig
    after = len(t.broker.state["trades"])
    filled = after > before
    ok = (expect == "buy" and filled) or (expect == "cancel" and not filled)
    print("%s %-34s 开盘%.2f → %s  (%s)" % ("✔" if ok else "✘", name, open_px,
          "已成交" if filled else "未成交", (res[0] if res else "无动作")[:60]))
    return ok

print("=== 计划单执行逻辑自检（信号价 6.54，止损 6.33）===")
oks = []
oks.append(run_case("正常小幅高开", 6.60, expect="buy"))
oks.append(run_case("平开", 6.54, expect="buy"))
oks.append(run_case("高开超过 1.5%（应取消）", 6.72, expect="cancel"))
oks.append(run_case("开盘即跌破止损（应取消）", 6.30, expect="cancel"))
# 执行日归属：今天到期→执行；明天到期→留在文件里等明天
today = datetime.now().strftime("%Y-%m-%d")
tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
oks.append(run_case("执行日=今天（盘前计划单应成交）", 6.60, expect="buy", for_date=today,
                    fresh=True))
oks.append(run_case("执行日=明天（明日计划单不该提前）", 6.60, expect="cancel",
                    for_date=tomorrow, fresh=True))
left = json.load(io.open(PLANS, encoding="utf-8"))
keep = bool(left) and left[0].get("for_date") == tomorrow
print("%s %-34s 计划单仍在文件中（%d 条，执行日 %s）"
      % ("✔" if keep else "✘", "未到期计划单保留",
         len(left), left[0].get("for_date") if left else "-"))
oks.append(keep)

# 清理：恢复账户与计划单（有备份则还原，没有则删除测试残留）
if os.path.exists(STATE):
    os.remove(STATE)          # 删掉测试过程中产生的空账户
if os.path.exists(BAK):
    shutil.move(BAK, STATE)
if os.path.exists(BAK_PLANS):
    shutil.move(BAK_PLANS, PLANS)
elif os.path.exists(PLANS):
    os.remove(PLANS)
print("\n结论：%s" % ("全部通过" if all(oks) else "有用例未通过"))
