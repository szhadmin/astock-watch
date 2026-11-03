# -*- coding: utf-8 -*-
"""输出与通知：控制台配色、蜂鸣、Webhook 推送、日志落盘。"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from urllib.request import Request, urlopen

try:                                    # Windows 蜂鸣
    import winsound
except Exception:                       # pragma: no cover
    winsound = None

# Windows 终端 ANSI 支持
if os.name == "nt":
    os.system("")

C = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m",
    "grey": "\033[90m",
}
_ENABLE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def color(text, name):
    if not _ENABLE_COLOR:
        return text
    return C.get(name, "") + str(text) + C["reset"]


def set_color(enable: bool):
    global _ENABLE_COLOR
    _ENABLE_COLOR = enable


def pct_color(v):
    if v is None:
        return "grey"
    return "red" if v > 0 else ("green" if v < 0 else "grey")   # A 股：红涨绿跌


def beep(kind="info"):
    """Windows 下蜂鸣提示（其它平台静默）。"""
    if winsound is None:
        sys.stdout.write("\a")
        sys.stdout.flush()
        return
    try:
        if kind == "action":
            for f in (880, 1180, 1480):
                winsound.Beep(f, 120)
        elif kind == "warn":
            winsound.Beep(1000, 250)
        else:
            winsound.Beep(660, 120)
    except Exception:
        pass


def push_webhook(cfg_notify, title, message):
    """企业微信 / 钉钉 / 自定义 Webhook 推送。失败不抛异常，只打印。"""
    hook = (cfg_notify or {}).get("webhook") or {}
    url = hook.get("url")
    if not url:
        return False
    kind = (hook.get("type") or "wecom").lower()
    text = "%s\n%s" % (title, message)
    if kind == "dingtalk":
        payload = {"msgtype": "text", "text": {"content": text}}
    elif kind == "custom":
        payload = {"title": title, "message": message,
                   "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    else:  # wecom 企业微信群机器人
        payload = {"msgtype": "text", "text": {"content": text}}
    try:
        req = Request(url, data=json.dumps(payload).encode("utf-8"),
                      headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=8) as resp:
            resp.read()
        return True
    except Exception as exc:            # noqa: BLE001
        print(color("  [推送失败] %s" % exc, "grey"))
        return False


def banner(text, width=72, ch="="):
    line = ch * width
    print(color(line, "grey"))
    print(color(text, "bold"))
    print(color(line, "grey"))
