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
from core.value_history import append_value, load_history, HISTORY_PATH
from core.options import build_occ
from core import snapshots

SECTORS    = ["光", "存", "配置", "半导体", "其他", "期权", "现金"]
CURRENCIES = ["USD", "CASH", "TWD", "GBp", "KRW", "HKD", "EUR", "CNY"]
OPT_TYPES  = ["call", "put"]

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


def _options_to_df(portfolio: dict) -> pd.DataFrame:
    rows = [
        {
            "显示名":  o.get("display", o.get("contract", "")),
            "标的":    o.get("underlying", ""),
            "到期":    o.get("expiry", ""),
            "方向":    o.get("type", "call"),
            "行权价":  float(o.get("strike", 0) or 0),
            "张数":    float(o.get("contracts", 1) or 1),
            "板块":    o.get("sector", "期权"),
            "手动价":  o.get("manual_mark"),
            "备注":    o.get("note", ""),
        }
        for o in portfolio.get("options", [])
    ]
    cols = ["显示名", "标的", "到期", "方向", "行权价", "张数", "板块", "手动价", "备注"]
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=cols)


def _save_portfolio(portfolio: dict, pos_df: pd.DataFrame,
                    manual_df: pd.DataFrame, options_df: pd.DataFrame | None = None) -> dict:
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

    if options_df is not None:
        options = []
        for _, r in options_df.iterrows():
            underlying = str(r.get("标的", "")).strip().upper()
            expiry     = str(r.get("到期", "")).strip()
            otype      = str(r.get("方向", "call")).strip().lower() or "call"
            if not underlying or not expiry:
                continue
            try:
                strike = float(r.get("行权价", 0) or 0)
                contract = build_occ(underlying, expiry, otype, strike)
            except Exception:
                continue
            display = str(r.get("显示名", "")).strip()
            manual_mark = r.get("手动价")
            options.append({
                "display":     display if display else f"{underlying} {strike:g} {otype.title()}",
                "underlying":  underlying,
                "expiry":      expiry,
                "type":        otype,
                "strike":      strike,
                "contract":    contract,
                "contracts":   float(r["张数"]) if pd.notna(r.get("张数")) else 1.0,
                "sector":      str(r.get("板块", "期权")).strip() or "期权",
                "manual_mark": float(manual_mark) if pd.notna(manual_mark) else None,
                "note":        str(r.get("备注", "")).strip(),
            })
        portfolio["options"] = options

    portfolio["last_modified"] = datetime.now().strftime("%Y-%m-%d")
    config.PORTFOLIO_PATH.write_text(
        json.dumps(portfolio, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return portfolio


def _build_view_df(portfolio: dict, cache: dict) -> tuple[pd.DataFrame, float]:
    prices  = cache.get("prices", {}) if cache else {}
    options = cache.get("options", {}) if cache else {}
    rows = []
    for account in portfolio.get("accounts", []):
        for pos in account.get("positions", []):
            price   = prices.get(pos["yf_ticker"])
            shares  = pos.get("shares")
            mkt_val = round(price * shares, 2) if (price and shares) else None
            rows.append({
                "_key":     pos["yf_ticker"],
                "股票":     pos["display"],
                "板块":     pos["sector"],
                "持股数":   shares,
                "现价 USD": price,
                "市值 USD": mkt_val,
                "货币":     pos.get("native_currency", "USD"),
                "备注":     pos.get("note", ""),
                "期权":     False,
            })
    # 期权（自动抓价 + 手动覆盖）
    for contract, od in options.items():
        src_tag = "手动" if od.get("source") == "manual" else "抓取"
        note = f"期权·{src_tag}"
        if od.get("flagged"):
            note += " ⚠偏移"
        rows.append({
            "_key":     contract,
            "股票":     od.get("display", contract),
            "板块":     od.get("sector", "期权"),
            "持股数":   od.get("contracts"),
            "现价 USD": od.get("mark"),
            "市值 USD": od.get("value"),
            "货币":     "USD",
            "备注":     note,
            "期权":     True,
        })
    # 纯手动价值（非期权）
    for name, val in portfolio.get("manual_values", {}).items():
        rows.append({
            "_key": name, "股票": name, "板块": "期权",
            "持股数": None, "现价 USD": None, "市值 USD": float(val),
            "货币": "USD", "备注": "手动", "期权": True,
        })

    df = pd.DataFrame(rows)
    total = df["市值 USD"].sum(skipna=True)
    df["占比"] = df["市值 USD"].apply(lambda v: v / total * 100 if (pd.notna(v) and total > 0) else None)

    # 与上一交易日快照对比 → 当日涨跌
    prior = snapshots.latest_prior_snapshot()
    prior_pos = (prior or {}).get("positions", {})

    def _day_pct(row):
        old = prior_pos.get(row["_key"], {})
        op, np_ = old.get("price"), row["现价 USD"]
        if isinstance(op, (int, float)) and isinstance(np_, (int, float)) and op:
            return (np_ - op) / op * 100
        return None

    def _day_val(row):
        old = prior_pos.get(row["_key"], {})
        ov, nv = old.get("value"), row["市值 USD"]
        if isinstance(ov, (int, float)) and isinstance(nv, (int, float)):
            return nv - ov
        return None

    df["涨跌%"]     = df.apply(_day_pct, axis=1)
    df["日变化 USD"] = df.apply(_day_val, axis=1)

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
        # 记录当日总净值到历史
        try:
            _, _total_nav = _build_view_df(portfolio, cache)
            append_value(_total_nav)
            sync_to_github(
                HISTORY_PATH, "data/portfolio_value_history.csv",
                "chore: record daily portfolio value",
            )
        except Exception:
            pass
        st.rerun()

tab_view, tab_history, tab_edit = st.tabs(["📊 持仓概览", "📈 净值历史", "✏️ 编辑持仓"])

# ═══════════════════════════════════════════════════
# TAB 1 — 持仓概览
# ═══════════════════════════════════════════════════
with tab_view:
    if cache is None:
        st.info("👆 点击「🔄 一键更新价格」获取当前行情后，持仓数据将自动显示。")
        st.stop()

    df, total_nav = _build_view_df(portfolio, cache)

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
        disp[["股票", "板块", "持股数", "现价 USD", "市值 USD", "涨跌%", "日变化 USD", "占比", "货币", "备注"]],
        column_config={
            "现价 USD": st.column_config.NumberColumn("现价 USD", format="$%.2f"),
            "市值 USD": st.column_config.NumberColumn("市值 USD", format="$%,.0f"),
            "涨跌%":    st.column_config.NumberColumn("当日涨跌%", format="%+.2f%%"),
            "日变化 USD": st.column_config.NumberColumn("日变化 USD", format="$%+,.0f"),
            "占比":     st.column_config.ProgressColumn(
                "占比", format="%.1f%%", min_value=0,
                max_value=float(disp["占比"].max(skipna=True)),  # already in %
            ),
            "持股数":   st.column_config.NumberColumn("持股数", format="%.4g"),
        },
        use_container_width=True, hide_index=True, height=500,
    )
    if snapshots.latest_prior_snapshot() is None:
        st.caption("ℹ️ 当日涨跌需至少两天快照对比；今天是首次记录，明天更新后即可显示。")

    # ── 期权明细 · 希腊字母 ──────────────────────────────────────────
    opt_cache = cache.get("options", {})
    if opt_cache:
        st.divider()
        st.subheader("🎯 期权明细 · 希腊字母")
        prior_pos = (snapshots.latest_prior_snapshot() or {}).get("positions", {})
        opt_rows = []
        for contract, od in opt_cache.items():
            old = prior_pos.get(contract, {})
            iv_prev = old.get("iv")
            dlt_prev = old.get("delta")
            iv_now = od.get("iv")
            opt_rows.append({
                "期权":     od.get("display", contract),
                "板块":     od.get("sector", "期权"),
                "张数":     od.get("contracts"),
                "标的价":   od.get("underlying_price"),
                "标价/股":  od.get("mark"),
                "来源":     "手动" if od.get("source") == "manual" else "抓取",
                "市值":     od.get("value"),
                "IV":       iv_now * 100 if isinstance(iv_now, (int, float)) else None,
                "ΔIV":      (iv_now - iv_prev) * 100 if (isinstance(iv_now, (int, float)) and isinstance(iv_prev, (int, float))) else None,
                "Delta":    od.get("delta"),
                "ΔDelta":   (od.get("delta") - dlt_prev) if (isinstance(od.get("delta"), (int, float)) and isinstance(dlt_prev, (int, float))) else None,
                "Gamma":    od.get("gamma"),
                "Theta/日": od.get("theta"),
                "Vega":     od.get("vega"),
                "到期天数": od.get("days_to_expiry"),
                "偏移提示": "⚠ 复核" if od.get("flagged") else "",
            })
        opt_df = pd.DataFrame(opt_rows)
        st.dataframe(
            opt_df,
            column_config={
                "标的价":   st.column_config.NumberColumn("标的价", format="$%.2f"),
                "标价/股":  st.column_config.NumberColumn("标价/股", format="$%.2f"),
                "市值":     st.column_config.NumberColumn("市值 USD", format="$%,.0f"),
                "IV":       st.column_config.NumberColumn("IV", format="%.1f%%"),
                "ΔIV":      st.column_config.NumberColumn("ΔIV", format="%+.2f%%"),
                "Delta":    st.column_config.NumberColumn("Delta", format="%.3f"),
                "ΔDelta":   st.column_config.NumberColumn("ΔDelta", format="%+.3f"),
                "Gamma":    st.column_config.NumberColumn("Gamma", format="%.5f"),
                "Theta/日": st.column_config.NumberColumn("Theta/日", format="%.4f"),
                "Vega":     st.column_config.NumberColumn("Vega", format="%.3f"),
            },
            use_container_width=True, hide_index=True,
        )
        st.caption(
            "IV 由 yfinance 提供；Delta/Gamma/Theta/Vega 由 Black-Scholes 计算。"
            "估值口径为中值 (bid+ask)/2；「来源=手动」表示已手动覆盖，"
            "「⚠ 复核」表示抓取值与手填偏移超 15%。ΔIV/ΔDelta 为较上一交易日变化。"
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
# TAB 2 — 净值历史
# ═══════════════════════════════════════════════════
with tab_history:
    st.subheader("📈 净值历史趋势")
    hist = load_history()

    if hist.empty or len(hist) < 1:
        st.info("暂无历史数据。每次点击「🔄 一键更新价格」会自动记录当日总净值，"
                "积累几天后这里就会显示趋势曲线。")
    else:
        # 时间范围筛选
        rng = st.radio("时间范围", ["1M", "3M", "6M", "全部"],
                       index=3, horizontal=True, label_visibility="collapsed")
        days_map = {"1M": 30, "3M": 90, "6M": 180}
        view = hist.copy()
        if rng in days_map:
            cutoff = pd.Timestamp.now().normalize() - pd.Timedelta(days=days_map[rng])
            view = view[view["date"] >= cutoff]
        if view.empty:
            view = hist.copy()

        view = view.sort_values("date").reset_index(drop=True)
        view["change"] = view["total_value"].diff()
        view["change_pct"] = view["total_value"].pct_change() * 100

        # KPI 卡片
        latest = float(view["total_value"].iloc[-1])
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("当前净值", f"${latest:,.0f}")

        def _delta_vs(days_back: int, label: str, col):
            ref_date = view["date"].iloc[-1] - pd.Timedelta(days=days_back)
            past = view[view["date"] <= ref_date]
            if past.empty:
                col.metric(label, "—")
                return
            base = float(past["total_value"].iloc[-1])
            if base > 0:
                pct = (latest / base - 1) * 100
                col.metric(label, f"${latest - base:,.0f}", delta=f"{pct:+.2f}%")
            else:
                col.metric(label, "—")

        _delta_vs(1, "较昨日", c2)
        _delta_vs(7, "较上周", c3)
        _delta_vs(30, "较上月", c4)

        st.divider()

        # 折线图：总净值
        fig_line = go.Figure()
        fig_line.add_trace(go.Scatter(
            x=view["date"], y=view["total_value"],
            mode="lines+markers", name="总净值",
            line=dict(color="#4C9BE8", width=2.5),
            marker=dict(size=6),
            fill="tozeroy", fillcolor="rgba(76,155,232,0.08)",
            hovertemplate="%{x|%Y-%m-%d}<br>净值: $%{y:,.0f}<extra></extra>",
        ))
        fig_line.update_layout(
            title="总净值走势 (USD)",
            height=380,
            margin=dict(t=50, b=20, l=20, r=20),
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
            yaxis=dict(showgrid=True, gridcolor="rgba(128,128,128,0.15)"),
            xaxis=dict(showgrid=False),
            hovermode="x unified",
        )
        st.plotly_chart(fig_line, use_container_width=True)

        # 柱状图：每日涨跌幅
        bars = view.dropna(subset=["change_pct"])
        if not bars.empty:
            bar_clr = ["#26a641" if v >= 0 else "#d73a4a" for v in bars["change_pct"]]
            fig_bar = go.Figure(go.Bar(
                x=bars["date"], y=bars["change_pct"],
                marker_color=bar_clr,
                text=[f"{v:+.2f}%" for v in bars["change_pct"]],
                textposition="outside",
                hovertemplate="%{x|%Y-%m-%d}<br>涨跌: %{y:+.2f}%<extra></extra>",
            ))
            fig_bar.add_hline(y=0, line_color="rgba(128,128,128,0.5)", line_width=1)
            fig_bar.update_layout(
                title="每日涨跌幅 (%)",
                height=300,
                margin=dict(t=50, b=20, l=20, r=20),
                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                yaxis=dict(showgrid=True, gridcolor="rgba(128,128,128,0.15)"),
                xaxis=dict(showgrid=False),
            )
            st.plotly_chart(fig_bar, use_container_width=True)

        with st.expander("📋 历史数据明细"):
            tbl = view[["date", "total_value", "change", "change_pct"]].copy()
            tbl["date"] = tbl["date"].dt.strftime("%Y-%m-%d")
            tbl.columns = ["日期", "总净值", "较前日变化", "涨跌幅%"]
            tbl = tbl.sort_values("日期", ascending=False)
            st.dataframe(tbl, use_container_width=True, hide_index=True)

# ═══════════════════════════════════════════════════
# TAB 3 — 编辑持仓
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

    st.subheader("🎯 期权持仓（自动抓价 + 希腊字母）")
    st.caption(
        "• 填 **标的 / 到期 / 方向 / 行权价 / 张数**，保存后系统自动生成 OCC 合约代码并抓取实时价与希腊字母。  \n"
        "• 估值默认用中值 (bid+ask)/2。**手动价**留空=自动；填入每股价则以手填为准（用于抓不到或与券商偏移较大时）。  \n"
        "• 市值 = 标价 × 100 × 张数。到期日格式 `YYYY-MM-DD`（如 `2027-06-17`）。"
    )
    options_df = _options_to_df(portfolio)
    edited_options = st.data_editor(
        options_df,
        num_rows="dynamic",
        column_config={
            "显示名": st.column_config.TextColumn("显示名", help="留空自动生成"),
            "标的":   st.column_config.TextColumn("标的 ✱", required=True, help="如 GLW、MRVL、SOXX"),
            "到期":   st.column_config.TextColumn("到期 ✱", required=True, help="YYYY-MM-DD"),
            "方向":   st.column_config.SelectboxColumn("方向", options=OPT_TYPES),
            "行权价": st.column_config.NumberColumn("行权价 ✱", min_value=0, format="%.2f"),
            "张数":   st.column_config.NumberColumn("张数", min_value=0, format="%.4g"),
            "板块":   st.column_config.SelectboxColumn("板块", options=SECTORS),
            "手动价": st.column_config.NumberColumn("手动价/股", min_value=0, format="$%.2f",
                        help="留空=自动中值；填入则手动覆盖"),
            "备注":   st.column_config.TextColumn("备注"),
        },
        use_container_width=True,
        key="options_editor",
    )

    st.subheader("🗒️ 手动价值（其他无法抓取的仓位）")
    st.caption("既不是股票也不是期权、无法自动抓取的仓位，直接填入当前市值 (USD)。")
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
            _save_portfolio(portfolio, edited_pos, edited_manual, edited_options)
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
            updated = _save_portfolio(portfolio, edited_pos, edited_manual, edited_options)
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
