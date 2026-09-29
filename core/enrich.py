"""core/enrich.py — 为统一股票行（core.ui.stock_table）拼数据

enrich(tickers, ratings, quotes) → DataFrame[ticker, name, price, chg, vr, score, d5, spark,
                                           action, volume_state, signals, group]
  · 评分 / 操作倾向 / 量能状态 来自 core.rating（全市场评分表）；不在评分股票池的（如杠杆产品）标注"不评分"
  · 价格 / 当日涨跌 优先用盘中报价（core.tracker.live_quotes），否则用最近完整日线
  · 20 日走势 = 近 20 根收盘价；可靠信号 = 当前触发的 A / B 级信号（信号成绩单评级）
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from core import daily_momentum as dm


def reliable_signals(tickers: list[str]) -> dict[str, str]:
    """{ticker: "Fib 强势贴高 A · 口袋支点 B"}，只含 A / B 级。"""
    from core import signal_backtest as sbt
    res = sbt.load_result()
    if not res:
        return {}
    vm = sbt.verdict_map(res["summary"])
    out = {}
    for t, sigs in sbt.current_signals(tickers).items():
        good = [(s, vm[s]["评级"][0]) for s in sorted(sigs) if vm.get(s, {}).get("评级", "")[:1] in "AB"]
        good.sort(key=lambda x: x[1])
        if good:
            out[t] = " · ".join(f"{s} {g}" for s, g in good)
    return out


def enrich(tickers: list[str], ratings: pd.DataFrame, quotes: pd.DataFrame | None = None,
           names: dict[str, str] | None = None, with_signals: bool = True) -> pd.DataFrame:
    names = names or {}
    tickers = [t.upper() for t in tickers if t.upper() != config.CASH_TICKER]
    r = ratings.set_index("ticker") if not ratings.empty else pd.DataFrame()
    closes = dm.fetch_histories(tickers, period="3mo")
    q = quotes.set_index("ticker") if quotes is not None and not quotes.empty else pd.DataFrame()
    sig = reliable_signals(tickers) if with_signals else {}
    rows = []
    for t in tickers:
        c = closes.get(t)
        row = {"ticker": t, "name": names.get(t) or (r.at[t, "name"] if t in r.index else t)}
        if t in q.index:
            row["price"], row["chg"] = float(q.at[t, "last"]), q.at[t, "chg"]
        elif c is not None and len(c) > 1:
            row["price"], row["chg"] = float(c.iloc[-1]), float(c.iloc[-1] / c.iloc[-2] - 1)
        row["spark"] = [round(float(x), 4) for x in c.tail(20)] if c is not None else []
        if t in r.index:
            s, s5 = r.at[t, "score"], r.at[t, "score_5d"]
            row.update({"score": s, "d5": (s - s5) if pd.notna(s5) else None, "vr": r.at[t, "vr"],
                        "action": r.at[t, "action"], "volume_state": r.at[t, "volume_state"]})
        else:
            lev = config.LEVERAGED_TICKERS.get(t)
            row.update({"score": None, "d5": None, "vr": None,
                        "action": "杠杆·不评分" if lev else "不评分", "volume_state": ""})
        row["signals"] = sig.get(t, "")
        rows.append(row)
    return pd.DataFrame(rows)
