# -*- coding: utf-8 -*-
"""磁盘缓存（SQLite）：把"算过一次就不想再算"的结果存盘，避免堆在内存里。

为什么：
    signals.build_report / 持仓分析 / 规则统计都很重，之前用全局 dict 缓存，
    内存随请求增长 → 容器 1GB 上限被 OOM 杀掉（502）。
    改为 SQLite 后内存只有连接句柄，数据全在磁盘，重启也不丢。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "data", "ai", "cache.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv(
  key TEXT PRIMARY KEY,
  ts REAL NOT NULL,
  val TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS i_ts ON kv(ts);
"""


def _conn():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB, timeout=10)
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=5000")
    except Exception:
        pass
    c.executescript(SCHEMA)
    return c


def get(key, ttl=None):
    """取缓存；ttl 秒内有效，过期或不存在返回 None。"""
    try:
        with closing(_conn()) as c:
            row = c.execute("SELECT ts, val FROM kv WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        ts, val = row
        if ttl is not None and (time.time() - ts) > float(ttl):
            return None
        return json.loads(val)
    except Exception:
        return None


def put(key, obj):
    try:
        with closing(_conn()) as c:
            c.execute("INSERT OR REPLACE INTO kv(key, ts, val) VALUES(?,?,?)",
                      (key, time.time(), json.dumps(obj, ensure_ascii=False, default=str)))
            c.commit()
        return True
    except Exception:
        return False


def purge(older_than=86400, verbose=False):
    """清掉超过 N 秒的缓存（默认 1 天），控制磁盘体积。"""
    try:
        with closing(_conn()) as c:
            cur = c.execute("DELETE FROM kv WHERE ts < ?", (time.time() - older_than,))
            n = cur.rowcount
            c.commit()
        if verbose and n:
            print("  磁盘缓存清理：%d 条" % n)
        return n
    except Exception:
        return 0


def size_mb():
    try:
        return round(os.path.getsize(DB) / 1048576.0, 2)
    except OSError:
        return 0.0
