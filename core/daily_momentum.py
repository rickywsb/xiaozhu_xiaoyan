"""core/daily_momentum.py — 持仓量能评分（纯函数库，无 CLI）

从 new/daily_top10.py 提取核心逻辑，仅对给定 ticker 列表计算日动量指标，
不依赖全宇宙 universe 构建，响应速度快（~35 只持仓 < 15s）。
"""

import math
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ─── 默认参数 ──────────────────────────────────────────────────────────────────
DEFAULT_DECAY  = 0.94
DEFAULT_WINDOW = 40
PERIODS        = [5, 10, 20, 60]          # 热力图展示的多周期
MULTI_WEIGHTS  = {5: 0.50, 10: 0.30, 20: 0.20}

# 价格历史回看长度：1 年，保证 EMA60 充分收敛、6-1 月趋势动量可计算
FETCH_PERIOD = "1y"

# 中期趋势动量：过去 6 个月收益，跳过最近 1 个月（1 周~1 月收益有短期反转效应）
TREND_LOOKBACK = 126
TREND_SKIP     = 21

# 低流动性判定（近 60 日）：零成交天数 ≥3，或日均成交额中位数 < 100 万美元
ILLIQUID_ZERO_VOL_DAYS = 3
ILLIQUID_DOLLAR_VOL    = 1_000_000


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
    ticker_map = _holding_map(portfolio)

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
    ticker_map = _holding_map(portfolio)

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


# ─── Volume Profile 筹码分布预警（POC / VAH / VAL）─────────────────────────────
VP_LOOKBACK   = 120        # 回看交易日（约半年）
VP_BINS       = 24         # 价格分箱数
VP_VALUE_AREA = 0.70       # 价值区覆盖的成交量比例
VP_NEAR       = 0.03       # 贴近关键位阈值（3%）


def volume_profile(ohlcv: pd.DataFrame,
                   lookback: int = VP_LOOKBACK,
                   bins: int = VP_BINS,
                   va: float = VP_VALUE_AREA) -> dict | None:
    """
    成交量分布（volume-by-price）：按价格分箱累加成交量。
      · POC (Point of Control)  成交量最大的价位（最强磁吸/支撑阻力）
      · VAH / VAL               包住 va(默认70%)成交量的价值区上/下沿
    每日成交量在其 [Low, High] 区间内均摊到重叠的价格箱。数据不足返回 None。
    """
    df = ohlcv.dropna().tail(lookback)
    if len(df) < 30:
        return None
    lo = float(df["Low"].min())
    hi = float(df["High"].max())
    if hi <= lo:
        return None

    edges = np.linspace(lo, hi, bins + 1)
    vol = np.zeros(bins)
    for low, high, v in zip(df["Low"].to_numpy(), df["High"].to_numpy(), df["Volume"].to_numpy()):
        low, high, v = float(low), float(high), float(v)
        if v <= 0 or high < low:
            continue
        lo_i = int(np.searchsorted(edges, low,  side="right") - 1)
        hi_i = int(np.searchsorted(edges, high, side="right") - 1)
        lo_i = max(0, min(bins - 1, lo_i))
        hi_i = max(0, min(bins - 1, hi_i))
        vol[lo_i:hi_i + 1] += v / (hi_i - lo_i + 1)

    total = float(vol.sum())
    if total <= 0:
        return None

    centers = (edges[:-1] + edges[1:]) / 2
    poc_i = int(vol.argmax())
    poc = float(centers[poc_i])

    # 价值区：从 POC 向两侧扩张，直到累计成交量 ≥ va*total
    target = va * total
    lo_i = hi_i = poc_i
    acc = float(vol[poc_i])
    while acc < target and (lo_i > 0 or hi_i < bins - 1):
        left  = float(vol[lo_i - 1]) if lo_i > 0 else -1.0
        right = float(vol[hi_i + 1]) if hi_i < bins - 1 else -1.0
        if right >= left:
            hi_i += 1
            acc += float(vol[hi_i])
        else:
            lo_i -= 1
            acc += float(vol[lo_i])

    return {
        "poc":   poc,
        "vah":   float(edges[hi_i + 1]),
        "val":   float(edges[lo_i]),
        "low":   lo,
        "high":  hi,
        "hist":  vol.tolist(),
        "edges": edges.tolist(),
    }


def vp_signal(ohlcv: pd.DataFrame, lookback: int = VP_LOOKBACK) -> dict | None:
    """
    基于筹码分布判断现价相对价值区(VAH/VAL/POC)的位置并给出预警。

    采用**贴近触发**（只在价格真正与关键位交互时预警，避免趋势股价格远离
    历史价值区时误报"早已突破"）：现价落在 POC/VAH/VAL 任一位 ±VP_NEAR 内才触发。
      · 上破 VAH（价≥VAH 且贴近）→ 放量走强(🟢)
      · 测试 VAH（价<VAH 且贴近）→ 逼近上沿阻力(🟡)
      · 跌破 VAL（价≤VAL 且贴近）→ 跌出价值区(🔴)
      · 测试 VAL（价>VAL 且贴近）→ 回踩下沿支撑(🟡)
      · 回踩 POC（贴近 POC）→ 磁吸/决策区(🟡)
    远离所有关键位（延伸段/区间中部）不触发。
    """
    vp = volume_profile(ohlcv, lookback)
    if vp is None:
        return None
    price = float(ohlcv["Close"].dropna().iloc[-1])
    poc, vah, val = vp["poc"], vp["vah"], vp["val"]
    if poc <= 0 or price <= 0:
        return None

    d_poc = (price - poc) / price
    d_vah = (price - vah) / price
    d_val = (price - val) / price

    # 位置（上下文信息灯）
    if price > vah:
        light, zone = "🟢", "价值区上方"
    elif price < val:
        light, zone = "🔴", "价值区下方"
    else:
        light, zone = "🟡", "价值区内"

    # 最近的关键位
    levels = [("POC", poc, d_poc), ("VAH", vah, d_vah), ("VAL", val, d_val)]
    name, _lvl_p, lvl_d = min(levels, key=lambda x: abs(x[2]))

    trigger, category = False, ""
    if abs(lvl_d) <= VP_NEAR:
        trigger = True
        if name == "POC":
            category, light = "回踩POC", "🟡"
        elif name == "VAH":
            category, light = ("上破VAH", "🟢") if price >= vah else ("测试VAH", "🟡")
        else:  # VAL
            category, light = ("跌破VAL", "🔴") if price <= val else ("测试VAL", "🟡")

    signal = f"{zone}·贴近{name}" if trigger else f"{zone}·远离关键位"

    return {
        "vp_light":  light,
        "vp_signal": signal,
        "category":  category,
        "trigger":   trigger,
        "nearest":   name,
        "price":     round(price, 2),
        "poc":       round(poc, 2),
        "vah":       round(vah, 2),
        "val":       round(val, 2),
        "d_poc":     round(d_poc, 4),       # 现价距 POC（小数，正=上方）
        "d_near":    round(lvl_d, 4),        # 现价距最近关键位（小数）
        "va_width":  round((vah - val) / poc, 4),   # 价值区宽度（相对 POC，越小越密集）
    }


def vp_alerts(portfolio: dict, lookback: int = VP_LOOKBACK) -> pd.DataFrame:
    """对持仓计算筹码分布信号，返回 DataFrame（触发预警的排在最前）。"""
    ticker_map = _holding_map(portfolio)

    histories = fetch_ohlcv_histories(list(ticker_map.keys()), complete_bars_only=True)

    rows = []
    for yf_t, display in ticker_map.items():
        ohlcv = histories.get(yf_t)
        if ohlcv is None:
            continue
        r = vp_signal(ohlcv, lookback)
        if r is None:
            continue
        rows.append({"ticker": yf_t, "display": display, **r})

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    # 触发优先，再按距最近关键位由近到远（真正贴近的在前）
    df["_absd"] = df["d_near"].abs()
    df = df.sort_values(["trigger", "_absd"], ascending=[False, True]).drop(columns="_absd")
    return df.reset_index(drop=True)


# ─── 相对强度 RS（vs 板块基准 + vs 大盘）──────────────────────────────────────
RS_WINDOWS = (21, 63)      # 相对收益回看：1 月 / 3 月（交易日）


def relative_strength(portfolio: dict,
                      windows: tuple[int, int] = RS_WINDOWS) -> pd.DataFrame:
    """
    计算各持仓相对**所属板块基准**（config.SECTOR_BENCHMARKS，按持仓 sector）
    以及相对大盘（config.DEFAULT_BENCHMARK）的相对强度，统一按美元收益计算。
      · benchmark      该持仓使用的板块基准
      · rs_1m / rs_3m  个股区间收益 − 板块基准区间收益（正=跑赢）；
                       基准上市不足 3 月时 rs_3m 为空，综合分与标签改用 rs_1m
      · rs_mkt_3m      个股 3 月收益 − 大盘 3 月收益
      · rs_score       综合相对收益（1 月 0.4 + 3 月 0.6）
      · rs_rank        组合内百分位排名（0-100，越高越领涨）
      · rs_tag         领涨(≥+10%) / 同步 / 落后(≤-10%)（按 3 月相对板块基准，缺则按 1 月）
    基准数据缺失时跳过对应持仓；全部缺失返回空 DataFrame（调用方优雅降级）。
    """
    import config

    ticker_map = _holding_map(portfolio)
    sector_of: dict[str, str] = {}
    for acc in portfolio.get("accounts", []):
        for pos in acc.get("positions", []):
            sector_of[pos["yf_ticker"]] = pos.get("sector", "")
    bench_of = {t: config.SECTOR_BENCHMARKS.get(sector_of.get(t, ""), config.DEFAULT_BENCHMARK)
                for t in ticker_map}
    benches = sorted(set(bench_of.values()) | {config.DEFAULT_BENCHMARK})

    hist = fetch_histories(list(ticker_map), usd=True)
    bench_hist = fetch_histories(benches)
    w_short, w_long = windows[0], windows[-1]
    bench_ret = {b: (_total_return(c, w_short), _total_return(c, w_long))
                 for b, c in bench_hist.items()}
    mkt_long = bench_ret.get(config.DEFAULT_BENCHMARK, (None, None))[1]

    rows = []
    for t, disp in ticker_map.items():
        c = hist.get(t)
        b_short, b_long = bench_ret.get(bench_of[t], (None, None))
        if c is None:
            continue
        s_short = _total_return(c, w_short)
        s_long  = _total_return(c, w_long)
        rs_1m = (s_short - b_short) if (s_short is not None and b_short is not None) else None
        rs_3m = (s_long - b_long) if (s_long is not None and b_long is not None) else None
        if rs_1m is None and rs_3m is None:
            continue
        if rs_1m is not None and rs_3m is not None:
            rs_score = 0.4 * rs_1m + 0.6 * rs_3m
        else:
            rs_score = rs_3m if rs_3m is not None else rs_1m
        rows.append({"ticker": t, "display": disp, "benchmark": bench_of[t],
                     "rs_1m": rs_1m, "rs_3m": rs_3m, "rs_score": rs_score,
                     "rs_mkt_3m": (s_long - mkt_long)
                     if (s_long is not None and mkt_long is not None) else None})

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["rs_rank"] = (df["rs_score"].rank(pct=True) * 100).round().astype(int)

    def _tag(x: float) -> str:
        if x >= 0.10:
            return "领涨"
        if x <= -0.10:
            return "落后"
        return "同步"

    df["rs_tag"] = df["rs_3m"].fillna(df["rs_1m"]).apply(_tag)
    return df.sort_values("rs_score", ascending=False).reset_index(drop=True)


# ─── MACD 背驰预警（缠论精髓·反转早期预警）─────────────────────────────────────
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
MACD_LOOKBACK = 120        # 回看交易日（约半年）
MACD_PIVOT_K  = 5          # 局部极值窗口半径（±k 根内最高/最低才算枢轴）
MACD_RECENT   = 30         # 最新枢轴须落在最近 N 根内才视为有效预警


def _macd(close: pd.Series,
          fast: int = MACD_FAST, slow: int = MACD_SLOW, signal: int = MACD_SIGNAL):
    ef = close.ewm(span=fast, adjust=False).mean()
    es = close.ewm(span=slow, adjust=False).mean()
    dif  = ef - es                                   # DIF（快线）
    dea  = dif.ewm(span=signal, adjust=False).mean() # DEA（慢线/信号）
    hist = (dif - dea) * 2                            # 柱（国内惯例 ×2）
    return dif, dea, hist


def _find_pivots(values: np.ndarray, k: int, kind: str) -> list[int]:
    """返回局部极值下标：kind='high' 为局部高点，'low' 为局部低点。"""
    n = len(values)
    piv = []
    for i in range(k, n - k):
        seg = values[i - k:i + k + 1]
        v = values[i]
        if kind == "high" and v == seg.max() and v > values[i - 1] and v >= values[i + 1]:
            piv.append(i)
        elif kind == "low" and v == seg.min() and v < values[i - 1] and v <= values[i + 1]:
            piv.append(i)
    return piv


def macd_divergence(close: pd.Series,
                    lookback: int = MACD_LOOKBACK,
                    k: int = MACD_PIVOT_K) -> dict | None:
    """
    MACD 背驰检测（缠论"背驰"可计算部分）：
      · 顶背驰🔴  价格创新高但 DIF 不创新高（且 DIF>0）→ 涨势衰竭，警惕
      · 底背驰🟢  价格创新低但 DIF 不创新低（且 DIF<0）→ 跌势衰竭，关注
    仅当最新枢轴落在最近 MACD_RECENT 根内才触发（保证时效）。数据不足返回 None。
    """
    c = close.dropna().sort_index()
    if len(c) < MACD_SLOW + MACD_SIGNAL + 2 * k + 5:
        return None

    win  = c.tail(lookback)
    dif, dea, hist = _macd(win)
    price = win.to_numpy()
    difv  = dif.to_numpy()
    n = len(price)

    cur_price = float(price[-1])
    cur_dif   = float(difv[-1])
    cur_dea   = float(dea.iloc[-1])
    cur_hist  = float(hist.iloc[-1])

    signal_type, light, trigger, note = "无背驰", "⚪", False, "近期无明显背驰"

    highs = _find_pivots(price, k, "high")
    if len(highs) >= 2:
        i1, i2 = highs[-2], highs[-1]
        if (n - 1 - i2) <= MACD_RECENT and price[i2] > price[i1] \
                and difv[i2] < difv[i1] and difv[i2] > 0:
            signal_type, light, trigger = "顶背驰", "🔴", True
            note = "价格创新高但 MACD 动能走弱，涨势或衰竭"

    if not trigger:
        lows = _find_pivots(price, k, "low")
        if len(lows) >= 2:
            j1, j2 = lows[-2], lows[-1]
            if (n - 1 - j2) <= MACD_RECENT and price[j2] < price[j1] \
                    and difv[j2] > difv[j1] and difv[j2] < 0:
                signal_type, light, trigger = "底背驰", "🟢", True
                note = "价格创新低但 MACD 动能转强，跌势或衰竭"

    macd_cross = "金叉" if cur_dif > cur_dea else "死叉"
    zero_state = "零轴上方" if cur_dif > 0 else "零轴下方"

    return {
        "div_light":  light,
        "signal":     signal_type,
        "trigger":    trigger,
        "note":       note,
        "price":      round(cur_price, 2),
        "dif":        round(cur_dif, 3),
        "dea":        round(cur_dea, 3),
        "macd_hist":  round(cur_hist, 3),
        "macd_state": f"{macd_cross}·{zero_state}",
    }


def divergence_alerts(portfolio: dict, lookback: int = MACD_LOOKBACK) -> pd.DataFrame:
    """对持仓做 MACD 背驰检测，返回 DataFrame（触发预警的排在最前，顶背驰在最前）。"""
    ticker_map = _holding_map(portfolio)

    histories = fetch_histories(list(ticker_map.keys()))

    rows = []
    for t, disp in ticker_map.items():
        c = histories.get(t)
        if c is None:
            continue
        r = macd_divergence(c, lookback)
        if r is None:
            continue
        rows.append({"ticker": t, "display": disp, **r})

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    order = {"顶背驰": 0, "底背驰": 1, "无背驰": 2}
    df["_o"] = df["signal"].map(order).fillna(3)
    df = df.sort_values(["trigger", "_o"], ascending=[False, True]).drop(columns="_o")
    return df.reset_index(drop=True)


# ─── 价格下载（共享缓存）──────────────────────────────────────────────────────
_OHLCV_FIELDS = ["Open", "High", "Low", "Close", "Volume"]
_DL_TTL = 900          # 同一批 ticker 15 分钟内复用，避免一个页面对同一持仓下载 5~6 次（Yahoo 429）
_dl_cache: dict[tuple, tuple[float, dict[str, pd.DataFrame]]] = {}

# 交易所后缀 → (时区, 收盘时, 收盘分)；用于剔除盘中未走完的 K 线
_EXCHANGE_CLOSE = {
    "":     ("America/New_York", 16, 0),
    ".KS":  ("Asia/Seoul",       15, 30),
    ".HK":  ("Asia/Hong_Kong",   16, 10),
    ".L":   ("Europe/London",    16, 35),
    ".ST":  ("Europe/Stockholm", 17, 30),
    ".PA":  ("Europe/Paris",     17, 35),
    ".TWO": ("Asia/Taipei",      13, 30),
}


def _holding_map(portfolio: dict) -> dict[str, str]:
    """持仓 {yf_ticker: display}（去重，排除现金）。"""
    import config
    out: dict[str, str] = {}
    for acc in portfolio.get("accounts", []):
        for pos in acc.get("positions", []):
            t = pos["yf_ticker"]
            if t.upper() != config.CASH_TICKER:
                out[t] = pos.get("display", t)
    return out


def _download(tickers: list[str], period: str) -> dict[str, pd.DataFrame]:
    """
    批量下载日线 OHLCV（auto_adjust），返回 {原样 ticker: DataFrame}。
    yfinance 返回的列名是大写（如 "Disk" → "DISK"），这里按大写匹配后映射回原 ticker。
    成功结果缓存 _DL_TTL 秒。
    """
    key = (tuple(sorted(set(tickers))), period)
    hit = _dl_cache.get(key)
    if hit and time.time() - hit[0] < _DL_TTL:
        return hit[1]

    try:
        raw = yf.download(list(key[0]), period=period, auto_adjust=True,
                          progress=False, threads=True)
    except Exception:
        return {}
    if raw is None or raw.empty:
        return {}

    out: dict[str, pd.DataFrame] = {}
    if isinstance(raw.columns, pd.MultiIndex):
        cols = {str(c).upper(): c for c in raw.columns.get_level_values(1).unique()}
        for t in key[0]:
            col = cols.get(t.upper())
            if col is None:
                continue
            sub = raw.xs(col, axis=1, level=1)
            df = sub[[f for f in _OHLCV_FIELDS if f in sub.columns]].dropna(subset=["Close"])
            if not df.empty:
                out[t] = df
    elif len(key[0]) == 1:
        df = raw[[f for f in _OHLCV_FIELDS if f in raw.columns]].dropna(subset=["Close"])
        if not df.empty:
            out[key[0][0]] = df

    if out:
        _dl_cache[key] = (time.time(), out)
    return out


def _drop_partial_bar(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """交易所尚未收盘时剔除当日 K 线：盘中成交量不完整，会让量比/CMF/放量突破系统性失真。"""
    if df.empty:
        return df
    suffix = "." + ticker.rsplit(".", 1)[1].upper() if "." in ticker else ""
    tz, hh, mm = _EXCHANGE_CLOSE.get(suffix, _EXCHANGE_CLOSE[""])
    now = datetime.now(ZoneInfo(tz))
    close_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(minutes=20)
    if pd.Timestamp(df.index[-1]).date() == now.date() and now < close_dt:
        return df.iloc[:-1]
    return df


def _fx_per_usd(currencies: set[str], period: str) -> dict[str, pd.Series]:
    """{货币: 每 1 USD 兑换的本币数量 时间序列}（Yahoo "KRW=X"）。GBp 按 GBP 取。"""
    pairs = {cur: f"{'GBP' if cur == 'GBp' else cur}=X" for cur in currencies}
    if not pairs:
        return {}
    data = _download(list(set(pairs.values())), period)
    return {cur: data[p]["Close"] for cur, p in pairs.items() if p in data}


def _to_usd(close: pd.Series, ticker: str, fx: dict[str, pd.Series]) -> pd.Series:
    """本币收盘价 → 美元（按各交易日汇率）。汇率缺失时退回本币。"""
    import config
    cur = config.CURRENCY_MAP.get(ticker)
    rate = fx.get(cur) if cur else None
    if rate is None:
        return close
    rate = rate.reindex(rate.index.union(close.index)).ffill().reindex(close.index)
    usd = close / rate
    if cur == "GBp":
        usd = usd / 100.0
    return usd.dropna()


def fetch_ohlcv_histories(tickers: list[str], period: str = FETCH_PERIOD,
                          complete_bars_only: bool = False) -> dict[str, pd.DataFrame]:
    """
    批量下载 OHLCV 历史（本币）。返回 {yf_ticker: DataFrame[Open, High, Low, Close, Volume]}。
    complete_bars_only=True 时剔除盘中未收盘的当日 K 线（量能类指标用）。
    """
    import config
    real = [t for t in tickers if t.upper() != config.CASH_TICKER]
    if not real:
        return {}
    data = _download(real, period)
    if complete_bars_only:
        data = {t: _drop_partial_bar(df, t) for t, df in data.items()}
    return data


def fetch_histories(tickers: list[str], period: str = FETCH_PERIOD,
                    usd: bool = False) -> dict[str, pd.Series]:
    """
    批量下载收盘价历史。返回 {yf_ticker: pd.Series(close)}。
    usd=True 时非美元标的按当日汇率换算为美元（收益/相对强度类指标用，
    与美元计价的实际盈亏一致）；技术位（EMA/Fib/MACD）用本币，与行情软件图表对齐。
    """
    import config
    data = fetch_ohlcv_histories(tickers, period)
    closes = {t: df["Close"] for t, df in data.items()}
    if usd:
        fx = _fx_per_usd({config.CURRENCY_MAP[t] for t in closes if t in config.CURRENCY_MAP}, period)
        closes = {t: _to_usd(c, t, fx) for t, c in closes.items()}
    return closes


def missing_tickers(portfolio: dict) -> list[str]:
    """持仓中下载不到数据的 ticker（页面据此提示，而不是悄悄从表里消失）。"""
    hm = _holding_map(portfolio)
    got = fetch_ohlcv_histories(list(hm))
    return [t for t in hm if t not in got]


def _liquidity(ohlcv: pd.DataFrame, usd_close: pd.Series) -> dict:
    """近 60 日流动性：零成交天数、成交额中位数（美元）、是否低流动性。"""
    seg = ohlcv.tail(60)
    vol = seg["Volume"] if "Volume" in seg.columns else pd.Series(dtype=float)
    zero_days = int((vol.fillna(0) <= 0).sum())
    dollar = (usd_close.reindex(seg.index) * vol).dropna()
    med = float(dollar.median()) if len(dollar) else None
    illiquid = zero_days >= ILLIQUID_ZERO_VOL_DAYS or (med is not None and med < ILLIQUID_DOLLAR_VOL)
    return {"zero_vol_days": zero_days, "dollar_vol_med": med, "illiquid": bool(illiquid)}


# ─── 单股指标 ─────────────────────────────────────────────────────────────────

def _trend_6_1(close: pd.Series) -> tuple[float | None, float | None]:
    """
    中期趋势动量：t-126 → t-21 的区间收益（跳过最近 1 个月）。
    返回 (收益, 风险调整后收益)；风险调整 = 收益 / (日波动 × √区间天数)。
    """
    c = close.dropna().sort_index()
    if len(c) < TREND_LOOKBACK + 1:
        return None, None
    start = float(c.iloc[-(TREND_LOOKBACK + 1)])
    end   = float(c.iloc[-(TREND_SKIP + 1)])
    if start <= 0:
        return None, None
    ret = end / start - 1
    rets = c.pct_change().dropna().iloc[-TREND_LOOKBACK:-TREND_SKIP]
    sd = float(rets.std(ddof=0)) if len(rets) > 20 else 0.0
    ra = ret / (sd * math.sqrt(len(rets))) if sd > 0 else None
    return ret, ra


def calc_metrics(ticker: str, display: str, close: pd.Series,
                 window: int = DEFAULT_WINDOW,
                 decay: float = DEFAULT_DECAY) -> dict | None:
    """
    计算单只股票的所有动量指标。数据不足时返回 None。
    close 应为美元价格（见 fetch_histories(usd=True)），保证跨市场可比。
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

    # 加速度：近 5 日 vs 之前 15 日（两段不重叠，避免 r20 里包含 r5 稀释信号）
    rets = c.pct_change().dropna()
    prior = rets.iloc[-20:-5]
    accel = r5 - float(prior.mean()) if len(prior) >= 8 else r5 - r20   # 正=加速 🟢，负=减速 🔴

    # 短期热度按波动率调整：否则高波动 / 杠杆产品天然排在最前
    vol = _vol_30d(c)
    sd_d = vol / math.sqrt(252) if vol else None
    heat_a = score_a / sd_d if sd_d else None
    heat_b = score_b / sd_d if sd_d else None

    trend_6_1, trend_ra = _trend_6_1(c)

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
        "heat_a":        heat_a,
        "heat_b":        heat_b,
        "trend_6_1":     trend_6_1,
        "trend_ra":      trend_ra,
        "accel":         accel,
        "avg_r5":        r5,
        "avg_r10":       r10,
        "avg_r20":       r20,
        # 风险
        "vol_30d":       vol,
        "drawdown_10d":  _drawdown_10d(c),
        "ma20_dev":      _price_vs_ma(c, 20),
        # 热力图数据
        **returns_by_period,
    }


def _direction(a) -> str:
    if pd.isna(a):  return "→"
    if a > 0.001:   return "↑↑"
    if a > 0.0003:  return "↑"
    if a < -0.001:  return "↓↓"
    if a < -0.0003: return "↓"
    return "→"


def _score_frame(labels: dict[str, str], window: int, decay: float) -> pd.DataFrame:
    """
    对 {yf_ticker: display} 计算动量并打分，返回按 composite 降序的 DataFrame。

    composite（组合内 z-score，只代表**相对**排名）：
      · heat    短期热度 = 衰减加权日收益 & 5/10/20 日加权日收益，均按 30 日波动率调整
      · trend_z 中期趋势 = 6-1 月动量（风险调整），跳过最近 1 个月
      composite = 0.5 × heat + 0.5 × trend_z；上市不足 ~7 个月无趋势分时只用 heat。
    另附 leverage（杠杆倍数）/ illiquid（低流动性）/ data_lag_days（数据滞后天数）标记。
    """
    import config

    ohlcv = fetch_ohlcv_histories(list(labels))
    fx = _fx_per_usd({config.CURRENCY_MAP[t] for t in ohlcv if t in config.CURRENCY_MAP},
                     FETCH_PERIOD)

    rows = []
    for t, disp in labels.items():
        df = ohlcv.get(t)
        if df is None or len(df) < 22:
            continue
        usd_close = _to_usd(df["Close"], t, fx)
        m = calc_metrics(t, disp, usd_close, window, decay)
        if not m:
            continue
        m.update(_liquidity(df, usd_close))
        m["leverage"] = config.LEVERAGED_TICKERS.get(t.upper(), 1)
        m["currency"] = config.CURRENCY_MAP.get(t, "USD")
        rows.append(m)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["heat"]    = _zscore(0.55 * _zscore(df["heat_a"]) + 0.45 * _zscore(df["heat_b"]))
    df["trend_z"] = _zscore(df["trend_ra"])
    df["composite"] = np.where(df["trend_z"].notna(),
                               0.5 * df["heat"] + 0.5 * df["trend_z"],
                               df["heat"])
    df["direction"] = df["accel"].apply(_direction)

    dates = pd.to_datetime(df["latest_date"])
    df["data_lag_days"] = (dates.max() - dates).dt.days

    df = df.sort_values("composite", ascending=False).reset_index(drop=True)
    df["rank"] = range(1, len(df) + 1)
    return df


# ─── 批量评分（持仓用）────────────────────────────────────────────────────────

def score_holdings(portfolio: dict,
                   window: int = DEFAULT_WINDOW,
                   decay: float = DEFAULT_DECAY) -> pd.DataFrame:
    """对 portfolio["accounts"] 中所有持仓计算动量指标，返回按综合得分排序的 DataFrame。"""
    return _score_frame(_holding_map(portfolio), window, decay)


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
    import config
    labels = labels or {}
    real = [t for t in tickers if t.upper() != config.CASH_TICKER]
    return _score_frame({t: labels.get(t, t) for t in real}, window, decay)


# ─── 绝对强弱背景（组合 vs 大盘）──────────────────────────────────────────────
CONTEXT_BENCHMARKS = ("SPY", "QQQ", "SOXX")


def benchmark_returns(benchmarks: tuple[str, ...] = CONTEXT_BENCHMARKS) -> dict[str, dict]:
    """{基准: {ret_5d, ret_20d}}，给"领涨/领跌"这类相对排名补上绝对参照。"""
    hist = fetch_histories(list(benchmarks))
    out = {}
    for b in benchmarks:
        c = hist.get(b)
        if c is None:
            continue
        out[b] = {"ret_5d": _total_return(c, 5), "ret_20d": _total_return(c, 20)}
    return out


def absolute_summary(df: pd.DataFrame) -> dict:
    """组合整体绝对表现：20 日收益中位数、上涨家数（与 composite 相对排名互补）。"""
    if df.empty or "ret_20d" not in df.columns:
        return {}
    r20 = df["ret_20d"].dropna()
    r5 = df["ret_5d"].dropna()
    return {
        "n":            int(len(df)),
        "median_20d":   float(r20.median()) if len(r20) else None,
        "median_5d":    float(r5.median()) if len(r5) else None,
        "n_up_20d":     int((r20 > 0).sum()),
        "n_up_5d":      int((r5 > 0).sum()),
    }
