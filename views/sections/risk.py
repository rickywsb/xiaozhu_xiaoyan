"""views/sections/11_Risk.py — 🛡️ 风险仪表盘：敞口 / 集中度 / β / VaR / 相关性 / 压力测试"""

import json
import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config
from core.stock_chart import clickable_table, click_hint
from core import risk as R
from core.price_updater import load_cache

_GREEN, _RED, _BLUE, _GRAY = "#26a641", "#d73a4a", "#4C9BE8", "#8b949e"

click_hint()
st.caption(
    "期权按 **delta 等效敞口**（delta × 标的价 × 100 × 张数）并入标的与板块。"
    "β / 波动 / VaR 用过去 1 年真实日收益 × 当前敞口回放；压力测试中期权用 Black-Scholes 全额重估。"
    "仅供风险认知，非投资建议。"
)


@st.cache_data(show_spinner="🛡️ 正在汇总持仓、抓取期权报价与历史行情…", ttl=900)
def _load(pf_json: str) -> tuple[pd.DataFrame, dict, dict]:
    pf = json.loads(pf_json)
    book, closes = R.build_book(pf, load_cache() or {})
    return book, closes, R.portfolio_risk(book, closes)


pf = json.loads(config.PORTFOLIO_PATH.read_text(encoding="utf-8"))
_, c_btn = st.columns([5, 1])
if c_btn.button("🔄 刷新", type="primary", width="stretch"):
    _load.clear()
    st.rerun()

book, closes, pr = _load(json.dumps(pf, sort_keys=True))
nav = pr["nav"]
betas = pr["betas"]

# ─── 顶部指标 ─────────────────────────────────────────────────────────────────
r1 = st.columns(4)
r1[0].metric("净值（股票+期权+现金）", f"${nav:,.0f}")
r1[1].metric("delta 调整总敞口", f"${pr['gross_exposure']:,.0f}", f"{pr['leverage']:.2f}× 净值",
             delta_color="off", delta_arrow="off")
r1[2].metric("组合 β vs SPY", f"{pr['beta_SPY']:.2f}",
             help="SPY 涨跌 1%，组合约涨跌 β%（按敞口加权、除以净值）")
r1[3].metric("组合 β vs SOXX", f"{pr['beta_SOXX']:.2f}")
r2 = st.columns(4)
r2[0].metric("年化波动（占净值）", f"{pr['vol'] * 100:.0f}%")
r2[1].metric("1 日 VaR 95%", f"${pr['var95']:,.0f}", f"{pr['var95'] / nav * 100:.1f}% 净值", delta_color="off", delta_arrow="off",
             help="过去 1 年里，按当前持仓回放，最差 5% 交易日的亏损门槛")
r2[2].metric("1 日 CVaR 95%", f"${pr['cvar95']:,.0f}", f"{pr['cvar95'] / nav * 100:.1f}% 净值",
             delta_color="off", delta_arrow="off", help="最差 5% 交易日的平均亏损")
r2[3].metric("回放最大回撤", f"{pr['max_dd'] * 100:.1f}%", f"有效持仓数 {pr['hhi_n']:.1f}", delta_color="off", delta_arrow="off",
             help="当前持仓若过去 1 年一直不变的最大回撤；有效持仓数 = 1/Σ权重²，越小越集中")

tab_exp, tab_corr, tab_stress, tab_custom = st.tabs(
    ["📊 敞口与集中度", "🔗 相关性", "🧯 压力测试", "🎚️ 自定义情景"])

# ═══════════════════════════════════════════════════════════════════════════════
# 敞口与集中度
# ═══════════════════════════════════════════════════════════════════════════════
with tab_exp:
    sec = R.sector_exposure(book)
    sec = sec[sec["sector"] != "现金"]
    fig = go.Figure()
    fig.add_trace(go.Bar(y=sec["sector"], x=sec["value_pct"] * 100, name="市值占净值%",
                         orientation="h", marker_color=_GRAY))
    fig.add_trace(go.Bar(y=sec["sector"], x=sec["exposure_pct"] * 100, name="delta 调整敞口占净值%",
                         orientation="h", marker_color=_BLUE,
                         text=[f"{v * 100:.0f}%" for v in sec["exposure_pct"]], textposition="outside",
                         cliponaxis=False))
    fig.update_layout(barmode="group", height=80 + 55 * len(sec), title="板块敞口",
                      yaxis=dict(autorange="reversed"), legend=dict(orientation="h", y=1.12),
                      margin=dict(t=60, b=10, l=10, r=40),
                      plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)")
    st.plotly_chart(fig, width="stretch")

    # 集中度预警
    exp = pr["exposure"]
    warns = []
    for t, v in exp.items():
        if v / nav > 0.15:
            warns.append(f"单一标的 **{t}** 敞口占净值 {v / nav:.0%}（>15%）")
    for _, r in sec.iterrows():
        if r["exposure_pct"] > 0.40:
            warns.append(f"板块 **{r['sector']}** 敞口占净值 {r['exposure_pct']:.0%}（>40%）")
    top5 = float(exp.head(5).sum() / nav)
    if top5 > 0.60:
        warns.append(f"前 5 大标的合计 {top5:.0%}（>60%）")
    rc_top = pr["risk_contrib"].head(3)
    warns.append("组合波动主要来自：" + "、".join(f"**{t}** {v:.0%}" for t, v in rc_top.items()))
    st.warning("  \n".join(f"⚠️ {w}" for w in warns))

    # 标的明细
    stock_v = book[book["kind"] == "stock"].groupby("underlying")["value"].sum()
    opt_e = book[book["kind"] == "option"].groupby("underlying")["exposure"].sum()
    sector_of = book.dropna(subset=["underlying"]).groupby("underlying")["sector"].first()
    tbl = pd.DataFrame({"股票市值": stock_v, "期权等效敞口": opt_e}).fillna(0.0)
    tbl["合计敞口"] = tbl.sum(axis=1)
    tbl = tbl.reindex(exp.index)
    tbl["板块"] = sector_of.reindex(tbl.index)
    tbl["占净值%"] = tbl["合计敞口"] / nav * 100
    tbl["风险贡献%"] = pr["risk_contrib"].reindex(tbl.index) * 100
    for b in ("SPY", "SOXX"):
        tbl[f"β {b}"] = betas[f"beta_{b}"].reindex(tbl.index) if f"beta_{b}" in betas else None
    tbl["年化波动%"] = betas["vol"].reindex(tbl.index) * 100 if "vol" in betas else None
    tbl = tbl.reset_index().rename(columns={"index": "标的", "underlying": "标的"})
    clickable_table(
        tbl[["标的", "板块", "股票市值", "期权等效敞口", "合计敞口", "占净值%", "风险贡献%",
             "β SPY", "β SOXX", "年化波动%"]],
        column_config={
            "股票市值": st.column_config.NumberColumn(format="$%,.0f"),
            "期权等效敞口": st.column_config.NumberColumn(format="$%,.0f"),
            "合计敞口": st.column_config.NumberColumn(format="$%,.0f"),
            "占净值%": st.column_config.ProgressColumn(format="%.1f%%", min_value=0,
                                                   max_value=max(30.0, float(tbl["占净值%"].max()))),
            "风险贡献%": st.column_config.ProgressColumn(format="%.1f%%", min_value=0,
                                                    max_value=max(30.0, float(tbl["风险贡献%"].max())),
                                                    help="该标的对组合总波动的贡献占比（考虑相关性）"),
            "β SPY": st.column_config.NumberColumn(format="%.2f"),
            "β SOXX": st.column_config.NumberColumn(format="%.2f"),
            "年化波动%": st.column_config.NumberColumn(format="%.0f%%"),
        },
        width="stretch", hide_index=True, height=min(760, 80 + len(tbl) * 35), tickers=list(tbl["标的"]), key="risk_exp"
    )
    st.caption("风险贡献 = 敞口 × (协方差 × 敞口) / 组合方差，高波动且与其他持仓高度相关的标的贡献更大；"
               "风险贡献明显高于敞口占比的，是组合真正的风险来源。"
               "亚洲市场标的（如 .KS / .HK）收盘早于美股，同日收益不同步，β 与相关性会被低估。")

# ═══════════════════════════════════════════════════════════════════════════════
# 相关性
# ═══════════════════════════════════════════════════════════════════════════════
with tab_corr:
    top_n = st.slider("显示敞口最大的前 N 个标的", 5, max(5, len(pr["exposure"])),
                      min(15, len(pr["exposure"])))
    keys = [t for t in pr["exposure"].index[:top_n] if t in pr["corr"].columns]
    corr = pr["corr"].loc[keys, keys]
    off = corr.where(~pd.DataFrame(
        [[i == j for j in range(len(keys))] for i in range(len(keys))], index=keys, columns=keys))
    avg = float(off.stack().mean()) if len(keys) > 1 else float("nan")
    st.metric("平均两两相关系数", f"{avg:.2f}",
              help="越接近 1，持仓越像同一笔押注；分散化效果越弱")
    fig = px.imshow(corr, zmin=-1, zmax=1, color_continuous_scale="RdBu_r", text_auto=".2f",
                    aspect="auto")
    fig.update_layout(height=max(420, 30 * len(keys) + 120), margin=dict(t=10, b=10, l=10, r=10))
    st.plotly_chart(fig, width="stretch")
    st.caption("过去 1 年日收益相关系数（美元计价）。")

# ═══════════════════════════════════════════════════════════════════════════════
# 压力测试
# ═══════════════════════════════════════════════════════════════════════════════
with tab_stress:
    scen = R.scenario_table(book, closes, betas)
    fig = go.Figure(go.Bar(
        x=scen["占净值"] * 100, y=scen["情景"], orientation="h",
        marker_color=[_RED if v < 0 else _GREEN for v in scen["组合盈亏"]],
        text=[f"${v:+,.0f}（{p * 100:+.1f}%）" for v, p in zip(scen["组合盈亏"], scen["占净值"])],
        textposition="outside", cliponaxis=False,
    ))
    lo = float((scen["占净值"] * 100).min())
    fig.update_layout(height=80 + 45 * len(scen), yaxis=dict(autorange="reversed"),
                      xaxis=dict(title="组合盈亏占净值 %", range=[lo * 1.6, max(5.0, -lo * 0.2)]),
                      margin=dict(t=10, b=10, l=10, r=10),
                      plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)")
    st.plotly_chart(fig, width="stretch")
    show = scen.copy()
    show["基准涨跌"] = show["基准涨跌"] * 100
    show["占净值"] = show["占净值"] * 100
    st.dataframe(
        show, hide_index=True, width="stretch",
        column_config={
            "基准涨跌": st.column_config.NumberColumn(format="%+.1f%%",
                                                  help="因子冲击为所选基准；历史重演为同期 SOXX 涨跌"),
            "组合盈亏": st.column_config.NumberColumn(format="$%+,.0f"),
            "占净值": st.column_config.NumberColumn(format="%+.1f%%"),
            "代理标的数": st.column_config.NumberColumn(help="当时尚未上市/无数据、改用 β×SPY 同期涨跌代替的标的数"),
        },
    )
    st.caption(
        "**因子冲击**：各标的按对该基准的 β 联动（β 线性外推在极端行情下可能高估或低估）。"
        "**历史重演**：把当时各标的真实涨跌套到当前持仓，期权按标的真实涨跌用 BS 重估（IV 不变，真实暴跌时 IV 通常上升，"
        "对多头 call 有部分缓冲）。"
    )

    pick = st.selectbox("查看某个情景的逐仓位明细", list(scen["情景"]))
    row = scen[scen["情景"] == pick].iloc[0]
    if row["类型"] == "历史重演":
        a, b = row["区间"].split(" → ")
        detail, _ = R.stress_window(book, closes, betas, a, b)
    else:
        bench = pick.split()[0]
        detail = R.stress_factor(book, betas, bench, row["基准涨跌"],
                                 iv_shift=0.10 if "IV" in pick else 0.0)
    by_sec = detail.groupby("sector")["pnl"].sum().sort_values()
    c1, c2 = st.columns([1, 2])
    with c1:
        st.markdown("**按板块**")
        st.dataframe(by_sec.rename("盈亏$").to_frame(), width="stretch",
                     column_config={"盈亏$": st.column_config.NumberColumn(format="$%+,.0f")})
    with c2:
        st.markdown("**亏损最大的仓位**")
        d = detail[detail["kind"] != "cash"].sort_values("pnl").head(10).copy()
        d["move"] = d["move"] * 100
        clickable_table(
            d[["display", "kind", "sector", "move", "value", "pnl"]].rename(columns={
                "display": "仓位", "kind": "类型", "sector": "板块", "move": "标的涨跌%",
                "value": "当前市值", "pnl": "盈亏$"}),
            hide_index=True, width="stretch",
            column_config={"标的涨跌%": st.column_config.NumberColumn(format="%+.1f%%"),
                           "当前市值": st.column_config.NumberColumn(format="$%,.0f"),
                           "盈亏$": st.column_config.NumberColumn(format="$%+,.0f")}, tickers=list(d["underlying"]), key="risk_worst", names=list(d["display"])
        )

# ═══════════════════════════════════════════════════════════════════════════════
# 自定义情景
# ═══════════════════════════════════════════════════════════════════════════════
with tab_custom:
    c1, c2, c3, c4 = st.columns(4)
    bench = c1.selectbox("基准", list(R.BETA_BENCHES), index=1)
    shock = c2.slider("基准涨跌 %", -40, 30, -10, 1) / 100
    iv_shift = c3.slider("期权 IV 变化（点）", -30, 30, 0, 5) / 100
    days = c4.slider("时间前进（天）", 0, 180, 0, 5)
    detail = R.stress_factor(book, betas, bench, shock, iv_shift=iv_shift, days=days)
    total = float(detail["pnl"].sum())
    opt_pnl = float(detail.loc[detail["kind"] == "option", "pnl"].sum())
    m = st.columns(3)
    m[0].metric("组合盈亏", f"${total:+,.0f}", f"{total / nav * 100:+.1f}% 净值")
    m[1].metric("其中股票", f"${total - opt_pnl:+,.0f}")
    m[2].metric("其中期权", f"${opt_pnl:+,.0f}",
                help="期权为 BS 全额重估：含凸性、IV 变化与时间衰减，并非 delta 线性")
    by_sec = detail.groupby("sector")["pnl"].sum().sort_values()
    fig = go.Figure(go.Bar(x=by_sec.values, y=by_sec.index, orientation="h",
                           marker_color=[_RED if v < 0 else _GREEN for v in by_sec.values],
                           text=[f"${v:+,.0f}" for v in by_sec.values], textposition="outside",
                           cliponaxis=False))
    span = float(by_sec.abs().max() or 1)
    fig.update_layout(height=80 + 45 * len(by_sec), title="按板块",
                      xaxis=dict(range=[-span * 1.4, span * 1.4]),
                      margin=dict(t=40, b=10, l=10, r=10),
                      plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)")
    st.plotly_chart(fig, width="stretch")
