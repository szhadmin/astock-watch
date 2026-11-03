# -*- coding: utf-8 -*-
"""astock-watch：A 股个股技术分析与实时盯盘工具。

模块：
    feed        行情数据源（东方财富公开接口，纯标准库）
    indicators  技术指标计算
    signals    信号评分与操作价位（核心决策引擎）
    analyze     单股 / 自选批量分析，输出中文报告
    monitor     盘中实时盯盘，触发条件即告警
    notify      控制台彩色输出、蜂鸣、Webhook 推送
    store       日志与快照落盘、收盘报告生成
"""

__version__ = "1.0.0"
__all__ = ["feed", "indicators", "signals", "analyze", "monitor", "notify", "store"]
