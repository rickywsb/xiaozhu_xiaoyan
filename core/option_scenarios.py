"""core/option_scenarios.py — 期权情景工具（参考 OptionStrat）

对单个期权仓位（或按标的合并）做"如果……会怎样"：
  · 价值曲线   标的价格 × 时间点（今天 / N 天后 / 到期）→ 仓位价值与相对现价的盈亏
  · 情景矩阵   标的涨跌 × IV 变化，在指定天数后的盈亏
  · 时间衰减   标的不动时，价值随时间的变化
  · 换月建议   规则判断 + CBOE 期权链中更远到期、delta 接近的候选合约

全部用 Black-Scholes 重估（与 core.options 一致）；盈亏相对**当前价值**（未记录买入成本）。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import options as O

MULT = O.CONTRACT_MULTIPLIER
ROLL_DTE = 120            # 剩余 < 120 天：theta 加速，考虑换月
DEEP_ITM_DELTA = 0.85     # delta 过高：像持有正股，资金效率下降
LOW_DELTA = 0.20          # delta 过低：到期价内概率小


def leg_from_book_row(r) -> dict:
    """风险账本中的期权行 → 情景计算用的 leg。"""
    return {"contract": r["ticker"], "display": r["display"], "underlying": r["underlying"],
            "type": r["option_type"], "K": float(r["strike"]), "T": float(r["T"]),
            "iv": float(r["iv"]), "S": float(r["S"]), "qty": float(r["qty"]),
            "mark": float(r["mark"]), "expiry": r["expiry"]}


def _value(legs: list[dict], S: float, days: float = 0.0, iv_shift: float = 0.0) -> float:
    """组合价值（美元）：各 leg 在标的价 S、前进 days 天、IV 平移后的 BS 价格。"""
    total = 0.0
    for lg in legs:
        T2 = max(lg["T"] - days / 365.0, 0.0)
        p = O.bs_greeks(S, lg["K"], T2, max(lg["iv"] + iv_shift, 0.01), option_type=lg["type"])["price"]
        total += p * MULT * lg["qty"]
    return total


def current_value(legs: list[dict]) -> float:
    return sum(lg["mark"] * MULT * lg["qty"] for lg in legs)


def value_curves(legs: list[dict], horizons_days: list[int], span: float = 0.5,
                 n: int = 81) -> pd.DataFrame:
    """标的价格 ±span 区间内，各时间点的盈亏（相对当前价值）。列：price, move, 各时间点。"""
    S0 = legs[0]["S"]
    prices = np.linspace(S0 * (1 - span), S0 * (1 + span), n)
    base = current_value(legs)
    out = {"price": prices, "move": prices / S0 - 1}
    max_T = max(lg["T"] for lg in legs) * 365
    for d in horizons_days:
        label = "到期" if d >= max_T else ("今天" if d == 0 else f"{d} 天后")
        out[label] = [_value(legs, p, min(d, max_T)) - base for p in prices]
    return pd.DataFrame(out)


def scenario_matrix(legs: list[dict], moves: list[float], iv_shifts: list[float],
                    days: int = 0) -> pd.DataFrame:
    """行 = IV 变化，列 = 标的涨跌，值 = 盈亏（美元，相对当前价值）。"""
    S0 = legs[0]["S"]
    base = current_value(legs)
    data = [[_value(legs, S0 * (1 + m), days, v) - base for m in moves] for v in iv_shifts]
    return pd.DataFrame(data, index=[f"IV {v * 100:+.0f}点" for v in iv_shifts],
                        columns=[f"{m:+.0%}" for m in moves])


def decay_path(legs: list[dict], step: int = 7) -> pd.DataFrame:
    """标的与 IV 不变时，仓位价值随时间的变化（直到最近一个到期日）。"""
    S0 = legs[0]["S"]
    last = int(min(lg["T"] for lg in legs) * 365)
    days = list(range(0, max(last, 1) + 1, step)) + ([last] if last % step else [])
    return pd.DataFrame({"days": days, "value": [_value(legs, S0, d) for d in days]})


def breakeven(leg: dict) -> float:
    """到期盈亏平衡价（相对当前价格买入）：call = K + 权利金，put = K − 权利金。"""
    return leg["K"] + leg["mark"] if leg["type"] == "call" else leg["K"] - leg["mark"]


def leg_stats(leg: dict) -> dict:
    g = O.bs_greeks(leg["S"], leg["K"], leg["T"], leg["iv"], option_type=leg["type"])
    be = breakeven(leg)
    return {
        "dte": int(round(leg["T"] * 365)),
        "delta": g["delta"], "theta_day_usd": g["theta"] * MULT * leg["qty"],
        "vega_usd": g["vega"] * MULT * leg["qty"],
        "breakeven": be, "breakeven_move": be / leg["S"] - 1,
        "leverage": g["delta"] * leg["S"] / leg["mark"] if leg["mark"] else None,   # 标的涨 1%，期权涨约几 %
        "intrinsic": max(leg["S"] - leg["K"], 0) if leg["type"] == "call" else max(leg["K"] - leg["S"], 0),
    }


def roll_advice(leg: dict) -> list[str]:
    """基于到期天数 / delta 的规则提示（非指令）。"""
    s = leg_stats(leg)
    tips = []
    if s["dte"] < ROLL_DTE:
        tips.append(f"⏳ 剩余 {s['dte']} 天（<{ROLL_DTE}），时间价值衰减加速，可考虑换到更远月份。")
    if leg["type"] == "call" and s["delta"] >= DEEP_ITM_DELTA:
        tips.append(f"💰 delta {s['delta']:.2f} 已深度价内，走势接近正股、杠杆下降；"
                    "可考虑上移行权价锁定部分利润并降低占用资金。")
    if abs(s["delta"]) <= LOW_DELTA:
        tips.append(f"🎲 delta {s['delta']:.2f} 偏低，到期价内概率小；若判断未变，可考虑下移行权价或延长期限。")
    time_value = leg["mark"] - s["intrinsic"]
    if leg["mark"] and time_value / leg["mark"] > 0.5 and s["dte"] < 180:
        tips.append(f"🧊 时间价值占权利金 {time_value / leg['mark']:.0%}，且不足半年到期，持有成本较高。")
    if not tips:
        tips.append("✅ 期限与价内程度都在舒适区，暂无换月必要。")
    return tips


def roll_candidates(leg: dict, max_n: int = 6) -> pd.DataFrame:
    """
    CBOE 期权链中比当前更远的到期日，每个到期日取 delta 最接近当前仓位的合约。
    列：合约, 到期, 剩余天数, 行权价, 买价/卖价/中间价, IV, delta, 每日theta($/张), 换月净成本($/张)。
    """
    chain = O._fetch_cboe_chain(leg["underlying"])
    if not chain:
        return pd.DataFrame()
    cur_delta = leg_stats(leg)["delta"]
    today = date.today()
    rows = []
    for occ_code, q in chain.items():
        p = O.parse_occ(occ_code)
        if not p or p["option_type"] != leg["type"] or p["expiry"] <= leg["expiry"]:
            continue
        bid, ask = q.get("bid") or 0.0, q.get("ask") or 0.0
        if bid <= 0 or ask <= 0 or not q.get("delta"):
            continue
        rows.append({"合约": occ_code, "到期": p["expiry"],
                     "剩余天数": (date.fromisoformat(p["expiry"]) - today).days,
                     "行权价": p["strike"], "买价": bid, "卖价": ask, "中间价": (bid + ask) / 2,
                     "IV": q.get("iv"), "delta": q.get("delta"),
                     "每日theta$": (q.get("theta") or 0.0) * MULT})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["_dd"] = (df["delta"] - cur_delta).abs()
    best = df.sort_values("_dd").groupby("到期", as_index=False).first().sort_values("到期").head(max_n)
    best["换月净成本$"] = (best["中间价"] - leg["mark"]) * MULT     # 每张：买新 − 卖旧（按中间价）
    return best.drop(columns="_dd").reset_index(drop=True)
