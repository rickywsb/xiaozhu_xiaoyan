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


# ─── EMA 量能评分（0-100 + 红绿灯）─────────────────────────────────────────────
EMA_SPANS = (10, 20, 60)          # 短 / 中 / 中长


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _clip(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def ema_momentum(close: pd.Series,
                 spans: tuple[int, int, int] = EMA_SPANS) -> dict | None:
    """
    基于 EMA 的量能评分（0-100 + 🟢🟡🔴）。数据不足返回 None。

    四个维度加权：
      ① 位置 30%   现价 vs EMA中期（站上/跌破，±5% 映射满分）
      ② 排列 30%   EMA短>中>长 多头排列（两条不等式各半）
      ③ 斜率 25%   EMA中期近 5 日斜率（±3% 映射满分）
      ④ 乖离 15%   过度正乖离惩罚（>8% 起扣分，防追高）
    """
    c = close.dropna().sort_index()
    s_span, m_span, l_span = spans
    if len(c) < m_span + 6:
        return None

    ema_s = _ema(c, s_span)
    ema_m = _ema(c, m_span)
    ema_l = _ema(c, l_span)

    price = float(c.iloc[-1])
    e_s, e_m, e_l = float(ema_s.iloc[-1]), float(ema_m.iloc[-1]), float(ema_l.iloc[-1])
    if e_m <= 0:
        return None

    dev_m = price / e_m - 1                       # 现价对 EMA 中期的乖离

    # ① 位置
    pos_score = _clip(50 + dev_m / 0.05 * 50, 0, 100)
    # ② 排列
    up = int(e_s > e_m) + int(e_m > e_l)
    align_score = up / 2 * 100
    # ③ 斜率（EMA 中期近 5 日变化）
    slope = float(ema_m.iloc[-1] / ema_m.iloc[-6] - 1)
    slope_score = _clip(50 + slope / 0.03 * 50, 0, 100)
    # ④ 乖离惩罚（正乖离 >8% 起扣，>20% 归零）
    over = max(0.0, dev_m - 0.08)
    dev_score = _clip(100 - over / 0.12 * 100, 0, 100)

    score = round(0.30 * pos_score + 0.30 * align_score
                  + 0.25 * slope_score + 0.15 * dev_score)

    if score >= 70:
        light = "🟢"
    elif score >= 40:
        light = "🟡"
    else:
        light = "🔴"

    align_txt = "多头排列" if up == 2 else ("空头排列" if up == 0 else "均线纠缠")
    slope_txt = "上行" if slope > 0.005 else ("下行" if slope < -0.005 else "走平")
    pos_txt = f"站上EMA{m_span}" if price >= e_m else f"跌破EMA{m_span}"
    hot = "·乖离过大防追高" if dev_m > 0.15 else ""
    state = f"{align_txt}·{pos_txt}·{slope_txt}{hot}"

    return {
        "ema_score": score,
        "light":     light,
        "state":     state,
        "price":     round(price, 2),
        "ema_mid":   round(e_m, 2),
        "dev":       round(dev_m, 4),      # 乖离（小数）
        "slope":     round(slope, 4),      # 中期斜率（5 日，小数）
        "align":     align_txt,
    }


def score_holdings_ema(portfolio: dict,
                       spans: tuple[int, int, int] = EMA_SPANS) -> pd.DataFrame:
    """对持仓做 EMA 量能评分，返回按分数降序的 DataFrame。"""
    import config

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
        if close is None:
            continue
        r = ema_momentum(close, spans)
        if r is None:
            continue
        rows.append({"ticker": yf_t, "display": display, **r})

    if not rows:
        return pd.DataFrame()

    return pd.DataFrame(rows).sort_values("ema_score", ascending=False).reset_index(drop=True)


# ─── Fibonacci 回撤持仓预警 ───────────────────────────────────────────────────
FIB_RATIOS   = (0.236, 0.382, 0.5, 0.618, 0.786)   # 标准回撤比例
FIB_KEY      = (0.382, 0.5, 0.618, 0.786)           # 关键支撑/阻力位
FIB_NEAR     = 0.02                                 # 贴近关键位阈值（2%）
FIB_LOOKBACK = 120                                  # 波段高低点回看（交易日，约半年）


def fib_signal(close: pd.Series, lookback: int = FIB_LOOKBACK) -> dict | None:
    """
    基于近期波段高低点计算 Fibonacci 回撤位，判断现价所处位置并给出预警。

    逻辑：
      · 取 lookback 窗口内的波段高 / 低点，按先后顺序判断趋势方向；
      · 先低后高 = 上升趋势 → 从高点向下画回撤（fib 位为支撑）；
      · 先高后低 = 下降趋势 → 从低点向上画反弹（fib 位为阻力）；
      · retr = 回撤/反弹进度（0=贴波段极值，1=回到起点，>1=突破起点）；
      · 触发预警：破位(retr>0.786) / 贴近关键 fib 位(±2%) / 强势贴高(retr≤0.15)。

    数据不足返回 None。
    """
    c = close.dropna().sort_index()
    if len(c) < 40:
        return None
    win = c.tail(lookback)
    hi = float(win.max())
    lo = float(win.min())
    hi_idx = win.idxmax()
    lo_idx = win.idxmin()
    if hi <= lo:
        return None

    rng   = hi - lo
    price = float(c.iloc[-1])
    uptrend = lo_idx < hi_idx          # 先低后高 → 上升趋势后的回调

    if uptrend:
        retr   = (hi - price) / rng
        levels = [(r, hi - rng * r) for r in FIB_RATIOS]
        kind, trend_txt = "支撑", "回调"
    else:
        retr   = (price - lo) / rng
        levels = [(r, lo + rng * r) for r in FIB_RATIOS]
        kind, trend_txt = "阻力", "反弹"
    retr = _clip(retr, 0.0, 2.0)

    nearest_r, nearest_p = min(levels, key=lambda x: abs(price - x[1]))
    dist = (price - nearest_p) / price if price > 0 else 0.0   # 现价距最近 fib 位（正=在其上方）
    near_key = abs(dist) <= FIB_NEAR and nearest_r in FIB_KEY

    # 分档信号灯
    if retr <= 0.236:
        light, zone = "🟢", "浅回撤·强势"
    elif retr <= 0.5:
        light, zone = "🟡", "健康回撤区"
    elif retr <= 0.786:
        light, zone = "🟡", "深回撤·临界"
    else:
        light, zone = "🔴", "破位·弱势"

    # 触发判定 + 类别（供预警清单筛选）
    trigger, category = False, ""
    if retr > 0.786:
        trigger, category, light = True, "破位预警", "🔴"
    elif near_key:
        trigger, category = True, f"贴近{nearest_r:.1%}{kind}"
    elif retr <= 0.15 and uptrend:
        trigger, category = True, "强势贴高"

    near_txt = f"·贴近{nearest_r:.1%}{kind}" if near_key else ""
    signal = f"{trend_txt}·{zone}{near_txt}"

    return {
        "fib_light":     light,
        "fib_signal":    signal,
        "category":      category,
        "trigger":       trigger,
        "retr":          round(retr, 3),          # 回撤/反弹比例
        "price":         round(price, 2),
        "swing_high":    round(hi, 2),
        "swing_low":     round(lo, 2),
        "nearest_fib":   f"{nearest_r:.1%}",
        "nearest_price": round(nearest_p, 2),
        "dist":          round(dist, 4),           # 距最近 fib 位（小数）
        "uptrend":       uptrend,
    }


def fib_alerts(portfolio: dict, lookback: int = FIB_LOOKBACK) -> pd.DataFrame:
    """对持仓计算 Fib 回撤信号，返回 DataFrame（触发预警的排在最前）。"""
    import config

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
        if close is None:
            continue
        r = fib_signal(close, lookback)
        if r is None:
            continue
        rows.append({"ticker": yf_t, "display": display, **r})

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    # 触发的优先，再按回撤深度降序（破位/深回撤在前）
    return df.sort_values(["trigger", "retr"], ascending=[False, False]).reset_index(drop=True)


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
