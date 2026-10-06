"""views/sections/sectors.py — 🧭 板块雷达：实时强弱榜 + 点选板块看成分股与龙头候选 / 轮动 / 宽度"""

import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core.stock_chart import click_hint
from core import sectors as sc
from core import watchlist
from core import intraday as INTRA
from core import rating as RT
from core import sector_live as SLV
from core import stock_chart as SC
from core import tracker as tk
from core.ui import pz_table, _clean

LIVE_REFRESH = 60

click_hint()
st.caption(
    "11 个 SPDR 行业 + 9 个主题 ETF + 自定义篮子（光模块 / 存储 / AI电力，等权合成）。"
    "强弱用与持仓**同一套动量打分**（短期热度 + 6-1 月趋势，按波动率调整，板块间相对排名）。"
    "仅供辅助研究，非投资建议。"
)


# ─── 缓存 ─────────────────────────────────────────────────────────────────────

@st.cache_data(show_spinner="🧭 正在计算板块强弱…", ttl=1800)
def _board() -> pd.DataFrame:
    return sc.sector_board()


@st.cache_data(show_spinner=False, ttl=1800)
def _trails(keys: tuple[str, ...]) -> pd.DataFrame:
    return sc.rrg_trails(list(keys))


@st.cache_data(show_spinner=False, ttl=86400)
def _sp500() -> pd.DataFrame:
    try:
        return sc.sp500_constituents()
    except Exception:
        return pd.DataFrame()


@st.cache_data(show_spinner="🌊 正在下载 S&P 500 成分股计算宽度…（约 15-60 秒）", ttl=21600)
def _breadth() -> pd.DataFrame:
    sp = _sp500()
    return sc.breadth(sp) if not sp.empty else pd.DataFrame()


@st.cache_data(show_spinner="🔍 正在整理各板块成分（含 ETF 真实持仓，每天一次）…", ttl=86400)
def _members_all() -> dict:
    sp = _sp500()
    return {k: SLV.members(k, sp) for k in sc.sector_board()["key"]}


@st.cache_data(show_spinner=False, ttl=15)
def _quotes(tickers: tuple[str, ...]) -> pd.DataFrame:
    return tk.live_quotes(list(tickers))




def _pct(v):
    return v * 100 if v is not None and pd.notna(v) else None


# ─── 顶栏 ─────────────────────────────────────────────────────────────────────

_, c_btn = st.columns([5, 1])
if c_btn.button("🔄 刷新", type="primary", width="stretch"):
    _board.clear()
    _trails.clear()
    _members_all.clear()
    st.rerun()

board = _board()
if board.empty:
    st.error("板块数据下载失败，请稍后点「🔄 刷新」重试。")
    st.stop()

top = board.iloc[0]
n_lead = int((board["quadrant"] == sc.QUADRANTS[(True, True)]).sum())
n_improve = int((board["quadrant"] == sc.QUADRANTS[(False, True)]).sum())
risers = board.dropna(subset=["rank_chg"]).sort_values("rank_chg", ascending=False)
k1, k2, k3, k4 = st.columns(4)
k1.metric("🏆 最强板块", f"{top['name']}", f"20日 {top['ret_20d']*100:+.1f}%")
k2.metric("🟢 领先象限", f"{n_lead} 个")
k3.metric("🔵 改善象限", f"{n_improve} 个", help="相对大盘仍偏弱但在转强，轮动的早期候选")
if not risers.empty and risers.iloc[0]["rank_chg"] > 0:
    r0 = risers.iloc[0]
    k4.metric("🚀 排名上升最快(5日)", r0["name"], f"+{int(r0['rank_chg'])} 名")

tab_rank, tab_rrg, tab_breadth = st.tabs(["🏆 强弱榜 · 成分龙头（实时）", "🔄 轮动图", "🌊 市场宽度"])

# ═══════════════════════════════════════════════════════════════════════════════
# 强弱榜（实时）+ 选中板块的成分股与龙头候选
# ═══════════════════════════════════════════════════════════════════════════════
status = tk.us_market_status()
is_live = status == "交易中"
held_s, watch_s, names_s = RT._portfolio_sets()
mem_of = _members_all()
etf_keys = tuple(sorted(board.loc[board["group"] != "自定义", "key"]))


def _chg(v):
    if pd.isna(v):
        return "🆕"
    v = int(v)
    return f"↑{v}" if v > 0 else (f"↓{-v}" if v < 0 else "→")


@st.fragment(run_every=LIVE_REFRESH if is_live else None)
def live_rank():
    now = INTRA.now_et()
    c_stat, c_b1, c_b2 = st.columns([5, 1, 1])
    if c_b1.button("🔄 刷新报价", width="stretch", key="sec_refresh_q"):
        _quotes.clear()
    force = c_b2.button("🧮 重算评分", width="stretch", disabled=not is_live, key="sec_force",
                        help="立即按盘中 K 线重算全市场评分（否则每 10 分钟自动一次，与驾驶舱共用）")
    with st.spinner("📡 正在读取全市场盘中数据（每 10 分钟一次，与驾驶舱共用）…"):
        snap = INTRA.snapshot(tk.last_close_date() or "", held_s, watch_s, names_s, force=force)
    badge = {"交易中": "🟢 美股交易中", "盘前": "🟡 美股盘前", "已收盘": "⚪ 美股已收盘", "休市": "⚪ 美股休市"}[status]
    score_txt = (f"成分评分 = 盘中预估（美东 {snap['time']} 重算，每 10 分钟）" if snap["tbl"] is not None
                 else f"成分评分 = 收盘（{snap['note']}）")
    c_stat.caption(f"{badge} ｜ ETF 报价更新于美东 {now:%H:%M:%S}" + (f"（每 {LIVE_REFRESH} 秒）" if is_live else "")
                   + f" ｜ {score_txt}")

    etf_q = _quotes(etf_keys + ("SPY",))
    stats = SLV.sector_live_stats(board, mem_of, snap["quotes"], etf_q)
    bd = board.merge(stats, on="key", how="left")
    allq = snap["quotes"]
    k = st.columns(4)
    live_rows = bd.dropna(subset=["today"]).sort_values("today", ascending=False)
    if not live_rows.empty:
        k[0].metric("今日最强板块", live_rows.iloc[0]["name"], f"{live_rows.iloc[0]['today'] * 100:+.2f}%")
        k[1].metric("今日最弱板块", live_rows.iloc[-1]["name"], f"{live_rows.iloc[-1]['today'] * 100:+.2f}%")
    if not allq.empty:
        k[2].metric("全市场上涨比例", f"{(allq['chg'] > 0).mean():.0%}", f"{len(allq)} 只股票池", delta_color="off", delta_arrow="off")
    spy = etf_q[etf_q["ticker"] == "SPY"] if not etf_q.empty else pd.DataFrame()
    if not spy.empty:
        k[3].metric("SPY", f"{spy.iloc[0]['last']:,.2f}", f"{spy.iloc[0]['chg'] * 100:+.2f}%")

    groups = st.multiselect("分组", ["行业", "主题", "自定义"], default=["行业", "主题", "自定义"], key="rank_groups")
    show = bd[bd["group"].isin(groups)].copy()
    sel = st.session_state.get("sec_sel") or (show.iloc[0]["key"] if len(show) else None)
    rows = [{
        "ticker": r["key"], "name": r["name"], "ticker_label": r["name"],
        "sub": f'{r["key"]} · {r["group"]}' if r["key"] != r["name"] else r["group"],
        "rank": int(r["rank"]), "chg_txt": _chg(r["rank_chg"]), "rank_chg": _clean(r["rank_chg"]),
        "composite": _clean(r["composite"]), "today": _pct(r["today"]), "up_pct": _pct(r["up_pct"]),
        "n_surge": _clean(r["n_surge"]), "flag": r["flag"] or "",
        "ret_5d": _pct(r["ret_5d"]), "ret_20d": _pct(r["ret_20d"]), "ret_60d": _pct(r["ret_60d"]),
        "rs_60d": _pct(r["rs_60d"]), "quadrant": r["quadrant"],
    } for _, r in show.iterrows()]
    _p = lambda key, lab, w="66px": {"key": key, "label": lab, "kind": "num", "decimals": 1, "sign": True, "color": True,
                                     "suffix": "%", "width": w, "sortable": True, "align": "right"}
    clicked = pz_table(rows, [
        {"key": "rank", "label": "排名", "kind": "num", "decimals": 0, "width": "46px", "sortable": True},
        {"key": "chg_txt", "label": "5日", "kind": "text", "width": "44px", "sortKey": "rank_chg", "sortable": True},
        {"key": "ticker_label", "label": "板块", "kind": "stock", "width": "minmax(110px,1.2fr)"},
        {"key": "composite", "label": "综合", "kind": "num", "decimals": 2, "sign": True, "color": True,
         "width": "58px", "sortable": True, "align": "right"},
        {**_p("today", "今日", "68px"), "decimals": 2},
        {"key": "up_pct", "label": "成分上涨", "kind": "bar", "suffix": "%", "width": "104px", "sortable": True},
        {"key": "n_surge", "label": "放量", "kind": "num", "decimals": 0, "hot": 3, "width": "48px", "sortable": True, "align": "right"},
        {"key": "flag", "label": "", "kind": "text", "colors": {"强势回调": "#8A4B0A", "弱势反弹": "#1C4F7A"}, "width": "70px"},
        _p("ret_5d", "5日"), _p("ret_20d", "20日"), _p("ret_60d", "60日"), _p("rs_60d", "vs SPY 60日", "84px"),
        {"key": "quadrant", "label": "象限", "kind": "pill", "width": "80px"},
    ], key="sec_rank", sort="", min_width=1100, select=True, selected=sel)
    if clicked and clicked != sel:
        st.session_state["sec_sel"] = clicked
        st.rerun(scope="fragment")
    st.caption("👆 **点任意板块，下方展开它的成分股与龙头候选**。今日 = ETF 实时涨跌（自定义篮子为成分等权）；"
               "成分上涨 / 放量 = 成分股今日上涨比例、预计全天量比 ≥1.5 的只数（每 10 分钟）；"
               "强势回调 = 20 日 > +3% 但今日 < −1.5%，弱势反弹反之。")

    if not sel or sel not in set(bd["key"]):
        return
    br = bd.set_index("key").loc[sel].to_dict()
    mem = mem_of.get(sel, pd.DataFrame())
    ratings_close, _ = RT.load_latest()
    ratings = snap["tbl"] if snap["tbl"] is not None else ratings_close
    # 所有板块的龙头（每 10 分钟一次）→ 记录，供日后检验
    bk = (INTRA.bucket(now), snap["time"])
    if st.session_state.get("_sec_leaders_key") != bk:
        by_sector = {}
        for kk, mm in mem_of.items():
            if kk not in set(bd["key"]) or mm.empty:
                continue
            row = bd.set_index("key").loc[kk].to_dict()
            ld = SLV.leaders(SLV.member_table(kk, mm, row, snap["quotes"], snap["panel_close"], None, ratings,
                                              ratings_close, snap["signals"]))
            by_sector[kk] = [{"ticker": r["ticker"], "score": r["score"], "rel20": r["rel20"]} for r in ld.to_dict("records")]
        if status in ("交易中", "已收盘"):
            SLV.log_leaders(now.date().isoformat(), by_sector, status)
        st.session_state["_sec_leaders_key"] = bk

    live = _quotes(tuple(sorted(mem["ticker"]))) if not mem.empty else pd.DataFrame()
    mt = SLV.member_table(sel, mem, br, snap["quotes"], snap["panel_close"], live, ratings, ratings_close, snap["signals"])
    st.divider()
    head = (f"### {br['name']}（{sel}）" if sel != br["name"] else f"### {br['name']}")
    st.markdown(head)
    h = st.columns([4, 1])
    h[0].caption(" ｜ ".join(x for x in [
        f"今日 {br['today'] * 100:+.2f}%" if br.get("today") == br.get("today") and br.get("today") is not None else "",
        f"{int(br['n_members'])} 只成分，上涨 {br['up_pct']:.0%}" if br.get("up_pct") == br.get("up_pct") and br.get("up_pct") is not None else "",
        f"排名 #{int(br['rank'])} {br['quadrant']}", f"20 日 {br['ret_20d'] * 100:+.1f}% · 60 日 {br['ret_60d'] * 100:+.1f}%"] if x))
    if br["group"] != "自定义" and h[1].button("📈 ETF 图表", key=f"sec_etf_{sel}", width="stretch"):
        st.session_state[SC._OPEN] = {"owner": "sec_members", "ticker": sel, "name": br["name"]}
    if mt.empty:
        st.info("该板块暂无成分数据（S&P 500 成分表 / ETF 持仓获取失败）。")
        return

    ld = SLV.leaders(mt)
    st.markdown("**🏆 龙头候选**（跑赢板块的股票里综合评分最高的 3 只；评分为已检验的全市场百分位）")
    cols = st.columns(len(ld) or 1)
    for col, r in zip(cols, ld.to_dict("records")):
        tag = "持仓" if r["ticker"] in held_s else ("关注" if r["ticker"] in watch_s else "")
        chg = f"{r['chg'] * 100:+.2f}%" if r.get("chg") is not None else ""
        col.html(
            f'<div class="pz-card" style="height:100%"><div style="display:flex;justify-content:space-between;align-items:baseline">'
            f'<span style="font-size:20px;font-weight:700">{r["ticker"]}</span>'
            f'<span class="pz-num" style="font-size:22px;font-weight:600;color:#155E36">{int(r["score"])}</span></div>'
            f'<div style="font-size:12px;color:#5E5B53;margin-bottom:6px">{r["name"]}{" · " + tag if tag else ""}'
            f'<span class="pz-num" style="float:right;color:{"#1F7A45" if (r.get("chg") or 0) >= 0 else "#B3362A"}">'
            f'{r["price"]:,.2f} {chg}</span></div>'
            f'<div style="font-size:12.5px;line-height:1.6">{r["why"]}</div></div>')

    st.markdown("**全部成分**（点任意一只打开个股详情）")
    rows = []
    for r in mt.sort_values(["score"], ascending=False, na_position="last").to_dict("records"):
        t = r["ticker"]
        tag = "持仓" if t in held_s else ("关注" if t in watch_s else "")
        rows.append({"ticker": t, "name": r["name"],
                     "sub": " · ".join(x for x in [r["name"] if str(r["name"]).upper() != t else "", tag,
                                                   "ETF 前十大" if _clean(r.get("weight")) else ""] if x),
                     "price_txt": f"{r['price']:,.2f}", "chg": _pct(r["chg"]), "vs": _pct(r["vs_etf_today"]),
                     "weight": _pct(r["weight"]), "score": int(r["score"]) if _clean(r.get("score")) is not None else None,
                     "d5": int(r["score_delta"]) if snap["tbl"] is not None and _clean(r.get("score_delta")) is not None else None,
                     "rel20": _pct(r["rel20"]), "ret60": _pct(r["ret60"]), "off_high": _pct(r["off_high"]),
                     "pvr": _clean(r.get("pvr")), "rev": _pct(r["rev_yoy"]), "eps": _pct(r["eps_yoy"]), "sig": r["signals"]})
    pz_table(rows, [
        {"key": "ticker", "label": "股票", "kind": "stock", "width": "minmax(130px,1.3fr)", "sortable": True},
        {"key": "price_txt", "label": "价格 · 今日", "kind": "price", "chg": "chg", "width": "96px", "sortable": True, "sortKey": "chg"},
        {**_p("vs", "强于板块", "70px"), "decimals": 1},
        {"key": "weight", "label": "ETF权重", "kind": "num", "decimals": 1, "suffix": "%", "width": "64px", "align": "right", "sortable": True},
        {"key": "score", "label": "评分", "kind": "score", "delta": "d5", "width": "86px", "sortable": True},
        _p("rel20", "相对板块20日", "92px"), _p("ret60", "60日"),
        {"key": "off_high", "label": "距52周高", "kind": "num", "decimals": 1, "suffix": "%", "color": True, "width": "70px",
         "align": "right", "sortable": True},
        {"key": "pvr", "label": "预计量比", "kind": "num", "decimals": 1, "suffix": "×", "hot": 1.5, "width": "66px",
         "align": "right", "sortable": True},
        _p("rev", "营收同比", "70px"), _p("eps", "EPS同比", "70px"),
        {"key": "sig", "label": "今日信号", "kind": "small", "width": "minmax(120px,1.2fr)"},
    ], key="sec_members", sort="", min_width=1260)
    st.caption("价格 / 今日为实时报价（每 60 秒）；评分旁箭头 = 盘中较昨收（非交易时段无）；相对板块 20 日 = 个股 20 日收益 − 板块 ETF 20 日收益；"
               "成分 = ETF 真实前十大持仓（含 TSM、ASML 等 ADR，标「ETF 前十大」）∪ S&P 500 行业映射。龙头候选每天记录，几周后检验是否跑赢板块。")
    cand = [t for t in mt["ticker"] if t not in watch_s and t not in held_s]
    c_sel, c_add = st.columns([4, 1])
    picks = c_sel.multiselect("加入 Watch List", cand, key=f"add_wl_{sel}", placeholder="选择要关注的成分股")
    if c_add.button("⭐ 加入", disabled=not picks, width="stretch", key=f"add_btn_{sel}"):
        watchlist.add(picks)
        st.success(f"已加入：{', '.join(picks)}")


with tab_rank:
    live_rank()

# ═══════════════════════════════════════════════════════════════════════════════
# 轮动图（RRG 风格）
# ═══════════════════════════════════════════════════════════════════════════════
_QUAD_COLOR = {"🟢 领先": "#26a641", "🟡 转弱": "#E8A84C", "🔴 落后": "#d73a4a", "🔵 改善": "#4C9BE8"}

with tab_rrg:
    st.caption(
        "横轴 **RS 趋势** = 60 日相对 SPY 收益；纵轴 **RS 动量** = 20 日相对收益较 10 日前的变化。"
        "板块通常按 **🔵改善 → 🟢领先 → 🟡转弱 → 🔴落后** 顺时针轮动。尾迹为过去 4 周（每 5 个交易日一个点）。"
    )
    c1, c2 = st.columns([3, 1])
    rrg_groups = c1.multiselect("分组", ["行业", "主题", "自定义"], default=["主题", "自定义"],
                                key="rrg_groups")
    show_tail = c2.toggle("显示尾迹", value=True)
    keys = tuple(board.loc[board["group"].isin(rrg_groups), "key"])
    tr = _trails(keys) if keys else pd.DataFrame()

    if tr.empty:
        st.info("请选择至少一个分组。")
    else:
        latest = tr[tr["t"] == 0]
        lim_x = max(0.05, float(tr["x"].abs().max()) * 1.15) * 100
        lim_y = max(0.05, float(tr["y"].abs().max()) * 1.15) * 100
        fig = go.Figure()
        for (x0, x1, y0, y1, color, label) in [
            (0, lim_x, 0, lim_y, "rgba(38,166,65,0.07)", "🟢 领先"),
            (0, lim_x, -lim_y, 0, "rgba(232,168,76,0.07)", "🟡 转弱"),
            (-lim_x, 0, -lim_y, 0, "rgba(215,58,74,0.07)", "🔴 落后"),
            (-lim_x, 0, 0, lim_y, "rgba(76,155,232,0.07)", "🔵 改善"),
        ]:
            fig.add_shape(type="rect", x0=x0, x1=x1, y0=y0, y1=y1, fillcolor=color, line_width=0,
                          layer="below")
            fig.add_annotation(x=(x0 + x1) / 2, y=y1 * 0.92 if y1 > 0 else y0 * 0.92, text=label,
                               showarrow=False, font=dict(size=13, color="rgba(128,128,128,0.8)"))
        for key, g in tr.groupby("key"):
            g = g.sort_values("t", ascending=False)       # 从旧到新
            last = g.iloc[-1]
            quad = sc.QUADRANTS[(last["x"] > 0, last["y"] > 0)]
            color = _QUAD_COLOR[quad]
            if show_tail and len(g) > 1:
                fig.add_trace(go.Scatter(
                    x=g["x"] * 100, y=g["y"] * 100, mode="lines+markers",
                    line=dict(color=color, width=1.5), marker=dict(size=4, color=color),
                    opacity=0.45, showlegend=False, hoverinfo="skip",
                ))
            fig.add_trace(go.Scatter(
                x=[last["x"] * 100], y=[last["y"] * 100], mode="markers+text",
                marker=dict(size=12, color=color, line=dict(width=1, color="white")),
                text=[last["name"]], textposition="top center", showlegend=False,
                hovertemplate=f"<b>{last['name']}</b> ({key})<br>RS趋势 %{{x:+.1f}}%"
                              f"<br>RS动量 %{{y:+.1f}}%<extra>{quad}</extra>",
            ))
        fig.add_hline(y=0, line_color="rgba(128,128,128,0.5)", line_width=1)
        fig.add_vline(x=0, line_color="rgba(128,128,128,0.5)", line_width=1)
        fig.update_layout(
            xaxis=dict(title="RS 趋势：60日相对SPY (%)", range=[-lim_x, lim_x], zeroline=False,
                       gridcolor="rgba(128,128,128,0.15)"),
            yaxis=dict(title="RS 动量：20日相对收益变化 (%)", range=[-lim_y, lim_y], zeroline=False,
                       gridcolor="rgba(128,128,128,0.15)"),
            height=620, margin=dict(t=20, b=20, l=20, r=20),
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(fig, width="stretch")

        lq = latest.merge(board[["key", "quadrant"]], on="key")
        quads = {q: lq.loc[lq["quadrant"] == q, "name"].tolist() for q in _QUAD_COLOR}
        cols = st.columns(4)
        for col, (q, names) in zip(cols, quads.items()):
            col.markdown(f"**{q}**  \n" + ("、".join(names) if names else "—"))

# ═══════════════════════════════════════════════════════════════════════════════
# 市场宽度
# ═══════════════════════════════════════════════════════════════════════════════
with tab_breadth:
    st.caption(
        "宽度 = 板块成分股中 **站上 MA20 / 站上 MA50 / 收盘创 20 日新高** 的比例。"
        "ETF 涨但宽度低 = **少数权重股拉动**，持续性存疑；宽度高且 20 日上涨 = 普涨，趋势更扎实；"
        "宽度高但 20 日仍下跌 = 跌后普遍反弹。"
        "成分来自 S&P 500（GICS 行业/细分行业）与自定义名单；成分 <5 只的板块不计算。"
    )
    if "breadth_on" not in st.session_state:
        st.session_state["breadth_on"] = False
    if not st.session_state["breadth_on"]:
        if st.button("🌊 计算市场宽度（下载约 500 只成分股，15-60 秒）", type="primary"):
            st.session_state["breadth_on"] = True
            st.rerun()
    else:
        br = _breadth()
        if br.empty:
            st.error("S&P 500 成分表获取失败（Wikipedia），请稍后重试。")
        else:
            br = br.merge(board[["key", "ret_20d"]], on="key", how="left")

            def _read(r) -> str:
                if r["above_ma20"] >= 0.6:
                    # 多数站上 MA20 但 20 日仍下跌 = 跌后普遍反弹，尚不是趋势性普涨
                    if pd.notna(r.get("ret_20d")) and r["ret_20d"] < 0:
                        return "🔵 普遍反弹"
                    return "🟢 普涨"
                if pd.notna(r.get("ret_20d")) and r["ret_20d"] > 0 and r["above_ma20"] < 0.4:
                    return "⚠️ 少数股拉动"
                if r["above_ma20"] <= 0.2:
                    return "🔴 普跌"
                return "🟡 分化"

            br["判读"] = br.apply(_read, axis=1)
            br = br.sort_values("above_ma20", ascending=False)
            rows = [{
                "ticker": (r["key"] if r["group"] in ("行业", "主题") else ("SPY" if r["key"] == "S&P 500" else None)),
                "name": r["name"], "ticker_label": r["name"],
                "sub": f'{r["key"]} · {r["group"]} · {int(r["n"])} 只成分',
                "read": r["判读"], "ma20": _pct(r["above_ma20"]), "ma50": _pct(r["above_ma50"]),
                "hi20": _pct(r["new_high20"]), "ret_20d": _pct(r["ret_20d"]),
            } for _, r in br.iterrows()]
            pz_table(rows, [
                {"key": "ticker_label", "label": "板块", "kind": "stock", "width": "minmax(150px,1.4fr)"},
                {"key": "read", "label": "判读", "kind": "pill", "width": "110px"},
                {"key": "ma20", "label": "站上 MA20", "kind": "bar", "suffix": "%", "width": "150px", "sortable": True},
                {"key": "ma50", "label": "站上 MA50", "kind": "bar", "suffix": "%", "width": "150px", "sortable": True},
                {"key": "hi20", "label": "创 20 日新高", "kind": "bar", "suffix": "%", "width": "150px", "sortable": True,
                 "barColor": "#1C4F7A"},
                {"key": "ret_20d", "label": "板块 20 日", "kind": "num", "decimals": 1, "sign": True, "color": True,
                 "suffix": "%", "width": "84px", "sortable": True, "align": "right"},
            ], key="sec_breadth", min_width=860)
