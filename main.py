# -*- coding: utf-8 -*-
"""astock-watch 命令行入口。

用法：
    python main.py serve                 启动 Web 控制台（推荐）
    python main.py analyze 000620        单股深度分析
    python main.py watch                 自选股一览
    python main.py monitor               实时盯盘（Ctrl+C 停止）
    python main.py daily                 生成当日收盘报告（Markdown）
    python main.py test                  数据源连通性自检
    python main.py backtest 000620       策略历史回测

    python main.py ai --doctor           AI 一键体检（网关/JSON/额度/协议/风控）
    python main.py ai --phase pick|decide|review|execute|auto
    python main.py ai --status           账户与绩效
    python main.py ai --loop             命令行版循环操盘
    python main.py ai --setup            交互式配置模型网关
    python main.py ai --ping             测试网关连通性
    python main.py ai --reset            重置模拟盘（保留手册/日记/教训）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

try:                                     # Windows 控制台中文
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from astock import (analyze, backup, chart_svg, monitor, notify, report,
                    signals, store)  # noqa: E402
from astock.notify import color  # noqa: E402

CONFIG_PATH = os.path.join(BASE, "config.json")


def load_config(path=CONFIG_PATH):
    if not os.path.exists(path):
        raise SystemExit("找不到配置文件：%s" % path)
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def save_config(cfg, path=CONFIG_PATH):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")


# --------------------------------------------------------------------------- #
# 分析与盯盘
# --------------------------------------------------------------------------- #
def cmd_analyze(args, cfg):
    codes = args.codes or (cfg.get("watchlist") or [])
    if not codes:
        raise SystemExit("未指定股票代码，且 watchlist 为空。")
    reports = []
    for code in codes:
        try:
            reports.append(analyze.print_report(code, cfg))
        except Exception as exc:                      # noqa: BLE001
            print(color("  [%s] 分析失败：%s" % (code, exc), "yellow"))
    if args.json and reports:
        print(json.dumps(reports, ensure_ascii=False, indent=2, default=str))


def cmd_watch(args, cfg):
    codes = args.codes or (cfg.get("watchlist") or [])
    reports = []
    for code in codes:
        try:
            reports.append(signals.build_report(code, cfg))
        except Exception as exc:                      # noqa: BLE001
            reports.append({"code": code, "error": str(exc)})
    notify.banner("自选股一览  %s" % datetime.now().strftime("%Y-%m-%d %H:%M"))
    print(analyze.render_brief(reports))
    print("")
    print(color("提示：python main.py analyze <代码> 查看单股详细报告", "grey"))


def cmd_monitor(args, cfg):
    w = monitor.Watcher(cfg)
    if args.interval:
        w.interval = float(args.interval)
    if args.once:
        notify.banner("盯盘自检（单轮）  %s" % datetime.now().strftime("%Y-%m-%d %H:%M"))
        w.run_once()
        w.status_line()
        return
    if not monitor.in_session():
        print(color("当前非交易时段（%s）。如仍要测试，加 --once 参数。"
                    % datetime.now().strftime("%a %H:%M"), "yellow"))
        if not cfg.get("monitor", {}).get("exit_when_closed"):
            print(color("等待开盘…（exit_when_closed=false）", "grey"))
    w.run()


def cmd_daily(args, cfg):
    """生成当日报告：Markdown + HTML（内嵌 SVG K线），并归档到 archive/<日期>/。"""
    notify.banner("收盘报告  %s" % datetime.now().strftime("%Y-%m-%d %H:%M"))
    res = report.build(codes=args.codes or None, cfg=cfg,
                       verbose=True, keep_days=getattr(args, "keep_days", None))
    if res["codes"]:
        print(color("已生成 %d 只标的的报告。" % len(res["codes"]), "bold"))
    if res.get("temp"):
        t = res["temp"]
        print(color("大盘温度 %d（%s）" % (t["temp"], t["level"]), "grey"))
    print(color("打开 HTML：%s" % res["html"], "grey"))


def cmd_test(args, cfg):
    from astock import feed
    notify.banner("数据源自检  %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    ok = True
    for code in (cfg.get("watchlist") or ["000620"]):
        try:
            q = feed.get_quote(code)
            d = feed.get_kline(code, 101, limit=30)
            print("  ✔ %s %s  现价 %s (%+.2f%%)  日线 %d 根，最新 %s 收 %s"
                  % (code, q["name"], q["price"], q["pct"] or 0, len(d),
                     d[-1]["date"], d[-1]["close"]))
        except Exception as exc:                      # noqa: BLE001
            ok = False
            print(color("  ✘ %s 失败：%s" % (code, exc), "yellow"))
    try:
        for i in feed.get_indices():
            print("  ✔ 指数 %s %s (%+.2f%%)" % (i["name"], i["price"], i["pct"]))
    except Exception as exc:                          # noqa: BLE001
        print(color("  ! 指数获取失败：%s" % exc, "grey"))
    try:
        f = feed.get_fundflow((cfg.get("watchlist") or ["000620"])[0], limit=3)
        print("  ✔ 资金流 %d 条，最新 %s 主力 %.0f 万"
              % (len(f), f[-1]["date"], f[-1]["main"] / 1e4))
    except Exception as exc:                          # noqa: BLE001
        print(color("  ! 资金流获取失败：%s" % exc, "grey"))
    print("")
    print(color("自检%s。" % ("通过" if ok else "存在失败项"), "bold"))


def cmd_backtest(args, cfg):
    from astock import backtest, feed
    code = args.code
    bars = feed.get_kline(code, 101, limit=args.bars, adjust=1)
    for b in bars:
        b["code"] = code
    flows = None
    if not args.no_flow:
        try:
            flows = {f["date"]: f["main"] for f in feed.get_fundflow(code, limit=300)}
        except Exception:
            flows = None
    notify.banner("策略回测  %s" % code)
    r = backtest.run(bars, flows=flows, capital=args.capital,
                     max_position_pct=args.max_pos)
    print(backtest.format_result(r))
    if args.export:
        import csv
        path = os.path.join(BASE, "data", "reports", "backtest_%s.csv" % code)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["date", "equity", "price", "position_pct"])
            w.writeheader()
            w.writerows(r["equity_curve"])
        print("权益曲线已导出：%s" % path)


# --------------------------------------------------------------------------- #
# AI 操盘手
# --------------------------------------------------------------------------- #
def cmd_ai_setup(cfg):
    """交互式配置 AI 网关，写入 config.json。"""
    ai = cfg.setdefault("ai", {})
    print(color("=== 配置 AI 大脑（OpenAI 兼容网关）===", "bold"))
    print(color("直接回车 = 保留当前值；输入 - 表示清空。", "grey"))
    print("当前 provider：%s｜base_url：%s｜model：%s｜key：%s"
          % (ai.get("provider"), ai.get("base_url") or "(默认)",
             ai.get("model") or "(默认)",
             ("已设置 ****" + str(ai.get("api_key"))[-4:]) if ai.get("api_key") else "未设置"))

    def ask(prompt, cur):
        v = input("%s [%s]: " % (prompt, cur if cur else "空")).strip()
        if v == "":
            return cur
        if v == "-":
            return ""
        return v

    provider = ask("供应商(openai/deepseek/ollama/custom)", ai.get("provider") or "custom")
    if provider not in ("openai", "deepseek", "ollama", "custom"):
        print(color("未知供应商，按 custom 处理。", "yellow"))
        provider = "custom"
    base = ask("base_url（如 https://api.openai.com/v1 或你的中转地址）", ai.get("base_url"))
    model = ask("model（如 gpt-4o-mini / deepseek-chat / qwen2.5:14b）", ai.get("model"))
    key = ask("api_key（本地 ollama 可留空）", ai.get("api_key"))
    ai.update({"provider": provider, "base_url": base, "model": model, "api_key": key})
    save_config(cfg)
    print(color("\n已写入 config.json。", "bold"))
    print(color("下一步：python main.py ai --doctor   # 一键体检", "grey"))


def cmd_ai_ping(cfg):
    from astock import llm as llm_mod
    ai = cfg.get("ai") or {}
    print(color("测试网关：provider=%s base=%s model=%s"
                % (ai.get("provider"), ai.get("base_url") or "(默认)",
                   ai.get("model") or "(默认)"), "bold"))
    try:
        client = llm_mod.make_llm(ai)
    except Exception as exc:                         # noqa: BLE001
        print(color("初始化失败：%s" % exc, "yellow"))
        return
    t0 = time.time()
    try:
        text, usage = client.chat(
            "你是一个只输出 JSON 的助手。",
            '{"task":"ping","要求":"输出 {\\"ok\\":true,\\"answer\\":\\"1+1=2\\"} 形式的JSON"}',
            json_mode=True, temperature=0.0)
    except Exception as exc:                         # noqa: BLE001
        print(color("调用失败：%s" % exc, "yellow"))
        return
    print(color("✔ 网关可用，耗时 %.2f 秒" % (time.time() - t0), "red"))
    print("  原始返回：%s" % (text or "")[:300].replace("\n", " "))
    print("  token 用量：%s" % json.dumps(usage, ensure_ascii=False))
    parsed = llm_mod.extract_json(text)
    print("  JSON 解析：%s" % ("成功 %s" % json.dumps(parsed, ensure_ascii=False)[:120]
                           if parsed else "失败（该模型可能不支持 json_object，请换模型）"))


def cmd_ai_doctor(cfg):
    """一键体检：网关 / JSON / 代码围栏 / 长输出截断 / 复盘双调用 / 硬风控。"""
    from astock import llm as llm_mod, ai_agent
    ai = cfg.get("ai") or {}
    notify.banner("AI 体检  %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    results = []
    client = None

    def step(name, fn):
        try:
            ok, detail = fn()
        except Exception as exc:                        # noqa: BLE001
            ok, detail = False, "%s: %s" % (type(exc).__name__, exc)
        results.append((name, ok, detail))
        print("  %s %-22s %s" % (color("✔", "red") if ok else color("✘", "green"), name, detail))
        return ok

    print("  配置：provider=%s  base_url=%s  model=%s  key=%s"
          % (ai.get("provider"), ai.get("base_url") or "(默认)",
             ai.get("model") or "(默认)",
             ("****" + str(ai.get("api_key"))[-4:]) if ai.get("api_key") else "未设置"))

    def t_conn():
        nonlocal client
        client = llm_mod.make_llm(ai)
        t0 = time.time()
        text, _ = client.chat("你只输出 JSON。",
                              '{"task":"ping","要求":"输出 {\\"ok\\":true}"}',
                              json_mode=True, temperature=0.0, max_tokens=200)
        obj = llm_mod.extract_json(text)
        return bool(obj), "耗时 %.1fs，返回 %s" % (time.time() - t0,
                                                 (text or "")[:60].replace("\n", " "))

    if step("网关连通 + JSON", t_conn):
        def t_fence():
            text, _ = client.chat("你只输出 JSON。",
                                  '{"task":"fence","要求":"输出带换行的 JSON: '
                                  '{\\"a\\":1,\\"b\\":[1,2]}"}',
                                  json_mode=True, temperature=0.0, max_tokens=300)
            fenced = (text or "").strip().startswith("```")
            obj = llm_mod.extract_json(text)
            return bool(obj), ("模型返回带 ``` 围栏，已自动剥离" if fenced else "无围栏") + \
                "，解析%s" % ("成功" if obj else "失败")

        step("代码围栏处理", t_fence)

        def t_budget():
            budget = int(ai.get("max_tokens_review") or 4000)
            t0 = time.time()
            text, _ = client.chat("你只输出 JSON。",
                                  '{"task":"budget","要求":"输出 JSON：{\\"text\\":\\"'
                                  '用中文写 800 字左右的技术分析段落，含标点与换行\\"}"}',
                                  json_mode=True, temperature=0.2, max_tokens=budget)
            trunc = getattr(client, "last_truncated", False)
            return (not trunc), ("%d 字，finish_reason=%s，耗时 %.1fs%s"
                                 % (len(text or ""), client.last_finish_reason,
                                    time.time() - t0,
                                    " ← 被截断，请调大 max_tokens_review" if trunc else ""))

        step("长输出额度(%d)" % int(ai.get("max_tokens_review") or 4000), t_budget)

        def t_protocol():
            # 复盘采用「双调用」：A) JSON 结论  B) 纯 Markdown 手册
            t1, _ = client.chat(
                "你只输出一个 JSON 对象，不输出任何其它文字。",
                '{"task":"protocol","要求":"输出 JSON：{\\"critique\\":\\"一句话严厉自评\\",'
                '\\"lessons\\":[\\"教训1\\",\\"教训2\\"]}"}',
                json_mode=True, temperature=0.2, max_tokens=1000)
            a = llm_mod.extract_json(t1)
            t2, _ = client.chat(
                "你是交易者，写自己的《交易手册》。纯 Markdown，从一级标题开始，"
                "不要代码围栏、不要前言。",
                '{"上一版手册":"# 交易手册 v1\\n\\n## 一、市场观\\n1. 顺势而为。",'
                '"要求":"写 300 字左右的手册，含 开仓条件 / 卖出条件 / 仓位纪律"}',
                json_mode=False, temperature=0.3,
                max_tokens=int(ai.get("max_tokens_playbook") or 5000))
            b_ok = bool(t2) and ("#" in t2) and len(t2) > 150
            trunc = getattr(client, "last_truncated", False)
            return (bool(a) and b_ok and not trunc), \
                "JSON %s / 手册 %d 字%s" % ("OK" if a else "失败", len(t2 or ""),
                                          "（被截断）" if trunc else "")

        step("复盘双调用(JSON+手册)", t_protocol)

    def t_rails():
        cap = float((cfg.get("account") or {}).get("total_capital", 15000))
        rails = ai_agent.RiskRails(ai.get("rails"), cap)
        ok = rails.cfg["max_position_pct"] <= 100 and rails.cfg["max_total_position_pct"] <= 100
        return ok, rails.describe()

    step("硬风控配置", t_rails)

    bad = [n for n, ok, _ in results if not ok]
    print("")
    if bad:
        print(color("体检未通过：%s" % "、".join(bad), "yellow"))
        print(color("  提示：JSON 失败→查 response_format；截断/空输出→调大对应 max_tokens；"
                    "两段式失败→换更强模型。", "grey"))
    else:
        print(color("体检全部通过，可以开始真实 AI 操盘。", "red"))
    print(color("详细失败原文在 data/ai/failed/，Web「AI 决策」页可查看。", "grey"))


def cmd_ai(args, cfg):
    from astock.ai_agent import AITrader
    if args.provider:
        cfg.setdefault("ai", {})["provider"] = args.provider
    if args.setup:
        cmd_ai_setup(cfg)
        return
    if args.ping:
        cmd_ai_ping(cfg)
        return
    if args.doctor:
        cmd_ai_doctor(cfg)
        return
    if args.reset:
        backup.make_backup("before-reset", verbose=True)   # 重置前先留档
        ai_dir = os.path.join(BASE, "data", "ai")
        state = os.path.join(ai_dir, "paper_state.json")
        if os.path.exists(state):
            os.remove(state)
            print(color("已重置模拟盘账户。", "yellow"))
        keep = ("playbook.md", "playbook_history", "journal", "lessons.json",
                "decisions", "reports", "prompts", "llm_usage.jsonl",
                "watchlist.json", "failed", "runs.jsonl")
        if os.path.isdir(ai_dir):
            import shutil
            pp = os.path.join(ai_dir, "plans")
            if os.path.isdir(pp):
                shutil.rmtree(pp, ignore_errors=True)   # 计划单与账户同生共死
            for name in os.listdir(ai_dir):
                if name not in keep:
                    p = os.path.join(ai_dir, name)
                    if os.path.isfile(p):
                        os.remove(p)
        print(color("策略手册与日记已保留（AI 的记忆不会因重置而丢失）。", "grey"))
        return
    try:
        trader = AITrader(cfg)
    except Exception as exc:                          # noqa: BLE001
        print(color("初始化失败：%s" % exc, "yellow"))
        print(color("提示：到 Web「设置」页或 config.json → ai 填写 provider / base_url / "
                    "model / api_key；然后运行 python main.py ai --doctor 体检。", "grey"))
        return
    if args.status:
        trader.status()
        return
    if args.loop:
        notify.banner("AI 操盘手循环运行（每 %.0f 分钟一次决策）" % args.interval_min)
        print(color("按 Ctrl+C 停止。非交易时段自动休眠。", "grey"))
        last_review = ""
        try:
            while True:
                now = datetime.now()
                if monitor.in_session():
                    trader.broker.new_day()
                    trader.phase_execute_plans()
                    trader.run_once("decide")
                    if not trader.watchlist:
                        trader.run_once("pick")
                elif now.hour >= 15 and last_review != now.strftime("%Y-%m-%d"):
                    trader.run_once("review")
                    last_review = now.strftime("%Y-%m-%d")
                else:
                    print(color("[%s] 非交易时段，休眠 %d 分钟…"
                                % (now.strftime("%H:%M"), int(args.interval_min)), "grey"))
                time.sleep(max(60.0, args.interval_min * 60))
        except KeyboardInterrupt:
            print("")
            print(color("AI 操盘手已停止。", "bold"))
        return
    trader.run_once(args.phase)


# --------------------------------------------------------------------------- #
# 数据备份 / 恢复 / 体检
# --------------------------------------------------------------------------- #
def _fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f TB" % n


def cmd_backup(args, cfg=None):
    if args.restore:
        ok, msg = backup.restore_backup(args.restore, verbose=True)
        print(("✔ " if ok else "✘ ") + msg)
        return
    if args.list:
        health = backup.data_health()
        notify.banner("数据体检  %s" % datetime.now().strftime("%Y-%m-%d %H:%M"))
        for it in health["items"]:
            flag = "✔" if it["exists"] else "·"
            detail = ("%d 个文件" % it["files"]) if it["kind"] == "dir" else \
                (_fmt_size(it.get("size", 0)) if it["exists"] else "-")
            print("  %s %-28s %-10s %s" % (flag, it["path"], detail, it["mtime"]))
        print("")
        print("  已有备份 %d 份（最近 10 份）：" % health["backup_count"])
        for b in health["backups"]:
            print("    %-48s %-9s %s" % (b["name"], _fmt_size(b["size"]), b["mtime"]))
        return
    path = backup.make_backup(args.reason or "manual", verbose=True)
    if not path:
        print("✘ 备份失败（data/ 可能为空）")
        return
    ok, missing = backup.verify_backup(os.path.basename(path), verbose=True)
    print("✔ 备份完成：%s" % path)
    print("  校验：%s" % ("核心数据齐全" if ok else "⚠ 缺少 %s" % missing))


# --------------------------------------------------------------------------- #
# Web 服务
# --------------------------------------------------------------------------- #
def cmd_serve(args, cfg):
    from astock import server
    server.serve(host=args.host, port=args.port,
                 scheduler=not args.no_scheduler,
                 open_browser=getattr(args, "open", False))


# --------------------------------------------------------------------------- #
def main():
    p = argparse.ArgumentParser(description="A 股个股分析、实时盯盘与 AI 自主操盘")
    sub = p.add_subparsers(dest="cmd")

    pa = sub.add_parser("analyze", help="单股深度分析")
    pa.add_argument("codes", nargs="*", help="股票代码，如 000620")
    pa.add_argument("--json", action="store_true", help="额外输出 JSON")
    pa.set_defaults(func=cmd_analyze)

    pw = sub.add_parser("watch", help="自选股一览")
    pw.add_argument("codes", nargs="*")
    pw.set_defaults(func=cmd_watch)

    pm = sub.add_parser("monitor", help="实时盯盘")
    pm.add_argument("--interval", type=float, help="轮询间隔秒数")
    pm.add_argument("--once", action="store_true", help="只跑一轮")
    pm.set_defaults(func=cmd_monitor)

    pd = sub.add_parser("daily", help="生成当日报告（HTML+MD+SVG 图，自动归档）")
    pd.add_argument("codes", nargs="*")
    pd.add_argument("--keep-days", type=int, help="归档保留天数（默认 60）")
    pd.set_defaults(func=cmd_daily)

    pt = sub.add_parser("test", help="数据源自检")
    pt.set_defaults(func=cmd_test)

    pb = sub.add_parser("backtest", help="策略历史回测（验证规则是否有效）")
    pb.add_argument("code", help="股票代码，如 000620")
    pb.add_argument("--bars", type=int, default=300, help="使用多少根日线")
    pb.add_argument("--capital", type=float, default=15000, help="初始资金")
    pb.add_argument("--max-pos", type=float, default=100, help="最大仓位百分比")
    pb.add_argument("--no-flow", action="store_true", help="不使用资金流因子")
    pb.add_argument("--export", action="store_true", help="导出权益曲线 CSV")
    pb.set_defaults(func=cmd_backtest)

    pai = sub.add_parser("ai", help="AI 自主操盘手（LLM 决策 + 硬风控 + 模拟盘）")
    pai.add_argument("--phase", choices=["pick", "decide", "review", "execute", "auto"],
                     default="auto", help="执行阶段：选股/操盘/复盘/执行计划单/自动")
    pai.add_argument("--status", action="store_true", help="查看账户与绩效")
    pai.add_argument("--reset", action="store_true", help="重置模拟盘（保留记忆）")
    pai.add_argument("--loop", action="store_true", help="盘中循环决策")
    pai.add_argument("--interval-min", type=float, default=30, help="循环决策间隔（分钟）")
    pai.add_argument("--provider", help="临时覆盖模型供应商（deepseek/openai/ollama/custom）")
    pai.add_argument("--setup", action="store_true", help="交互式配置 AI 网关")
    pai.add_argument("--ping", action="store_true", help="测试模型网关连通性")
    pai.add_argument("--doctor", action="store_true",
                     help="一键体检：网关/JSON/围栏/截断/双调用/风控")
    pai.set_defaults(func=cmd_ai)

    pk = sub.add_parser("backup", help="备份 / 恢复 / 体检 AI 数据（账户、记忆、成交）")
    pk.add_argument("--list", action="store_true", help="查看数据体检与已有备份")
    pk.add_argument("--reason", help="备份原因备注")
    pk.add_argument("--restore", metavar="ZIP", help="从指定备份恢复")
    pk.set_defaults(func=cmd_backup)

    ps = sub.add_parser("serve", help="启动 Web 控制台（浏览器操作 AI 操盘系统）")
    ps.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    ps.add_argument("--port", type=int, default=8770, help="端口，默认 8770")
    ps.add_argument("--no-scheduler", action="store_true", help="不启用自动调度")
    ps.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    ps.set_defaults(func=cmd_serve)

    args = p.parse_args()
    if not getattr(args, "func", None):
        p.print_help()
        return
    cfg = load_config()
    args.func(args, cfg)


if __name__ == "__main__":
    main()
