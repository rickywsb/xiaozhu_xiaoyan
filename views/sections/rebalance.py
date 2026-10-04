"""views/sections/rebalance.py — 持仓 · 情景调仓：事件情景 → 每只持仓的风险收益 → 角色 → 减仓 / 期权保护方案 → 沙盘对比 → 保存"""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config
from core import rebalance as RB
from core import risk as R
from core import rating as RT
from core import signal_lab as SL
from core import option_screener as OS
from core.price_updater import load_cache
from core.stock_chart import click_hint
from core.ui import pz_table, pct, _clean

st.caption(
    "面对已知风险事件，先看清**每只持仓在下跌情景里会亏多少、换来多少期望收益**，再决定减哪只、留哪只、用期权怎么保护。"
    "风险端（β、压力测试）较可靠；收益端是评分与信号的历史平均（幅度小），所以本页目标是**用最小的收益代价把风险降下来**，"
    "不是预测涨跌。只做模拟，不改动真实持仓。仅供研究，非投资建议。")
click_hint()


# ─── 数据 ─────────────────────────────────────────────────────────────────────

@st.cache_data(show_spinner="🧮 正在汇总持仓与历史行情…", ttl=900)
def _load(pf_json: str):
    return R.build_book(json.loads(pf_json), load_cache() or {})


@st.cache_data(show_spinner="📜 读取 1962 年以来的中期选举行情…", ttl=86400)
def _hist() -> pd.DataFrame:
    return RB.midterm_history()


@st.cache_data(show_spinner="📡 读取期权链…", ttl=1200)
def _chains(tickers: tuple[str, ...]) -> dict:
    with ThreadPoolExecutor(4) as ex:
        return dict(zip(tickers, ex.map(OS.chain, tickers)))


pf = json.loads(config.PORTFOLIO_PATH.read_text(encoding="utf-8"))
book, closes = _load(json.dumps(pf, sort_keys=True))
nav = float(book["value"].sum())

# ─── ① 事件与情景 ─────────────────────────────────────────────────────────────
st.markdown("#### ① 事件与情景")
c1, c2, c3 = st.columns([2, 1.2, 1])
ev_name = c1.text_input("风险事件", value="美国中期选举", key="rb_ev_name")
ev_date = c2.date_input("事件日期", value=RB.election_day(2026), key="rb_ev_date")
post = c3.number_input("事件后观察（交易日）", 5, 60, 15, key="rb_post")
hdays = RB.horizon_days(ev_date, int(post))

hist = _hist()
with st.expander(f"📜 历史参考：1962–2022 年 {len(hist)} 次中期选举（选举前 21 个交易日 → 选举后 15 个交易日的 S&P 500）"):
    if not hist.empty:
        st.dataframe(hist.style.format({c: "{:+.1%}" for c in ["选举前", "选举后", "全程", "期间最大回撤"]}),
                     hide_index=True, width="stretch", height=300)
        st.caption(f"全程上涨 {int((hist['全程'] > 0).sum())} / {len(hist)} 次，平均 {hist['全程'].mean():+.1%}；"
                   f"但窗口内最大回撤中位数 {hist['期间最大回撤'].median():+.1%}、最差 {hist['期间最大回撤'].min():+.1%}——"
                   "**风险主要在路径上**（选前的波动），所以「下跌」情景用最大回撤最差四分之一的均值。样本只有十几次，仅作参考。")

st.caption(f"三个情景（可改）：事件期 = 今天 → {ev_date} 后 {post} 个交易日，共约 {hdays} 个交易日。"
           "个股涨跌 = SPY 涨跌 × β（下跌情景用**下行 β**，即只用 SPY 下跌日估计，抛售时联动更强）；期权按 Black-Scholes 重估。")
scen0 = pd.DataFrame(RB.default_scenarios(hist))
scen0 = scen0.assign(**{"SPY 涨跌 %": scen0["SPY"] * 100, "IV 变化（点）": scen0["IV"] * 100, "概率 %": scen0["概率"] * 100})[
    ["情景", "SPY 涨跌 %", "IV 变化（点）", "概率 %"]]
scen_df = st.data_editor(scen0, hide_index=True, width="stretch", key="rb_scen", num_rows="fixed",
                         column_config={"SPY 涨跌 %": st.column_config.NumberColumn(format="%.1f"),
                                        "IV 变化（点）": st.column_config.NumberColumn(format="%.0f"),
                                        "概率 %": st.column_config.NumberColumn(format="%.0f", min_value=0, max_value=100)})
ptot = float(scen_df["概率 %"].sum()) or 1.0
scen = [{"情景": r["情景"], "SPY": float(r["SPY 涨跌 %"]) / 100, "IV": float(r["IV 变化（点）"]) / 100,
         "概率": float(r["概率 %"]) / ptot} for r in scen_df.to_dict("records")]
if abs(ptot - 100) > 0.5:
    st.caption(f"概率合计 {ptot:.0f}%，已按比例归一。")
down_name = min(scen, key=lambda s: s["SPY"])["情景"]

# ─── ② 现状与每只持仓的风险收益 ────────────────────────────────────────────────
ratings, _ = RT.load_latest()
lab = SL.load_summary()
tbl, ctx = RB.holding_table(book, closes, scen, hdays, ratings, RT.load_validation(),
                            lab["summary"] if lab else None, SL.load_latest().get("latest", {}))
user_roles = RB.load_roles()
tbl = RB.assign_roles(tbl, user_roles)
alpha_of = dict(zip(tbl["ticker"], tbl["alpha20"]))
sm0 = RB.summary(book, closes, ctx, scen, hdays, alpha_of)
down0 = sm0["scen"][down_name]

st.markdown("#### ② 现状")
k = st.columns(5)
k[0].metric("净值", f"${nav:,.0f}", f"现金 {sm0['cash_pct']:.0%}", delta_color="off", delta_arrow="off")
k[1].metric("组合 β / 下行 β", f"{sm0['beta']:.2f} / {sm0['beta_down']:.2f}")
k[2].metric(f"「{down_name}」情景亏损", f"${down0:,.0f}", f"{down0 / nav:+.1%} 净值", delta_color="off", delta_arrow="off")
k[3].metric("情景加权期望盈亏", f"${sm0['exp_pnl']:+,.0f}", f"{sm0['exp_pnl'] / nav:+.1%} 净值", delta_color="off", delta_arrow="off")
k[4].metric("1 日 VaR 95%", f"${sm0['var95']:,.0f}", f"{sm0['var95'] / nav:.1%} 净值", delta_color="off", delta_arrow="off")

st.markdown("**每只持仓的风险收益**（风险收益比 = 期望盈亏 ÷ 下跌情景亏损，越低越该先处理）")
rows = []
for r in tbl.sort_values("rr", na_position="last").to_dict("records"):
    rows.append({"ticker": r["ticker"], "name": r["display"],
                 "sub": " · ".join(x for x in [r["sector"], "你标注" if r["role_user"] else r["role_why"]] if x),
                 "role": f"{RB.ROLE_ICON[r['role']]} {r['role']}", "value": r["value"], "weight": pct(r["weight"]),
                 "beta": _clean(r["beta"]), "beta_down": _clean(r["beta_down"]),
                 "score": int(r["score"]) if _clean(r["score"]) is not None else None,
                 "down": r["down_pnl"], "exp": r["exp_pnl"], "rr": _clean(r["rr"]), "alpha": pct(r["alpha20"])})
pz_table(rows, [
    {"key": "ticker", "label": "标的", "kind": "stock", "width": "minmax(150px,1.4fr)", "sortable": True},
    {"key": "role", "label": "角色", "kind": "text", "width": "84px", "sortable": True},
    {"key": "value", "label": "市值", "kind": "num", "decimals": 0, "prefix": "$", "width": "90px", "align": "right", "sortable": True},
    {"key": "weight", "label": "仓位", "kind": "bar", "max": 25, "suffix": "%", "decimals": 1, "width": "104px", "sortable": True},
    {"key": "beta", "label": "β", "kind": "num", "decimals": 2, "width": "56px", "align": "right", "sortable": True},
    {"key": "beta_down", "label": "下行 β", "kind": "num", "decimals": 2, "width": "64px", "align": "right", "sortable": True},
    {"key": "score", "label": "评分", "kind": "score", "width": "64px", "sortable": True},
    {"key": "alpha", "label": "20日超额", "kind": "num", "decimals": 1, "suffix": "%", "sign": True, "color": True,
     "width": "76px", "align": "right", "sortable": True},
    {"key": "down", "label": f"{down_name}亏损", "kind": "num", "decimals": 0, "prefix": "$", "color": True,
     "width": "92px", "align": "right", "sortable": True},
    {"key": "exp", "label": "期望盈亏", "kind": "num", "decimals": 0, "prefix": "$", "sign": True, "color": True,
     "width": "92px", "align": "right", "sortable": True},
    {"key": "rr", "label": "风险收益比", "kind": "num", "decimals": 2, "width": "84px", "align": "right", "sortable": True},
], key="rb_tbl", sort=None, min_width=1160)
st.caption("20 日超额 = 综合评分所在十分组的历史超额 + 当前 A/B 信号历史超额 ×0.5（截断 ±5%）。"
           "大部分持仓的超额接近 0 → 风险收益比主要由「下行 β 是否比普通 β 更大」决定（下跌时跌得更狠的排前面）。"
           "港股 / 韩股收盘早于美股，日收益错位，β 估计偏差较大。")

with st.expander("🏷 标注角色（核心 = 长期持有，只用期权保护、不卖股；其余可覆盖建议）", expanded=not user_roles):
    ed = st.data_editor(
        pd.DataFrame({"标的": tbl["ticker"], "建议": tbl["role_suggest"], "理由": tbl["role_why"],
                      "你的角色": [user_roles.get(t, "（用建议）") for t in tbl["ticker"]]}),
        hide_index=True, width="stretch", key="rb_roles", disabled=["标的", "建议", "理由"],
        column_config={"你的角色": st.column_config.SelectboxColumn(options=["（用建议）", *RB.ROLES], required=True)})
    st.caption("各角色最多可减比例：" + " · ".join(f"{RB.ROLE_ICON[r]} {r} {RB.MAX_TRIM[r]:.0%}" for r in RB.ROLES))
    if st.button("💾 保存角色", key="rb_save_roles"):
        RB.save_roles({r["标的"]: r["你的角色"] for r in ed.to_dict("records") if r["你的角色"] != "（用建议）"})
        st.rerun()

# ─── ③ 方案 ───────────────────────────────────────────────────────────────────
st.markdown("#### ③ 方案")
cur_loss = -down0 / nav
c1, c2 = st.columns([1.2, 2])
limit = c1.slider(f"「{down_name}」情景亏损上限（% 净值）", 2.0, max(30.0, round(cur_loss * 100 + 1)),
                  float(round(cur_loss * 100 * 0.6)), 0.5, key="rb_limit") / 100
mode = c1.radio("思路", ["组合：先减「可减仓」，再用指数 put 补足", "只减仓", "只用指数 put（不卖股）"], key="rb_mode")
need_total = max(0.0, -down0 - limit * nav)

fr = RB.frontier(tbl, nav)
fig = go.Figure(go.Scatter(x=fr["down_loss"] * 100, y=fr["exp_pnl"], mode="lines+markers", name="只靠减仓",
                           hovertemplate="下跌亏损 %{x:.1f}%<br>期望盈亏 $%{y:,.0f}<extra></extra>"))
fig.add_vline(x=limit * 100, line_dash="dot", line_color="#C7711F")
fig.update_layout(height=230, margin=dict(t=30, b=10, l=10, r=10), title=dict(text="只靠减仓：每降一点下跌亏损要放弃多少期望盈亏", font=dict(size=13)),
                  xaxis=dict(title="下跌情景亏损（% 净值）", autorange="reversed"), yaxis=dict(title="期望盈亏 $"),
                  plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)")
c2.plotly_chart(fig, width="stretch", config={"displayModeBar": False})

if mode.startswith("只减仓"):
    targets, new_loss, short = RB.auto_trims(tbl, nav, limit)
elif mode.startswith("只用指数"):
    targets, short = {r.ticker: r.shares for r in tbl.itertuples()}, need_total
else:
    only_trim = tbl.assign(role=[r if r == "可减仓" else "核心" for r in tbl["role"]])
    targets, new_loss, short = RB.auto_trims(only_trim, nav, limit)

core_off = tbl[tbl["role"].isin(["核心", "进攻"]) & ~tbl["ticker"].str.contains(r"\.", regex=True)]
trimmed = {t: tbl.set_index("ticker").at[t, "shares"] - v for t, v in targets.items()
           if tbl.set_index("ticker").at[t, "shares"] - v > 1e-9}
need_chains = tuple(sorted({"QQQ", "SOXX", "SPY"} | set(core_off["ticker"]) |
                           {t for t, n in trimmed.items() if n >= 100 and "." not in t}))
chains = _chains(need_chains)
idx_ideas = RB.index_hedge_ideas(chains, closes, ctx, scen, ev_date, short) if short > 0 and not mode.startswith("只减仓") else []
per_ideas = []
for r in core_off.itertuples():
    per_ideas += RB.overlay_ideas(r.ticker, r.shares, r.price or 0, ev_date, r.role, chains.get(r.ticker))
for t, n in trimmed.items():
    if n >= 100:
        pr_ = tbl.set_index("ticker").at[t, "price"]
        per_ideas += [i for i in RB.overlay_ideas(t, 0, pr_ or 0, ev_date, "可减仓", chains.get(t), trimmed=n)]

# ─── ④ 沙盘 ───────────────────────────────────────────────────────────────────
st.markdown("#### ④ 沙盘（可改目标股数、勾选期权方案，右侧实时对比）")
stock_rows = tbl[tbl["stock_value"] > 0]
sand = st.data_editor(
    pd.DataFrame({"标的": stock_rows["ticker"], "角色": stock_rows["role"], "现有股数": stock_rows["shares"],
                  "目标股数": [round(float(targets.get(t, s)), 3) for t, s in zip(stock_rows["ticker"], stock_rows["shares"])],
                  "价格": stock_rows["price"]}),
    hide_index=True, width="stretch", key=f"rb_sand_{mode}_{limit}", disabled=["标的", "角色", "现有股数", "价格"],
    column_config={"目标股数": st.column_config.NumberColumn(min_value=0.0, format="%.3f"),
                   "价格": st.column_config.NumberColumn(format="%.2f")}, height=280)
tgt_edit = {r["标的"]: float(r["目标股数"]) for r in sand.to_dict("records")}

chosen = []
if idx_ideas:
    st.markdown(f"**指数保护**（把「{down_name}」亏损再降约 ${short:,.0f}，选一个即可）")
    pick = st.radio(" ", ["不用", *[f"{i['ticker']}：{i['why']}" for i in idx_ideas]], index=1, key=f"rb_idx_{mode}_{limit}",
                    label_visibility="collapsed")
    chosen += [i for i in idx_ideas if pick.startswith(i["ticker"] + "：")]
if per_ideas:
    with st.expander(f"🎯 单只持仓的期权方案（{len(per_ideas)} 个：保护性 put / 领口 / 备兑 call / 实值 call 替代 / 卖 put 回补）"):
        for j, i in enumerate(per_ideas):
            if st.checkbox(f"**{i['ticker']} · {i['kind']}** — {i['why']}", key=f"rb_idea_{j}_{i['ticker']}_{i['kind']}"):
                chosen.append(i)

book2 = RB.apply_plan(book, tgt_edit, chosen)
sm1 = RB.summary(book2, closes, ctx, scen, hdays, alpha_of)
core_t = set(tbl.loc[tbl["role"].isin(["核心", "进攻"]), "ticker"])
ex0, ex1 = R.exposures(book), R.exposures(book2)
keep = float(ex1.reindex(list(core_t)).fillna(0).sum()) / float(ex0.reindex(list(core_t)).fillna(0).sum() or 1)

cmp_rows = [("组合 β", f"{sm0['beta']:.2f}", f"{sm1['beta']:.2f}"),
            ("下行 β", f"{sm0['beta_down']:.2f}", f"{sm1['beta_down']:.2f}"),
            ("1 日 VaR 95%", f"${sm0['var95']:,.0f}", f"${sm1['var95']:,.0f}"),
            ("现金占比", f"{sm0['cash_pct']:.0%}", f"{sm1['cash_pct']:.0%}"),
            *[(f"「{s['情景']}」{s['SPY']:+.1%}（{s['概率']:.0%}）", f"${sm0['scen'][s['情景']]:+,.0f}",
               f"${sm1['scen'][s['情景']]:+,.0f}") for s in scen],
            ("情景加权期望盈亏", f"${sm0['exp_pnl']:+,.0f}", f"${sm1['exp_pnl']:+,.0f}"),
            ("核心 + 进攻 敞口保留", "100%", f"{keep:.0%}")]
cA, cB = st.columns([1.1, 1])
cA.dataframe(pd.DataFrame(cmp_rows, columns=["指标", "现状", "方案"]), hide_index=True, width="stretch")
fig2 = go.Figure()
for name, sm, col in (("现状", sm0, "#8A877E"), ("方案", sm1, "#1C4F7A")):
    fig2.add_trace(go.Bar(name=name, x=[s["情景"] for s in scen], y=[sm["scen"][s["情景"]] for s in scen], marker_color=col,
                          text=[f"${sm['scen'][s['情景']] / 1000:+.0f}k" for s in scen], textposition="outside", cliponaxis=False))
fig2.update_layout(barmode="group", height=300, margin=dict(t=30, b=10, l=10, r=10),
                   title=dict(text="各情景组合盈亏：现状 vs 方案", font=dict(size=13)),
                   plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)", legend=dict(orientation="h", y=1.12))
cB.plotly_chart(fig2, width="stretch", config={"displayModeBar": False})
st.caption("期权盈亏含事件期的时间损耗（按到期前剩余时间重估）；指数 put 在上涨情景会亏掉大部分权利金——这就是保险费。"
           "注意：「下跌」情景按期间**最低点**估值（假设在低点附近平仓），且叠加了 IV 上升，对 put 偏有利；"
           "如果加了 put 后期望盈亏反而变高，说明你设的下跌概率 / 幅度比期权市场的定价更悲观，而不是 put 白赚。")

# ─── ⑤ 订单清单 + 保存 ────────────────────────────────────────────────────────
orders = []
sells = {i["ticker"]: i["sell_shares"] for i in chosen if i.get("sell_shares")}
for t, tgt in tgt_edit.items():
    cur = float(stock_rows.set_index("ticker").at[t, "shares"])
    tgt2 = min(tgt, cur - sells.get(t, 0)) if t in sells else tgt
    if abs(cur - tgt2) > 1e-9:
        px = float(stock_rows.set_index("ticker").at[t, "price"] or 0)
        orders.append({"动作": "卖出" if tgt2 < cur else "买入", "标的": t, "数量": f"{abs(cur - tgt2):g} 股",
                       "约金额": f"${abs(cur - tgt2) * px:,.0f}", "说明": f"{cur:g} → {tgt2:g} 股"})
for i in chosen:
    for lg in i["legs"]:
        orders.append({"动作": ("买入开仓" if lg["side"] > 0 else "卖出开仓"),
                       "标的": f"{lg['underlying']} {lg['expiry']} {lg['strike']:g} {lg['option_type'].upper()}",
                       "数量": f"{lg['contracts']} 张", "约金额": f"${lg['contracts'] * lg['mark'] * 100:,.0f}"
                                                         + ("（付出）" if lg["side"] > 0 else "（收入）"),
                       "说明": i["kind"]})
st.markdown("#### ⑤ 订单清单")
if orders:
    st.dataframe(pd.DataFrame(orders), hide_index=True, width="stretch")
else:
    st.caption("方案与现状相同。")

c1, c2 = st.columns([3, 1])
plan_name = c1.text_input("方案名称", value=f"{ev_name} {mode.split('：')[0]} 上限 {limit:.0%}", key="rb_plan_name")
if c2.button("💾 保存方案", type="primary", width="stretch", disabled=not orders):
    path = RB.save_plan({"name": plan_name, "saved": date.today().isoformat(), "event": ev_name, "event_date": ev_date,
                         "post_days": int(post), "scenarios": scen, "limit": limit, "mode": mode,
                         "targets": tgt_edit, "options": [{k: v for k, v in i.items() if k != "why"} | {"why": i["why"]} for i in chosen],
                         "orders": orders, "before": {k: v for k, v in sm0.items()}, "after": {k: v for k, v in sm1.items()},
                         "prices": {r.ticker: r.price for r in tbl.itertuples()}})
    st.success(f"已保存：{path.name}。事件过后可以对照「按方案」与「实际持仓」的表现。")
plans = RB.list_plans()
if plans:
    with st.expander(f"📁 已保存的方案（{len(plans)}）"):
        for p in plans[:10]:
            st.markdown(f"- **{p.get('name')}**（{p.get('saved')}，{len(p.get('orders', []))} 笔）："
                        f"{down_name}亏损 ${p['before']['scen'].get(down_name, 0):,.0f} → ${p['after']['scen'].get(down_name, 0):,.0f}"
                        if p.get("before") and p.get("after") else f"- {p.get('name')}")
