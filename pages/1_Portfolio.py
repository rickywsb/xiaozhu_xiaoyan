"""pages/1_Portfolio.py — 持仓净值页面（Phase 1 + Phase 2）"""

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
from core.price_updater import load_cache, update_all_prices
from core.github_storage import sync_to_github

SECTORS    = ["光", "存", "配置", "半导体", "其他", "期权", "现金"]
CURRENCIES = ["USD", "CASH", "TWD", "GBp", "KRW", "HKD", "EUR", "CNY"]

# ─── 工具函数 ──────────────────────────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def _load_portfolio() -> dict:
    return json.loads(config.PORTFOLIO_PATH.read_text(encoding="utf-8"))


def _portfolio_to_edit_df(portfolio: dict) -> pd.DataFrame:
    rows = [
        {
            "显示名":    pos.get("display", pos["yf_ticker"]),
            "YF Ticker": pos["yf_ticker"],
            "持股数":    pos.get("shares", 0.0),
            "板块":      pos.get("sector", "其他"),
            "货币":      pos.get("native_currency", "USD"),
            "备注":      pos.get("note", ""),
        }
        for account in portfolio.get("accounts", [])
        for pos in account.get("positions", [])
    ]
    return pd.DataFrame(rows)


def _manual_to_df(portfolio: dict) -> pd.DataFrame:
    rows = [{"名称": k, "市值 USD": float(v)}
            for k, v in portfolio.get("manual_values", {}).items()]
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=["名称", "市值 USD"])


def _save_portfolio(portfolio: dict, pos_df: pd.DataFrame, manual_df: pd.DataFrame) -> dict:
    valid = pos_df.dropna(subset=["YF Ticker"]).copy()
    valid = valid[valid["YF Ticker"].astype(str).str.strip() != ""]
    positions = []
    for _, r in valid.iterrows():
        display = str(r.get("显示名", "")).strip()
        ticker  = str(r["YF Ticker"]).strip()
        positions.append({
            "display":         display if display else ticker,
            "yf_ticker":       ticker,
            "shares":          float(r["持股数"]) if pd.notna(r.get("持股数")) else 0.0,
            "sector":          str(r.get("板块", "其他")).strip() or "其他",
            "native_currency": str(r.get("货币", "USD")).strip() or "USD",
            "note":            str(r.get("备注", "")).strip(),
        })
    manual_values: dict[str, float] = {}
    for _, r in manual_df.iterrows():
        name = str(r.get("名称", "")).strip()
        if name:
            manual_values[name] = float(r["市值 USD"]) if pd.notna(r.get("市值 USD")) else 0.0
    portfolio["accounts"][0]["positions"] = positions
    portfolio["manual_values"] = manual_values
    portfolio["last_modified"] = datetime.now().strftime("%Y-%m-%d")
    config.PORTFOLIO_PATH.write_text(
        json.dumps(portfolio, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return portfolio


def _build_view_df(portfolio: dict, prices: dict) -> tuple[pd.DataFrame, float]:
    rows = []
    for account in portfolio.get("accounts", []):
        for pos in account.get("positions", []):
            price   = prices.get(pos["yf_ticker"])
            shares  = pos.get("shares")
            mkt_val = round(price * shares, 2) if (price and shares) else None
            rows.append({
                "股票":     pos["display"],
                "板块":     pos["sector"],
                "持股数":   shares,
                "现价 USD": price,
                "市值 USD": mkt_val,
                "货币":     pos.get("native_currency", "USD"),
                "备注":     pos.get("note", ""),
            })
    for name, val in portfolio.get("manual_values", {}).items():
        rows.append({
            "股票": name, "板块": "期权",
            "持股数": None, "现价 USD": None, "市值 USD": float(val),
            "货币": "USD", "备注": "手动",
        })
    df = pd.DataFrame(rows)
    total = df["市值 USD"].sum(skipna=True)
    df["占比"] = df["市值 USD"].apply(lambda v: v / total * 100 if (pd.notna(v) and total > 0) else None)
    df = df.sort_values("市值 USD", ascending=False, na_position="last").reset_index(drop=True)
    return df, float(total)


# ─── 页面 ─────────────────────────────────────────────────────────────────────

st.title("💼 持仓净值")
portfolio = _load_portfolio()
cache     = load_cache()

# 顶栏
col_info, col_btn = st.columns([5, 1])
with col_info:
    if cache:
        rates = cache.get("fx_rates", {})
        st.caption(
            f"📅 最后更新: **{cache['updated_at']}**　　"
            + "  ".join(f"{k}={v:.4f}" for k, v in rates.items())
        )
    else:
        st.caption("⚠️ 尚未获取价格，请点击右侧「一键更新」")
with col_btn:
    if st.button("🔄 一键更新价格", type="primary", use_container_width=True):
        with st.spinner("正在获取最新价格…（约 20-30 秒）"):
            cache = update_all_prices(portfolio)
            st.cache_data.clear()
        if cache.get("failed"):
            st.warning(f"⚠️ 失败: {', '.join(cache['failed'])}")
        else:
            st.success(f"✅ {len(cache['prices'])} 只全部更新成功")
        st.rerun()

tab_view, tab_edit = st.tabs(["📊 持仓概览", "✏️ 编辑持仓"])

# ═══════════════════════════════════════════════════
# TAB 1 — 持仓概览
# ═══════════════════════════════════════════════════
with tab_view:
    if cache is None:
        st.info("👆 点击「🔄 一键更新价格」获取当前行情后，持仓数据将自动显示。")
        st.stop()

    df, total_nav = _build_view_df(portfolio, cache["prices"])

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("📊 总净值 (USD)", f"${total_nav:,.0f}")
    k2.metric("📋 持仓数量", len(df))
    top_row = df.iloc[0]
    k3.metric("🏆 最大持仓",
        f"{top_row['股票']}  {top_row['占比']:.1f}%" if pd.notna(top_row["占比"]) else top_row["股票"])
    failed = cache.get("failed", [])
    k4.metric("⚠️ 获取失败", len(failed),
        delta=", ".join(failed) if failed else None, delta_color="inverse")

    st.divider()
    st.subheader("持仓明细")
    all_sectors = ["全部"] + sorted(df["板块"].dropna().unique().tolist())
    sel = st.selectbox("筛选板块", all_sectors, label_visibility="collapsed")
    disp = df if sel == "全部" else df[df["板块"] == sel]
    st.dataframe(
        disp[["股票", "板块", "持股数", "现价 USD", "市值 USD", "占比", "货币", "备注"]],
        column_config={
            "现价 USD": st.column_config.NumberColumn("现价 USD", format="$%.2f"),
            "市值 USD": st.column_config.NumberColumn("市值 USD", format="$%,.0f"),
            "占比":     st.column_config.ProgressColumn(
                "占比", format="%.1f%%", min_value=0,
                max_value=float(disp["占比"].max(skipna=True)),  # already in %
            ),
            "持股数":   st.column_config.NumberColumn("持股数", format="%.4g"),
        },
        use_container_width=True, hide_index=True, height=500,
    )

    st.divider()
    chart_l, chart_r = st.columns(2)
    with chart_l:
        st.subheader("板块分布")
        sec_df = df.groupby("板块")["市值 USD"].sum().reset_index().sort_values("市值 USD", ascending=False)
        color_map = {k: v for k, v in config.SECTOR_COLORS.items() if k in sec_df["板块"].values}
        fig_pie = px.pie(sec_df, values="市值 USD", names="板块",
                         color="板块", color_discrete_map=color_map, hole=0.38)
        fig_pie.update_traces(
            textposition="auto",
            textinfo="percent+label",
            insidetextorientation="auto",
        )
        fig_pie.update_layout(
            showlegend=True,
            legend=dict(orientation="v", x=1.0, y=0.5),
            margin=dict(t=10, b=10, l=10, r=120),
            height=380,
            uniformtext_minsize=9,
            uniformtext_mode="hide",
        )
        st.plotly_chart(fig_pie, use_container_width=True)
    with chart_r:
        st.subheader("市值 Top 15")
        top15 = df.dropna(subset=["市值 USD"]).head(15).sort_values("市值 USD")
        fig_bar = go.Figure(go.Bar(
            x=top15["市值 USD"], y=top15["股票"], orientation="h",
            marker_color=[config.SECTOR_COLORS.get(s, "#888") for s in top15["板块"]],
            text=top15["市值 USD"].apply(lambda v: f"${v:,.0f}"),
            textposition="outside",
            hovertemplate="%{y}: $%{x:,.0f}<extra></extra>",
        ))
        fig_bar.update_layout(
            xaxis_title=None, yaxis_title=None,
            margin=dict(t=10,b=10,l=10,r=80), height=380,
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
            xaxis=dict(showgrid=True, gridcolor="rgba(128,128,128,0.2)"),
        )
        st.plotly_chart(fig_bar, use_container_width=True)

    st.divider()
    st.subheader("板块汇总")
    summary = (df.groupby("板块").agg(持仓数=("股票","count"), 市值=("市值 USD","sum"))
               .reset_index().sort_values("市值", ascending=False))
    summary["占比"] = summary["市值"] / total_nav * 100
    st.dataframe(summary,
        column_config={
            "市值": st.column_config.NumberColumn("市值 USD", format="$%,.0f"),
            "占比": st.column_config.ProgressColumn(
                "占比", format="%.1f%%", min_value=0,
                max_value=float(summary["占比"].max()),
            ),
        },
        hide_index=True, use_container_width=True)

# ═══════════════════════════════════════════════════
# TAB 2 — 编辑持仓
# ═══════════════════════════════════════════════════
with tab_edit:
    st.subheader("📋 股票持仓")
    st.caption(
        "• **YF Ticker** 为 yfinance 真实代码（如 `MRVL`、`IQE.L`、`000660.KS`）。  \n"
        "• **现金仓位**：YF Ticker 填 `CASH`，货币选 `CASH`，持股数填金额(USD)，板块选「现金」。  \n"
        "• 最左侧 ☑ 勾选行后，工具栏🗑可删除；底部 **＋** 可新增行。  \n"
        "• **板块** 可下拉选择预设，也可**直接输入自定义名称**。  \n"
        "• 编辑完成后点击下方按钮保存。"
    )
    edit_df = _portfolio_to_edit_df(portfolio)
    edited_pos = st.data_editor(
        edit_df,
        num_rows="dynamic",
        column_config={
            "显示名": st.column_config.TextColumn(
                "显示名", help="表格中展示的名称（留空则自动用 YF Ticker）"),
            "YF Ticker": st.column_config.TextColumn(
                "YF Ticker ✱", required=True, help="yfinance 查询用的真实 ticker"),
            "持股数": st.column_config.NumberColumn("持股数", min_value=0, format="%.4g"),
            "板块": st.column_config.SelectboxColumn(
                "板块", options=SECTORS, help="下拉选择，或直接输入自定义板块名"),
            "货币": st.column_config.SelectboxColumn(
                "货币", options=CURRENCIES, help="原始计价货币，自动换算为 USD"),
            "备注": st.column_config.TextColumn("备注"),
        },
        use_container_width=True,
        height=520,
        key="pos_editor",
    )

    st.subheader("🗒️ 手动价值（期权 / 其他）")
    st.caption("无法自动抓取的仓位（如期权），直接填入当前市值 (USD)。")
    manual_df = _manual_to_df(portfolio)
    edited_manual = st.data_editor(
        manual_df,
        num_rows="dynamic",
        column_config={
            "名称":     st.column_config.TextColumn("名称 ✱", required=True),
            "市值 USD": st.column_config.NumberColumn("市值 USD", format="$%,.2f", min_value=0),
        },
        use_container_width=True,
        key="manual_editor",
    )

    st.divider()
    btn_l, btn_r = st.columns(2)
    with btn_l:
        if st.button("💾 保存持仓配置", use_container_width=True):
            _save_portfolio(portfolio, edited_pos, edited_manual)
            st.cache_data.clear()
            ok, msg = sync_to_github(
                config.PORTFOLIO_PATH, "data/portfolio.json",
                "feat: update portfolio via UI",
            )
            if ok is True:
                st.success(f"✅ 持仓已保存并同步到 GitHub！")
            elif ok is False:
                st.warning(f"✅ 本地已保存，GitHub 同步失败：{msg}")
            else:
                st.success("✅ 持仓配置已保存！切换到「📊 持仓概览」查看。")
            st.rerun()
    with btn_r:
        if st.button("🚀 保存 + 获取最新价格", type="primary", use_container_width=True):
            updated = _save_portfolio(portfolio, edited_pos, edited_manual)
            st.cache_data.clear()
            ok, msg = sync_to_github(
                config.PORTFOLIO_PATH, "data/portfolio.json",
                "feat: update portfolio via UI",
            )
            with st.spinner("正在获取最新价格…（约 20-30 秒）"):
                cache = update_all_prices(updated)
            if cache.get("failed"):
                st.warning(f"已更新，⚠️ 失败: {', '.join(cache['failed'])}")
            else:
                label = "并同步到 GitHub" if ok is True else ""
                st.success(f"✅ 持仓已保存{label}，{len(cache['prices'])} 只价格全部更新！")
            st.rerun()
