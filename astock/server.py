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

from . import ai_agent, feed, memory, monitor, paper, screener
from .ai_agent import AI_DIR, REPORT_DIR, STATE_PATH, WATCHLIST_PATH

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
CONFIG_PATH = os.path.join(BASE, "config.json")

_QUOTE_CACHE = {"ts": 0.0, "data": {}}
_CAND_CACHE = {"ts": 0.0, "data": None, "running": False}

DEFAULT_SCHEDULE = {"enabled": True, "decide_every_min": 30,
                    "pick_at": "15:12", "review_at": "15:05"}

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
    elif kind == "review":
        trader.phase_review()
    elif kind == "execute":
        trader.phase_execute_plans()
    elif kind == "auto":
        trader.run_once("auto")
    else:
        raise ValueError("未知任务 %s" % kind)
    snap = trader._snapshot()
    return {"equity": snap["equity"], "cash": snap["cash"],
            "positions": len(snap["positions"]),
            "tokens": trader.llm.usage}


def scan_candidates(params):
    """后台跑选股初筛（全市场快照 + 60 只 K 线），结果缓存给前端。"""
    global _CAND_CACHE
    try:
        feats = screener.screen((load_config().get("ai") or {}).get("screen"))
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
    if kind not in ("pick", "decide", "review", "auto", "scan", "execute"):
        return None, "未知任务类型"
    if kind == "scan":
        return api_scan(body)
    jid, err = start_job(kind, run_phase, {"kind": kind})
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
    "/api/job": api_job,
    "/api/jobs": api_jobs,
}

POST_ROUTES = {
    "/api/watchlist": api_watchlist_set,
    "/api/config": api_config_set,
    "/api/run": api_run,
    "/api/reset": api_reset,
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


def serve(host="127.0.0.1", port=8770, scheduler=True, open_browser=False):
    global SCHEDULER
    install_log_router()
    memory.ensure_dirs()
    os.makedirs(REPORT_DIR, exist_ok=True)
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
