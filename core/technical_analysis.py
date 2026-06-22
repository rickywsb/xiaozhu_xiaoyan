"""core/technical_analysis.py — 个股技术指标计算与 K 线图构建"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import yfinance as yf
from plotly.subplots import make_subplots

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ─── 数据获取 ─────────────────────────────────────────────────────────────────

def get_ohlcv(ticker: str, period: str = "6mo", interval: str = "1d") -> pd.DataFrame:
    """
    获取 OHLCV 数据，返回带 Date 列的 DataFrame。
    失败时返回空 DataFrame。
    """
    try:
        raw = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=False)
    except Exception:
        return pd.DataFrame()

    if raw is None or raw.empty:
        return pd.DataFrame()

    df = raw.reset_index()
    # 统一日期列名
    date_col = "Date" if "Date" in df.columns else df.columns[0]
    df = df.rename(columns={date_col: "Date"})
    keep = ["Date", "Open", "High", "Low", "Close", "Volume"]
    df = df[[c for c in keep if c in df.columns]].dropna(subset=["Close"])
    return df.reset_index(drop=True)


# ─── 指标计算 ─────────────────────────────────────────────────────────────────

def calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI。"""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def calc_macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
              ) -> tuple[pd.Series, pd.Series, pd.Series]:
    """返回 (macd_line/DIF, signal_line/DEA, histogram)。"""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def calc_bollinger(close: pd.Series, period: int = 20, std: float = 2.0
                   ) -> tuple[pd.Series, pd.Series, pd.Series]:
    """返回 (upper, mid, lower)。"""
    mid = close.rolling(period, min_periods=period // 2).mean()
    sd = close.rolling(period, min_periods=period // 2).std(ddof=0)
    upper = mid + std * sd
    lower = mid - std * sd
    return upper, mid, lower


# ─── 图表构建 ─────────────────────────────────────────────────────────────────

def build_candlestick_chart(
    df: pd.DataFrame,
    ticker: str,
    mas: list[int] | None = None,
    show_volume: bool = True,
    show_rsi: bool = True,
    show_macd: bool = False,
    show_bollinger: bool = False,
) -> go.Figure:
    """
    构建多子图 K 线图：主图(蜡烛+MA+布林) / 成交量 / RSI / MACD。
    """
    if mas is None:
        mas = [5, 20, 60]

    close = df["Close"]

    # 决定子图行数与高度
    rows = 1
    row_heights = [0.55]
    titles = [f"{ticker}  K线图"]
    vol_row = rsi_row = macd_row = None

    if show_volume:
        rows += 1; vol_row = rows; row_heights.append(0.15); titles.append("成交量")
    if show_rsi:
        rows += 1; rsi_row = rows; row_heights.append(0.18); titles.append("RSI(14)")
    if show_macd:
        rows += 1; macd_row = rows; row_heights.append(0.18); titles.append("MACD")

    # 归一化高度
    total = sum(row_heights)
    row_heights = [h / total for h in row_heights]

    fig = make_subplots(
        rows=rows, cols=1, shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=row_heights,
        subplot_titles=titles,
    )

    # 主图：蜡烛
    fig.add_trace(go.Candlestick(
        x=df["Date"], open=df["Open"], high=df["High"],
        low=df["Low"], close=df["Close"],
        name="K线",
        increasing_line_color="#26a641", decreasing_line_color="#d73a4a",
    ), row=1, col=1)

    # MA 叠加
    ma_colors = {5: "#E8A84C", 20: "#4C9BE8", 60: "#B44CE8", 120: "#888888"}
    for m in mas:
        ma = close.rolling(m, min_periods=max(2, m // 2)).mean()
        fig.add_trace(go.Scatter(
            x=df["Date"], y=ma, name=f"MA{m}",
            line=dict(width=1.3, color=ma_colors.get(m, "#999999")),
        ), row=1, col=1)

    # 布林带
    if show_bollinger:
        up, mid, low = calc_bollinger(close)
        fig.add_trace(go.Scatter(x=df["Date"], y=up, name="BB上轨",
                                 line=dict(width=1, color="rgba(150,150,150,0.6)", dash="dot")),
                      row=1, col=1)
        fig.add_trace(go.Scatter(x=df["Date"], y=low, name="BB下轨",
                                 line=dict(width=1, color="rgba(150,150,150,0.6)", dash="dot"),
                                 fill="tonexty", fillcolor="rgba(150,150,150,0.06)"),
                      row=1, col=1)

    # 成交量
    if vol_row:
        vol_colors = ["#26a641" if c >= o else "#d73a4a"
                      for o, c in zip(df["Open"], df["Close"])]
        fig.add_trace(go.Bar(x=df["Date"], y=df["Volume"], name="成交量",
                             marker_color=vol_colors, showlegend=False),
                      row=vol_row, col=1)

    # RSI
    if rsi_row:
        rsi = calc_rsi(close)
        fig.add_trace(go.Scatter(x=df["Date"], y=rsi, name="RSI",
                                 line=dict(color="#A0529E", width=1.5), showlegend=False),
                      row=rsi_row, col=1)
        fig.add_hline(y=70, line=dict(color="rgba(215,58,74,0.5)", dash="dash"), row=rsi_row, col=1)
        fig.add_hline(y=30, line=dict(color="rgba(38,166,65,0.5)", dash="dash"), row=rsi_row, col=1)
        fig.update_yaxes(range=[0, 100], row=rsi_row, col=1)

    # MACD
    if macd_row:
        dif, dea, hist = calc_macd(close)
        hist_colors = ["#26a641" if v >= 0 else "#d73a4a" for v in hist]
        fig.add_trace(go.Bar(x=df["Date"], y=hist, name="MACD柱",
                             marker_color=hist_colors, showlegend=False),
                      row=macd_row, col=1)
        fig.add_trace(go.Scatter(x=df["Date"], y=dif, name="DIF",
                                 line=dict(color="#4C9BE8", width=1.2)),
                      row=macd_row, col=1)
        fig.add_trace(go.Scatter(x=df["Date"], y=dea, name="DEA",
                                 line=dict(color="#E8A84C", width=1.2)),
                      row=macd_row, col=1)

    fig.update_layout(
        height=260 + 180 * (rows - 1),
        margin=dict(t=40, b=20, l=20, r=20),
        xaxis_rangeslider_visible=False,
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", y=1.02, x=0),
        hovermode="x unified",
    )
    # 隐藏非交易日空隙（仅对日线）
    fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])])
    for r in range(1, rows + 1):
        fig.update_yaxes(showgrid=True, gridcolor="rgba(128,128,128,0.12)", row=r, col=1)

    return fig
