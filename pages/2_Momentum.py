"""pages/2_Momentum.py — 量能健康报告 + 技术图表"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
from core.daily_momentum import PERIODS, score_holdings, fetch_histories, calc_metrics, DEFAULT_DECAY, DEFAULT_WINDOW
from core.technical_analysis import get_ohlcv, build_candlestick_chart
from core import accumulation as accum

# ─── 工具 ─────────────────────────────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def _load_portfolio() -> dict:
    return json.loads(config.PORTFOLIO_PATH.read_text(encoding="utf-8"))


@st.cache_data(show_spinner="⚡ 正在计算量能数据…（首次约 15 秒）", ttl=1800)
def _cached_score(portfolio_hash: str, window: int, decay: float) -> pd.DataFrame:
    """缓存 30 分钟；portfolio_hash 变化时自动失效。"""
    portfolio = json.loads(config.PORTFOLIO_PATH.read_text(encoding="utf-8"))
    return score_holdings(portfolio, window=window, decay=decay)


def _portfolio_hash(portfolio: dict) -> str:
    tickers = sorted(
        pos["yf_ticker"]
        for acc in portfolio.get("accounts", [])
        for pos in acc.get("positions", [])
    )
    return "|".join(tickers)


@st.cache_data(show_spinner=False, ttl=3600)
def _cached_ohlcv(ticker: str, period: str) -> pd.DataFrame:
    return get_ohlcv(ticker, period=period)


@st.cache_data(show_spinner="🏦 正在扫描主力吸筹信号…", ttl=1800)
def _cached_accum(portfolio_hash: str) -> pd.DataFrame:
    portfolio = json.loads(config.PORTFOLIO_PATH.read_text(encoding="utf-8"))
    return accum.scan_holdings(portfolio)


@st.cache_data(show_spinner=False, ttl=21600)
def _cached_13f(ticker: str) -> dict | None:
    return accum.institutional_summary(ticker)


def _chart_options(portfolio: dict) -> list[tuple[str, str]]:
    """返回 [(label, ticker), …]：持仓 ∪ 关注列表。"""
    opts: dict[str, str] = {}
    for acc in portfolio.get("accounts", []):
        for pos in acc.get("positions", []):
            t = pos["yf_ticker"].upper()
            name = pos.get("name") or pos.get("display") or t
            opts[t] = f"{name} ({t})"
    # 关注列表
    if config.WATCHLIST_PATH.exists():
        try:
            wl = json.loads(config.WATCHLIST_PATH.read_text(encoding="utf-8"))
            for t in wl.get("watchlist", []):
                tu = t.upper().strip()
                opts.setdefault(tu, f"⭐ {tu}")
        except Exception:
            pass
    return [(label, tic) for tic, label in sorted(opts.items())]


# ─── 颜色工具 ─────────────────────────────────────────────────────────────────
_GREEN = "#26a641"
_RED   = "#d73a4a"
_GRAY  = "#8b949e"

def _accel_color(v):
    if pd.isna(v): return _GRAY
    return _GREEN if v > 0 else _RED


# ─── 页面 ─────────────────────────────────────────────────────────────────────
st.title("📊 量能健康报告")

portfolio = _load_portfolio()

# 侧边栏参数
with st.sidebar:
    st.subheader("⚙️ 参数")
    decay  = st.slider("衰减因子 decay", 0.88, 0.99, DEFAULT_DECAY, 0.01,
                       help="越大→近期权重越集中")
    window = st.slider("回看窗口 window", 20, 60, DEFAULT_WINDOW, 5,
                       help="计算衰减得分的交易日数")
    st.caption("修改参数后点击「⚡ 刷新量能」重新计算。")

# 顶栏
col_title, col_btn = st.columns([5, 1])
with col_btn:
    refresh = st.button("⚡ 刷新量能", type="primary", use_container_width=True)

if refresh:
    st.cache_data.clear()
    st.rerun()

# 计算
ph = _portfolio_hash(portfolio)
with st.spinner("正在加载量能数据…"):
    df = _cached_score(ph, window, decay)

tab_momentum, tab_accum, tab_chart = st.tabs(["📈 量能报告", "🏦 主力吸筹", "🕯 技术图表"])

# ═══════════════════════════════════════════════════════════════════════════════
# 技术图表 TAB （独立于量能数据，先渲染以避免量能 st.stop 影响）
# ═══════════════════════════════════════════════════════════════════════════════
# 主力吸筹 TAB （量价行为代理信号）
# ═══════════════════════════════════════════════════════════════════════════════
with tab_accum:
    st.subheader("🏦 主力/机构 吸筹·派发 信号")
    st.caption("基于日线量价行为的**代理信号**：CMF 资金流 / OBV 能量潮 / 量比 / "
               "涨跌量比 / MFI / 放量突破。⚠️ yfinance 无 Level-2 逐笔大单数据，"
               "本页为行为推断，非真实主力资金流向，仅供辅助判断。")

    accum_df = _cached_accum(ph)
    if accum_df.empty:
        st.warning("暂无有效数据，请检查持仓 ticker 或稍后重试。")
    else:
        n_buy = int((accum_df["评分"] >= 3).sum())
        n_sell = int((accum_df["评分"] <= -3).sum())
        n_neu = len(accum_df) - n_buy - n_sell
        m1, m2, m3 = st.columns(3)
        m1.metric("🟢 疑似吸筹", n_buy)
        m2.metric("🟡 中性", n_neu)
        m3.metric("🔴 疑似派发", n_sell)

        st.dataframe(
            accum_df.drop(columns=["_reasons"]).style.format({
                "量比": "{:.2f}", "CMF": "{:+.3f}", "涨跌量比": "{:.2f}",
                "MFI": "{:.0f}", "OBV斜率": "{:+.4f}",
            }, na_rep="—"),
            use_container_width=True, hide_index=True,
        )

        st.caption("**评分逻辑**：CMF>0.05 +2 / OBV上行 +1 / 涨跌量比>1.2 +1 / "
                   "放量上涨 +1 / 放量突破新高 +2 / MFI<20 +1；反向对称扣分。"
                   "评分 ≥3 判定吸筹，≤-3 判定派发。")

        with st.expander("📋 各股信号解读"):
            for _, r in accum_df.iterrows():
                st.markdown(f"**{r['股票']} ({r['代码']})** — {r['判定']}（评分 {r['评分']}）")
                if r["_reasons"]:
                    for rs in str(r["_reasons"]).split("；"):
                        if rs:
                            st.markdown(f"- {rs}")

    # ── 机构 13F 持仓（季度真实数据，作为参考）──────────────────────────────
    st.divider()
    st.subheader("🏛️ 机构 13F 持仓（季度参考）")
    st.caption("来自 SEC 13F 披露的**真实**机构持仓，但按季申报、滞后约 1~2 个月，"
               "适合看中长期机构态度，与上方实时量价信号互补。")

    accum_options = _chart_options(portfolio)
    if accum_options:
        labels_13f = [o[0] for o in accum_options]
        sel_label_13f = st.selectbox("选择标的查看机构持仓", labels_13f, index=0, key="inst_ticker")
        sel_13f_ticker = dict((l, t) for l, t in accum_options)[sel_label_13f]

        with st.spinner(f"加载 {sel_13f_ticker} 机构持仓…"):
            info = _cached_13f(sel_13f_ticker)

        if not info:
            st.info(f"暂无 {sel_13f_ticker} 的机构持仓数据（部分海外/小盘股 yfinance 无 13F）。")
        else:
            g1, g2, g3, g4 = st.columns(4)
            g1.metric("机构持股占比", f"{info['inst_pct']:.1%}" if info["inst_pct"] else "—")
            g2.metric("机构家数", f"{int(info['inst_count']):,}" if info["inst_count"] else "—")
            if info["net_pct"] is not None:
                arrow = "🟢 净增持" if info["net_pct"] > 0 else ("🔴 净减持" if info["net_pct"] < 0 else "持平")
                g3.metric("Top机构季度净增减", f"{info['net_pct']:+.1%}", delta=arrow,
                          help="Top 机构按持股份额加权的季度环比增减仓；>0=整体加仓")
            else:
                g3.metric("Top机构季度净增减", "—")
            g4.metric("数据截至", info["as_of"] or "—",
                      help=f"增持 {info['n_up']} 家 / 减持 {info['n_down']} 家"
                           if info["n_up"] is not None else "")

            if info["top"] is not None and not info["top"].empty:
                st.dataframe(
                    info["top"].style.format({
                        "持股占比": "{:.2%}", "股数": "{:,.0f}",
                        "市值": "${:,.0f}", "季度变化": "{:+.1%}",
                    }, na_rep="—"),
                    use_container_width=True, hide_index=True,
                )
                st.caption("「季度变化」为各机构相对上一季的持股变动；+100% 通常代表新建仓或翻倍。")

# ═══════════════════════════════════════════════════════════════════════════════
with tab_chart:
    st.subheader("🕯 个股技术分析")
    options = _chart_options(portfolio)
    if not options:
        st.info("暂无可选标的，请先在「投资组合」或「关注列表」中添加股票。")
    else:
        c1, c2 = st.columns([3, 2])
        with c1:
            labels = [o[0] for o in options]
            sel_label = st.selectbox("选择标的", labels, index=0, key="tech_ticker")
            sel_tech_ticker = dict((l, t) for l, t in options)[sel_label]
        with c2:
            period_label = st.radio(
                "周期", ["1M", "3M", "6M", "1Y"], index=2,
                horizontal=True, key="tech_period",
            )
        period_map = {"1M": "1mo", "3M": "3mo", "6M": "6mo", "1Y": "1y"}

        ind_cols = st.columns(4)
        show_vol  = ind_cols[0].checkbox("成交量", value=True, key="t_vol")
        show_rsi  = ind_cols[1].checkbox("RSI", value=True, key="t_rsi")
        show_macd = ind_cols[2].checkbox("MACD", value=False, key="t_macd")
        show_boll = ind_cols[3].checkbox("布林带", value=False, key="t_boll")

        with st.spinner(f"加载 {sel_tech_ticker} K线数据…"):
            ohlcv = _cached_ohlcv(sel_tech_ticker, period_map[period_label])

        if ohlcv.empty or len(ohlcv) < 5:
            st.warning(f"暂无 {sel_tech_ticker} 的历史 K 线数据。")
        else:
            fig = build_candlestick_chart(
                ohlcv, sel_tech_ticker,
                mas=[5, 20, 60],
                show_volume=show_vol,
                show_rsi=show_rsi,
                show_macd=show_macd,
                show_bollinger=show_boll,
            )
            st.plotly_chart(fig, use_container_width=True)

            # 简要快照
            last = ohlcv.iloc[-1]
            prev = ohlcv.iloc[-2]
            chg_pct = (last["Close"] / prev["Close"] - 1) * 100
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("最新收盘", f"${last['Close']:,.2f}", delta=f"{chg_pct:+.2f}%")
            k2.metric("当日最高", f"${last['High']:,.2f}")
            k3.metric("当日最低", f"${last['Low']:,.2f}")
            k4.metric("成交量", f"{last['Volume']:,.0f}")

# ═══════════════════════════════════════════════════════════════════════════════
# 量能报告 TAB
# ═══════════════════════════════════════════════════════════════════════════════
with tab_momentum:
    if df.empty:
        st.warning("数据不足，请检查持仓 ticker 是否正确，或点击「⚡ 刷新量能」重试。")
        st.stop()

    n_ok = len(df)
    n_total = sum(len(a["positions"]) for a in portfolio.get("accounts", []))
    st.caption(f"📅 基于缓存（30分钟内复用） | {n_ok}/{n_total} 只有效数据 | "
               f"decay={decay}  window={window}")

    st.divider()

    # ═══════════════════════════════════════════════════════════════════════════
    # ① 多周期收益热力图
    # ═══════════════════════════════════════════════════════════════════════════
    st.subheader("① 多周期收益热力图")
    st.caption("颜色：🟢 正收益 / 🔴 负收益。按综合得分由高到低排列。")

    period_cols = [f"ret_{p}d" for p in PERIODS]
    heat_df = df[["display"] + period_cols].copy()
    heat_df = heat_df.set_index("display")
    heat_df.columns = [f"{p}D" for p in PERIODS]

    # 转为百分比
    heat_pct = heat_df.multiply(100)

    # 颜色范围：对称区间
    abs_max = float(heat_pct.abs().max().max())
    abs_max = max(abs_max, 1.0)

    fig_heat = px.imshow(
        heat_pct,
        color_continuous_scale="RdYlGn",
        zmin=-abs_max, zmax=abs_max,
        aspect="auto",
        text_auto=".1f",
        labels={"color": "收益%"},
    )
    fig_heat.update_traces(textfont_size=11)
    fig_heat.update_layout(
        xaxis_title=None,
        yaxis_title=None,
        coloraxis_colorbar=dict(title="收益%", thickness=12),
        margin=dict(t=10, b=10, l=10, r=80),
        height=max(320, n_ok * 22),
    )
    st.plotly_chart(fig_heat, use_container_width=True)

    st.divider()

    # ═══════════════════════════════════════════════════════════════════════════
    # ② 综合动量得分排名
    # ═══════════════════════════════════════════════════════════════════════════
    st.subheader("② 综合动量得分排名")
    st.caption("横向柱：综合得分 (z-score)。颜色=动量方向：🟢加速 / 🔴减速。右标=趋势箭头。")

    rank_df = df[["display", "composite", "accel", "direction", "avg_r5", "avg_r20"]].copy()
    rank_df = rank_df.sort_values("composite")   # plotly horizontal bar: bottom=low

    colors = [_accel_color(v) for v in rank_df["accel"]]
    labels = [f"{d} {r5*100:+.2f}%"
              for d, r5 in zip(rank_df["direction"], rank_df["avg_r5"])]

    fig_rank = go.Figure(go.Bar(
        x=rank_df["composite"],
        y=rank_df["display"],
        orientation="h",
        marker_color=colors,
        text=labels,
        textposition="outside",
        hovertemplate=(
            "<b>%{y}</b><br>"
            "综合得分: %{x:.3f}<br>"
            "<extra></extra>"
        ),
    ))
    fig_rank.add_vline(x=0, line_color="rgba(128,128,128,0.5)", line_width=1)

    # 警告区阴影：底部 20%
    threshold = float(rank_df["composite"].quantile(0.20))
    fig_rank.add_vrect(
        x0=rank_df["composite"].min() - 0.1, x1=threshold,
        fillcolor="rgba(255,200,0,0.08)", line_width=0,
        annotation_text="⚠️ 预警区", annotation_position="top left",
    )

    fig_rank.update_layout(
        xaxis_title="综合得分 (z-score)",
        yaxis_title=None,
        margin=dict(t=10, b=10, l=10, r=120),
        height=max(320, n_ok * 22),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(showgrid=True, gridcolor="rgba(128,128,128,0.15)"),
    )
    st.plotly_chart(fig_rank, use_container_width=True)

    # 预警提示
    warn_df = df[df["composite"] < threshold]
    if not warn_df.empty:
        st.warning(
            "⚠️ **动量预警**（综合得分后 20%）：  \n"
            + "  ".join(f"`{r['display']}`" for _, r in warn_df.iterrows())
        )

    st.divider()

    # ═══════════════════════════════════════════════════════════════════════════
    # ③ 个股详情卡片
    # ═══════════════════════════════════════════════════════════════════════════
    st.subheader("③ 个股详情")
    st.caption("点击展开查看 60 日价格走势 + 动量加速度。")

    # 按得分分组展示（强→中→弱）
    top_n    = max(1, n_ok // 3)
    strong   = df.head(top_n)
    moderate = df.iloc[top_n: top_n * 2]
    weak     = df.tail(n_ok - top_n * 2)

    for group_label, group_df in [("🟢 强势", strong), ("🟡 中性", moderate), ("🔴 弱势 / 预警", weak)]:
        if group_df.empty:
            continue
        st.markdown(f"**{group_label}**")
        cols = st.columns(min(4, len(group_df)))
        for col_idx, (_, row) in enumerate(group_df.iterrows()):
            with cols[col_idx % len(cols)]:
                accel_str = f"{row['accel']*100:+.3f}%/d" if pd.notna(row['accel']) else "N/A"
                badge = row['direction']
                delta_color = "normal" if row['accel'] > 0 else "inverse"
                st.metric(
                    label=f"{badge} {row['display']}",
                    value=f"${row['latest_close']:,.2f}",
                    delta=accel_str,
                    delta_color=delta_color,
                )

    st.divider()

    # 展开个股走势
    selected = st.selectbox(
        "🔍 查看个股走势详情",
        options=df["display"].tolist(),
        index=0,
    )

    sel_row = df[df["display"] == selected].iloc[0]
    sel_ticker = sel_row["ticker"]

    with st.spinner(f"加载 {selected} 历史数据…"):
        hist = fetch_histories([sel_ticker], period="3mo")
        close = hist.get(sel_ticker)

    if close is not None and len(close) >= 5:
        close_df = close.reset_index()
        close_df.columns = ["date", "close"]
        close_df["ma20"] = close_df["close"].rolling(20, min_periods=5).mean()
        # 5日动量加速度（滚动）
        close_df["r5"]  = close_df["close"].pct_change(5).rolling(1).mean()
        close_df["r20"] = close_df["close"].pct_change(20).rolling(1).mean()
        close_df["accel_roll"] = (close_df["r5"] - close_df["r20"]) * 100

        fig_detail = go.Figure()
        # 收盘价
        fig_detail.add_trace(go.Scatter(
            x=close_df["date"], y=close_df["close"],
            name="收盘价", line=dict(color="#4C9BE8", width=2),
        ))
        # MA20
        fig_detail.add_trace(go.Scatter(
            x=close_df["date"], y=close_df["ma20"],
            name="MA20", line=dict(color="#E8844C", width=1.5, dash="dot"),
        ))
        # 动量加速度（右轴）
        fig_detail.add_trace(go.Bar(
            x=close_df["date"], y=close_df["accel_roll"],
            name="动量加速度%", yaxis="y2",
            marker_color=close_df["accel_roll"].apply(
                lambda v: "rgba(38,166,65,0.5)" if v >= 0 else "rgba(215,58,74,0.5)"
            ),
        ))
        fig_detail.update_layout(
            title=f"{selected}  ({sel_ticker})  方向: {sel_row['direction']}",
            yaxis=dict(title="价格 (USD)"),
            yaxis2=dict(title="加速度 (5d−20d) %", overlaying="y", side="right",
                        showgrid=False),
            legend=dict(orientation="h", y=1.08),
            hovermode="x unified",
            margin=dict(t=60, b=20, l=20, r=60),
            height=420,
            plot_bgcolor="rgba(0,0,0,0)",
            xaxis=dict(showgrid=True, gridcolor="rgba(128,128,128,0.15)"),
        )
        st.plotly_chart(fig_detail, use_container_width=True)
    else:
        st.info(f"暂无 {selected} 的历史价格数据。")

    # 指标明细
    with st.expander(f"📋 {selected} 指标明细"):
        metrics = {
            "最新收盘价": f"${sel_row['latest_close']:,.2f}",
            "综合得分 (z)": f"{sel_row['composite']:.4f}",
            "动量加速度/日": f"{sel_row['accel']*100:+.4f}%",
            "趋势方向": sel_row["direction"],
            "5日均日收益": f"{sel_row['avg_r5']*100:+.3f}%",
            "10日均日收益": f"{sel_row['avg_r10']*100:+.3f}%" if 'avg_r10' in sel_row else "N/A",
            "20日均日收益": f"{sel_row['avg_r20']*100:+.3f}%",
            "30日年化波动": f"{sel_row['vol_30d']*100:.1f}%" if pd.notna(sel_row.get('vol_30d')) else "N/A",
            "10日最大回撤": f"{sel_row['drawdown_10d']*100:.2f}%" if pd.notna(sel_row.get('drawdown_10d')) else "N/A",
            "距MA20偏离": f"{sel_row['ma20_dev']*100:+.2f}%" if pd.notna(sel_row.get('ma20_dev')) else "N/A",
        }
        st.table(pd.DataFrame(list(metrics.items()), columns=["指标", "值"]))
