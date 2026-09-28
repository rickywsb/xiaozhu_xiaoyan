"""core/volume.py — 放量 / 缩量信号（日线）+ 盘中放量折算

信号（均基于"最近一根完整日线"，量比 = 当日量 / 前 50 日均量）：
  看多  放量上涨     量比 ≥2 且 当日涨 ≥3%
        放量突破     收盘突破前 20 日最高收盘 且 量比 ≥1.5
        口袋支点     上涨日，当日量 > 前 10 日中任一下跌日的量，且站上 MA50（O'Neil Pocket Pivot）
        缩量回调     站上 MA50，近 5 日跌 ≥3%，且 5 日均量 < 0.8× 50 日均量（健康回调）
  看空  放量下跌     量比 ≥2 且 当日跌 ≥3%
        放量滞涨     量比 ≥2 但 |涨跌| <1%，且收盘距 20 日高 ≤3%（高位换手 / 派发嫌疑）

回测（core.signal_backtest）与页面当前状态都调用 volume_signals()，口径完全一致。
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

SURGE = 2.0            # 放量量比
BREAKOUT_VR = 1.5      # 突破所需量比
BIG_MOVE = 0.03        # 放量上涨 / 下跌的涨跌幅门槛
FLAT_MOVE = 0.01       # 滞涨：涨跌幅在 ±1% 内
DRY_VR = 0.8           # 缩量：5 日均量 < 0.8× 50 日均量

VOLUME_SIGNALS: list[tuple[str, int]] = [
    ("放量上涨", +1), ("放量突破20日高", +1), ("口袋支点", +1), ("缩量回调", +1),
    ("放量下跌", -1), ("放量滞涨", -1),
]


def volume_stats(ohlcv: pd.DataFrame) -> dict | None:
    """最近一根日线的量价统计：量比、当日涨跌、距 20 日高、5 日涨跌与 5 日量比、是否站上 MA50。"""
    d = ohlcv.dropna(subset=["Close"]).tail(61)
    if len(d) < 55 or "Volume" not in d:
        return None
    c = d["Close"].to_numpy(float)
    v = d["Volume"].fillna(0).to_numpy(float)
    base = float(np.mean(v[-51:-1]))
    if base <= 0 or v[-1] <= 0:
        return None
    prior_high = float(c[-21:-1].max())
    return {
        "vr": v[-1] / base,
        "ret_1d": c[-1] / c[-2] - 1,
        "ret_5d": c[-1] / c[-6] - 1,
        "vr_5d": float(np.mean(v[-5:])) / base,
        "breakout": c[-1] > prior_high,
        "dist_high20": c[-1] / prior_high - 1,
        "above_ma50": c[-1] > float(c[-50:].mean()),
        "vol": v[-1], "avg_vol50": base,
        # 口袋支点：前 10 日下跌日的最大成交量
        "max_down_vol10": max([v[j] for j in range(-11, -1) if c[j] < c[j - 1]], default=None),
    }


def volume_signals(ohlcv: pd.DataFrame) -> set[str]:
    """最近一根日线触发的放量信号名集合（名称与 VOLUME_SIGNALS 一致）。"""
    s = volume_stats(ohlcv)
    if not s:
        return set()
    on = set()
    if s["vr"] >= SURGE and s["ret_1d"] >= BIG_MOVE:
        on.add("放量上涨")
    if s["breakout"] and s["vr"] >= BREAKOUT_VR:
        on.add("放量突破20日高")
    if s["ret_1d"] > 0 and s["above_ma50"] and s["max_down_vol10"] and s["vol"] > s["max_down_vol10"]:
        on.add("口袋支点")
    if s["above_ma50"] and s["ret_5d"] <= -BIG_MOVE and s["vr_5d"] < DRY_VR:
        on.add("缩量回调")
    if s["vr"] >= SURGE and s["ret_1d"] <= -BIG_MOVE:
        on.add("放量下跌")
    if s["vr"] >= SURGE and abs(s["ret_1d"]) < FLAT_MOVE and s["dist_high20"] >= -0.03:
        on.add("放量滞涨")
    return on


# ─── 盘中放量折算 ─────────────────────────────────────────────────────────────
# 美股典型日内累计成交量占比（U 形：开盘与收盘两端量大），按开盘后分钟数线性插值。经验近似值。
_US_CURVE_MIN = [0, 30, 60, 90, 120, 150, 180, 210, 240, 270, 300, 330, 360, 390]
_US_CURVE_PCT = [0.0, 0.13, 0.21, 0.28, 0.34, 0.39, 0.44, 0.49, 0.54, 0.60, 0.66, 0.73, 0.83, 1.0]
MIN_MINUTES = 30       # 开盘 30 分钟内折算误差太大，不给盘中放量结论


def session_fraction(now_et: datetime) -> float | None:
    """美股开盘以来按典型日内分布估算的"已完成成交量占比"；非交易时段返回 None。"""
    minutes = (now_et.hour * 60 + now_et.minute) - (9 * 60 + 30)
    if minutes < MIN_MINUTES or minutes > 390:
        return None
    return float(np.interp(minutes, _US_CURVE_MIN, _US_CURVE_PCT))


def projected_vr(vol_so_far: float, avg_vol50: float, frac: float) -> float | None:
    """盘中量 ÷ 已完成占比 = 预计全天量；再除以 50 日均量得到预计量比。"""
    if not (vol_so_far and avg_vol50 and frac):
        return None
    return vol_so_far / frac / avg_vol50
