"""views/sections/live.py — Day 0 追踪（持仓页）。盘中看板已并入驾驶舱（home_overview.py）。"""

import json
import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config
from core.stock_chart import click_hint
from core.ui import pz_table, pct, _clean
from core import tracker as tk
from core.fx import get_fx_rates
from core.github_storage import sync_to_github

REFRESH_SECONDS = 15
_GREEN, _RED = "#26a641", "#d73a4a"

click_hint()


# ─── 数据 ─────────────────────────────────────────────────────────────────────

def _universe() -> dict[str, dict]:
    pf = json.loads(config.PORTFOLIO_PATH.read_text(encoding="utf-8"))
    wl = []
    if config.WATCHLIST_PATH.exists():
        wl = json.loads(config.WATCHLIST_PATH.read_text(encoding="utf-8")).get("watchlist", [])
    return tk.universe(pf, wl)


@st.cache_data(show_spinner=False, ttl=10)
def _quotes(tickers: tuple[str, ...]) -> pd.DataFrame:
    return tk.live_quotes(list(tickers))


@st.cache_data(show_spinner=False, ttl=3600)
def _levels(tickers: tuple[str, ...]) -> dict:
    return tk.key_levels(list(tickers))


@st.cache_data(show_spinner=False, ttl=3600)
def _fx() -> dict:
    return get_fx_rates()


@st.cache_data(show_spinner="📅 正在计算 Day 0 以来的表现…", ttl=1800)
def _history(tr_json: str, uni_json: str) -> dict:
    tr, uni = json.loads(tr_json), json.loads(uni_json)
    h = tk.track_history(tr, uni)
    h["holdings_cum"] = tk.holdings_value_since(tr, uni, h["cum"])
    return h


def _color_pct(v):
    if isinstance(v, str):                      # 已格式化的金额文字（"+1,234" / "-56" / "—"）
        return f"color: {_GREEN}" if v.startswith("+") else (f"color: {_RED}" if v.startswith("-") else "")
    if v is None or pd.isna(v):
        return ""
    return f"color: {_GREEN}" if v > 0 else (f"color: {_RED}" if v < 0 else "")


def _money(v) -> str:
    """盈亏金额转文字：带样式的表格里 NaN 会显示成 None，这里统一显示"—"。"""
    return "—" if v is None or pd.isna(v) else f"{v:+,.0f}"


uni = _universe()


def _sub(name, ticker, *extra) -> str:
    return " · ".join(x for x in [name if name and str(name).upper() != ticker else "", *extra] if x)


_PCT = lambda k, lab, w="76px", d=2: {"key": k, "label": lab, "kind": "num", "decimals": d, "sign": True,
                                      "color": True, "suffix": "%", "width": w, "sortable": True, "align": "right"}
_USD = lambda k, lab, w="84px": {"key": k, "label": lab, "kind": "num", "decimals": 0, "sign": True, "color": True,
                                 "prefix": "$", "width": w, "sortable": True, "align": "right"}
status = tk.us_market_status()
live = status == "交易中"

_SECTION = globals().get("SECTION", "day0")

# ═══════════════════════════════════════════════════════════════════════════════
# Day 0 追踪
# ═══════════════════════════════════════════════════════════════════════════════
if _SECTION == "day0":
    tr, changed = tk.ensure_tracker(uni)
    if tr is None:
        st.error("无法确定最近收盘日（SPY 行情获取失败），请稍后重试。")
        st.stop()
    if changed:
        path = tk.save_tracker(tr)
        sync_to_github(path, "data/day0_tracker.json", "chore: update day0 tracker")

    h = _history(json.dumps(tr, sort_keys=True), json.dumps(uni, sort_keys=True))
    table = h["table"]

    st.caption(
        f"**Day 0 = {tr['day0']}**（以当日收盘价为基准）｜ 今天是 **Day {h['day_n']}** ｜ "
        f"追踪 {len(tr['tickers'])} 只 ｜ 之后新加入持仓/关注的股票，从加入当日收盘开始计。"
        "收益按美元计价；每日收盘价随时从行情补算，不打开 App 也不会断档。"
    )

    if h["day_n"] == 0:
        st.info(f"📍 Day 0 基准已建立（{tr['day0']} 收盘）。下一个交易日收盘后开始出现追踪数据。")
    else:
        # ── 顶部指标 ──
        cols = st.columns(4)
        hc = h.get("holdings_cum")
        if hc is not None and len(hc):
            cols[0].metric("持仓股票（按当前持股回溯）", f"{hc.iloc[-1] * 100:+.2f}%")
        for col, b in zip(cols[1:], tk.BENCHMARKS):
            r = table[table["ticker"] == b]
            if not r.empty:
                col.metric(f"{b} 自 Day 0", f"{r.iloc[0]['cum'] * 100:+.2f}%")

        # ── 累计走势：持仓 / 关注 等权 vs 基准 ──
        cum = h["cum"]
        lines = {}
        if hc is not None and len(hc):
            lines["持仓（市值加权）"] = hc
        for g in ("关注",):
            members = [t for t in table.loc[table["group"] == g, "ticker"] if t in cum.columns]
            if members:
                lines[f"{g}（等权）"] = cum[members].mean(axis=1)
        for b in tk.BENCHMARKS:
            if b in cum.columns:
                lines[b] = cum[b]
        if lines:
            day0_row = pd.DataFrame({k: [0.0] for k in lines}, index=[pd.Timestamp(tr["day0"])])
            lc = pd.concat([day0_row, pd.DataFrame(lines)]).sort_index() * 100
            fig = px.line(lc, labels={"value": "自 Day 0 累计 %", "index": "", "variable": ""},
                          markers=True)
            fig.add_hline(y=0, line_color="rgba(128,128,128,0.5)", line_width=1)
            fig.update_layout(height=380, margin=dict(t=10, b=10, l=10, r=10),
                              legend=dict(orientation="h", y=1.08),
                              plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)")
            st.plotly_chart(fig, width="stretch")

        # ── 排行榜 ──
        groups = st.multiselect("分组", ["持仓", "关注", "基准", "已移出"], default=["持仓", "关注"],
                                key="track_groups")
        show = table[table["group"].isin(groups)].copy()
        rows = [{"ticker": r["ticker"], "name": r["name"], "sub": _sub(r["name"], r["ticker"], f"加入 {r['added']}"),
                 "group": r["group"], "cum": pct(r["cum"]), "excess": pct(r["excess"]),
                 "wins": f"{r['up_days']}/{r['n_days']}" if r["n_days"] else "—",
                 "win_rate": (r["up_days"] / r["n_days"]) if r["n_days"] else None,
                 "best": pct(r["best_day"]), "worst": pct(r["worst_day"]), "last": _clean(r["last"])}
                for _, r in show.iterrows()]
        pz_table(rows, [
            {"key": "ticker", "label": "股票", "kind": "stock", "width": "minmax(140px,1.4fr)", "sortable": True},
            {"key": "group", "label": "分组", "kind": "pill", "width": "60px"},
            _PCT("cum", "累计"), _PCT("excess", "vs SPY"),
            {"key": "wins", "label": "上涨天数", "kind": "text", "width": "72px", "sortKey": "win_rate", "sortable": True},
            _PCT("best", "最大单日涨"), _PCT("worst", "最大单日跌"),
            {"key": "last", "label": "最新价 $", "kind": "num", "decimals": 2, "width": "90px", "align": "right"},
        ], key="day0_board", sort="cum", min_width=820)

        # ── 每日涨跌热力图 ──
        daily = h["daily"]
        tick_order = [t for t in show["ticker"] if t in daily.columns]
        if tick_order and not daily.empty:
            names = dict(zip(table["ticker"], table["name"]))
            hm = daily[tick_order].T * 100
            hm.index = [f"{names.get(t, t)} ({t})" if names.get(t, t) != t else t for t in hm.index]
            hm.columns = [d.strftime("%m-%d") for d in hm.columns]
            lim = max(1.0, float(hm.abs().max().max()))
            fig = px.imshow(hm, color_continuous_scale="RdYlGn", zmin=-lim, zmax=lim, aspect="auto",
                            text_auto=".1f", labels={"color": "当日%"})
            fig.update_layout(title="每日涨跌（%）", height=max(320, 22 * len(hm) + 80),
                              margin=dict(t=40, b=10, l=10, r=10))
            st.plotly_chart(fig, width="stretch")

    with st.expander("⚙️ 重设 Day 0"):
        st.caption("把 Day 0 改为最近一个收盘日，所有股票重新从那天开始计（原有追踪记录会被覆盖）。")
        confirm = st.checkbox("我确认要重设 Day 0")
        if st.button("重设", disabled=not confirm):
            new_tr, _ = tk.ensure_tracker(uni, reset=True)
            if new_tr:
                path = tk.save_tracker(new_tr)
                sync_to_github(path, "data/day0_tracker.json", "chore: reset day0 tracker")
                _history.clear()
                st.rerun()
