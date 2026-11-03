# -*- coding: utf-8 -*-
"""数据备份与恢复：账户、成交、AI 记忆（手册/日记/教训/决策）一律不丢。

设计：
    · 备份是**全量 zip**（data/ 整个目录，排除 backups 自身），拿到就能换机器继续跑；
    · 保留最近 N 份，自动轮转；
    · 关键动作前自动备份（启动服务、每次 AI 阶段结束、重置模拟盘之前）；
    · 恢复前会先给当前数据再做一份备份，避免"恢复即毁灭"。
"""
from __future__ import annotations

import json
import os
import shutil
import time
import zipfile
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE, "data")
BACKUP_DIR = os.path.join(BASE, "backups")
KEEP = 30
# 明确"必须保住"的核心文件（用于校验）
CRITICAL = [
    "ai/paper_state.json",      # 账户与成交
    "ai/playbook.md",           # AI 策略手册（风格）
    "ai/lessons.json",          # 教训库
    "ai/watchlist.json",        # 观察池
    "ai/plans/pending_plans.json",  # 待执行计划单
    "holdings.json",            # 我的真实持仓（资金量/成本）
    "ai/hypotheses.jsonl",      # AI 每次判断的假设与回填结果（经验引擎燃料）
    "ai/rules.json",            # 可证伪经验规则库（生效/候选/淘汰）
]


def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)


def _safe_name(reason):
    r = "".join(ch for ch in (reason or "auto") if ch.isalnum() or ch in "-_")[:20]
    return r or "auto"


def make_backup(reason="auto", keep=KEEP, verbose=False):
    """把 data/ 打包成 backups/astock-data-<时间>-<原因>.zip，返回路径。"""
    ensure_dirs()
    if not os.path.isdir(DATA_DIR):
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(BACKUP_DIR, "astock-data-%s-%s.zip" % (stamp, _safe_name(reason)))
    n_files = 0
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for root, dirs, files in os.walk(DATA_DIR):
                dirs[:] = [d for d in dirs if d not in ("__pycache__",)]
                for fn in files:
                    if fn.endswith((".pyc", ".lock")):
                        continue
                    full = os.path.join(root, fn)
                    rel = os.path.relpath(full, DATA_DIR).replace("\\", "/")
                    if rel.startswith("backups/"):
                        continue
                    try:
                        z.write(full, "data/" + rel)
                        n_files += 1
                    except OSError:
                        continue
    except Exception as exc:                        # noqa: BLE001
        if verbose:
            print("备份失败：%s" % exc)
        return None
    # 轮转
    try:
        items = sorted([f for f in os.listdir(BACKUP_DIR) if f.startswith("astock-data-")])
        for old in items[:-keep]:
            os.remove(os.path.join(BACKUP_DIR, old))
    except Exception:
        pass
    if verbose:
        print("已备份 %d 个文件 → %s" % (n_files, path))
    return path


def list_backups():
    ensure_dirs()
    out = []
    for fn in sorted(os.listdir(BACKUP_DIR), reverse=True):
        if not fn.startswith("astock-data-") or not fn.endswith(".zip"):
            continue
        p = os.path.join(BACKUP_DIR, fn)
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append({"name": fn, "size": st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")})
    return out


def verify_backup(name, verbose=False):
    """检查备份里是否含核心文件，返回 (ok, 缺失列表)。"""
    p = os.path.join(BACKUP_DIR, os.path.basename(name))
    if not os.path.isfile(p):
        return False, ["备份文件不存在"]
    try:
        with zipfile.ZipFile(p) as z:
            names = set(z.namelist())
    except Exception as exc:                        # noqa: BLE001
        return False, ["无法读取备份：%s" % exc]
    missing = [c for c in CRITICAL if ("data/" + c) not in names]
    # 核心文件可以缺（例如还没交易时没有 paper_state），但至少要有手册或日记之一
    has_memory = any(n.startswith("data/ai/") for n in names)
    if verbose:
        print("备份内容：%d 项｜核心文件缺失：%s" % (len(names), missing or "无"))
    return (has_memory and len(missing) < len(CRITICAL)), missing


def restore_backup(name, verbose=True):
    """恢复：先把当前数据再备份一次，然后解压覆盖。"""
    p = os.path.join(BACKUP_DIR, os.path.basename(name))
    if not os.path.isfile(p):
        return False, "备份不存在：%s" % name
    pre = make_backup("before-restore")
    if verbose and pre:
        print("已为当前数据留下快照：%s" % os.path.basename(pre))
    try:
        with zipfile.ZipFile(p) as z:
            for member in z.namelist():
                if not member.startswith("data/"):
                    continue
                rel = member[len("data/"):]
                if not rel:
                    continue
                target = os.path.join(DATA_DIR, rel.replace("/", os.sep))
                if member.endswith("/"):
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        return True, "已恢复：%s" % os.path.basename(p)
    except Exception as exc:                        # noqa: BLE001
        return False, "恢复失败：%s" % exc


def data_health():
    """数据体检：核心文件是否存在、大小、最近修改时间。"""
    items = []
    for rel in CRITICAL + ["ai/journal", "ai/decisions"]:
        p = os.path.join(DATA_DIR, rel.replace("/", os.sep))
        if os.path.isdir(p):
            files = [f for f in os.listdir(p) if f.endswith((".md", ".jsonl"))]
            items.append({"path": rel, "exists": True, "kind": "dir",
                          "files": len(files),
                          "mtime": datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M")})
        elif os.path.isfile(p):
            st = os.stat(p)
            items.append({"path": rel, "exists": True, "kind": "file", "size": st.st_size,
                          "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")})
        else:
            items.append({"path": rel, "exists": False, "kind": "-", "mtime": "-"})
    bks = list_backups()
    return {"items": items, "backups": bks[:10],
            "backup_count": len(bks),
            "latest_backup": bks[0] if bks else None}


if __name__ == "__main__":
    print(json.dumps(data_health(), ensure_ascii=False, indent=1))
