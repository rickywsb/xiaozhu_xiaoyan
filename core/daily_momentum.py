"""core/daily_momentum.py — 持仓量能评分（纯函数库，无 CLI）

从 new/daily_top10.py 提取核心逻辑，仅对给定 ticker 列表计算日动量指标，
不依赖全宇宙 universe 构建，响应速度快（~35 只持仓 < 15s）。
"""

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ─── 默认参数 ──────────────────────────────────────────────────────────────────
DEFAULT_DECAY  = 0.94
DEFAULT_WINDOW = 40
PERIODS        = [5, 10, 20, 60]          # 热力图展示的多周期
MULTI_WEIGHTS  = {5: 0.50, 10: 0.30, 20: 0.20}

# 价格历史回看长度（交易日），确保 60d 指标可计算
FETCH_PERIOD = "6mo"


# ─── 基础指标 ─────────────────────────────────────────────────────────────────

def _avg_return(close: pd.Series, days: int) -> float | None:
    """最近 days 个交易日的平均日收益率。"""
    c = close.dropna().sort_index()
    rets = c.pct_change().dropna().tail(days)
    if len(rets) < max(3, days // 2):
        return None
    return float(rets.mean())


def _total_return(close: pd.Series, days: int) -> float | None:
    """最近 days 个交易日的区间总收益率（用于热力图）。"""
    c = close.dropna().sort_index()
    if len(c) < days + 1:
        return None
    start = float(c.iloc[-(days + 1)])
    end   = float(c.iloc[-1])
    return (end / start - 1) if start > 0 else None


def _decay_return(close: pd.Series, window: int, decay: float) -> float | None:
    """指数衰减加权日收益率（近期权重高）。"""
    c = close.dropna().sort_index()
    rets = c.pct_change().dropna().tail(window).values
    if len(rets) < max(5, window // 4):
        return None
    n = len(rets)
    w = np.array([decay ** (n - 1 - i) for i in range(n)])
    return float(np.dot(w, rets) / w.sum())


def _vol_30d(close: pd.Series) -> float | None:
    c = close.dropna().sort_index()
    r = c.pct_change().dropna().tail(30)
    return float(r.std(ddof=0) * math.sqrt(252)) if len(r) >= 15 else None


def _drawdown_10d(close: pd.Series) -> float | None:
    c = close.dropna().sort_index().tail(10)
    if len(c) < 2:
        return None
    high = float(c.max())
    return float(c.iloc[-1] / high - 1) if high > 0 else None


def _price_vs_ma(close: pd.Series, period: int = 20) -> float | None:
    c = close.dropna().sort_index()
    if len(c) < period:
        return None
    return float(c.iloc[-1] / c.tail(period).mean() - 1)


def _zscore(s: pd.Series) -> pd.Series:
    s = pd.to_numeric(s, errors="coerce")
    mu, sigma = s.mean(skipna=True), s.std(skipna=True, ddof=0)
    if pd.isna(sigma) or sigma == 0:
        return pd.Series(np.zeros(len(s)), index=s.index)
    return (s - mu) / sigma


# ─── 价格下载 ─────────────────────────────────────────────────────────────────

def fetch_histories(tickers: list[str], period: str = FETCH_PERIOD) -> dict[str, pd.Series]:
    """
    批量下载收盘价历史。
    返回 {yf_ticker: pd.Series(close, index=DatetimeIndex)}。
    CASH ticker 跳过（不需要历史）。
    """
    import config
    real_tickers = [t for t in tickers if t.upper() != config.CASH_TICKER]
    if not real_tickers:
        return {}

    try:
        raw = yf.download(
            real_tickers,
            period=period,
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception:
        return {}

    result: dict[str, pd.Series] = {}

    if len(real_tickers) == 1:
        # single ticker: raw has flat columns
        close = raw["Close"] if "Close" in raw.columns else raw.iloc[:, 0]
        result[real_tickers[0]] = close.dropna()
    else:
        close_df = raw["Close"] if "Close" in raw.columns else raw
        for t in real_tickers:
            if t in close_df.columns:
                result[t] = close_df[t].dropna()

    return result


# ─── 单股指标 ─────────────────────────────────────────────────────────────────

def calc_metrics(ticker: str, display: str, close: pd.Series,
                 window: int = DEFAULT_WINDOW,
                 decay: float = DEFAULT_DECAY) -> dict | None:
    """
    计算单只股票的所有动量指标。数据不足时返回 None。
    """
    c = close.dropna().sort_index()
    if len(c) < 22:
        return None

    r5  = _avg_return(c, 5)
    r10 = _avg_return(c, 10)
    r20 = _avg_return(c, 20)
    if None in (r5, r10, r20):
        return None

    score_a = _decay_return(c, window, decay)
    if score_a is None:
        return None

    score_b = (MULTI_WEIGHTS[5] * r5 + MULTI_WEIGHTS[10] * r10 + MULTI_WEIGHTS[20] * r20)
    accel   = r5 - r20   # 正=加速 🟢，负=减速 🔴

    # 多周期区间收益（用于热力图）
    returns_by_period = {}
    for p in PERIODS:
        returns_by_period[f"ret_{p}d"] = _total_return(c, p)

    return {
        "ticker":        ticker,
        "display":       display,
        "latest_close":  round(float(c.iloc[-1]), 2),
        "latest_date":   c.index[-1].date().isoformat(),
        # 评分原料
        "score_a":       score_a,
        "score_b":       score_b,
        "accel":         accel,
        "avg_r5":        r5,
        "avg_r10":       r10,
        "avg_r20":       r20,
        # 风险
        "vol_30d":       _vol_30d(c),
        "drawdown_10d":  _drawdown_10d(c),
        "ma20_dev":      _price_vs_ma(c, 20),
        # 热力图数据
        **returns_by_period,
    }


# ─── 批量评分（持仓用）────────────────────────────────────────────────────────

def score_holdings(portfolio: dict,
                   window: int = DEFAULT_WINDOW,
                   decay: float = DEFAULT_DECAY) -> pd.DataFrame:
    """
    对 portfolio["accounts"] 中所有持仓下载历史并计算动量指标。
    返回按综合得分排序的 DataFrame。
    """
    import config

    # 收集持仓：{yf_ticker: display_name}（去重，CASH 排除）
    ticker_map: dict[str, str] = {}
    for acc in portfolio.get("accounts", []):
        for pos in acc.get("positions", []):
            yf_t = pos["yf_ticker"]
            if yf_t.upper() != config.CASH_TICKER:
                ticker_map[yf_t] = pos["display"]

    histories = fetch_histories(list(ticker_map.keys()))

    rows = []
    for yf_t, display in ticker_map.items():
        close = histories.get(yf_t)
        if close is None or len(close) < 22:
            continue
        m = calc_metrics(yf_t, display, close, window, decay)
        if m:
            rows.append(m)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    # Z-score 综合得分
    df["z_a"]   = _zscore(df["score_a"])
    df["z_b"]   = _zscore(df["score_b"])
    df["z_vol"] = _zscore(df["vol_30d"])
    df["composite"] = (0.55 * df["z_a"] + 0.45 * df["z_b"] - 0.08 * df["z_vol"].fillna(0))

    # 趋势方向标签
    def _direction(a):
        if pd.isna(a):     return "→"
        if a > 0.0003:     return "↑↑" if a > 0.001 else "↑"
        if a < -0.0003:    return "↓↓" if a < -0.001 else "↓"
        return "→"

    df["direction"] = df["accel"].apply(_direction)

    return df.sort_values("composite", ascending=False).reset_index(drop=True)


# ─── 批量评分（Watch List 用）────────────────────────────────────────────────

def score_ticker_list(tickers: list[str],
                      labels: dict[str, str] | None = None,
                      window: int = DEFAULT_WINDOW,
                      decay: float = DEFAULT_DECAY) -> pd.DataFrame:
    """
    对平铺的 ticker 列表评分（不需要 portfolio dict 结构）。
    labels: {yf_ticker: display_name}，可选；未提供则直接用 ticker 作为显示名。
    返回按综合得分排序的 DataFrame（与 score_holdings 结构一致）。
    """
    if labels is None:
        labels = {}

    import config
    real = [t for t in tickers if t.upper() != config.CASH_TICKER]
    histories = fetch_histories(real)

    rows = []
    for t in real:
        close = histories.get(t)
        if close is None or len(close) < 22:
            continue
        m = calc_metrics(t, labels.get(t, t), close, window, decay)
        if m:
            rows.append(m)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["z_a"]   = _zscore(df["score_a"])
    df["z_b"]   = _zscore(df["score_b"])
    df["z_vol"] = _zscore(df["vol_30d"])
    df["composite"] = (0.55 * df["z_a"] + 0.45 * df["z_b"] - 0.08 * df["z_vol"].fillna(0))

    def _direction(a):
        if pd.isna(a):  return "→"
        if a > 0.001:   return "↑↑"
        if a > 0.0003:  return "↑"
        if a < -0.001:  return "↓↓"
        if a < -0.0003: return "↓"
        return "→"

    df["direction"] = df["accel"].apply(_direction)
    df["rank"]      = range(1, len(df) + 1)

    return df.sort_values("composite", ascending=False).reset_index(drop=True)
