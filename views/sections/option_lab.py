"""views/sections/12_OptionLab.py — 🧮 期权情景：价值曲线 / 情景矩阵 / 时间衰减 / 换月建议"""

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
from core import option_scenarios as OS
from core.price_updater import load_cache

click_hint()
st.caption(
    "对持有的期权做「如果……会怎样」：标的涨跌、IV 变化、时间流逝对价值的影响。"
    "Black-Scholes 重估，IV 由当前中间价反解（今天、标的不动时盈亏 = 0）。"
    "**盈亏相对当前价值**（未记录买入成本）。仅供辅助研究，非投资建议。"
)


@st.cache_data(show_spinner="🧮 正在抓取期权报价…", ttl=900)
def _book(pf_json: str) -> pd.DataFrame:
    book, _ = R.build_book(json.loads(pf_json), load_cache() or {})
    return book


@st.cache_data(show_spinner="🔁 正在查询期权链…", ttl=1800)
def _rolls(leg_json: str) -> pd.DataFrame:
    return OS.roll_candidates(json.loads(leg_json))


pf = json.loads(config.PORTFOLIO_PATH.read_text(encoding="utf-8"))
_, c_btn = st.columns([5, 1])
if c_btn.button("🔄 刷新报价", type="primary", width="stretch"):
    _book.clear()
    st.rerun()

book = _book(json.dumps(pf, sort_keys=True))
opts = book[(book["kind"] == "option") & book["iv"].notna() & book["S"].notna()]
if opts.empty:
    st.info("暂无可分析的期权持仓（或期权报价获取失败，请稍后刷新）。")
    st.stop()

legs_all = [OS.leg_from_book_row(r) for _, r in opts.iterrows()]

# ─── 全部期权一览 ─────────────────────────────────────────────────────────────
st.subheader("📋 期权持仓一览")
rows = []
for lg in legs_all:
    s = OS.leg_stats(lg)
    tips = OS.roll_advice(lg)
    rows.append({"期权": lg["display"], "标的": lg["underlying"], "张数": lg["qty"], "剩余天数": s["dte"],
                 "标的价": lg["S"], "行权价": lg["K"], "当前价值$": OS.current_value([lg]),
                 "delta": s["delta"], "每日theta$": s["theta_day_usd"], "杠杆倍数": s["leverage"],
                 "盈亏平衡涨幅%": s["breakeven_move"] * 100, "IV%": lg["iv"] * 100,
                 "提示": tips[0].split("，")[0] if tips else ""})
over = pd.DataFrame(rows)
clickable_table(
    over, hide_index=True, width="stretch",
    column_config={
        "标的价": st.column_config.NumberColumn(format="%.2f"),
        "行权价": st.column_config.NumberColumn(format="%.2f"),
        "当前价值$": st.column_config.NumberColumn(format="%.0f"),
        "delta": st.column_config.NumberColumn(format="%.2f"),
        "每日theta$": st.column_config.NumberColumn(format="%.1f", help="标的与 IV 不变时每天损失的价值"),
        "杠杆倍数": st.column_config.NumberColumn(format="%.1f×", help="标的涨 1%，期权约涨几 %"),
        "盈亏平衡涨幅%": st.column_config.NumberColumn(format="%+.1f%%",
                                                  help="按当前价格买入，到期时标的需要涨多少才回本"),
        "IV%": st.column_config.NumberColumn(format="%.0f%%"),
    }, tickers=list(over["标的"]), key="optlab_over"
)
tot_theta = sum(OS.leg_stats(lg)["theta_day_usd"] for lg in legs_all)
st.caption(f"全部期权合计：价值 \\${sum(OS.current_value([lg]) for lg in legs_all):,.0f} ｜ "
           f"每日时间损耗 {-tot_theta:,.1f} 美元 ｜ 每月约 {-tot_theta * 30:,.0f} 美元")

st.divider()

# ─── 选择分析对象 ─────────────────────────────────────────────────────────────
choices: dict[str, list[dict]] = {lg["display"]: [lg] for lg in legs_all}
by_under: dict[str, list[dict]] = {}
for lg in legs_all:
    by_under.setdefault(lg["underlying"], []).append(lg)
for u, lgs in by_under.items():
    if len(lgs) > 1:
        choices[f"【合并】{u}（{len(lgs)} 个合约）"] = lgs
label = st.selectbox("选择期权（同一标的的多个合约可合并分析）", list(choices))
legs = choices[label]
S0 = legs[0]["S"]
min_dte = int(min(lg["T"] for lg in legs) * 365)

value_now = OS.current_value(legs)
stats = [OS.leg_stats(lg) for lg in legs]
m = st.columns(5)
m[0].metric("当前价值", f"${value_now:,.0f}")
m[1].metric("标的现价", f"{S0:,.2f}")
m[2].metric("每日 theta", f"${sum(s['theta_day_usd'] for s in stats):,.1f}")
m[3].metric("vega（IV 每 1 点）", f"${sum(s['vega_usd'] for s in stats):,.0f}")
if len(legs) == 1:
    s = stats[0]
    m[4].metric("到期盈亏平衡", f"{s['breakeven']:,.2f}", f"{s['breakeven_move'] * 100:+.1f}%",
                delta_color="off", delta_arrow="off")

tab_curve, tab_grid, tab_decay, tab_roll = st.tabs(["📈 价值曲线", "🧊 情景矩阵", "⏳ 时间衰减", "🔁 换月建议"])

# ═══════════════════════════════════════════════════════════════════════════════
with tab_curve:
    span = st.slider("标的价格范围 ±%", 10, 80, 50, 5, key="curve_span") / 100
    horizons = sorted({0, min(30, min_dte), min(90, min_dte), min_dte + 1})
    vc = OS.value_curves(legs, horizons, span=span)
    fig = go.Figure()
    palette = ["#4C9BE8", "#E8A84C", "#B44CE8", "#d73a4a"]
    for i, col in enumerate([c for c in vc.columns if c not in ("price", "move")]):
        fig.add_trace(go.Scatter(x=vc["price"], y=vc[col], name=col, mode="lines",
                                 line=dict(width=3 if col in ("今天", "到期") else 2,
                                           dash="solid" if col in ("今天", "到期") else "dot",
                                           color=palette[i % len(palette)]),
                                 customdata=vc["move"] * 100,
                                 hovertemplate="标的 %{x:,.2f}（%{customdata:+.0f}%）<br>盈亏 $%{y:+,.0f}"
                                               f"<extra>{col}</extra>"))
    fig.add_hline(y=0, line_color="rgba(128,128,128,0.6)", line_width=1)
    fig.add_vline(x=S0, line_dash="dash", line_color="rgba(128,128,128,0.8)",
                  annotation_text="现价", annotation_position="top")
    if len(legs) == 1:
        fig.add_vline(x=stats[0]["breakeven"], line_dash="dot", line_color="#26a641",
                      annotation_text="到期盈亏平衡", annotation_position="bottom right")
    fig.update_layout(height=460, xaxis_title="标的价格", yaxis_title="盈亏 $（相对当前价值）",
                      legend=dict(orientation="h", y=1.08), hovermode="x unified",
                      margin=dict(t=30, b=10, l=10, r=10),
                      plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)")
    st.plotly_chart(fig, width="stretch")
    st.caption("曲线越往后越接近到期的折线；今天与到期之间的差距就是时间价值。IV 假设保持不变。")

# ═══════════════════════════════════════════════════════════════════════════════
with tab_grid:
    days = st.slider("多少天后", 0, max(min_dte, 1), min(30, min_dte), 5, key="grid_days")
    moves = [-0.3, -0.2, -0.1, -0.05, 0.0, 0.05, 0.1, 0.2, 0.3]
    ivs = [0.2, 0.1, 0.0, -0.1, -0.2]
    mat = OS.scenario_matrix(legs, moves, ivs, days=days)
    lim = float(mat.abs().max().max()) or 1.0
    fig = px.imshow(mat, color_continuous_scale="RdYlGn", zmin=-lim, zmax=lim, aspect="auto",
                    text_auto=",.0f", labels={"x": "标的涨跌", "y": "IV 变化", "color": "盈亏$"})
    fig.update_layout(height=380, margin=dict(t=10, b=10, l=10, r=10))
    st.plotly_chart(fig, width="stretch")
    st.caption(f"{days} 天后，在不同「标的涨跌 × IV 变化」下的仓位盈亏（美元，相对当前价值 \\${value_now:,.0f}）。"
               "财报或大跌后 IV 往往先升后降——看 IV 那一行的差距就是 vega 风险。")

# ═══════════════════════════════════════════════════════════════════════════════
with tab_decay:
    dp = OS.decay_path(legs, step=7)
    fig = go.Figure(go.Scatter(x=dp["days"], y=dp["value"], mode="lines+markers", line=dict(color="#E8A84C")))
    fig.update_layout(height=380, xaxis_title="从今天起的天数", yaxis_title="仓位价值 $",
                      margin=dict(t=10, b=10, l=10, r=10),
                      plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)")
    st.plotly_chart(fig, width="stretch")
    if len(dp) > 1:
        lost = dp["value"].iloc[0] - dp["value"].iloc[-1]
        st.caption(f"标的与 IV 都不变时，到最近到期日共损失约 \\${lost:,.0f}（{lost / dp['value'].iloc[0]:.0%}）；"
                   "曲线越到后段越陡，就是临近到期时 theta 加速。")

# ═══════════════════════════════════════════════════════════════════════════════
with tab_roll:
    for lg in legs:
        st.markdown(f"**{lg['display']}**（{lg['expiry']} 到期）")
        for tip in OS.roll_advice(lg):
            st.markdown(f"- {tip}")
        cand = _rolls(json.dumps(lg, sort_keys=True))
        if cand.empty:
            st.caption("未取到更远月份的期权链（CBOE 延迟行情）。")
            continue
        show = cand.copy()
        show["IV"] = show["IV"] * 100
        st.dataframe(
            show, hide_index=True, width="stretch",
            column_config={
                "行权价": st.column_config.NumberColumn(format="%.2f"),
                "买价": st.column_config.NumberColumn(format="%.2f"),
                "卖价": st.column_config.NumberColumn(format="%.2f"),
                "中间价": st.column_config.NumberColumn(format="%.2f"),
                "IV": st.column_config.NumberColumn(format="%.0f%%"),
                "delta": st.column_config.NumberColumn(format="%.2f"),
                "每日theta$": st.column_config.NumberColumn(format="%.2f", help="每张每天的时间损耗"),
                "换月净成本$": st.column_config.NumberColumn(
                    format="%+.0f", help="每张：买入新合约 − 卖出现有合约（按中间价，未计买卖价差与佣金）"),
            },
        )
    st.caption("候选 = 每个更远到期日中 delta 最接近当前仓位的合约（保持相近的方向敞口）。"
               "延长期限可降低每日 theta，但需要补付时间价值；实际成交请看买卖价差。")
