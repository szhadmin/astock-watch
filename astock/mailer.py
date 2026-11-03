# -*- coding: utf-8 -*-
"""邮件通知：成交（买/卖）、每日复盘、次日计划、我的持仓操作建议。

配置位置：config.json → notify.email
    {
      "enabled": true,
      "smtp_host": "smtp.qq.com",
      "smtp_port": 465,
      "use_ssl": true,            // 465 用 SSL；587 改 false 走 STARTTLS
      "user": "you@qq.com",
      "password": "授权码",        // QQ/163 要用"授权码"，不是登录密码
      "to": "you@qq.com",
      "from_name": "astock-watch",
      "events": {"trade": true, "review": true, "plan": true}
    }

设计：全部静默失败——邮件发不出去绝不能影响交易主流程；发送走后台线程不阻塞。
"""
from __future__ import annotations

import io
import json
import os
import smtplib
import ssl
import threading
from datetime import datetime
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE, "config.json")
LOG_PATH = os.path.join(BASE, "data", "notify_log.jsonl")

DEFAULT = {
    "enabled": False, "smtp_host": "", "smtp_port": 465, "use_ssl": True,
    "user": "", "password": "", "to": "", "from_name": "astock-watch",
    "events": {"trade": True, "review": True, "plan": True},
}

CSS = """
body{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:#f6f8fb;
     margin:0;padding:22px;color:#1f2937}
.wrap{max-width:680px;margin:0 auto;background:#fff;border-radius:12px;padding:22px 24px;
      box-shadow:0 2px 12px rgba(15,23,42,.08)}
h2{margin:0 0 4px;font-size:19px}
.sub{color:#6b7280;font-size:12px;margin-bottom:14px}
h3{font-size:14px;margin:18px 0 6px;color:#374151;border-left:3px solid #2563eb;padding-left:8px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:7px 9px;border-bottom:1px solid #eef2f7;text-align:right}
th:first-child,td:first-child{text-align:left}
th{color:#6b7280;font-weight:500;font-size:12px;background:#fafbfd}
.up{color:#dc2626;font-weight:600}.down{color:#059669;font-weight:600}
.badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;
       background:#eef2ff;color:#3730a3;margin-right:6px}
.buy{background:#fee2e2;color:#b91c1c}.sell{background:#d1fae5;color:#065f46}
.warn{background:#fef3c7;color:#92400e}
.foot{margin-top:22px;padding-top:12px;border-top:1px solid #eef2f7;color:#9ca3af;font-size:11px}
pre{background:#f8fafc;border:1px solid #eef2f7;border-radius:8px;padding:10px;font-size:12px;
    white-space:pre-wrap;word-break:break-word;color:#334155;max-height:420px;overflow:auto}
"""


def conf(cfg=None):
    c = dict(DEFAULT)
    try:
        if cfg is None:
            with io.open(CONFIG_PATH, encoding="utf-8-sig") as f:
                cfg = json.load(f)
        e = ((cfg.get("notify") or {}).get("email") or {})
        c.update({k: v for k, v in e.items() if not k.startswith("_")})
        c["events"] = {**DEFAULT["events"], **(e.get("events") or {})}
        c["smtp_port"] = int(c.get("smtp_port") or 465)
    except Exception:
        pass
    return c


def available(cfg=None):
    c = conf(cfg)
    missing = [k for k in ("smtp_host", "user", "password", "to") if not c.get(k)]
    if not c.get("enabled"):
        return False, "邮件通知未启用（config.json → notify.email.enabled）"
    if missing:
        return False, "邮件配置不完整：缺少 " + "、".join(missing)
    return True, "ok"


def _log(entry):
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        entry["ts"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with io.open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _build_msg(subject, html, c, attachments=None):
    """构造邮件：无附件用简单 HTML；有附件用 multipart（正文 + 附件）。"""
    if not attachments:
        msg = MIMEText(html, "html", "utf-8")
    else:
        msg = MIMEMultipart()
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(html, "html", "utf-8"))
        msg.attach(alt)
        for path in attachments:
            try:
                if not path or not os.path.isfile(path):
                    continue
                with open(path, "rb") as f:
                    raw = f.read()
                name = os.path.basename(path)
                if name.lower().endswith((".html", ".htm")):
                    part = MIMEApplication(raw, _subtype="html")
                elif name.lower().endswith(".md"):
                    part = MIMEApplication(raw, _subtype="octet-stream")
                else:
                    part = MIMEApplication(raw)
                part.add_header("Content-Disposition", "attachment", filename=name)
                msg.attach(part)
            except Exception:
                continue
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header(c.get("from_name") or "astock-watch", "utf-8")),
                              c["user"]))
    msg["To"] = c["to"]
    return msg


def _send_sync(subject, html, text, c, attachments=None):
    msg = _build_msg(subject, html, c, attachments)
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header(c.get("from_name") or "astock-watch", "utf-8")),
                              c["user"]))
    msg["To"] = c["to"]
    host, port = c["smtp_host"], int(c["smtp_port"])
    if c.get("use_ssl", True):
        ctx = ssl.create_default_context()
        srv = smtplib.SMTP_SSL(host, port, timeout=20, context=ctx)
    else:
        srv = smtplib.SMTP(host, port, timeout=20)
        try:
            srv.starttls(context=ssl.create_default_context())
        except Exception:
            pass
    try:
        srv.login(c["user"], c["password"])
        srv.sendmail(c["user"], [x.strip() for x in str(c["to"]).replace(";", ",").split(",")
                                if x.strip()], msg.as_string())
    finally:
        try:
            srv.quit()
        except Exception:
            pass


def send(subject, html, text=None, cfg=None, block=False, attachments=None):
    """发一封 HTML 邮件。失败只记日志，不抛异常。"""
    ok, why = available(cfg)
    if not ok:
        _log({"event": "skip", "subject": subject, "reason": why})
        return False, why
    c = conf(cfg)

    def _run():
        try:
            _send_sync(subject, html, text, c, attachments)
            _log({"event": "sent", "subject": subject, "to": c["to"],
                  "attachments": [os.path.basename(a) for a in (attachments or [])]})
            print("  ✉ 邮件已发送：%s → %s" % (subject, c["to"]))
        except Exception as exc:                       # noqa: BLE001
            _log({"event": "fail", "subject": subject, "error": str(exc)[:300]})
            print("  ✉ 邮件发送失败（不影响交易）：%s" % str(exc)[:120])

    if block:
        _run()
    else:
        threading.Thread(target=_run, daemon=True).start()
    return True, "已提交"


def _wrap(title, sub, body, foot=""):
    return ("<!doctype html><meta charset='utf-8'><style>%s</style><div class='wrap'>"
            "<h2>%s</h2><div class='sub'>%s</div>%s"
            "<div class='foot'>%s<br>本邮件由 astock-watch 自动发送，仅供参考，不构成投资建议。</div>"
            "</div>" % (CSS, title, sub, body, foot or datetime.now().strftime("%Y-%m-%d %H:%M")))


def send_trade(side, code, name, shares, price, amount, reason, account=None, cfg=None):
    """成交通知（买入/卖出）。"""
    ev = conf(cfg)["events"]
    if not ev.get("trade", True):
        return False, "事件已关闭"
    is_buy = str(side).upper() == "BUY"
    rows = "".join(
        "<tr><td>%s</td><td>%s %s</td><td>%s</td><td>%s</td></tr>"
        % (k, v, "", "", "") for k, v in [])            # 占位保持结构清晰
    acct = ""
    if account:
        acct = ("<h3>账户</h3><table>"
                "<tr><th>权益</th><td>%.2f</td><th>现金</th><td>%.2f</td></tr>"
                "<tr><th>持仓市值</th><td>%.2f</td><th>累计收益</th><td class='%s'>%+.2f%%</td></tr>"
                "</table>" % (account.get("equity", 0), account.get("cash", 0),
                              account.get("market_value", 0),
                              "up" if account.get("total_return_pct", 0) >= 0 else "down",
                              account.get("total_return_pct", 0)))
    body = ("<div><span class='badge %s'>%s</span><span class='badge'>%s %s</span></div>"
            "<h3>成交明细</h3><table>"
            "<tr><th>代码</th><th>方向</th><th>股数</th><th>成交价</th></tr>"
            "<tr><td>%s <b>%s</b></td><td class='%s'>%s</td><td>%d</td><td>%.3f</td></tr>"
            "<tr><th>成交金额</th><th>理由</th></tr>"
            "<tr><td>%.2f</td><td>%s</td></tr></table>%s"
            % ("buy" if is_buy else "sell", "买入" if is_buy else "卖出",
               code, name or "", code, name or "",
               "up" if is_buy else "down", "BUY" if is_buy else "SELL",
               shares, price, amount, reason or "-", acct))
    subject = "[astock] %s %s %s %d股 @%.3f" % ("买入" if is_buy else "卖出",
                                               code, name or "", shares, price)
    return send(subject, _wrap("%s %s" % ("买入成交" if is_buy else "卖出成交",
                                          name or code), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), body),
                cfg=cfg)


def send_digest(kind, journal="", plans=None, advices=None, account=None, temp=None,
                cfg=None, attachments=None, extra_html=""):
    """每日复盘 / 次日计划 / 持仓建议 汇总邮件。"""
    ev = conf(cfg)["events"]
    key = "plan" if kind in ("plan", "daily") else "review"
    if not ev.get(key, True):
        return False, "事件已关闭"
    titles = {"review": "收盘复盘", "plan": "次日交易计划", "daily": "每日报告"}
    title = titles.get(kind, "操盘日报")
    parts = []
    if temp:
        parts.append("<div><span class='badge'>大盘温度 %s（%s）</span>"
                     "<span class='badge warn'>%s</span></div>"
                     % (temp.get("temp"), temp.get("level"), (temp.get("note") or "")[:60]))
    if plans:
        rows = "".join(
            "<tr><td>%s</td><td class='%s'>%s</td><td>%d</td><td>%.3f</td><td>%s</td></tr>"
            % (("%s %s" % (p.get("code"), p.get("name") or "")).strip(),
               "up" if p.get("action") == "BUY" else "down",
               "买入" if p.get("action") == "BUY" else "卖出",
               int(p.get("shares") or 0), float(p.get("signal_price") or 0),
               (p.get("reason") or "")[:60]) for p in plans)
        parts.append("<h3>明日计划单（%d 条）</h3><table>"
                     "<tr><th>标的</th><th>方向</th><th>股数</th><th>信号价</th><th>理由</th></tr>"
                     "%s</table>" % (len(plans), rows))
    else:
        parts.append("<h3>明日计划单</h3><div class='sub'>无（AI 判断明日不动作，空仓也是一种决策）</div>")
    if advices:
        rows = "".join(
            "<tr><td>%s</td><td class='%s'>%s</td><td>%s</td><td>%s</td></tr>"
            % (("%s %s" % (a.get("code"), a.get("name") or "")).strip(),
               "up" if a.get("advice") in ("加仓", "买入") else
               ("down" if a.get("advice") in ("减仓", "清仓", "止损", "卖出") else ""),
               a.get("advice") or "-", a.get("trigger") or "-", (a.get("reason") or "")[:70])
            for a in advices)
        parts.append("<h3>我的持仓操作建议</h3><table>"
                     "<tr><th>标的</th><th>建议</th><th>触发位</th><th>理由</th></tr>%s</table>" % rows)
    if account:
        parts.append("<h3>AI 模拟盘</h3><table>"
                     "<tr><th>权益</th><td>%.2f</td><th>累计收益</th><td class='%s'>%+.2f%%</td></tr>"
                     "<tr><th>现金</th><td>%.2f</td><th>持仓</th><td>%d 只</td></tr></table>"
                     % (account.get("equity", 0),
                        "up" if account.get("total_return_pct", 0) >= 0 else "down",
                        account.get("total_return_pct", 0), account.get("cash", 0),
                        len(account.get("positions") or [])))
    if journal:
        parts.append("<h3>操盘日记</h3><pre>%s</pre>" % str(journal)[:6000])
    if extra_html:
        parts.append(extra_html)
    subject = "[astock] %s %s" % (title, datetime.now().strftime("%m-%d"))
    return send(subject, _wrap(title, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                               "".join(parts)), cfg=cfg, attachments=attachments)


def test(cfg=None, block=True):
    """测试发送（同步，方便前端拿到结果）。"""
    ok, why = available(cfg)
    if not ok:
        return False, why
    html = _wrap("邮件通知测试", datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 "<p>如果你收到这封邮件，说明 astock-watch 的邮件通知已经配置成功。</p>"
                 "<p>之后会在：<b>买入/卖出成交</b>、<b>收盘复盘</b>、<b>次日计划</b> 时给你发信。</p>")
    try:
        _send_sync("astock-watch 邮件测试", html, "", conf(cfg))
        _log({"event": "test-ok", "to": conf(cfg)["to"]})
        return True, "测试邮件已发送到 %s" % conf(cfg)["to"]
    except Exception as exc:                           # noqa: BLE001
        _log({"event": "test-fail", "error": str(exc)[:300]})
        return False, "发送失败：%s" % str(exc)[:200]
