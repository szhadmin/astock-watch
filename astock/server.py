# -*- coding: utf-8 -*-
"""astock-watch Web 服务：把 AI 操盘手变成一个可远程观察、可手动干预的系统。

零依赖：Python 标准库 http.server + 原生前端，不用 npm、不用打包。

启动：
    python main.py serve                 # http://127.0.0.1:8770
    python main.py serve --port 9000 --host 0.0.0.0 --open

组成：
    · HTTP 服务     静态页面 + JSON API
    · 任务队列      异步并发（默认上限 4），线程级 stdout 路由 → 日志互不串台
                    账户类任务（决策/执行）互斥串行，避免持仓与现金写冲突
    · 调度器        开盘执行计划单、盘中定时决策、收盘复盘、收盘后选股；每分钟记录权益
"""
from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime, time as dtime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import (ai_agent, altdata, backup, discover, evolution, feed, holdings,
               hypothesis, lab, mailer, market, memory, monitor, paper,
               priors, regime, replay, report, rules, screener)
from .ai_agent import AI_DIR, REPORT_DIR, STATE_PATH, WATCHLIST_PATH

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
CONFIG_PATH = os.path.join(BASE, "config.json")

_QUOTE_CACHE = {"ts": 0.0, "data": {}}
_CAND_CACHE = {"ts": 0.0, "data": None, "running": False}

DEFAULT_SCHEDULE = {"enabled": True, "decide_every_min": 30,
                    "pick_at": "15:12", "review_at": "15:05",
                    "plan_at": "15:15", "report_at": "15:20"}

ALLOWED_AI_KEYS = {"provider", "api_key", "model", "base_url", "temperature",
                   "max_tokens", "timeout_sec", "bars_in_prompt",
                   "max_tokens_pick", "max_tokens_decide", "max_tokens_review",
                   "max_tokens_playbook"}
ALLOWED_RAIL_KEYS = set(ai_agent.RAILS_DEFAULT.keys())
ALLOWED_EXEC_KEYS = set(ai_agent.DEFAULT_EXEC.keys())


# --------------------------------------------------------------------------- #
# 配置 / 账户 / 小工具
# --------------------------------------------------------------------------- #
def load_config():
    with open(CONFIG_PATH, encoding="utf-8-sig") as f:
        return json.load(f)


def save_config(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, CONFIG_PATH)


def broker():
    return paper.PaperBroker(STATE_PATH, capital=float(
        (load_config().get("account") or {}).get("total_capital", 15000)))


def quotes(codes, ttl=15):
    """带缓存的行情，避免前端刷新把接口打爆。"""
    now = time.time()
    stale = [c for c in codes if now - _QUOTE_CACHE["ts"] > ttl
             or c not in _QUOTE_CACHE["data"]]
    if stale:
        for c in stale:
            try:
                _QUOTE_CACHE["data"][c] = feed.get_quote(c)
            except Exception:
                _QUOTE_CACHE["data"][c] = None
        _QUOTE_CACHE["ts"] = now
    return {c: _QUOTE_CACHE["data"].get(c) for c in codes}


def mask_key(k):
    if not k:
        return ""
    return "****" + str(k)[-4:]


def read_text_safe(path, limit=200000):
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()[:limit]


def safe_child(root, name):
    """防目录穿越：只允许 root 下的普通文件名。"""
    name = os.path.basename(name or "")
    p = os.path.join(root, name)
    return p if os.path.isfile(p) else None


def list_dir(root, exts=(".md", ".txt", ".jsonl"), limit=200):
    if not os.path.isdir(root):
        return []
    out = []
    for fn in os.listdir(root):
        if not fn.endswith(exts):
            continue
        p = os.path.join(root, fn)
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append({"name": fn, "size": st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")})
    out.sort(key=lambda x: x["name"], reverse=True)
    return out[:limit]


def llm_usage_total():
    path = os.path.join(AI_DIR, "llm_usage.jsonl")
    tot = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
    if not os.path.exists(path):
        return tot
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                u = d.get("usage") or {}
                tot["calls"] += 1
                tot["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
                tot["completion_tokens"] += int(u.get("completion_tokens") or 0)
    except Exception:
        pass
    return tot


# --------------------------------------------------------------------------- #
# 异步任务：线程级日志路由 + 并发
# --------------------------------------------------------------------------- #
JOBS = {}
JOB_SEQ = {"n": 0}
CURRENT = {"id": None}
JOB_LOCK = threading.Lock()
MAX_CONCURRENT = 4

ACCOUNT_LOCK = threading.Lock()          # 会改持仓/现金的任务必须串行
ACCOUNT_KINDS = ("decide", "execute", "auto")
_TL = threading.local()


class ThreadLogRouter(io.TextIOBase):
    """把 stdout 按线程路由到对应任务的日志（多任务并发必需）。"""

    @property
    def encoding(self):
        return "utf-8"

    def write(self, s):
        if not s:
            return 0
        sink = getattr(_TL, "sink", None)
        if sink:
            try:
                sink(s)
            except Exception:
                pass
        try:
            sys.__stdout__.write(s)
        except Exception:
            pass
        return len(s)

    def flush(self):
        try:
            sys.__stdout__.flush()
        except Exception:
            pass


def install_log_router():
    if not isinstance(sys.stdout, ThreadLogRouter):
        sys.stdout = ThreadLogRouter()


def running_jobs():
    with JOB_LOCK:
        return [j for j in JOBS.values() if j["status"] == "running"]


def start_job(kind, func, params=None):
    """异步启动任务，可并发（上限 MAX_CONCURRENT）。返回 (job_id, error)。"""
    with JOB_LOCK:
        running = [j for j in JOBS.values() if j["status"] == "running"]
        if len(running) >= MAX_CONCURRENT:
            return None, "并发任务已达上限 %d，请稍后再试（运行中：%s）" % (
                MAX_CONCURRENT, "、".join(j["id"] for j in running))
        JOB_SEQ["n"] += 1
        jid = "job%d" % JOB_SEQ["n"]
        job = {"id": jid, "kind": kind, "status": "running",
               "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "started_ts": time.time(), "log": [], "result": None,
               "params": params or {}, "exclusive": kind in ACCOUNT_KINDS}
        JOBS[jid] = job
        CURRENT["id"] = jid

    def runner():
        install_log_router()
        _TL.sink = job["log"].append
        try:
            if job["exclusive"]:
                with ACCOUNT_LOCK:
                    job["waiting"] = False
                    result = func(params or {})
            else:
                result = func(params or {})
            job["result"] = result
            job["status"] = "done"
        except Exception as exc:                      # noqa: BLE001
            job["status"] = "error"
            job["result"] = {"error": str(exc)}
            job["log"].append("\n[ERROR] %s\n%s" % (exc, traceback.format_exc()[-1500:]))
        finally:
            _TL.sink = None
            job["finished"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            job["elapsed"] = round(time.time() - job.get("started_ts", time.time()), 1)
            _append_run(job)

    job["waiting"] = job["exclusive"] and ACCOUNT_LOCK.locked()
    threading.Thread(target=runner, daemon=True).start()
    return jid, None


def _append_run(job):
    try:
        os.makedirs(AI_DIR, exist_ok=True)
        with open(os.path.join(AI_DIR, "runs.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": job["id"], "kind": job["kind"],
                                "status": job["status"], "started": job["started"],
                                "finished": job.get("finished"),
                                "result": job.get("result")}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def run_phase(params):
    kind = params.get("kind")
    cfg = load_config()
    trader = ai_agent.AITrader(cfg)
    if kind == "pick":
        trader.phase_pick()
    elif kind == "decide":
        trader.phase_decide()
    elif kind == "plan":
        # 收盘后：基于当日完整日线，只生成「次日计划单」，不以收盘价成交
        trader.phase_decide(force_plan=True)
    elif kind == "review":
        trader.phase_review()
    elif kind == "execute":
        trader.phase_execute_plans()
    elif kind == "discover":
        res = discover.discover(verbose=True)
        rules.link_from_hypotheses(verbose=True)
        return {"discover": res, "rules": rules.active_for_prompt()}
    elif kind == "replay":
        from . import discover as disc
        res = replay.run(max_codes=int((params or {}).get("codes") or 25),
                         days=int((params or {}).get("days") or 600), verbose=True)
        dres = disc.discover(verbose=True)
        rules.link_from_hypotheses(verbose=True)
        return {"replay": res, "discover": dres}
    elif kind == "daily":
        res = report.build(cfg=cfg, verbose=True)
        try:
            tr = holdings.trades_overview()["stats"]
            mailer.send_digest(
                "daily", journal=_latest_journal(),
                plans=trader._load_plans(), advices=_holdings_advice(),
                account=trader._snapshot(), cfg=cfg,
                attachments=[res.get("html"), res.get("md")],
                extra_html=_real_stats_html(tr))
        except Exception:
            pass
        restore_cfg = None
        return {"report": {"html": res["html"], "md": res["md"],
                           "archive": res["archive"], "codes": res["codes"]}}
    elif kind == "auto":
        trader.run_once("auto")
    else:
        raise ValueError("未知任务 %s" % kind)
    if kind in ("review", "plan"):
        try:                                  # 复盘/次日计划 → 邮件
            temp = None
            try:
                t = market.market_temp()
                temp = {"temp": t["temp"], "level": t["level"],
                        "note": market.gate(t)["note"]}
            except Exception:
                pass
            mailer.send_digest(kind, journal=_latest_journal(),
                               plans=trader._load_plans(),
                               advices=_holdings_advice(),
                               account=trader._snapshot(), temp=temp, cfg=cfg)
        except Exception:
            pass
    snap = trader._snapshot()
    try:
        backup.make_backup("phase-" + str(kind))   # 每轮决策/复盘后自动留档
    except Exception:
        pass
    return {"equity": snap["equity"], "cash": snap["cash"],
            "positions": len(snap["positions"]),
            "tokens": trader.llm.usage}


def scan_candidates(params):
    """后台跑选股初筛（全市场快照 + 60 只 K 线），结果缓存给前端。"""
    global _CAND_CACHE
    try:
        ai_cfg = load_config().get("ai") or {}
        feats = screener.screen({**(ai_cfg.get("screen") or {}), "_ai_cfg": ai_cfg})
        _CAND_CACHE = {"ts": time.time(), "data": feats, "running": False}
        print("候选池就绪：%d 只" % len(feats))
        return {"count": len(feats)}
    except Exception as exc:                          # noqa: BLE001
        _CAND_CACHE["running"] = False
        raise exc


# --------------------------------------------------------------------------- #
# 调度器
# --------------------------------------------------------------------------- #
def _has_something_to_review(today):
    """今天还没复盘过，且今天确实有交易或决策 → 才调模型复盘（省钱）。"""
    try:
        hist = memory.decision_history(1)
    except Exception:
        return True
    if any((d.get("ts") or "").startswith(today) and d.get("kind") == "review"
           for d in hist):
        return False
    try:
        b = broker()
        if any(t["ts"].startswith(today) for t in b.state["trades"]):
            return True
    except Exception:
        pass
    return any((d.get("ts") or "").startswith(today) for d in hist)


def _picked_today(today):
    """观察池今天已更新过，就不重复跑选股会。"""
    try:
        w = _read_watchlist() or {}
        return str(w.get("updated") or "").startswith(today) and bool(w.get("codes"))
    except Exception:
        return False


class Scheduler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.last_decide = 0.0
        self.last_pick_day = ""
        self.last_review_day = ""
        self.last_exec_day = ""
        self.last_report_day = ""
        self.last_plan_day = ""
        self.last_equity_ts = 0.0
        self.enabled = True

    def _job_running(self, kinds=None):
        rj = running_jobs()
        if kinds:
            rj = [j for j in rj if j["kind"] in kinds]
        return bool(rj)

    def run(self):
        while True:
            try:
                self.tick()
            except Exception:
                pass
            time.sleep(20)

    def tick(self):
        cfg = load_config()
        sch = {**DEFAULT_SCHEDULE, **((cfg.get("ai") or {}).get("schedule") or {})}
        if not sch.get("enabled", True):
            return
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        t = now.time()

        # 每分钟记录一次权益（仅交易时段）
        if monitor.in_session(now) and time.time() - self.last_equity_ts > 60 \
                and not self._job_running(("decide", "execute")):
            self.last_equity_ts = time.time()
            try:
                b = broker()
                prices = {}
                for c in list(b.positions.keys()):
                    q = feed.get_quote(c)
                    prices[c] = q.get("price")
                b.record_equity(b.mark(prices))
            except Exception:
                pass

        def at_or_after(hhmm):
            hh, mm = [int(x) for x in hhmm.split(":")]
            return t >= dtime(hh, mm)

        # 开盘后执行上一交易日的计划单
        if dtime(9, 31) <= t <= dtime(11, 30) and self.last_exec_day != today \
                and not self._job_running(("execute",)):
            self.last_exec_day = today
            start_job("execute", run_phase, {"kind": "execute"})
        # 收盘复盘
        if at_or_after(sch["review_at"]) and self.last_review_day != today \
                and not self._job_running(("review",)) and _has_something_to_review(today):
            self.last_review_day = today
            start_job("review", run_phase, {"kind": "review"})
        # 收盘后选股
        if at_or_after(sch["pick_at"]) and self.last_pick_day != today \
                and not self._job_running(("pick", "scan")) and not _picked_today(today):
            self.last_pick_day = today
            start_job("pick", run_phase, {"kind": "pick"})
        # 收盘后生成「次日买卖计划单」（基于当日完整数据，不成交）
        if at_or_after(sch.get("plan_at", "15:15")) and self.last_plan_day != today \
                and not self._job_running(("plan", "decide")):
            self.last_plan_day = today
            start_job("plan", run_phase, {"kind": "plan"})
        # 收盘后自动生成日报并归档
        if at_or_after(sch.get("report_at", "15:20")) and self.last_report_day != today \
                and not self._job_running(("daily",)):
            self.last_report_day = today
            start_job("daily", run_phase, {"kind": "daily"})
        # 盘中定时决策
        if monitor.in_session(now) \
                and time.time() - self.last_decide >= float(sch["decide_every_min"]) * 60 \
                and not self._job_running(("decide", "execute")):
            self.last_decide = time.time()
            start_job("decide", run_phase, {"kind": "decide"})


SCHEDULER = None


# --------------------------------------------------------------------------- #
# 只读小工具
# --------------------------------------------------------------------------- #
def _read_watchlist():
    if not os.path.exists(WATCHLIST_PATH):
        return {"codes": []}
    try:
        with open(WATCHLIST_PATH, encoding="utf-8-sig") as f:
            d = json.load(f)
        if isinstance(d, list):
            return {"codes": d}
        return d
    except Exception:
        return {"codes": []}


def _read_plans():
    p = os.path.join(AI_DIR, "plans", "pending_plans.json")
    if not os.path.exists(p):
        return []
    try:
        with open(p, encoding="utf-8-sig") as f:
            return json.load(f) or []
    except Exception:
        return []


def _sma(values, n):
    out, s = [], 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
def api_overview(_q):
    cfg = load_config()
    b = broker()
    codes = list(b.positions.keys()) + list((_read_watchlist() or {}).get("codes") or [])
    qs = quotes(codes)
    prices = {c: (qs.get(c) or {}).get("price") for c in codes}
    snap = b.mark({k: v for k, v in prices.items() if v})
    stats = b.stats()
    try:
        idx = feed.get_indices()
    except Exception:
        idx = []
    ai = cfg.get("ai") or {}
    sch = {**DEFAULT_SCHEDULE, **(ai.get("schedule") or {})}
    rj = running_jobs()
    return {
        "account": {"capital": b.state["capital"], "cash": snap["cash"],
                    "market_value": snap["market_value"], "equity": snap["equity"],
                    "total_return_pct": snap["total_return_pct"]},
        "stats": stats,
        "positions": snap["positions"],
        "watch": [{"code": c, "name": (qs.get(c) or {}).get("name"),
                   "price": (qs.get(c) or {}).get("price"),
                   "pct": (qs.get(c) or {}).get("pct")}
                  for c in ((_read_watchlist() or {}).get("codes") or [])],
        "indices": idx,
        "ai": {"provider": ai.get("provider"), "model": ai.get("model") or "(默认)",
               "key": mask_key(ai.get("api_key")), "base_url": ai.get("base_url") or "(默认)"},
        "schedule": sch,
        "rails": {**ai_agent.RAILS_DEFAULT, **(ai.get("rails") or {})},
        "rails_desc": ai_agent.RiskRails(ai.get("rails"), b.state["capital"]).describe(),
        "plans": _read_plans(),
        "usage": llm_usage_total(),
        "job": (rj[0]["id"] if rj else None),
        "running": [j["id"] for j in rj],
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "in_session": monitor.in_session(),
    }


def api_equity(_q):
    b = broker()
    return {"equity": b.state["equity_history"][-2000:],
            "capital": b.state["capital"]}


def api_decisions(q):
    days = int((q.get("days") or ["3"])[0])
    items = memory.decision_history(days)
    for it in items:
        it.pop("output", None)
    return {"items": items[-60:][::-1]}


def api_decision_report(q):
    fn = (q.get("f") or [""])[0]
    p = safe_child(REPORT_DIR, fn)
    if not p:
        return {"error": "文件不存在"}
    return {"name": fn, "content": read_text_safe(p)}


def api_reports(_q):
    return {"items": list_dir(REPORT_DIR)}


def api_prompts(_q):
    return {"items": list_dir(os.path.join(AI_DIR, "prompts"))}


def api_prompt(q):
    fn = (q.get("f") or [""])[0]
    p = safe_child(os.path.join(AI_DIR, "prompts"), fn)
    if not p:
        return {"error": "文件不存在"}
    return {"name": fn, "content": read_text_safe(p)}


def api_candidates(_q):
    return {"running": _CAND_CACHE["running"],
            "age_sec": int(time.time() - _CAND_CACHE["ts"]) if _CAND_CACHE["ts"] else None,
            "items": _CAND_CACHE["data"] or []}


def api_scan(params):
    global _CAND_CACHE
    if _CAND_CACHE["running"]:
        return None, "扫描已在进行中"
    _CAND_CACHE["running"] = True

    def f(_p):
        return scan_candidates({})
    jid, err = start_job("scan", f, params)
    return ({"job": jid} if jid else None), err


def api_watchlist_get(_q):
    return _read_watchlist()


def api_watchlist_set(body):
    codes = [str(c).strip() for c in (body.get("codes") or []) if str(c).strip()]
    codes = [c for c in codes if c.isdigit() and len(c) == 6][:10]
    os.makedirs(AI_DIR, exist_ok=True)
    with open(WATCHLIST_PATH, "w", encoding="utf-8") as f:
        json.dump({"codes": codes, "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
                   "meta": {"source": "web"}}, f, ensure_ascii=False, indent=1)
    return {"codes": codes}


def api_journal(q):
    d = (q.get("d") or [""])[0]
    jdir = os.path.join(AI_DIR, "journal")
    if d:
        p = safe_child(jdir, "%s.md" % d)
        return {"date": d, "content": read_text_safe(p) if p else None}
    return {"items": list_dir(jdir)}


def api_failed(q):
    root = os.path.join(AI_DIR, "failed")
    fn = (q.get("f") or [""])[0]
    if fn:
        p = safe_child(root, fn)
        return {"name": fn, "content": read_text_safe(p) if p else "(文件不存在)"}
    return {"items": list_dir(root, (".txt",))}


def api_lessons(_q):
    return {"items": memory.read_lessons(60)[::-1]}


def api_playbook(_q):
    hist = []
    hdir = os.path.join(AI_DIR, "playbook_history")
    if os.path.isdir(hdir):
        hist = list_dir(hdir)[:30]
    return {"text": memory.read_playbook(), "history": hist}


def api_playbook_file(q):
    fn = (q.get("f") or [""])[0]
    p = safe_child(os.path.join(AI_DIR, "playbook_history"), fn)
    if not p:
        return {"error": "文件不存在"}
    return {"name": fn, "content": read_text_safe(p)}


def api_trades(_q):
    b = broker()
    return {"items": b.state["trades"][-200:][::-1],
            "stats": b.stats()}


def api_config_get(_q):
    cfg = load_config()
    ai = cfg.get("ai") or {}
    return {"ai": {**{k: ai.get(k) for k in ALLOWED_AI_KEYS},
                   "api_key": mask_key(ai.get("api_key"))},
            "rails": {**ai_agent.RAILS_DEFAULT, **(ai.get("rails") or {})},
            "execution": {**ai_agent.DEFAULT_EXEC, **(ai.get("execution") or {})},
            "schedule": {**DEFAULT_SCHEDULE, **(ai.get("schedule") or {})},
            "account": cfg.get("account") or {}}


def api_config_set(body):
    cfg = load_config()
    ai = cfg.setdefault("ai", {})
    changed = []
    for k, v in (body.get("ai") or {}).items():
        if k not in ALLOWED_AI_KEYS:
            continue
        if k == "api_key":
            if isinstance(v, str) and v.startswith("****"):
                continue
            ai["api_key"] = v
        elif k == "temperature":
            ai[k] = float(v)
        elif k in ("max_tokens", "max_tokens_pick", "max_tokens_decide",
                   "max_tokens_review", "max_tokens_playbook",
                   "timeout_sec", "bars_in_prompt"):
            ai[k] = int(v)
        else:
            ai[k] = v
        changed.append("ai." + k)
    rails = ai.setdefault("rails", {})
    for k, v in (body.get("rails") or {}).items():
        if k in ALLOWED_RAIL_KEYS:
            rails[k] = float(v)
            changed.append("rails." + k)
    ex = ai.setdefault("execution", {})
    for k, v in (body.get("execution") or {}).items():
        if k in ALLOWED_EXEC_KEYS:
            ex[k] = v
            changed.append("execution." + k)
    sch = ai.setdefault("schedule", {})
    for k, v in (body.get("schedule") or {}).items():
        if k in DEFAULT_SCHEDULE:
            sch[k] = v
            changed.append("schedule." + k)
    save_config(cfg)
    return {"changed": changed, "config": api_config_get({})}


def api_run(body):
    kind = (body.get("kind") or "").strip()
    if kind not in ("pick", "decide", "review", "auto", "scan", "execute",
                    "daily", "plan", "replay", "discover"):
        return None, "未知任务类型"
    if kind == "scan":
        return api_scan(body)
    params = {"kind": kind}
    if kind == "replay":                       # 回放参数从页面传进来
        if body.get("codes"):
            params["codes"] = int(body["codes"])
        if body.get("days"):
            params["days"] = int(body["days"])
    jid, err = start_job(kind, run_phase, params)
    return ({"job": jid} if jid else None), err


def api_job(q):
    jid = (q.get("id") or [""])[0]
    job = JOBS.get(jid)
    if not job:
        return {"error": "任务不存在"}
    return {"id": job["id"], "kind": job["kind"], "status": job["status"],
            "started": job["started"], "finished": job.get("finished"),
            "exclusive": job.get("exclusive"), "waiting": job.get("waiting"),
            "elapsed": job.get("elapsed"),
            "log": "".join(job["log"])[-30000:], "result": job.get("result")}


def api_jobs(_q):
    with JOB_LOCK:
        jobs = sorted(JOBS.values(), key=lambda x: x["id"], reverse=True)[:30]
        items = [{"id": j["id"], "kind": j["kind"], "status": j["status"],
                  "started": j["started"], "finished": j.get("finished"),
                  "exclusive": j.get("exclusive"), "waiting": j.get("waiting"),
                  "elapsed": j.get("elapsed"),
                  "log_size": len("".join(j["log"]))} for j in jobs]
        running = [j["id"] for j in jobs if j["status"] == "running"]
    return {"items": items, "running": running, "current": CURRENT["id"],
            "max_concurrent": MAX_CONCURRENT,
            "account_busy": ACCOUNT_LOCK.locked()}


def api_search(q):
    """股票搜索：代码 / 名称 / 拼音首字母。"""
    kw = (q.get("q") or [""])[0]
    try:
        limit = int((q.get("limit") or ["10"])[0])
    except ValueError:
        limit = 10
    try:
        return {"items": feed.search_stock(kw, limit)}
    except Exception as exc:                          # noqa: BLE001
        return {"items": [], "error": str(exc)}


def api_held(_q):
    """当前持有的股票（我的持仓表 + 成交流水推导），供"卖出"选择。"""
    try:
        return {"items": holdings.held_positions()}
    except Exception as exc:                          # noqa: BLE001
        return {"items": [], "error": str(exc)}


def api_priors(_q):
    """系统已验证的先验（代码维护，AI 不可修改）。"""
    return {"items": priors.VERIFIED, "text": priors.text()}


def api_regime(_q):
    """市场状态细分（牛/熊/窄幅缩量/宽幅剧震/常规震荡）+ 对应策略。"""
    try:
        r = regime.now()
        return {"regime": r, "policy": regime.policy(r)}
    except Exception as exc:                          # noqa: BLE001
        return {"error": str(exc)}


def api_market(_q):
    """大盘温度 + 行业板块强度 + 当前收紧策略（前端仪表盘用）。"""
    try:
        temp = market.market_temp()
    except Exception as exc:                          # noqa: BLE001
        return {"error": "大盘温度获取失败：%s" % exc}
    try:
        sectors = market.sector_board(8)
    except Exception:
        sectors = []
    g = market.gate(temp)
    return {"temp": temp, "gate": g,
            "sectors": [{"name": s["name"], "pct": s["pct"],
                         "main_in_wan": round(s["main_in"] / 1e4, 0),
                         "up": s["up"], "down": s["down"],
                         "leader": s["leader"]} for s in sectors]}


def api_report_archive(_q):
    """已归档的日报列表（按日期倒序）。"""
    return {"items": report.archive_list(30)}


def _latest_journal(limit=1500):
    """最近一篇操盘日记的尾部内容（邮件正文用）。"""
    d = os.path.join(BASE, "data", "ai", "journal")
    try:
        files = sorted(f for f in os.listdir(d) if f.endswith(".md"))
        if not files:
            return ""
        with open(os.path.join(d, files[-1]), encoding="utf-8-sig") as f:
            txt = f.read()
        return txt[-limit:]
    except Exception:
        return ""


def _real_stats_html(st):
    """实盘绩效小卡片（邮件正文用）。"""
    if not st or not st.get("trades"):
        return ""
    return ("<h3>我的实盘绩效</h3><table>"
            "<tr><th>已实现盈亏</th><td>%+.2f</td><th>胜率</th><td>%.1f%%</td></tr>"
            "<tr><th>盈亏比</th><td>%s</td><th>平仓笔数</th><td>%d</td></tr>"
            "<tr><th>总手续费</th><td>%.2f</td><th>买/卖</th><td>%d / %d</td></tr>"
            "</table>" % (st.get("realized") or 0, st.get("win_rate_pct") or 0,
                          st.get("profit_factor") or "--", st.get("closed") or 0,
                          st.get("fee_total") or 0, st.get("buys") or 0,
                          st.get("sells") or 0))


def _holdings_advice():
    """AI 上一次给真实持仓的操作建议。"""
    p = os.path.join(BASE, "data", "holdings_advice.json")
    try:
        with open(p, encoding="utf-8-sig") as f:
            return json.load(f).get("items") or []
    except Exception:
        return []


def api_lab(_q):
    """研究台：特征分层（优先走 SQLite 聚合，内存恒定）。"""
    try:
        from . import hypdb
        if hypdb.has_data():
            ov = hypdb.overview()
            return {
                "samples": ov["closed"], "stocks": len({r[0] for r in hypdb._q(
                    "SELECT DISTINCT code FROM hyps")}),
                "range": list(hypdb._q("SELECT MIN(date), MAX(date) FROM hyps")[0]),
                "baseline": {"avg": round((hypdb._q(
                    "SELECT AVG(COALESCE(excess,end_ret)) FROM hyps WHERE hit IS NOT NULL")[0][0] or 0), 2)},
                "score": hypdb.buckets("score", [(0, 40), (40, 50), (50, 60), (60, 70), (70, 101)]),
                "temp": hypdb.buckets("temp", [(0, 25), (25, 40), (40, 60), (60, 75), (75, 101)]),
                "horizon": hypdb.horizon_stats(),
                "states": hypdb.state_stats(),
                "regime": hypdb.regime_stats(),
                "features": {"评分变化率": hypdb.buckets(
                    "score_chg", [(-999, -5), (-5, 0), (0, 5), (5, 999)])},
                "source": "sqlite",
            }
    except Exception as exc:                          # noqa: BLE001
        print("  （SQLite 分层失败，回退 jsonl：%s）" % exc)
    try:
        rows = lab.load()
        if not rows:
            return {"error": "还没有回放样本，先在研究台点「历史回放」"}
        rets = [r["result"].get("end_ret") or 0 for r in rows]
        hits = sum(1 for r in rows if r["result"].get("hit"))
        return {
            "samples": len(rows),
            "stocks": len({r["code"] for r in rows}),
            "range": [min(r["date"] for r in rows), max(r["date"] for r in rows)],
            "baseline": {"avg": round(sum(rets) / len(rets), 2),
                         "net": round(sum(rets) / len(rets) - lab.COST, 2),
                         "hit": round(hits / len(rows) * 100, 1)},
            "score": lab.bucket_stats(rows, "score",
                                      [(0, 40), (40, 50), (50, 60), (60, 70), (70, 101)]),
            "temp": lab.bucket_stats(rows, "temp",
                                     [(0, 25), (25, 40), (40, 60), (60, 75), (75, 101)]),
            "horizon": lab.horizon_table(rows),
            "states": lab.state_stats(rows),
            "features": lab.feature_stats(rows),
            "regime": lab.regime_stats(rows),
        }
    except Exception as exc:                          # noqa: BLE001
        return {"error": "分层计算失败：%s" % exc}


def api_evolution(_q):
    """手册版本 A/B + 错题本。"""
    try:
        return evolution.summary()
    except Exception as exc:                          # noqa: BLE001
        return {"error": str(exc)}


def api_playbook_rollback(body):
    ok, msg = evolution.rollback((body or {}).get("version"))
    return ({"ok": True, "message": msg} if ok else None), (None if ok else msg)


_RULES_CACHE = {"ts": 0.0, "data": None}
RULES_TTL = 60.0


def api_rules(_q):
    """经验规则库：生效/候选/淘汰 + 升格门槛（60 秒缓存，避免每次刷新都全量统计）。"""
    import time as _t
    now = _t.time()
    if _RULES_CACHE["data"] and now - _RULES_CACHE["ts"] < RULES_TTL:
        return _RULES_CACHE["data"]
    try:
        rules.link_from_hypotheses()
    except Exception:
        pass
    s = rules.summary()
    s["prompt_view"] = rules.active_for_prompt()
    _RULES_CACHE.update({"ts": now, "data": s})
    return s


def api_rules_lesson_check(body):
    """手动检查一条教训是否可证伪（前端"体检"用）。"""
    return rules.check_verifiable((body or {}).get("text") or "")


def api_hypotheses(_q):
    """判断记分卡：AI 过往判断的命中率与平均收益。"""
    try:
        hypothesis.backfill()
    except Exception:
        pass
    card = hypothesis.scorecard()
    card["recent"] = hypothesis.recent(12)
    return card


def api_real_trades(_q):
    """实盘成交流水 + 由流水推导的持仓 + 真实绩效（胜率/盈亏比）。"""
    return holdings.trades_overview()


def api_real_trades_add(body):
    try:
        rec = holdings.add_trade(body)
    except Exception as exc:                          # noqa: BLE001
        return None, str(exc)
    try:
        backup.make_backup("real-trade")
    except Exception:
        pass
    res = holdings.trades_overview()
    res["added"] = rec
    return res, None


def api_real_trades_del(body):
    n = holdings.del_trade((body.get("id") or "").strip())
    res = holdings.trades_overview()
    res["left"] = n
    return res, None


def api_real_trades_rebuild(body):
    try:
        n = holdings.rebuild_holdings_from_trades(
            force=bool((body or {}).get("force")))
    except Exception as exc:                          # noqa: BLE001
        return None, "同步失败：%s" % exc
    return {"ok": True, "count": n}, None


def api_holdings_advice(_q):
    p = os.path.join(BASE, "data", "holdings_advice.json")
    try:
        with open(p, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {"items": [], "ts": None,
                "hint": "还没有建议。点「立即决策」或在盘中/收盘后自动决策一次即可生成。"}


def api_mail_get(_q):
    c = mailer.conf()
    ok, why = mailer.available()
    out = {k: v for k, v in c.items()}
    if out.get("password"):
        out["password"] = "****" + str(out["password"])[-4:]
    out["ready"] = ok
    out["status"] = why
    return out


def api_mail_save(body):
    cfg = load_config()
    cur = mailer.conf(cfg)
    e = (cfg.setdefault("notify", {}).setdefault("email", {}))
    for k in ("enabled", "smtp_host", "smtp_port", "use_ssl", "user", "to", "from_name"):
        if k in body:
            e[k] = body[k]
    if "password" in body and not str(body["password"]).startswith("****"):
        e["password"] = body["password"]
    if "events" in body:
        e["events"] = {**cur["events"], **(body["events"] or {})}
    e["smtp_port"] = int(e.get("smtp_port") or 465)
    with open(os.path.join(BASE, "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    ok, why = mailer.available(cfg)
    return {"ok": True, "ready": ok, "status": why, "config": api_mail_get({})}


def api_mail_test(_body):
    ok, msg = mailer.test()
    return {"ok": ok, "message": msg}


def api_holdings(_q):
    """我的持仓：资金量 + 手工录入的股票与成本 + 逐只分析。"""
    try:
        return holdings.analyze(load_config())
    except Exception as exc:                          # noqa: BLE001
        return {"error": "分析失败：%s" % exc, "items": [], "summary": {}}


def api_holdings_save(body):
    """保存资金量与持仓（保存后自动备份，避免丢数据）。"""
    try:
        saved = holdings.save({"capital": body.get("capital"),
                               "items": body.get("items") or []})
    except Exception as exc:                          # noqa: BLE001
        return None, "保存失败：%s" % exc
    try:
        backup.make_backup("holdings")
    except Exception:
        pass
    res = holdings.analyze(load_config())
    res["saved"] = {"capital": saved["capital"], "count": len(saved["items"]),
                    "updated": saved["updated"]}
    return res, None


def api_altdata(q):
    """单只股票的扩展数据：财报 / 股东户数 / 龙虎榜。"""
    code = (q.get("code") or [""])[0].strip()
    if not code or not code.isdigit():
        return {"error": "请提供 6 位股票代码"}
    cfg = load_config()
    ai = cfg.get("ai") or {}
    use = bool(ai.get("use_altdata", True))
    force = (q.get("force") or [""])[0] in ("1", "true")
    data = altdata.brief(code, token=ai.get("tushare_token", ""), use=use, force=force)
    st = altdata.status()
    if not data:
        return {"code": code, "available": False, "status": st,
                "hint": st.get("note")}
    data["available"] = True
    data["status"] = st
    data["compact"] = altdata.compact(code, token=ai.get("tushare_token", ""), use=use)
    return data


def api_lhb(q):
    """全市场龙虎榜（近 N 日）。"""
    days = int((q.get("days") or ["30"])[0])
    limit = int((q.get("limit") or ["80"])[0])
    code = (q.get("code") or [""])[0].strip() or None
    rows, err = altdata.lhb_recent(days=days, limit=limit, code=code)
    if rows is None:
        return {"error": err, "items": []}
    return {"items": rows, "days": days, "count": len(rows),
            "status": altdata.status()}


def api_altstatus(_q):
    """可选扩展数据源（akshare / tushare）安装状态。"""
    return altdata.status()


def api_backups(_q):
    health = backup.data_health()
    health["note"] = ("账户与成交、AI 手册、日记、教训、观察池、计划单都在 data/，"
                      "已自动备份到 backups/")
    return health


def api_backup(body):
    path = backup.make_backup(body.get("reason") or "web", verbose=True)
    if not path:
        return None, "备份失败（data/ 可能为空）"
    ok, missing = backup.verify_backup(os.path.basename(path))
    return {"ok": True, "file": os.path.basename(path), "verified": ok,
            "missing": missing, "backups": backup.list_backups()[:10]}, None


def api_restore(body):
    if running_jobs():
        return None, "有任务正在运行，先等它们结束再恢复"
    name = (body.get("name") or "").strip()
    if not name:
        return None, "请指定备份文件名"
    ok, msg = backup.restore_backup(name)
    return ({"ok": True, "message": msg} if ok else None), (None if ok else msg)


def api_reset(body):
    if running_jobs():
        return None, "有任务正在运行，请等它们结束后再重置"
    if os.path.exists(STATE_PATH):
        os.remove(STATE_PATH)
    broker()
    return {"ok": True, "kept": "策略手册 / 日记 / 教训 已保留"}, None


# --------------------------------------------------------------------------- #
# K 线复盘：K线 + 均线 + AI 成交点 + 计划单 + 决策时点
# --------------------------------------------------------------------------- #
PERIOD_MAP = {
    "min1": 1, "min5": 5, "min15": 15, "min30": 30, "min60": 60,
    "day": 101, "week": 102, "month": 103,
}
PERIOD_DEFAULT_DAYS = {"min1": 240, "min5": 192, "min15": 160, "min30": 120,
                       "min60": 90, "day": 120, "week": 120, "month": 120, "trend": 1}


def _merge_live(bars, quote, is_daily):
    """把实时快照合并进最后一根 K 线（盘中图表才"活"）。"""
    if not bars or not quote or not quote.get("price"):
        return bars, False
    px = quote["price"]
    today = datetime.now().strftime("%Y-%m-%d")
    last = bars[-1]
    live = False
    if is_daily:
        if last["date"] == today:
            last["close"] = px
            last["high"] = max(last["high"], quote.get("high") or px)
            last["low"] = min(last["low"], quote.get("low") or px)
            last["vol"] = quote.get("volume") or last["vol"]
            live = True
        elif monitor.in_session():
            bars.append({"date": today, "open": quote.get("open") or px,
                         "close": px, "high": quote.get("high") or px,
                         "low": quote.get("low") or px,
                         "vol": quote.get("volume") or 0,
                         "amount": quote.get("amount") or 0,
                         "pct": quote.get("pct") or 0, "turnover": quote.get("turnover") or 0})
            live = True
    return bars, live


def api_kline(q):
    """K 线 / 分时图数据。

    period: trend(分时) | min1 | min5 | min15 | min30 | min60 | day | week | month
    days:   返回多少根 K 线（分时为当日点数）
    """
    code = (q.get("code") or [""])[0].strip()
    period = (q.get("period") or ["day"])[0].strip().lower()
    if not code or not code.isdigit():
        return {"error": "请提供 6 位股票代码"}
    if period not in PERIOD_MAP and period != "trend":
        period = "day"
    days = int((q.get("days") or ["0"])[0]) or PERIOD_DEFAULT_DAYS.get(period, 120)
    days = max(20, min(days, 400))

    q0 = quotes([code], ttl=3).get(code) or {}
    name = q0.get("name") or code
    prev_close = q0.get("prev_close")

    # ---------------- 分时 ----------------
    if period == "trend":
        points = feed.get_trends(code)
        if not points:
            return {"error": "分时数据为空（可能停牌或非交易日）"}
        prices = [p["price"] for p in points]
        avgs = [p["avg"] if p["avg"] else p["price"] for p in points]
        base = prev_close or (prices[0] if prices else 0)
        pcts = [round((p / base - 1) * 100, 2) if base else 0 for p in prices]
        return {"code": code, "name": name, "period": "trend",
                "date": datetime.now().strftime("%Y-%m-%d"),
                "price": q0.get("price") or (prices[-1] if prices else None),
                "pct": q0.get("pct"), "prev_close": base,
                "updated": datetime.now().strftime("%H:%M:%S"),
                "live": bool(monitor.in_session()), "in_session": monitor.in_session(),
                "trend": {"times": [p["time"][-5:] for p in points],
                          "prices": prices, "avgs": avgs, "vols": [p["vol"] for p in points],
                          "pcts": pcts},
                "bars": [], "ma": {}, "trades": [], "plans": [], "decisions": [],
                "position": None}

    # ---------------- K 线 ----------------
    klt = PERIOD_MAP[period]
    is_daily = period == "day"
    try:
        bars_all = feed.get_kline(code, klt, limit=days + 70, adjust=1)
    except Exception as exc:                          # noqa: BLE001
        return {"error": "K 线获取失败：%s" % exc}
    bars_all, live = _merge_live(bars_all, q0, is_daily)
    closes = [b["close"] for b in bars_all]
    ma = {}
    for n in (5, 10, 20, 60):
        if len(closes) >= n:
            ma["ma%d" % n] = [round(x, 3) if x is not None else None
                              for x in _sma(closes, n)][-days:]
        else:
            ma["ma%d" % n] = [None] * min(days, len(bars_all))
    bars = bars_all[-days:]
    out_bars = []
    for idx, b in enumerate(bars):
        gi = len(bars_all) - len(bars) + idx
        prev = closes[gi - 1] if gi > 0 else b["close"]
        out_bars.append({
            "t": b["date"], "o": b["open"], "h": b["high"], "l": b["low"],
            "c": b["close"], "v": b["vol"], "amt": b.get("amount") or 0,
            "pct": round((b["close"] / prev - 1) * 100, 2) if prev else 0,
            "turn": b.get("turnover") or 0,
        })

    b = broker()
    tr = [t for t in b.state["trades"] if t.get("code") == code][-100:]
    plans = [p for p in _read_plans() if p.get("code") == code]
    decisions = []
    try:
        for d in memory.decision_history(10):
            for a in (d.get("actions") or []):
                if str(a.get("code")) != code:
                    continue
                decisions.append({"ts": d.get("ts"), "kind": d.get("kind"),
                                  "action": a.get("action"), "shares": a.get("shares"),
                                  "reason": a.get("reason"), "view": d.get("market_view")})
    except Exception:
        pass
    pos = None
    for p in b.mark({code: q0.get("price")}).get("positions", []):
        if p["code"] == code:
            pos = p

    return {"code": code, "name": name, "period": period,
            "price": q0.get("price") or (out_bars[-1]["c"] if out_bars else None),
            "pct": q0.get("pct"), "prev_close": prev_close,
            "updated": datetime.now().strftime("%H:%M:%S"),
            "live": live, "in_session": monitor.in_session(),
            "bars": out_bars, "ma": ma,
            "trades": [{"ts": t["ts"], "date": t["ts"][:10], "side": t["side"],
                        "price": t["price"], "shares": t["shares"], "pnl": t.get("pnl"),
                        "pct": t.get("pct"), "note": t.get("note")} for t in tr],
            "plans": plans, "decisions": decisions[-40:], "position": pos}


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
GET_ROUTES = {
    "/api/overview": api_overview,
    "/api/equity": api_equity,
    "/api/decisions": api_decisions,
    "/api/decision": api_decision_report,
    "/api/reports": api_reports,
    "/api/prompts": api_prompts,
    "/api/prompt": api_prompt,
    "/api/candidates": api_candidates,
    "/api/watchlist": api_watchlist_get,
    "/api/journal": api_journal,
    "/api/lessons": api_lessons,
    "/api/failed": api_failed,
    "/api/playbook": api_playbook,
    "/api/playbook/file": api_playbook_file,
    "/api/trades": api_trades,
    "/api/kline": api_kline,
    "/api/config": api_config_get,
    "/api/backups": api_backups,
    "/api/market": api_market,
    "/api/regime": api_regime,
    "/api/priors": api_priors,
    "/api/search": api_search,
    "/api/held": api_held,
    "/api/altstatus": api_altstatus,
    "/api/altdata": api_altdata,
    "/api/holdings": api_holdings,
    "/api/holdings_advice": api_holdings_advice,
    "/api/real_trades": api_real_trades,
    "/api/hypotheses": api_hypotheses,
    "/api/rules": api_rules,
    "/api/evolution": api_evolution,
    "/api/lab": api_lab,
    "/api/mail": api_mail_get,
    "/api/lhb": api_lhb,
    "/api/report_archive": api_report_archive,
    "/api/job": api_job,
    "/api/jobs": api_jobs,
}

POST_ROUTES = {
    "/api/watchlist": api_watchlist_set,
    "/api/config": api_config_set,
    "/api/run": api_run,
    "/api/reset": api_reset,
    "/api/backup": api_backup,
    "/api/holdings": api_holdings_save,
    "/api/real_trades": api_real_trades_add,
    "/api/real_trades/delete": api_real_trades_del,
    "/api/real_trades/rebuild": api_real_trades_rebuild,
    "/api/rules/check": api_rules_lesson_check,
    "/api/playbook/rollback": api_playbook_rollback,
    "/api/mail": api_mail_save,
    "/api/mail/test": api_mail_test,
    "/api/restore": api_restore,
}


class Handler(BaseHTTPRequestHandler):
    server_version = "astock-watch/1.0"

    def log_message(self, fmt, *args):
        if os.environ.get("ASTOCK_VERBOSE"):
            sys.__stderr__.write("[web] %s\n" % (fmt % args))

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, obj)

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        if path.startswith("/reports/"):
            return self._report_file(path[len("/reports/"):])
        fn = GET_ROUTES.get(path)
        if fn:
            try:
                return self._json(fn(q))
            except Exception as exc:                  # noqa: BLE001
                return self._json({"error": str(exc),
                                   "trace": traceback.format_exc()[-800:]}, 500)
        return self._json({"error": "not found"}, 404)

    def _static(self, name):
        p = safe_child(WEB_DIR, name)
        if not p:
            return self._json({"error": "asset not found"}, 404)
        ctype = ("text/html; charset=utf-8" if name.endswith(".html") else
                 "text/css; charset=utf-8" if name.endswith(".css") else
                 "application/javascript; charset=utf-8" if name.endswith(".js") else
                 "application/octet-stream")
        with open(p, "rb") as f:
            data = f.read()
        self._send(200, data, ctype)

    def _report_file(self, rel):
        """只允许访问 data/reports 下的文件（防目录穿越）。"""
        rel = rel.replace("\\", "/")
        if ".." in rel or rel.startswith("/"):
            return self._json({"error": "bad path"}, 400)
        root = os.path.join(BASE, "data", "reports")
        p = os.path.abspath(os.path.join(root, rel))
        if not p.startswith(os.path.abspath(root)) or not os.path.isfile(p):
            return self._json({"error": "not found"}, 404)
        ctype = ("text/html; charset=utf-8" if p.endswith(".html") else
                 "image/svg+xml" if p.endswith(".svg") else
                 "text/markdown; charset=utf-8" if p.endswith(".md") else
                 "text/plain; charset=utf-8")
        with open(p, "rb") as f:
            data = f.read()
        self._send(200, data, ctype)

    def do_POST(self):
        u = urlparse(self.path)
        fn = POST_ROUTES.get(u.path)
        if not fn:
            return self._json({"error": "not found"}, 404)
        body = self._read_body()
        try:
            res = fn(body)
            if isinstance(res, tuple):
                data, err = res
                if err:
                    return self._json({"error": err}, 409)
                return self._json(data or {"ok": True})
            return self._json(res)
        except Exception as exc:                      # noqa: BLE001
            return self._json({"error": str(exc),
                               "trace": traceback.format_exc()[-800:]}, 500)


def _port_in_use(host, port):
    """探测端口上是否已有服务在响应（Windows 下 allow_reuse_address 会导致静默共存）。"""
    import socket
    target = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.8)
    try:
        if s.connect_ex((target, port)) == 0:
            try:                                  # 看它是不是本项目的服务
                s2 = socket.create_connection((target, port), timeout=0.8)
                s2.sendall(b"GET /api/jobs HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
                data = s2.recv(200).decode("utf-8", "replace")
                s2.close()
                return True, "astock-watch" if "\"items\"" in data or "200" in data else "其他服务"
            except Exception:
                return True, "未知服务"
    except Exception:
        pass
    finally:
        s.close()
    return False, ""


def serve(host="127.0.0.1", port=8770, scheduler=True, open_browser=False):
    global SCHEDULER
    install_log_router()
    memory.ensure_dirs()
    try:                                   # 样本库增量同步进 SQLite
        from . import hypdb
        hypdb.sync(verbose=True)
        hypdb.purge_older_than_days(730)
    except Exception as exc:
        print("  （样本库同步失败：%s）" % exc)
    try:
        backup.make_backup("server-start")
    except Exception:
        pass
    os.makedirs(REPORT_DIR, exist_ok=True)
    busy, who = _port_in_use(host, port)
    if busy:
        print("")
        print("!" * 66)
        print("  端口 %d 已被占用（%s）——很可能是一个**旧的 astock-watch 还在跑**。" % (port, who))
        print("  Windows 下两个进程可以静默共用端口，新代码不会生效，页面就会报接口 404。")
        print("")
        print("  请先关掉旧的服务窗口，或执行：")
        print("      netstat -ano | findstr :%d        # 查 PID" % port)
        print("      taskkill /PID <上面的PID> /F      # 结束它")
        print("  然后重新启动；也可以直接换端口：python main.py serve --port 8780")
        print("!" * 66)
        raise SystemExit(3)
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        print("")
        print("启动失败：端口 %d 无法监听（%s）" % (port, exc))
        print("可能已被另一个 astock-watch 占用。换个端口试试：")
        print("    python main.py serve --port 8780")
        print("或在 Windows 上查看/结束占用进程：")
        print("    netstat -ano | findstr :%d" % port)
        print("    taskkill /PID <上面查到的PID> /F")
        raise
    if scheduler and SCHEDULER is None:
        SCHEDULER = Scheduler()
        SCHEDULER.start()
    url = "http://%s:%d" % ("127.0.0.1" if host == "0.0.0.0" else host, port)
    print("=" * 66)
    print("  astock-watch Web 控制台已启动")
    print("  地址：%s" % url)
    print("  调度：%s" % ("开启（开盘执行计划单 / 盘中定时决策 / 收盘复盘与选股）"
                        if scheduler else "关闭"))
    print("  停止：Ctrl+C")
    print("=" * 66)
    if open_browser:
        import webbrowser

        def _open():
            try:
                webbrowser.open(url)
            except Exception:
                pass
        threading.Timer(1.2, _open).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
    finally:
        httpd.server_close()
