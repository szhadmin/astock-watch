# -*- coding: utf-8 -*-
"""落盘：告警日志、行情快照、收盘报告。目录默认 ./data。"""
from __future__ import annotations

import csv
import os
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE, "data")


def ensure_dirs():
    for sub in ("", "logs", "snapshots", "reports"):
        os.makedirs(os.path.join(DATA_DIR, sub), exist_ok=True)


def append_alert(level, code, name, message):
    ensure_dirs()
    day = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(DATA_DIR, "logs", "alerts_%s.log" % day)
    line = "%s\t%s\t%s\t%s\t%s\n" % (
        datetime.now().strftime("%H:%M:%S"), level, code, name, message)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
    return path


def append_snapshot(report):
    """每次分析把关键指标追加到当日 CSV，便于事后复盘。"""
    ensure_dirs()
    day = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(DATA_DIR, "snapshots", "quotes_%s.csv" % day)
    new = not os.path.exists(path)
    row = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "code": report["code"], "name": report["name"],
        "price": report["price"], "pct": report["pct"],
        "score": report["scores"]["total"],
        "trend": report["scores"]["trend"], "position": report["scores"]["position"],
        "momentum": report["scores"]["momentum"], "volume": report["scores"]["volume"],
        "capital": report["scores"]["capital"],
        "rsi6": report["extra"]["rsi6"],
        "ma5": report["levels"]["ma"]["5"], "ma10": report["levels"]["ma"]["10"],
        "ma20": report["levels"]["ma"]["20"],
        "main_flow_today": round(report["extra"]["flow"]["today"] / 1e4, 0),
        "verdict": report["verdict"]["level"],
    }
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if new:
            w.writeheader()
        w.writerow(row)
    return path


def write_report_md(content, tag="daily"):
    ensure_dirs()
    stamp = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(DATA_DIR, "reports", "%s_%s.md" % (tag, stamp))
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def latest_alerts(n=20):
    ensure_dirs()
    day = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(DATA_DIR, "logs", "alerts_%s.log" % day)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return f.readlines()[-n:]
