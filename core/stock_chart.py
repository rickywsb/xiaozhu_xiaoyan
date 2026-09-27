"""core/stock_chart.py — 通用个股技术图表弹窗 + 可点击表格

任何页面的股票表格改用 clickable_table()：点击一行 → 弹出该股票的技术图表
（K 线 / 均线 / 成交量 / RSI / MACD / 布林 + Fib 与筹码关键价位 + 各信号状态与历史检验判定）。

弹窗状态：
  · 打开时记录 (表格 key, ticker)；页面或自动刷新重跑时继续渲染，弹窗不会被刷新关掉
  · 关闭（on_dismiss）时清除状态，并更换表格 key 以清掉行选中——下次点同一行可直接再打开
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import daily_momentum as dm
from core.technical_analysis import build_candlestick_chart, get_ohlcv

_OPEN = "_chart_open"          # session_state: {"owner": 表格 key, "ticker": ..., "name": ...}
_GEN = "_chart_gen"            # session_state: {表格 key: 代数}，关闭弹窗后 +1 以重置行选中
_PERIODS = {"1M": "1mo", "3M": "3mo", "6M": "6mo", "1Y": "1y", "2Y": "2y"}


# ─── 数据 ─────────────────────────────────────────────────────────────────────

@st.cache_data(show_spinner=False, ttl=1800)
def _ohlcv(ticker: str, period: str) -> pd.DataFrame:
    return get_ohlcv(ticker, period=period)


@st.cache_data(show_spinner=False, ttl=1800)
def _context(ticker: str) -> dict:
    """1 年完整日线上的信号状态与关键价位（本币）。"""
    data = dm.fetch_ohlcv_histories([ticker], complete_bars_only=True).get(ticker)
    if data is None or len(data) < 40:
        return {}
    c = data["Close"]
    out: dict = {
        "last": float(c.iloc[-1]),
        "chg_1d": float(c.iloc[-1] / c.iloc[-2] - 1) if len(c) > 1 else None,
        "ret_20d": float(c.iloc[-1] / c.iloc[-21] - 1) if len(c) > 21 else None,
        "dist_high52": float(c.iloc[-1] / c.tail(252).max() - 1),
        "ema": dm.ema_momentum(c),
        "fib": dm.fib_signal(c),
        "vp": dm.vp_signal(data),
        "div": dm.macd_divergence(c),
    }
    try:
        from core.accumulation import compute_signals
        out["acc"] = compute_signals(data)
    except Exception:
        out["acc"] = None
    levels = []
    f = out["fib"]
    if f:
        rng = f["swing_high"] - f["swing_low"]
        for r in dm.FIB_KEY:
            p = f["swing_high"] - rng * r if f["uptrend"] else f["swing_low"] + rng * r
            levels.append((f"Fib {r:.1%}", p, "#B44CE8"))
    vp = dm.volume_profile(data)
    if vp:
        levels += [("POC", vp["poc"], "#E8A84C"), ("VAH", vp["vah"], "#26a641"), ("VAL", vp["val"], "#d73a4a")]
    out["levels"] = levels
    return out


def _verdicts() -> dict:
    try:
        from core import signal_backtest as sbt
        res = sbt.load_result()
        return sbt.verdict_map(res["summary"]) if res else {}
    except Exception:
        return {}


def _signal_rows(ctx: dict) -> list[tuple[str, str, str | None]]:
    """[(类别, 当前状态, 对应信号名)]，信号名用于查历史检验判定。"""
    rows = []
    e = ctx.get("ema")
    if e:
        sig = "EMA量能 🟢强(≥70)" if e["ema_score"] >= 70 else ("EMA量能 🔴弱(<40)" if e["ema_score"] < 40 else None)
        rows.append(("EMA 量能", f"{e['light']} {e['ema_score']} 分 · {e['state']}", sig))
    f = ctx.get("fib")
    if f:
        cat = f["category"]
        sig = {"强势贴高": "Fib 强势贴高", "破位预警": "Fib 破位(>78.6%)"}.get(cat) or \
            ("Fib 贴近关键支撑" if cat.endswith("支撑") else None)
        rows.append(("Fib 回撤", f"{f['fib_light']} {f['fib_signal']}（回撤 {f['retr']:.0%}）", sig))
    v = ctx.get("vp")
    if v:
        sig = {"上破VAH": "筹码 上破VAH", "跌破VAL": "筹码 跌破VAL", "测试VAL": "筹码 测试/回踩VAL"}.get(v["category"])
        rows.append(("筹码分布", f"{v['vp_light']} {v['vp_signal']}", sig))
    d = ctx.get("div")
    if d:
        sig = {"顶背驰": "MACD 顶背驰", "底背驰": "MACD 底背驰"}.get(d["signal"])
        rows.append(("MACD", f"{d['div_light']} {d['signal']} · {d['macd_state']}", sig))
    a = ctx.get("acc")
    if a:
        sig = {"疑似吸筹": "吸筹 疑似吸筹", "疑似派发": "吸筹 疑似派发"}.get(a["verdict"])
        rows.append(("主力吸筹", f"{a['emoji']} {a['verdict']}（评分 {a['score']}）", sig))
    return rows


# ─── 弹窗 ─────────────────────────────────────────────────────────────────────

def _on_dismiss() -> None:
    opened = st.session_state.pop(_OPEN, None)
    if opened:
        gen = st.session_state.setdefault(_GEN, {})
        gen[opened["owner"]] = gen.get(opened["owner"], 0) + 1


@st.dialog("📈 技术图表", width="large", on_dismiss=_on_dismiss)
def _chart_dialog(ticker: str, name: str | None = None) -> None:
    title = f"{name}（{ticker}）" if name and name != ticker else ticker
    st.markdown(f"### {title}")

    c1, c2 = st.columns([2, 3])
    period = c1.radio("周期", list(_PERIODS), index=2, horizontal=True, key="dlg_period")
    ind = c2.columns(5)
    show_vol = ind[0].checkbox("成交量", True, key="dlg_vol")
    show_rsi = ind[1].checkbox("RSI", True, key="dlg_rsi")
    show_macd = ind[2].checkbox("MACD", False, key="dlg_macd")
    show_boll = ind[3].checkbox("布林", False, key="dlg_boll")
    show_lv = ind[4].checkbox("关键价位", True, key="dlg_lv", help="Fib 回撤位（紫）+ 筹码 POC/VAH/VAL")

    ohlcv = _ohlcv(ticker, _PERIODS[period])
    ctx = _context(ticker)
    if ohlcv.empty or len(ohlcv) < 5:
        st.warning(f"暂无 {ticker} 的 K 线数据。")
        return

    fig = build_candlestick_chart(ohlcv, ticker, mas=[5, 20, 60], show_volume=show_vol,
                                  show_rsi=show_rsi, show_macd=show_macd, show_bollinger=show_boll)
    if show_lv and ctx.get("levels"):
        lo, hi = float(ohlcv["Low"].min()), float(ohlcv["High"].max())
        pad = (hi - lo) * 0.15
        for label, price, color in ctx["levels"]:
            if lo - pad <= price <= hi + pad:        # 只画当前视窗附近的价位
                fig.add_hline(y=price, line_dash="dot", line_width=1, line_color=color,
                              annotation_text=f"{label} {price:,.2f}", annotation_position="top left",
                              annotation_font_size=10, row=1, col=1)
    st.plotly_chart(fig, width="stretch")

    if ctx:
        m = st.columns(4)
        m[0].metric("最新收盘（本币）", f"{ctx['last']:,.2f}",
                    f"{ctx['chg_1d'] * 100:+.2f}%" if ctx.get("chg_1d") is not None else None)
        m[1].metric("20 日", f"{ctx['ret_20d'] * 100:+.1f}%" if ctx.get("ret_20d") is not None else "—")
        m[2].metric("距 52 周高", f"{ctx['dist_high52'] * 100:+.1f}%")
        m[3].metric("关键价位数", len(ctx.get("levels", [])))

        verdicts = _verdicts()
        rows = [{"信号": k, "当前状态": v, "该信号历史表现": (verdicts.get(sig, {}).get("判定") if sig else "—") or "—"}
                for k, v, sig in _signal_rows(ctx)]
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
            st.caption("历史表现来自量能健康页的📐信号成绩单；「—」表示当前没有触发可检验的信号。")

    from core import watchlist
    t_up = ticker.upper()
    if t_up not in set(watchlist.load()):
        if st.button("⭐ 加入 Watch List", key="dlg_add_wl"):
            watchlist.add([t_up])
            st.success(f"已加入：{t_up}")


# ─── 可点击表格 ───────────────────────────────────────────────────────────────

def clickable_table(data, tickers: list, key: str, names: list | None = None, **kwargs):
    """
    st.dataframe 的替代：点击某一行弹出该行股票的技术图表。
      data     DataFrame 或 Styler（与 st.dataframe 相同）
      tickers  与 data 行一一对应的 yfinance 代码；None 的行（如自定义篮子）点击不弹窗
      key      表格唯一 key
      names    可选显示名
    其余参数原样传给 st.dataframe。
    """
    gen = st.session_state.setdefault(_GEN, {}).get(key, 0)
    event = st.dataframe(data, key=f"{key}__{gen}", on_select="rerun", selection_mode="single-row", **kwargs)
    rows = event.selection.rows if event is not None else []
    opened = st.session_state.get(_OPEN)

    if rows:
        i = rows[0]
        t = tickers[i] if i < len(tickers) else None
        if t and not (opened and opened["owner"] == key and opened["ticker"] == t):
            opened = {"owner": key, "ticker": t, "name": (names[i] if names else None)}
            st.session_state[_OPEN] = opened
    if opened and opened["owner"] == key:
        _chart_dialog(opened["ticker"], opened.get("name"))
    return event


def click_hint() -> None:
    st.caption("👆 点击表格中任意一行，查看该股票的技术图表。")
