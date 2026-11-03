# -*- coding: utf-8 -*-
"""实时盯盘引擎：轮询行情，命中条件立刻告警（控制台 + 蜂鸣 + 日志 + 可选 Webhook）。

告警类型：
    止损 / 支撑 / 阻力 / 加仓触发 / 目标位 / 急涨急跌 / 放量 / 均线收复失守 / 评分变化
"""
from __future__ import annotations

import time
from datetime import datetime, time as dtime

from . import feed, indicators as ind, notify, signals, store
from .notify import color, pct_color


def is_trading_day(dt=None):
    dt = dt or datetime.now()
    return dt.weekday() < 5


def in_session(dt=None, include_auction=True):
    dt = dt or datetime.now()
    if not is_trading_day(dt):
        return False
    t = dt.time()
    if include_auction and dtime(9, 15) <= t <= dtime(11, 30):
        return True
    if not include_auction and dtime(9, 30) <= t <= dtime(11, 30):
        return True
    return dtime(13, 0) <= t <= dtime(15, 0)


class SymbolState:
    def __init__(self, code):
        self.code = code
        self.name = code
        self.price = None
        self.pct = None
        self.ticks = []                 # [(ts, price)]
        self.above_ma5 = None
        self.above_ma10 = None
        self.above_ma20 = None
        self.last_score = None
        self.report = None
        self.report_ts = 0.0
        self.cooldown = {}              # key -> ts

    def recent_price(self, seconds):
        cutoff = time.time() - seconds
        for ts, p in reversed(self.ticks):
            if ts <= cutoff:
                return p
        return self.ticks[0][1] if self.ticks else None


class Watcher:
    def __init__(self, cfg):
        self.cfg = cfg
        m = cfg.get("monitor") or {}
        self.interval = float(m.get("interval_sec", 15))
        self.refresh = float(m.get("refresh_sec", 120))
        self.status_every = float(m.get("status_sec", 60))
        self.exit_when_closed = bool(m.get("exit_when_closed", False))
        self.codes = list(cfg.get("watchlist") or [])
        self.states = {c: SymbolState(c) for c in self.codes}
        self.alerts_cfg = {**signals.DEFAULTS["alerts"], **(cfg.get("alerts") or {})}
        self.notify_cfg = cfg.get("notify") or {}
        self._last_status = 0.0
        self._last_refresh = 0.0
        self.alert_count = 0

    # ------------------------------------------------------------------ #
    def _cool(self, st, key):
        cd = float(self.alerts_cfg.get("cooldown_sec", 900))
        last = st.cooldown.get(key, 0)
        if time.time() - last < cd:
            return False
        st.cooldown[key] = time.time()
        return True

    def _fire(self, st, level, key, message):
        if not self._cool(st, key):
            return
        ts = datetime.now().strftime("%H:%M:%S")
        tag = {"ACTION": color("【操作】", "red"),
               "WARN": color("【注意】", "yellow"),
               "INFO": color("【提示】", "cyan")}.get(level, "【信息】")
        line = "%s %s %s(%s) %s" % (ts, tag, st.name, st.code, message)
        print(line)
        store.append_alert(level, st.code, st.name, message)
        self.alert_count += 1
        if level == "ACTION":
            notify.beep("action")
        elif level == "WARN":
            notify.beep("warn")
        if self.notify_cfg.get("webhook", {}).get("enabled") and level in ("ACTION", "WARN"):
            notify.push_webhook(self.notify_cfg, "%s %s(%s)" % (level, st.name, st.code), message)

    # ------------------------------------------------------------------ #
    def _refresh_report(self, code):
        """重建完整分析（日线指标 / 评分 / 价位 / 资金）。"""
        st = self.states[code]
        try:
            r = signals.build_report(code, self.cfg)
        except Exception as exc:                        # noqa: BLE001
            print(color("  [%s] 分析失败：%s" % (code, exc), "grey"))
            return None
        st.report = r
        st.report_ts = time.time()
        st.name = r["name"] or code
        prev = st.last_score
        st.last_score = r["scores"]["total"]
        store.append_snapshot(r)
        if prev is not None and abs(st.last_score - prev) >= 8:
            self._fire(st, "INFO", "score_shift",
                       "评分 %d → %d（%s）：%s" % (prev, st.last_score,
                                                  r["verdict"]["level"], r["verdict"]["text"]))
        return r

    # ------------------------------------------------------------------ #
    def _check(self, st):
        r = st.report
        if not r:
            return
        price = st.price
        if not price:
            return
        lv = r["levels"]
        cfg_sym = (self.cfg.get("symbols") or {}).get(st.code, {}) or {}
        stop = lv["stop"]

        # 1) 止损（最高优先级）
        if price <= stop:
            self._fire(st, "ACTION", "stop",
                       "现价 %.2f 已触及止损线 %.2f！按纪律执行离场（收盘确认）" % (price, stop))

        # 2) 支撑：上方接近 → 提示；有效跌破 → 警示（逐级）
        for i, s in enumerate(lv["support"]):
            if price >= s * 0.997 and abs(price - s) / s <= 0.006:
                self._fire(st, "INFO", "sup%d" % i,
                           "回踩支撑 %.2f（第 %d 道），观察是否止跌" % (s, i + 1))
                break
        for i, s in enumerate(lv["support"]):
            if price < s * 0.994:            # 已有效跌破第 i 道
                nxt = lv["support"][i + 1] if i + 1 < len(lv["support"]) else None
                self._fire(st, "WARN" if i == 0 else "ACTION", "brk_sup%d" % i,
                           "跌破第 %d 道支撑 %.2f%s"
                           % (i + 1, s, "，下一道支撑 %.2f" % nxt if nxt else "，已无支撑"))
                break

        # 3) 阻力突破
        for i, res in enumerate(lv["resistance"]):
            if price >= res:
                self._fire(st, "ACTION" if i == 0 else "WARN", "res%d" % i,
                           "突破阻力 %.2f（第 %d 道），若放量可考虑加仓" % (res, i + 1))
                break

        # 4) 加仓触发价
        trig = cfg_sym.get("add_trigger")
        if trig and price >= float(trig):
            self._fire(st, "ACTION", "add_trigger",
                       "触及加仓触发价 %.2f，确认量能后执行加仓计划" % float(trig))

        # 5) 目标位
        for i, t in enumerate(lv["targets"]):
            if price >= t:
                self._fire(st, "ACTION", "tgt%d" % i,
                           "到达第 %d 目标 %.2f，建议分批止盈" % (i + 1, t))
                break

        # 6) 急涨急跌（默认 5 分钟）
        win = float(self.alerts_cfg.get("pct_5min_window_sec", 300))
        ref = st.recent_price(win)
        if ref:
            move = (price / ref - 1) * 100
            th = float(self.alerts_cfg.get("pct_5min", 1.5))
            if abs(move) >= th:
                self._fire(st, "WARN", "fast_move%d" % int(move > 0),
                           "%d 秒内%s %.2f%%（%.2f → %.2f），留意异动"
                           % (win, "拉升" if move > 0 else "跳水", abs(move), ref, price))

        # 7) 当日涨跌幅阈值
        thd = float(self.alerts_cfg.get("pct_day", 5.0))
        if st.pct is not None and abs(st.pct) >= thd:
            self._fire(st, "WARN", "day_pct%d" % int(st.pct > 0),
                       "当日涨跌幅 %.2f%%，已达 %.1f%% 预警线" % (st.pct, thd))

        # 8) 放量
        vr = r["extra"]["vol_ratio5"]
        thv = float(self.alerts_cfg.get("vol_ratio", 2.5))
        if vr >= thv:
            self._fire(st, "WARN", "vol",
                       "量能放大至 5 日均量的 %.2f 倍，%s" %
                       (vr, "资金进场" if (st.pct or 0) > 0 else "警惕出货"))

        # 9) 均线收复 / 失守（用实时价重算，K 线带 2 分钟缓存）
        try:
            closes = self._closes_cache(st)
            if not closes:
                return
            series = closes + [price]
            for n, key in ((5, "above_ma5"), (10, "above_ma10"), (20, "above_ma20")):
                ma = ind.sma(series, n)[-1]
                if ma is None:
                    continue
                now_above = price > ma
                prev = getattr(st, key)
                setattr(st, key, now_above)
                if prev is None:
                    continue
                if now_above and not prev:
                    self._fire(st, "ACTION" if n >= 10 else "INFO", "ma%d_up" % n,
                               "实时价收复 MA%d（%.2f），短线转强信号" % (n, ma))
                elif prev and not now_above:
                    self._fire(st, "WARN", "ma%d_dn" % n,
                               "实时价失守 MA%d（%.2f），注意走弱" % (n, ma))
        except Exception:                               # noqa: BLE001
            pass

    def _closes_cache(self, st):
        """取历史收盘价（剔除当日未完成 K 线），带 TTL 缓存。"""
        ttl = float((self.cfg.get("monitor") or {}).get("ma_cache_sec", 120))
        cache = getattr(st, "closes_cache", None)
        if cache and time.time() - cache[0] < ttl:
            return cache[1]
        daily = feed.get_kline(st.code, 101, limit=80, adjust=1)
        today = datetime.now().date().isoformat()
        closes = [b["close"] for b in daily]
        if daily[-1]["date"] == today:
            closes = closes[:-1]
        st.closes_cache = (time.time(), closes)
        return closes

    # ------------------------------------------------------------------ #
    def status_line(self):
        now = datetime.now().strftime("%H:%M:%S")
        parts = []
        for code in self.codes:
            st = self.states[code]
            if st.price is None:
                parts.append("%s --" % code)
                continue
            arrow = "▲" if (st.pct or 0) > 0 else ("▼" if (st.pct or 0) < 0 else "—")
            parts.append("%s %s %.2f %s%+.2f%% 评分%s" % (
                st.name, code, st.price, arrow, st.pct or 0,
                st.last_score if st.last_score is not None else "--"))
        print(color("[%s] " % now, "grey") + " | ".join(parts))

    # ------------------------------------------------------------------ #
    def run_once(self):
        for code in self.codes:
            st = self.states[code]
            try:
                q = feed.get_quote(code)
            except Exception as exc:                    # noqa: BLE001
                print(color("  [%s] 行情获取失败：%s" % (code, exc), "grey"))
                continue
            st.name = q["name"] or code
            st.price = q["price"]
            st.pct = q["pct"]
            st.ticks.append((time.time(), q["price"]))
            st.ticks = st.ticks[-2000:]
            if st.report is None or time.time() - st.report_ts >= self.refresh:
                self._refresh_report(code)
            self._check(st)

    def run(self):
        notify.banner("astock-watch 实时盯盘已启动  %s" % datetime.now().strftime("%Y-%m-%d %H:%M"))
        print(color("监控标的：%s" % "、".join(self.codes), "bold"))
        print(color("轮询 %.0f 秒 / 指标刷新 %.0f 秒 / 停盘短信 %s"
                    % (self.interval, self.refresh,
                       "退出" if self.exit_when_closed else "继续等待"), "grey"))
        print(color("按 Ctrl+C 停止。告警同时写入 data/logs/", "grey"))
        try:
            while True:
                if not in_session():
                    if self.exit_when_closed:
                        print(color("非交易时段，退出。", "yellow"))
                        break
                    nxt = "下一交易时段 09:15"
                    print(color("[%s] 非交易时段，%s 秒后重试（%s）"
                                % (datetime.now().strftime("%H:%M:%S"),
                                   int(self.interval * 4), nxt), "grey"))
                    time.sleep(self.interval * 4)
                    continue
                self.run_once()
                if time.time() - self._last_status >= self.status_every:
                    self.status_line()
                    self._last_status = time.time()
                time.sleep(self.interval)
        except KeyboardInterrupt:
            print("")
            print(color("盯盘结束，本次共触发 %d 条告警。" % self.alert_count, "bold"))
            print(color("日志：data/logs/（当日 alerts_YYYY-MM-DD.log）", "grey"))
