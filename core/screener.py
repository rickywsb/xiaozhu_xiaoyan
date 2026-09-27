"""core/screener.py — 全市场选股器（参考 Finviz / TradingView Screener）

股票池 = S&P 500 ∪ Nasdaq-100 ∪ 自定义篮子 ∪ 持仓 ∪ 关注（排除杠杆 / 反向产品）。
对每只股票用 1 年日线计算技术面指标，再按预设策略或自定义条件筛选：

  · RS 评级     IBD 式相对强度：0.4×3月 + 0.2×6月 + 0.2×9月 + 0.2×12月 收益，在股票池内取百分位（1-99）
  · 距 52 周高、站上 MA50 / MA200、20 日突破（收盘创前 20 日新高）、量比（当日量 / 50 日均量）
  · 6-1 月趋势、ATR%（波动）、日均成交额、所属板块在板块雷达中的象限
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from core import daily_momentum as dm
from core import sectors as sc

_NDX_URL = "https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies"
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15"

# GICS 行业 → 板块雷达中的 SPDR 行业 key
_GICS_TO_KEY = {src[1]: k for k, _, _, src in sc._ETF_UNIVERSE if src[0] == "gics"}
# GICS 细分行业 → 主题 key
_SUB_TO_KEY = {sub: k for k, _, _, src in sc._ETF_UNIVERSE if src[0] == "sub" for sub in src[1]}


# ─── 股票池 ───────────────────────────────────────────────────────────────────

def nasdaq100() -> pd.DataFrame:
    """Nasdaq-100 成分：ticker, name, industry（ICB）。失败返回空表。"""
    try:
        html = requests.get(_NDX_URL, headers={"User-Agent": _UA}, timeout=30).text
        for t in pd.read_html(io.StringIO(html)):
            cols = {str(c).split("[")[0].strip(): c for c in t.columns}
            if "Ticker" in cols and len(t) >= 90:
                ind = cols.get("ICB Industry") or cols.get("GICS Sector")
                return pd.DataFrame({
                    "ticker": t[cols["Ticker"]].astype(str).str.upper().str.replace(".", "-", regex=False),
                    "name": t[cols.get("Company", cols["Ticker"])],
                    "industry": t[ind] if ind is not None else "",
                })
    except Exception:
        pass
    return pd.DataFrame(columns=["ticker", "name", "industry"])


def build_universe(holdings: set[str], watchlist: set[str]) -> pd.DataFrame:
    """ticker, name, sector, sub_industry, sector_key（板块雷达 key），source。"""
    rows: dict[str, dict] = {}
    try:
        sp = sc.sp500_constituents()
    except Exception:
        sp = pd.DataFrame()
    for r in sp.itertuples():
        rows[r.ticker] = {"name": r.name, "sector": r.gics_sector, "sub_industry": r.sub_industry,
                          "sector_key": _SUB_TO_KEY.get(r.sub_industry) or _GICS_TO_KEY.get(r.gics_sector),
                          "source": "S&P 500"}
    for r in nasdaq100().itertuples():
        if r.ticker in rows:
            rows[r.ticker]["source"] += " · NDX"
        else:
            rows[r.ticker] = {"name": r.name, "sector": r.industry, "sub_industry": "",
                              "sector_key": None, "source": "NDX"}
    for key, members in config.CUSTOM_BASKETS.items():
        for t in members:
            row = rows.setdefault(t, {"name": t, "sector": "", "sub_industry": "", "sector_key": None,
                                      "source": "自定义篮子"})
            row["sector_key"] = key          # 自定义篮子优先（光模块 / 存储 / AI电力）
    for t in sorted(holdings | watchlist):
        if t not in rows:
            rows[t] = {"name": t, "sector": "", "sub_industry": "", "sector_key": None,
                       "source": "持仓/关注"}
    df = pd.DataFrame([{"ticker": t, **v} for t, v in rows.items()])
    df = df[~df["ticker"].isin(config.LEVERAGED_TICKERS) & (df["ticker"] != config.CASH_TICKER)]
    return df.reset_index(drop=True)


# ─── 指标 ─────────────────────────────────────────────────────────────────────

def _ret(c: pd.Series, n: int) -> float | None:
    return float(c.iloc[-1] / c.iloc[-(n + 1)] - 1) if len(c) > n else None


def _metrics(df: pd.DataFrame) -> dict | None:
    df = df.dropna(subset=["Close"])
    if len(df) < 60:
        return None
    c, v = df["Close"], df["Volume"].fillna(0)
    h, lo = df["High"], df["Low"]
    last = float(c.iloc[-1])
    prev_tr = pd.concat([h - lo, (h - c.shift()).abs(), (lo - c.shift()).abs()], axis=1).max(axis=1)
    vol50 = float(v.iloc[-51:-1].mean()) if len(v) > 51 else float(v.mean())
    r = {n: _ret(c, n) for n in (5, 20, 63, 126, 189, 252)}
    parts = [(0.4, r[63]), (0.2, r[126]), (0.2, r[189]), (0.2, r[252])]
    got = [(w, x) for w, x in parts if x is not None]
    rs_raw = sum(w * x for w, x in got) / sum(w for w, _ in got) if got else None
    trend, _ = dm._trend_6_1(c)
    return {
        "last": last,
        "ret_5d": r[5], "ret_20d": r[20], "ret_63d": r[63], "ret_252d": r[252],
        "rs_raw": rs_raw, "trend_6_1": trend,
        "dist_high52": last / float(c.tail(252).max()) - 1,
        "above_ma50": last > float(c.tail(50).mean()),
        "above_ma200": (last > float(c.tail(200).mean())) if len(c) >= 200 else None,
        "breakout20": last > float(c.iloc[-21:-1].max()),
        "vol_ratio": float(v.iloc[-1] / vol50) if vol50 else None,
        "atr_pct": float(prev_tr.tail(14).mean() / last) if last else None,
        "dollar_vol": float((c * v).tail(50).median()),
        "bars": len(c),
    }


def scan(universe: pd.DataFrame, chunk: int = 100) -> pd.DataFrame:
    """下载股票池 1 年日线（剔除盘中未收完的 K 线）并计算指标，附 RS 评级与板块象限。"""
    tickers = list(universe["ticker"])
    data: dict[str, pd.DataFrame] = {}
    for i in range(0, len(tickers), chunk):
        data.update(dm.fetch_ohlcv_histories(tickers[i:i + chunk], complete_bars_only=True))

    from core.fx import get_fx_rates
    from core.price_updater import to_usd
    fx = get_fx_rates()

    rows = []
    for r in universe.itertuples():
        df = data.get(r.ticker)
        if df is None:
            continue
        m = _metrics(df)
        if m:
            m["dollar_vol"] = to_usd(m["dollar_vol"], r.ticker, fx)   # 本币成交额 → 美元（如 IQE.L 便士）
            rows.append({"ticker": r.ticker, "name": r.name, "sector": r.sector,
                         "sub_industry": r.sub_industry, "sector_key": r.sector_key,
                         "source": r.source, **m})
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out["rs"] = (out["rs_raw"].rank(pct=True) * 98 + 1).round().astype("Int64")

    try:
        board = sc.sector_board()
        quad = dict(zip(board["key"], board["quadrant"]))
        names = dict(zip(board["key"], board["name"]))
    except Exception:
        quad, names = {}, {}
    out["sector_name"] = out["sector_key"].map(names).fillna(out["sector"])
    out["quadrant"] = out["sector_key"].map(quad).fillna("—")
    return out.sort_values("rs", ascending=False).reset_index(drop=True)


# ─── 预设策略 ─────────────────────────────────────────────────────────────────

PRESETS: dict[str, dict] = {
    "🚀 新高突破": {
        "desc": "距 52 周高 ≤3%，今日放量（量比 ≥1.5），站上 MA50",
        "rule": lambda d: (d["dist_high52"] >= -0.03) & (d["vol_ratio"] >= 1.5) & d["above_ma50"],
    },
    "💪 强者恒强": {
        "desc": "RS ≥90，站上 MA50 与 MA200，6-1 月趋势为正",
        "rule": lambda d: (d["rs"] >= 90) & d["above_ma50"] & (d["above_ma200"] == True)  # noqa: E712
                          & (d["trend_6_1"] > 0),
    },
    "🔄 轮动 + 突破": {
        "desc": "所属板块处于🔵改善 / 🟢领先象限，个股收盘突破 20 日高点，RS ≥70",
        "rule": lambda d: d["quadrant"].isin(["🔵 改善", "🟢 领先"]) & d["breakout20"] & (d["rs"] >= 70),
    },
    "📉 强势回调": {
        "desc": "RS ≥80，近 5 日跌 ≥5%，仍站上 MA200",
        "rule": lambda d: (d["rs"] >= 80) & (d["ret_5d"] <= -0.05) & (d["above_ma200"] == True),  # noqa: E712
        "caution": "⚠️ 信号成绩单显示：近期行情里「回踩 / 抄底」类信号历史表现为 ❌ 反向，买回调需额外确认。",
    },
}


def apply_filters(df: pd.DataFrame, preset: str | None = None, *, min_rs: int = 0,
                  max_dist_high: float | None = None, need_ma50: bool = False,
                  need_ma200: bool = False, need_breakout: bool = False,
                  min_vol_ratio: float = 0.0, min_dollar_vol: float = 0.0,
                  sectors: list[str] | None = None, quadrants: list[str] | None = None,
                  exclude: set[str] | None = None) -> pd.DataFrame:
    """预设策略 + 通用条件（流动性、板块、象限、排除名单）叠加筛选。"""
    d = df.copy()
    mask = pd.Series(True, index=d.index)
    if preset and preset in PRESETS:
        mask &= PRESETS[preset]["rule"](d).fillna(False).astype(bool)
    if min_rs:
        mask &= d["rs"].fillna(0) >= min_rs
    if max_dist_high is not None:
        mask &= d["dist_high52"] >= -max_dist_high
    if need_ma50:
        mask &= d["above_ma50"]
    if need_ma200:
        mask &= d["above_ma200"] == True  # noqa: E712
    if need_breakout:
        mask &= d["breakout20"]
    if min_vol_ratio:
        mask &= d["vol_ratio"].fillna(0) >= min_vol_ratio
    if min_dollar_vol:
        mask &= d["dollar_vol"].fillna(0) >= min_dollar_vol
    if sectors:
        mask &= d["sector_name"].isin(sectors)
    if quadrants:
        mask &= d["quadrant"].isin(quadrants)
    if exclude:
        mask &= ~d["ticker"].isin(exclude)
    return d[mask].reset_index(drop=True)
