"""pages/1_Portfolio.py — 持仓净值页面"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
from core.price_updater import load_cache, update_all_prices

# ─── 数据加载 ─────────────────────────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def _load_portfolio() -> dict:
    return json.loads(config.PORTFOLIO_PATH.read_text(encoding="utf-8"))


def _build_df(portfolio: dict, prices: dict) -> pd.DataFrame:
    """将持仓 + 价格合并为展示用 DataFrame。"""
    rows = []
    for account in portfolio.get("accounts", []):
        for pos in account.get("positions", []):
            price = prices.get(pos["yf_ticker"])
            shares = pos.get("shares")
            mkt_val = round(price * shares, 2) if (price and shares) else None
            rows.append({
                "账户":      account["name"],
                "股票":      pos["display"],
                "板块":      pos["sector"],
                "持股数":    shares,
                "现价 USD":  price,
                "市值 USD":  mkt_val,
                "货币":      pos.get("native_currency", "USD"),
                "备注":      pos.get("note", ""),
            })

    # 手工价值（期权等）
    for name, val in portfolio.get("manual_values", {}).items():
        rows.append({
            "账户":   "—",
            "股票":   name,
            "板块":   "期权",
            "持股数": None,
            "现价 USD": None,
            "市值 USD": val,
            "货币":   "USD",
            "备注":   "手动",
        })

    df = pd.DataFrame(rows)
    total = df["市值 USD"].sum(skipna=True)
    df["占比"] = df["市值 USD"].apply(
        lambda v: v / total if (pd.notna(v) and total > 0) else None
    )
    df = df.sort_values("市值 USD", ascending=False, na_position="last").reset_index(drop=True)
    return df, total


# ─── 页面布局 ─────────────────────────────────────────────────────────────────

st.title("💼 持仓净值")

portfolio = _load_portfolio()
cache = load_cache()

# —— 顶栏：更新状态 + 一键更新按钮 ——
col_info, col_btn = st.columns([5, 1])
with col_info:
    if cache:
        st.caption(f"📅 最后更新: **{cache['updated_at']}**　　"
                   f"FX: TWD={cache['fx_rates'].get('TWD', '—'):.4f}  "
                   f"KRW={cache['fx_rates'].get('KRW', '—'):.6f}  "
                   f"HKD={cache['fx_rates'].get('HKD', '—'):.4f}  "
                   f"EUR={cache['fx_rates'].get('EUR', '—'):.4f}  "
                   f"GBP={cache['fx_rates'].get('GBP', '—'):.4f}")
    else:
        st.caption("⚠️ 尚未获取价格，请点击右侧「一键更新」")

with col_btn:
    if st.button("🔄 一键更新价格", type="primary", use_container_width=True):
        with st.spinner("正在获取最新价格…（约 20-30 秒）"):
            cache = update_all_prices(portfolio)
            st.cache_data.clear()
        n_ok = len(cache["prices"])
        n_fail = len(cache.get("failed", []))
        if n_fail:
            st.warning(f"✅ {n_ok} 只更新成功，⚠️ 失败: {', '.join(cache['failed'])}")
        else:
            st.success(f"✅ {n_ok} 只全部更新成功")
        st.rerun()

# —— 无缓存时提示 ——
if cache is None:
    st.info("👆 点击「🔄 一键更新价格」获取当前行情后，持仓数据将自动显示。")
    st.stop()

# —— 构建数据 ——
df, total_nav = _build_df(portfolio, cache["prices"])

# ─── KPI 卡片 ─────────────────────────────────────────────────────────────────
st.divider()
k1, k2, k3, k4 = st.columns(4)
k1.metric("📊 总净值（USD）", f"${total_nav:,.0f}")
k2.metric("📋 持仓数量", len(df))
top_row = df.iloc[0]
k3.metric("🏆 最大持仓",
          f"{top_row['股票']}  {top_row['占比']:.1%}" if pd.notna(top_row['占比']) else top_row['股票'])
failed = cache.get("failed", [])
k4.metric("⚠️ 价格获取失败", len(failed),
          delta=", ".join(failed) if failed else None,
          delta_color="inverse")

st.divider()

# ─── 持仓明细表 ───────────────────────────────────────────────────────────────
st.subheader("持仓明细")

# 板块筛选
sectors = ["全部"] + sorted(df["板块"].dropna().unique().tolist())
sel_sector = st.selectbox("筛选板块", sectors, label_visibility="collapsed")
disp_df = df if sel_sector == "全部" else df[df["板块"] == sel_sector]

st.dataframe(
    disp_df[["股票", "板块", "持股数", "现价 USD", "市值 USD", "占比", "货币", "备注"]],
    column_config={
        "现价 USD": st.column_config.NumberColumn("现价 (USD)", format="$%.2f"),
        "市值 USD": st.column_config.NumberColumn("市值 (USD)", format="$%,.0f"),
        "占比":     st.column_config.ProgressColumn("占比", format="%.1f%%",
                                                    min_value=0, max_value=1),
        "持股数":   st.column_config.NumberColumn("持股数", format="%.4g"),
    },
    use_container_width=True,
    hide_index=True,
    height=520,
)

# ─── 可视化图表 ───────────────────────────────────────────────────────────────
st.divider()
chart_l, chart_r = st.columns(2)

# —— 左：板块分布饼图 ——
with chart_l:
    st.subheader("板块分布")
    sector_df = (
        df.groupby("板块")["市值 USD"]
        .sum()
        .reset_index()
        .sort_values("市值 USD", ascending=False)
    )
    fig_pie = px.pie(
        sector_df,
        values="市值 USD",
        names="板块",
        color="板块",
        color_discrete_map=config.SECTOR_COLORS,
        hole=0.38,
    )
    fig_pie.update_traces(textposition="outside", textinfo="percent+label")
    fig_pie.update_layout(
        showlegend=False,
        margin=dict(t=10, b=10, l=10, r=10),
        height=380,
    )
    st.plotly_chart(fig_pie, use_container_width=True)

# —— 右：市值 Top 15 横向柱状图 ——
with chart_r:
    st.subheader("市值 Top 15")
    top15 = df.dropna(subset=["市值 USD"]).head(15).copy()
    top15 = top15.sort_values("市值 USD")  # plotly 横向 bar 由下到上

    color_map = config.SECTOR_COLORS
    fig_bar = go.Figure(go.Bar(
        x=top15["市值 USD"],
        y=top15["股票"],
        orientation="h",
        marker_color=[color_map.get(s, "#888") for s in top15["板块"]],
        text=top15["市值 USD"].apply(lambda v: f"${v:,.0f}"),
        textposition="outside",
        hovertemplate="%{y}: $%{x:,.0f}<extra></extra>",
    ))
    fig_bar.update_layout(
        xaxis_title=None,
        yaxis_title=None,
        margin=dict(t=10, b=10, l=10, r=80),
        height=380,
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(showgrid=True, gridcolor="rgba(128,128,128,0.2)"),
    )
    st.plotly_chart(fig_bar, use_container_width=True)

# ─── 底部：板块汇总表 ─────────────────────────────────────────────────────────
st.divider()
st.subheader("板块汇总")
summary = (
    df.groupby("板块")
    .agg(持仓数=("股票", "count"), 市值=("市值 USD", "sum"))
    .reset_index()
    .sort_values("市值", ascending=False)
)
summary["占比"] = summary["市值"] / total_nav
st.dataframe(
    summary,
    column_config={
        "市值":  st.column_config.NumberColumn("市值 (USD)", format="$%,.0f"),
        "占比":  st.column_config.ProgressColumn("占比", format="%.1f%%", min_value=0, max_value=1),
    },
    hide_index=True,
    use_container_width=True,
)
