"""views/sections/10_Live.py — 📡 盘中看板：当日涨跌 / 当日盈亏 / 关键价位预警 + Day 0 追踪"""

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config
from core.stock_chart import clickable_table, click_hint
from core import tracker as tk
from core import signal_backtest as sbt
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
status = tk.us_market_status()
live = status == "交易中"

# 由入口页决定渲染哪一块：SECTION = "live"（盘中，驾驶舱）/ "day0"（Day 0 追踪，持仓）
_SECTION = globals().get("SECTION", "live")

# ═══════════════════════════════════════════════════════════════════════════════
# 盘中
# ═══════════════════════════════════════════════════════════════════════════════
if _SECTION == "live":

    @st.fragment(run_every=REFRESH_SECONDS if live else None)
    def live_panel():
        quotes = _quotes(tuple(uni))
        now_et = datetime.now(ZoneInfo(config.MARKET_TZ)).strftime("%H:%M:%S")
        c_stat, c_btn = st.columns([5, 1])
        badge = {"交易中": "🟢 美股交易中", "盘前": "🟡 美股盘前", "已收盘": "⚪ 美股已收盘",
                 "休市": "⚪ 美股休市"}[status]
        c_stat.caption(
            f"{badge} ｜ 更新于美东 {now_et}"
            + (f" ｜ 每 {REFRESH_SECONDS} 秒自动刷新" if live else " ｜ 非交易时段，显示最近一个交易日的涨跌")
            + " ｜ 行情来自 Yahoo，可能有数秒到数分钟延迟"
        )
        if c_btn.button("🔄 刷新", width="stretch"):
            _quotes.clear()
            st.rerun(scope="fragment")

        if quotes.empty:
            st.error("报价获取失败，请稍后刷新。")
            return
        board = tk.intraday_board(uni, quotes, _fx())
        held = board[board["group"] == "持仓"]

        # ── 顶部指标 ──
        pnl = float(held["pnl_usd"].sum())
        prev_val = float(held["prev_value_usd"].sum())
        n_up, n_dn = int((held["chg"] > 0).sum()), int((held["chg"] < 0).sum())
        cols = st.columns(5)
        cols[0].metric("持仓当日盈亏（股票）", f"${pnl:+,.0f}",
                       f"{pnl / prev_val * 100:+.2f}%" if prev_val else None)
        cols[1].metric("持仓涨 / 跌", f"{n_up} / {n_dn}")
        for col, b in zip(cols[2:], tk.BENCHMARKS):
            r = board[board["ticker"] == b]
            if not r.empty:
                col.metric(b, f"{r.iloc[0]['last']:,.2f}", f"{r.iloc[0]['chg'] * 100:+.2f}%")

        # ── 板块盈亏 ──
        sp = tk.sector_pnl(board)
        if not sp.empty:
            fig = go.Figure(go.Bar(
                x=sp["pnl_usd"], y=sp["sector"], orientation="h",
                marker_color=[_GREEN if v >= 0 else _RED for v in sp["pnl_usd"]],
                text=[f"${v:+,.0f}（{c * 100:+.2f}%）" for v, c in zip(sp["pnl_usd"], sp["chg"])],
                textposition="outside", cliponaxis=False,
                hovertemplate="%{y}: $%{x:+,.0f}<extra></extra>",
            ))
            # 两侧留出标签空间：正值标签在右、负值标签在左
            lo, hi = min(0.0, float(sp["pnl_usd"].min())), max(0.0, float(sp["pnl_usd"].max()))
            pad = (hi - lo) * 0.45 or 1.0
            fig.update_layout(title="持仓板块当日盈亏", height=60 + 42 * len(sp),
                              margin=dict(t=40, b=10, l=10, r=10),
                              xaxis=dict(range=[lo - (pad if lo < 0 else 0), hi + pad]),
                              yaxis=dict(autorange="reversed"),
                              plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)")
            st.plotly_chart(fig, width="stretch")

        # ── 涨跌榜 ──
        stocks = board[board["group"] != "基准"]
        cL, cR = st.columns(2)
        for col, title, part in [(cL, "🟢 涨幅榜", stocks.head(8)),
                                 (cR, "🔴 跌幅榜", stocks.tail(8).iloc[::-1])]:
            with col:
                st.markdown(f"**{title}**")
                t = part[["name", "ticker", "group", "chg", "pnl_usd"]].copy()
                t["chg"] = t["chg"] * 100
                t["pnl_usd"] = t["pnl_usd"].map(_money)
                clickable_table(
                    t.rename(columns={"name": "名称", "ticker": "代码", "group": "分组",
                                      "chg": "当日%", "pnl_usd": "盈亏$"})
                     .style.map(_color_pct, subset=["当日%", "盈亏$"])
                     .format({"当日%": "{:+.2f}%"}, na_rep="—"),
                    width="stretch", hide_index=True, tickers=list(part["ticker"]), key=f"live_mv_{title}", names=list(part["name"])
                )

        # ── 盘中放量 ──
        res_bt = sbt.load_result()
        vm_bt = sbt.verdict_map(res_bt["summary"]) if res_bt else {}
        va, vnote = tk.volume_alerts(board[board["group"] != "基准"], status,
                                     datetime.now(ZoneInfo(config.MARKET_TZ)), vm_bt)
        st.markdown("**📢 放量预警**（预计全天量比 ≥1.5）")
        st.caption(vnote + "。信号后括号为📐信号成绩单评级：A 可靠 / B 参考 / C 噪音 / D 反向。")
        if va.empty:
            if "分钟内" not in vnote:
                st.caption("暂无明显放量。")
        else:
            vv = va.copy()
            vv["chg"] = vv["chg"] * 100
            vv["预计信号"] = [f"{sg}（{gr[0]}）" if sg and gr else (sg or "") for sg, gr in zip(vv["signal"], vv["grade"])]
            vv["突破"] = vv["breakout"].map({True: "✅", False: ""})
            clickable_table(
                vv[["name", "ticker", "group", "pvr", "chg", "突破", "预计信号"]].rename(columns={
                    "name": "名称", "ticker": "代码", "group": "分组", "pvr": "预计量比", "chg": "当日%"})
                .style.map(lambda x: "background-color: rgba(232,168,76,0.25); font-weight: 600"
                           if isinstance(x, (int, float)) and x >= 2 else "", subset=["预计量比"])
                .map(_color_pct, subset=["当日%"])
                .format({"预计量比": "{:.2f}", "当日%": "{:+.2f}%"}, na_rep="—"),
                tickers=list(vv["ticker"]), names=list(vv["name"]), key="live_vol",
                hide_index=True, width="stretch",
            )

        # ── 关键价位预警（持仓）──
        st.markdown("**🎯 关键价位预警**（持仓：今日穿越或距离 ±1% 以内的 Fib / 筹码价位）")
        lv = _levels(tuple(held["ticker"]))
        res = sbt.load_result()
        alerts = tk.level_alerts(held, lv, sbt.verdict_map(res["summary"]) if res else {})
        if alerts.empty:
            st.caption("暂无持仓接近关键价位。")
        else:
            a = alerts.copy()
            a["dist"] = a["dist"] * 100
            a["chg"] = a["chg"] * 100
            clickable_table(
                a[["name", "ticker", "event", "kind", "level", "last", "dist", "chg", "verdict"]]
                .rename(columns={"name": "名称", "ticker": "代码", "event": "事件", "kind": "类型",
                                 "level": "价位", "last": "现价", "dist": "距价位%", "chg": "当日%",
                                 "verdict": "该类信号历史表现"})
                .style.map(_color_pct, subset=["当日%"])
                .format({"价位": "{:,.2f}", "现价": "{:,.2f}", "距价位%": "{:+.1f}%",
                         "当日%": "{:+.2f}%"}, na_rep="—"),
                width="stretch", hide_index=True, tickers=list(a["ticker"]), key="live_alerts", names=list(a["name"])
            )
            st.caption("价位按截至昨日的日线计算（本币）；「该类信号历史表现」来自量能健康页的📐信号成绩单，"
                       "❌/🟡 表示这类信号过去并不可靠。")

        # ── 全部 ──
        with st.expander(f"📋 全部 {len(board)} 只（持仓 + 关注 + 基准）"):
            t = board[["name", "ticker", "group", "sector", "last", "prev_close", "chg",
                       "pnl_usd", "bar_date"]].copy()
            t["chg"] = t["chg"] * 100
            t["pnl_usd"] = t["pnl_usd"].map(_money)
            clickable_table(
                t.rename(columns={"name": "名称", "ticker": "代码", "group": "分组", "sector": "板块",
                                  "last": "最新价", "prev_close": "昨收", "chg": "当日%",
                                  "pnl_usd": "盈亏$", "bar_date": "K线日期"})
                 .style.map(_color_pct, subset=["当日%", "盈亏$"])
                 .format({"最新价": "{:,.2f}", "昨收": "{:,.2f}", "当日%": "{:+.2f}%",
                          }, na_rep="—"),
                width="stretch", hide_index=True, height=min(700, 80 + len(t) * 35), tickers=list(board["ticker"]), key="live_all", names=list(board["name"])
            )
            st.caption("最新价 / 昨收为本币；盈亏按最新汇率折美元。期权与现金不在此计算。"
                       "K线日期早于其他股票的，多为当地休市（如韩国中秋）。")

    live_panel()

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
        show["胜率"] = show.apply(lambda r: f"{r['up_days']}/{r['n_days']}" if r["n_days"] else "—", axis=1)
        for c in ("cum", "excess", "best_day", "worst_day"):
            show[c] = show[c] * 100
        clickable_table(
            show[["name", "ticker", "group", "added", "cum", "excess", "胜率", "best_day", "worst_day",
                  "last"]].rename(columns={
                "name": "名称", "ticker": "代码", "group": "分组", "added": "加入日",
                "cum": "累计%", "excess": "vs SPY%", "胜率": "上涨天数",
                "best_day": "最大单日涨%", "worst_day": "最大单日跌%", "last": "最新价$",
            }).style.map(_color_pct, subset=["累计%", "vs SPY%", "最大单日涨%", "最大单日跌%"])
              .format({"累计%": "{:+.2f}%", "vs SPY%": "{:+.2f}%", "最大单日涨%": "{:+.2f}%",
                       "最大单日跌%": "{:+.2f}%", "最新价$": "{:,.2f}"}, na_rep="—"),
            width="stretch", hide_index=True, height=min(700, 80 + len(show) * 35), tickers=list(show["ticker"]), key="day0_board", names=list(show["name"])
        )

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
