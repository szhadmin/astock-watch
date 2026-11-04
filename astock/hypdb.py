# -*- coding: utf-8 -*-
"""样本库落到磁盘（SQLite）：统计全走 SQL 聚合，内存占用恒定。

为什么：
    之前把 jsonl 全量读进内存做统计，14 万条样本（99MB）会膨胀到 1GB+，
    容器 1GB 上限直接被 OOM 杀掉（502 就是这么来的）。
    改成 SQLite 后：数据在磁盘，聚合在数据库里算，进程内存只跟"结果集"大小有关。

用法：
    python -m astock.hypdb --sync        # 把 jsonl 增量导入数据库
    python -m astock.hypdb --stats       # 打印统计与库体积
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AI_DIR = os.path.join(BASE, "data", "ai")
DB = os.path.join(AI_DIR, "hyp.db")
JSONL = [os.path.join(AI_DIR, "hypotheses.jsonl"),
         os.path.join(AI_DIR, "hypotheses_replay.jsonl")]

SCHEMA = """
CREATE TABLE IF NOT EXISTS hyps(
  id TEXT PRIMARY KEY,
  date TEXT, code TEXT, direction TEXT, source TEXT,
  temp REAL, score REAL, score_chg REAL, vol_ratio REAL, ma20_dev REAL,
  idx20 REAL, regime TEXT, sector_pct REAL,
  horizon INTEGER, ref_price REAL, invalidate REAL,
  hit INTEGER, end_ret REAL, excess REAL,
  ret5 REAL, ret10 REAL, ret20 REAL, exc5 REAL, exc10 REAL, exc20 REAL,
  reason TEXT
);
CREATE INDEX IF NOT EXISTS i_date ON hyps(date);
CREATE INDEX IF NOT EXISTS i_dir ON hyps(direction);
CREATE INDEX IF NOT EXISTS i_src ON hyps(source);
CREATE INDEX IF NOT EXISTS i_temp ON hyps(temp);
CREATE INDEX IF NOT EXISTS i_score ON hyps(score);
CREATE INDEX IF NOT EXISTS i_regime ON hyps(regime);
CREATE INDEX IF NOT EXISTS i_code ON hyps(code);
"""


def connect():
    os.makedirs(AI_DIR, exist_ok=True)
    conn = sqlite3.connect(DB, timeout=15)
    conn.executescript(SCHEMA)
    return conn


def _row(d):
    ctx = d.get("ctx") or {}
    res = d.get("result") or {}
    rets = res.get("rets") or {}
    excs = res.get("excess_map") or {}

    def f(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    return (
        d.get("id") or "%s_%s_%s" % (d.get("code"), d.get("date"), d.get("direction")),
        d.get("date"), d.get("code"), d.get("direction") or "",
        d.get("source") or "decide",
        f(ctx.get("temp")), f(ctx.get("score")), f(ctx.get("score_chg")),
        f(ctx.get("vol_ratio")), f(ctx.get("ma20_dev")), f(ctx.get("idx20")),
        ctx.get("regime"), f(ctx.get("sector_pct")),
        d.get("horizon"), f(d.get("ref_price")), f(d.get("invalidate")),
        1 if res.get("hit") else 0 if res else None,
        f(res.get("end_ret")), f(res.get("excess")),
        f(rets.get("h5")), f(rets.get("h10")), f(rets.get("h20")),
        f(excs.get("h5")), f(excs.get("h10")), f(excs.get("h20")),
        (d.get("reason") or "")[:120],
    )


def sync(verbose=False):
    """把 jsonl 增量导入 SQLite（按 id 去重），返回新增条数。"""
    n_new = 0
    with closing(connect()) as conn:
        before = conn.execute("SELECT COUNT(*) FROM hyps").fetchone()[0]
        for path in JSONL:
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8-sig") as f:
                batch = []
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    batch.append(_row(d))
                    if len(batch) >= 2000:
                        conn.executemany(
                            "INSERT OR IGNORE INTO hyps VALUES (%s)"
                            % ",".join("?" * 26), batch)
                        batch = []
                if batch:
                    conn.executemany(
                        "INSERT OR IGNORE INTO hyps VALUES (%s)" % ",".join("?" * 26),
                        batch)
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM hyps").fetchone()[0]
        n_new = after - before
    if verbose:
        print("  SQLite 同步：+%d 条，共 %d 条｜库体积 %.1f MB"
              % (n_new, after, os.path.getsize(DB) / 1048576 if os.path.exists(DB) else 0))
    return n_new


# --------------------------------------------------------------------------- #
# 统计（全部走 SQL，内存恒定）
# --------------------------------------------------------------------------- #
def _bucket_case(field, edges):
    """生成 CASE WHEN 分档表达式。edges 形如 [(0,40),(40,50),...]"""
    whens = " ".join("WHEN {f} >= {lo} AND {f} < {hi} THEN '{lo}~{hi}'".format(
        f=field, lo=lo, hi=hi) for lo, hi in edges)
    return "CASE %s ELSE NULL END" % whens


def buckets(field, edges, value_col="excess"):
    """按某字段分档统计：返回 [{档,n,超额%,命中率%}]（超额为空时用 end_ret）。"""
    case = _bucket_case(field, edges)
    sql = """
    SELECT %s AS b,
           COUNT(*) n,
           AVG(COALESCE(%s, end_ret)) v,
           AVG(hit)*100 h
    FROM hyps
    WHERE %s IS NOT NULL AND result_ok
    GROUP BY b ORDER BY b
    """ % (case, value_col, field)
    sql = sql.replace("result_ok", "hit IS NOT NULL")
    rows = _q(sql)
    return [{"档": r[0], "n": r[1], "超额%": round(r[2], 2) if r[2] is not None else None,
             "命中率%": round(r[3], 1) if r[3] is not None else None} for r in rows]


def _q(sql, args=()):
    with closing(connect()) as conn:
        return conn.execute(sql, args).fetchall()


def append(records):
    """把一批样本直接写入 SQLite（回放边算边写，进程不攒内存）。"""
    rows = [_row(d) for d in records]
    if not rows:
        return 0
    with closing(connect()) as conn:
        conn.executemany("INSERT OR IGNORE INTO hyps VALUES (%s)"
                         % ",".join("?" * 26), rows)
        conn.commit()
    return len(rows)


def has_data():
    """库里是否已有可用样本（供各接口决定是否走 SQL 路径）。"""
    try:
        return _q("SELECT COUNT(*) FROM hyps WHERE hit IS NOT NULL")[0][0] > 0
    except Exception:
        return False


def overview():
    rows = _q("""SELECT direction, source, COUNT(*) FROM hyps GROUP BY direction, source""")
    tot = _q("SELECT COUNT(*) FROM hyps WHERE hit IS NOT NULL")[0][0]
    return {"rows": sum(r[2] for r in rows), "closed": tot,
            "by_dir_src": [{"direction": r[0], "source": r[1], "n": r[2]} for r in rows]}


def direction_stats():
    rows = _q("""SELECT direction, COUNT(*), AVG(hit)*100,
                        AVG(COALESCE(excess,end_ret)), COUNT(DISTINCT code)
                 FROM hyps WHERE hit IS NOT NULL GROUP BY direction""")
    name = {"bullish": "看涨", "bearish": "看跌", "hold": "持有"}
    return [{"方向": name.get(r[0], r[0]), "n": r[1],
             "命中率%": round(r[2], 1) if r[2] is not None else None,
             "超额%": round(r[3], 2) if r[3] is not None else None,
             "股票数": r[4]} for r in rows]


def regime_stats():
    rows = _q("""SELECT regime, COUNT(*), AVG(COALESCE(excess,end_ret)), AVG(hit)*100
                 FROM hyps WHERE hit IS NOT NULL AND regime IS NOT NULL
                 GROUP BY regime ORDER BY 3 DESC""")
    return [{"状态细分": r[0], "n": r[1],
             "超额%": round(r[2], 2) if r[2] is not None else None,
             "命中率%": round(r[3], 1) if r[3] is not None else None} for r in rows]


def horizon_stats():
    out = []
    for h in ("5", "10", "20"):
        rows = _q("""SELECT COUNT(*), AVG(ret%s), AVG(exc%s), AVG(COALESCE(exc%s, ret%s))
                     FROM hyps WHERE ret%s IS NOT NULL""" % (h, h, h, h, h))
        if rows and rows[0][0]:
            n, raw, exc, fallback = rows[0]
            out.append({"期限": "H%s" % h, "n": n,
                        "原始%": round(raw, 2) if raw is not None else None,
                        "超额%": round(exc if exc is not None else fallback, 2)})
    return out


def state_stats():
    """按当时指数 20 日涨跌分熊/震荡/牛。"""
    sql = """SELECT CASE WHEN idx20 < -5 THEN '熊市'
                          WHEN idx20 > 5 THEN '牛市' ELSE '震荡' END AS s,
                    COUNT(*), AVG(COALESCE(excess,end_ret)), AVG(hit)*100
             FROM hyps WHERE hit IS NOT NULL AND idx20 IS NOT NULL GROUP BY s"""
    return [{"状态": r[0], "n": r[1],
             "超额%": round(r[2], 2) if r[2] is not None else None,
             "命中率%": round(r[3], 1) if r[3] is not None else None} for r in _q(sql)]


def rule_stats(cond, direction=None, forbid=False):
    """给一条规则（cond={field,op,value}）算样本/命中/期望 —— 直接用 SQL 过滤。"""
    fields = {"temp": "temp", "score": "score", "sector_pct": "sector_pct",
              "score_chg": "score_chg", "vol_ratio": "vol_ratio", "ma20_dev": "ma20_dev"}
    col = fields.get((cond or {}).get("field"))
    if not col:
        return None
    op = "<" if cond.get("op") == "<" else ">"
    where = ["hit IS NOT NULL", "%s %s ?" % (col, op)]
    args = [float(cond.get("value"))]
    if direction:
        where.append("direction = ?")
        args.append(direction)
    sql = ("SELECT COUNT(*), AVG(hit)*100, AVG(COALESCE(excess,end_ret)),"
           " COUNT(DISTINCT code), SUM(CASE WHEN source='decide' THEN 1 ELSE 0 END)"
           " FROM hyps WHERE " + " AND ".join(where))
    r = _q(sql, args)[0]
    if not r[0]:
        return None
    n, hit, avg, stocks, real = r
    sign = -1.0 if forbid else 1.0
    hit_eff = (100 - (hit or 0)) if forbid else (hit or 0)
    ret_eff = (avg or 0) * sign
    return {"n": n, "hit": round(hit_eff, 1), "avg": round(ret_eff, 2),
            "net": round(ret_eff - 0.3, 2), "stocks": stocks, "real": real or 0}


def purge_older_than_days(days=730, verbose=False):
    """只保留最近 N 天的样本（默认 2 年），返回清理条数。"""
    with closing(connect()) as conn:
        cur = conn.execute("DELETE FROM hyps WHERE date IS NOT NULL AND date < date('now', ?)",
                           ("-%d days" % days,))
        n = cur.rowcount
        conn.commit()
    if verbose:
        print("  SQLite 保留策略：清除 %d 条早于 %d 天的样本" % (n, days))
    return n


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if "--sync" in sys.argv:
        sync(verbose=True)
    print(json.dumps(overview(), ensure_ascii=False, indent=1))
    print("方向：", json.dumps(direction_stats(), ensure_ascii=False))
    print("状态细分：", json.dumps(regime_stats(), ensure_ascii=False))
    print("持有期：", json.dumps(horizon_stats(), ensure_ascii=False))
    print("市场状态：", json.dumps(state_stats(), ensure_ascii=False))
    print("温度分档：", json.dumps(buckets("temp", [(0, 25), (25, 40), (40, 60), (60, 75), (75, 101)]),
                                   ensure_ascii=False))
    print("评分分档：", json.dumps(buckets("score", [(0, 40), (40, 50), (50, 60), (60, 70), (70, 101)]),
                                   ensure_ascii=False))
