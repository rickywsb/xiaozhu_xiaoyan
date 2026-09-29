"""views/sections/home_overview.py — 驾驶舱 · 总览：市场状态 / 组合概况 / 持仓评分 / 异动 / 轮动 / 日报摘要"""

import io
import json
import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config
from core import rating as RT
from core import posture
from core import sectors as sc
from core import tracker as tk
from core import risk as RK
from core.enrich import enrich
from core.price_updater import load_cache
from core.ui import stock_list


# ─── 数据（缓存）──────────────────────────────────────────────────────────────

@st.cache_data(show_spinner="🧮 正在计算全市场评分…（每个交易日首次约 1 分钟）", ttl=3600)
def _ratings(day_key: str) -> tuple[pd.DataFrame, str | None, dict]:
    df, as_of = RT.get_ratings()
    return df, as_of, dict(df.attrs)


@st.cache_data(show_spinner=False, ttl=1800)
def _board() -> pd.DataFrame:
    return sc.sector_board()


@st.cache_data(show_spinner=False, ttl=1800)
def _posture(breadth50: float | None) -> dict:
    return posture.compute(breadth50, _board())


@st.cache_data(show_spinner=False, ttl=900)
def _risk(pf_json: str) -> dict:
    book, closes = RK.build_book(json.loads(pf_json), load_cache() or {})
    pr = RK.portfolio_risk(book, closes)
    top_rc = pr["risk_contrib"].index[0] if len(pr["risk_contrib"]) else None
    return {"nav": pr["nav"], "var95": pr["var95"], "beta_SPY": pr.get("beta_SPY"),
            "top_rc": top_rc, "leverage": pr["leverage"]}


@st.cache_data(show_spinner=False, ttl=15)
def _quotes(tickers: tuple[str, ...]) -> pd.DataFrame:
    return tk.live_quotes(list(tickers))


@st.cache_data(show_spinner="📋 正在汇总持仓评分…", ttl=600)
def _holdings_table(tickers: tuple[str, ...], ratings_json: str, quotes_json: str, names_json: str) -> pd.DataFrame:
    return enrich(list(tickers), pd.read_json(io.StringIO(ratings_json)), pd.read_json(io.StringIO(quotes_json)),
                  json.loads(names_json))


pf = json.loads(config.PORTFOLIO_PATH.read_text(encoding="utf-8"))
names = {p["yf_ticker"].upper(): p.get("display", p["yf_ticker"])
         for a in pf.get("accounts", []) for p in a.get("positions", []) if p["yf_ticker"].upper() != config.CASH_TICKER}
from core import watchlist as _wl
watch = [t for t in _wl.load() if t not in names and t != config.CASH_TICKER]

ratings, as_of, meta = _ratings(config.market_today().isoformat())
post = _posture(meta.get("breadth50"))

# ─── 市场状态 ─────────────────────────────────────────────────────────────────
if post:
    items = "".join(
        f'<div style="display:flex;flex-direction:column;gap:6px;min-width:0">'
        f'<div style="display:flex;justify-content:space-between;font-size:13px"><span style="color:#C9C5BB">{k}</span>'
        f'<span class="pz-num" style="font-weight:600">{v["score"]:.0f}</span></div>'
        f'<div style="height:6px;border-radius:3px;background:#33363D"><div style="height:6px;border-radius:3px;'
        f'width:{v["score"]:.0f}%;background:{"#6FBF8E" if v["score"] >= 50 else "#E8A84C"}"></div></div>'
        f'<span style="font-size:11px;color:#9A968C">{v["note"]}</span></div>'
        for k, v in post["items"].items())
    st.html(
        f'<div class="pz-dark" style="display:flex;gap:32px;align-items:center">'
        f'<div style="display:flex;flex-direction:column;gap:6px;width:290px;flex-shrink:0">'
        f'<span style="font-size:12px;color:#9A968C;letter-spacing:1px">市场状态</span>'
        f'<span style="font-size:26px;font-weight:700">{post["headline"]}</span>'
        f'<span style="font-size:14px;color:#E8A84C">{post["stance"]}</span></div>'
        f'<div style="flex-grow:1;display:grid;grid-template-columns:repeat({len(post["items"])},minmax(0,1fr));gap:18px">'
        f'{items}</div></div>')

# ─── 组合概况 ─────────────────────────────────────────────────────────────────
held_t = list(names)
quotes = _quotes(tuple(sorted(set(held_t) | set(watch) | {"SPY"})))
if not quotes.empty:
    from core.fx import get_fx_rates
    board = tk.intraday_board(tk.universe(pf, watch), quotes, get_fx_rates())
else:
    board = pd.DataFrame()
risk = _risk(json.dumps(pf, sort_keys=True))
k = st.columns(4)
k[0].metric("净值", f"${risk['nav']:,.0f}", help="股票 + 期权 + 现金")
if not board.empty:
    h = board[board["group"] == "持仓"]
    pnl, prev = float(h["pnl_usd"].sum()), float(h["prev_value_usd"].sum())
    k[1].metric("今日盈亏（股票）", f"${pnl:+,.0f}",
                f"{pnl / prev * 100:+.2f}% · 涨 {int((h['chg'] > 0).sum())} / 跌 {int((h['chg'] < 0).sum())}")
k[2].metric("1 日 VaR 95%", f"${risk['var95']:,.0f}", f"{risk['var95'] / risk['nav'] * 100:.1f}% 净值 · 风险贡献第一 {risk['top_rc']}",
            delta_color="off", delta_arrow="off")
k[3].metric("组合 β vs SPY", f"{risk['beta_SPY']:.2f}", f"总敞口 {risk['leverage']:.2f}× 净值",
            delta_color="off", delta_arrow="off")

# ─── 异动 / 轮动 / 日报摘要（一行三栏）──────────────────────────────────────────
c_mv, c_rot, c_ai = st.columns([1, 1.15, 1], gap="large")
with c_mv:
    st.markdown("#### 今日异动")
    if not board.empty:
        mv = board[board["group"] != "基准"].copy()
        mv["abs"] = mv["chg"].abs()
        rsub = ratings.set_index("ticker")
        for _, r in mv.sort_values("abs", ascending=False).head(5).iterrows():
            vs = rsub.at[r["ticker"], "volume_state"] if r["ticker"] in rsub.index else ""
            color = "#1F7A45" if r["chg"] > 0 else "#B3362A"
            st.html(f'<div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;'
                    f'border-bottom:1px solid #F0EEE8"><div><div style="font-weight:700">{r["ticker"]}</div>'
                    f'<div style="font-size:12px;color:#5E5B53">{r["group"]}{" · " + vs if vs and vs != "正常" else ""}</div></div>'
                    f'<span class="pz-num" style="color:{color};font-weight:600">{r["chg"] * 100:+.2f}%</span></div>')

with c_rot:
    st.markdown("#### 板块轮动")
    bd = _board()
    if not bd.empty:
        sub = bd[bd["group"].isin(["主题", "自定义"]) | (bd["key"] == "XLK")].dropna(subset=["rrg_x", "rrg_y"])
        colors = {"🟢 领先": "#1F6B3E", "🔵 改善": "#1C4F7A", "🟡 转弱": "#8A4B0A", "🔴 落后": "#8E2E22"}
        fig = go.Figure(go.Scatter(
            x=sub["rrg_x"] * 100, y=sub["rrg_y"] * 100, mode="markers+text", text=sub["name"],
            textposition="top center", textfont=dict(size=10),
            marker=dict(size=9, color=[colors.get(q, "#8A877E") for q in sub["quadrant"]]),
            hovertemplate="%{text}<br>RS趋势 %{x:+.1f}%<br>RS动量 %{y:+.1f}%<extra></extra>"))
        lim_x = max(5.0, float((sub["rrg_x"] * 100).abs().max()) * 1.2)
        lim_y = max(5.0, float((sub["rrg_y"] * 100).abs().max()) * 1.2)
        for x0, x1, y0, y1, c in [(0, lim_x, 0, lim_y, "rgba(38,166,65,0.08)"), (0, lim_x, -lim_y, 0, "rgba(232,168,76,0.10)"),
                                  (-lim_x, 0, -lim_y, 0, "rgba(215,58,74,0.07)"), (-lim_x, 0, 0, lim_y, "rgba(76,155,232,0.08)")]:
            fig.add_shape(type="rect", x0=x0, x1=x1, y0=y0, y1=y1, fillcolor=c, line_width=0, layer="below")
        fig.update_layout(height=250, margin=dict(t=6, b=6, l=6, r=6), showlegend=False,
                          xaxis=dict(range=[-lim_x, lim_x], showgrid=False, zeroline=False, showticklabels=False),
                          yaxis=dict(range=[-lim_y, lim_y], showgrid=False, zeroline=False, showticklabels=False),
                          plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)")
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
        st.caption("右上领先 · 左上改善 · 右下转弱 · 左下落后。详见「市场 · 板块雷达」。")

with c_ai:
    st.markdown("#### AI 日报摘要")
    rp = config.DATA_DIR / "daily_report_latest.json"
    rep = None
    if rp.exists():
        try:
            d = json.loads(rp.read_text(encoding="utf-8"))
            rep = d["report"] if d.get("date") == config.market_today().isoformat() else None
        except Exception:
            rep = None
    if rep:
        st.markdown(f"**{rep.get('headline', '')}**")
        if rep.get("market_note"):
            st.caption(rep["market_note"])
    else:
        st.caption("今日日报尚未生成，到「日报」页一键生成。")

# ─── 持仓评分（整行宽度）──────────────────────────────────────────────────────
st.markdown("#### 持仓评分")
val = RT.load_validation() or {}
tt = (val.get("top_tier") or {}).get(f"≥{RT.TOP_TIER}", {})
st.caption(
    f"评分 = 全市场约 {val.get('n_tickers', 540)} 只中的百分位（{as_of}）。**强势筛选**：历史上 ≥{RT.TOP_TIER} 分之后 20 日"
    + (f"平均跑赢 {tt['excess'] * 100:+.1f}%（前后两段都成立）" if tt else "表现待验证")
    + f"；{RT.TOP_TIER} 分以下只作排名参考。操作倾向历史区分度弱，仅作状态提示。点击任意一行打开个股详情。")
tbl = _holdings_table(tuple(held_t), ratings.to_json(), quotes.to_json(), json.dumps(names))
tbl = tbl.sort_values("score", ascending=False, na_position="last").reset_index(drop=True)
_sectors = {p["yf_ticker"].upper(): p.get("sector", "") for a in pf.get("accounts", []) for p in a.get("positions", [])}
stock_list(tbl, key="home_hold", sectors=_sectors)

