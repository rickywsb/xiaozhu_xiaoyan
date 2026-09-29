"""core/posture.py — 市场状态（参考 KovaView Market Posture）

五项各打 0–100 分，汇总成一句话结论：
  趋势  SPY 相对 MA50 / MA200 的位置 + MA50 的 20 日斜率
  宽度  全市场（评分股票池约 540 只）站上 MA50 的比例
  信用  高收益债 / 国债（HYG / IEF）相对其 50 日均线与 20 日变化——信用利差收窄 = 风险偏好健康
  波动  VIX 在过去 1 年中的分位（越低越好）
  领涨  板块雷达中处于领先 / 改善象限的板块占比
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import daily_momentum as dm


def _clip(x: float) -> float:
    return float(np.clip(x, 0, 100))


def compute(breadth50: float | None = None, board=None) -> dict:
    data = dm.fetch_ohlcv_histories(["SPY", "HYG", "IEF", "^VIX"], period="1y", complete_bars_only=True)
    out: dict = {}

    spy = data.get("SPY")
    if spy is not None and len(spy) > 200:
        c = spy["Close"]
        ma50, ma200 = c.rolling(50).mean(), c.rolling(200).mean()
        slope = float(ma50.iloc[-1] / ma50.iloc[-21] - 1)
        trend = 30 * (c.iloc[-1] > ma50.iloc[-1]) + 30 * (c.iloc[-1] > ma200.iloc[-1]) + _clip(50 + slope / 0.04 * 50) * 0.4
        out["趋势"] = {"score": _clip(trend),
                     "note": f"SPY {'站上' if c.iloc[-1] > ma50.iloc[-1] else '跌破'} 50 日线、"
                             f"{'站上' if c.iloc[-1] > ma200.iloc[-1] else '跌破'} 200 日线"}
    if breadth50 is not None:
        out["宽度"] = {"score": _clip(breadth50 * 100), "note": f"全市场 {breadth50:.0%} 站上 50 日线"}
    hyg, ief = data.get("HYG"), data.get("IEF")
    if hyg is not None and ief is not None:
        r = (hyg["Close"] / ief["Close"]).dropna()
        if len(r) > 60:
            above = r.iloc[-1] > r.rolling(50).mean().iloc[-1]
            chg = float(r.iloc[-1] / r.iloc[-21] - 1)
            out["信用"] = {"score": _clip(50 + 25 * (1 if above else -1) + chg / 0.02 * 25),
                         "note": "高收益债相对国债" + ("走强" if chg > 0 else "走弱")}
    vix = data.get("^VIX")
    if vix is not None and len(vix) > 60:
        v = vix["Close"]
        pct = float((v < v.iloc[-1]).mean())
        out["波动"] = {"score": _clip((1 - pct) * 100), "note": f"VIX {v.iloc[-1]:.1f}，处于 1 年 {pct:.0%} 分位"}
    if board is not None and not board.empty:
        n = len(board)
        lead = (board["quadrant"] == "🟢 领先").sum()
        imp = (board["quadrant"] == "🔵 改善").sum()
        out["领涨"] = {"score": _clip((lead + 0.5 * imp) / n * 100 * 2),
                     "note": f"领先 {lead} 个 · 改善 {imp} 个板块"}

    if not out:
        return {}
    avg = float(np.mean([x["score"] for x in out.values()]))
    t = out.get("趋势", {}).get("score", 50)
    b = out.get("宽度", {}).get("score", 50)
    lead_s = out.get("领涨", {}).get("score", 50)
    head = "上升趋势" if t >= 60 else ("下降趋势" if t < 40 else "震荡")
    if head == "上升趋势" and (b < 40 or lead_s < 40):
        head += " · 承压"
    elif b >= 65:
        head += " · 普涨"
    stance = ("偏进攻" if avg >= 65 else "偏进攻 · 精选个股" if avg >= 50 and t >= 60
              else "中性 · 精选个股" if avg >= 45 else "偏防守")
    return {"headline": head, "stance": stance, "score": avg, "items": out}
