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


@st.cache_data(show_spinner=False, ttl=1800)
def _signal_history(ticker: str, bars: int = 130) -> list[dict]:
    """
    近 bars 个交易日里 A / B 级信号的"首次出现"（与信号成绩单同一函数逐日重算）：
    [{date, signal, grade, expect(+1 看多 / -1 看空)}]。
    """
    from core import signal_backtest as sbt
    vm = _verdicts()
    data = dm.fetch_ohlcv_histories([ticker], complete_bars_only=True).get(ticker)
    if data is None or len(data) < sbt.WARMUP:
        return []
    out, prev = [], set()
    start = max(sbt.WARMUP, len(data) - bars)
    for i in range(start, len(data)):
        sub = data.iloc[max(0, i + 1 - sbt.INPUT_BARS): i + 1]
        cur = sbt._day_signals(sub)
        for sig in cur - prev:
            v = vm.get(sig, {})
            if v.get("评级", "")[:1] in ("A", "B"):
                out.append({"date": sub.index[-1].date().isoformat(), "signal": sig, "grade": v["评级"][0],
                            "expect": 1 if v.get("预期") == "看多" else -1})
        prev = cur
    return out


@st.cache_data(show_spinner=False, ttl=600)
def _rating_row(ticker: str) -> dict | None:
    from core import rating as RT
    df, as_of = RT.load_latest()
    if df.empty:
        return None
    r = df[df["ticker"] == ticker.upper()]
    return {**r.iloc[0].to_dict(), "as_of": as_of} if not r.empty else None


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

    rr = _rating_row(ticker)
    if rr:
        from core import rating as RT
        sc_ = int(rr["score"])
        d5 = (sc_ - rr["score_5d"]) if rr.get("score_5d") == rr.get("score_5d") and rr.get("score_5d") is not None else None
        bg = "#155E36" if sc_ >= 90 else "#2F8A57" if sc_ >= 70 else "#8A877E" if sc_ >= 50 else "#C7711F" if sc_ >= 30 else "#B3362A"
        act_css = {"持有": ("#DDF0E4", "#1F6B3E"), "可关注": ("#DDF0E4", "#1F6B3E"), "注意": ("#FBEFD9", "#8A4B0A"),
                   "等待": ("#ECEAE4", "#5E5B53"), "考虑减仓": ("#F7E0DC", "#8E2E22"), "回避": ("#F7E0DC", "#8E2E22")}
        ab, af = act_css.get(rr["action"], ("#ECEAE4", "#5E5B53"))
        tier = f"≥{RT.TOP_TIER} 强势筛选" if sc_ >= RT.TOP_TIER else "描述性排名"
        c_l, c_r = st.columns([1, 1.6])
        with c_l:
            st.html(
                f'<div style="background:#16181D;color:#F7F6F2;border-radius:14px;padding:16px 18px;display:flex;flex-direction:column;gap:8px">'
                f'<span style="font-size:12px;color:#9A968C">综合评分 · 全市场百分位（{rr["as_of"]}）</span>'
                f'<div style="display:flex;align-items:baseline;gap:10px"><span style="font-family:IBM Plex Mono,monospace;'
                f'font-size:46px;font-weight:600;line-height:1;background:{bg};padding:2px 10px;border-radius:10px">{sc_}</span>'
                + (f'<span style="font-size:14px;color:{"#6FBF8E" if d5 >= 0 else "#E8A84C"}">{d5:+.0f} 较 5 日前</span>' if d5 is not None else "")
                + f'</div><div style="display:flex;gap:8px;align-items:center"><span style="font-size:13px;font-weight:700;padding:4px 12px;'
                f'border-radius:999px;background:{ab};color:{af}">{rr["action"]}</span>'
                f'<span style="font-size:12px;color:#C9C5BB">量能：{rr["volume_state"]} · {tier}</span></div></div>')
        with c_r:
            import plotly.graph_objects as go
            comps = ["趋势", "量能", "板块", "健康"]
            vals = [rr.get(f"c_{k}") or 0.0 for k in comps]
            f2 = go.Figure(go.Bar(x=vals, y=comps, orientation="h", text=[f"{v:+.1f}" for v in vals],
                                  textposition="outside", cliponaxis=False,
                                  marker_color=["#2F8A57" if v >= 0 else "#C7711F" for v in vals]))
            lim = max(10.0, max(abs(v) for v in vals) * 1.4)
            f2.update_layout(height=170, margin=dict(t=24, b=4, l=4, r=4), title=dict(text="评分构成（相对全市场均值）", font=dict(size=13)),
                             xaxis=dict(range=[-lim, lim], zeroline=True, zerolinecolor="#8A877E", showgrid=False),
                             yaxis=dict(autorange="reversed"), plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)")
            st.plotly_chart(f2, width="stretch", config={"displayModeBar": False})
    else:
        st.caption("该标的不在评分股票池（如杠杆产品），无综合评分。")

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
    # A / B 级信号标记（近半年首次出现的位置）
    show_sig = True
    hist_sig = _signal_history(ticker) if show_sig else []
    if hist_sig:
        import plotly.graph_objects as go
        dmap = {d.date().isoformat(): i for i, d in enumerate(ohlcv["Date"])}
        for expect, sym, color, col, off in ((1, "triangle-up", "#1F7A45", "Low", 0.97), (-1, "triangle-down", "#B3362A", "High", 1.03)):
            pts = [h for h in hist_sig if h["expect"] == expect and h["date"] in dmap]
            if not pts:
                continue
            fig.add_trace(go.Scatter(
                x=[ohlcv["Date"].iloc[dmap[h["date"]]] for h in pts],
                y=[float(ohlcv[col].iloc[dmap[h["date"]]]) * off for h in pts],
                mode="markers", marker=dict(symbol=sym, size=11, color=color, line=dict(width=1, color="white")),
                name="A/B 看多信号" if expect > 0 else "A/B 看空信号",
                text=[f'{h["date"]} {h["signal"]}（{h["grade"]}）' for h in pts],
                hovertemplate="%{text}<extra></extra>"), row=1, col=1)

    if show_lv and ctx.get("levels"):
        lo, hi = float(ohlcv["Low"].min()), float(ohlcv["High"].max())
        pad = (hi - lo) * 0.15
        for label, price, color in ctx["levels"]:
            if lo - pad <= price <= hi + pad:        # 只画当前视窗附近的价位
                fig.add_hline(y=price, line_dash="dot", line_width=1, line_color=color,
                              annotation_text=f"{label} {price:,.2f}", annotation_position="top left",
                              annotation_font_size=10, row=1, col=1)
    st.plotly_chart(fig, width="stretch")
    if hist_sig:
        st.caption(f"▲ / ▼ = 近半年 A / B 级信号首次出现的位置（共 {len(hist_sig)} 次，悬停看信号名）。")

    if ctx:
        m = st.columns(4)
        m[0].metric("最新收盘（本币）", f"{ctx['last']:,.2f}",
                    f"{ctx['chg_1d'] * 100:+.2f}%" if ctx.get("chg_1d") is not None else None)
        m[1].metric("20 日", f"{ctx['ret_20d'] * 100:+.1f}%" if ctx.get("ret_20d") is not None else "—")
        m[2].metric("距 52 周高", f"{ctx['dist_high52'] * 100:+.1f}%")
        m[3].metric("关键价位数", len(ctx.get("levels", [])))

        verdicts = _verdicts()
        order = {"A": 0, "B": 1, "D": 2, "C": 3}
        rows = []
        for k, v, sig in _signal_rows(ctx):
            vv = verdicts.get(sig, {}) if sig else {}
            g = (vv.get("评级") or "")[:1]
            ex = vv.get("20日平均超额%")
            rows.append({"评级": g or "—", "信号": k, "当前状态": v,
                         "历史20日超额": f"{ex:+.1f}%" if ex is not None else "—", "_o": order.get(g, 4)})
        if rows:
            sd = pd.DataFrame(rows).sort_values("_o")
            main, rest = sd[sd["_o"] <= 2].drop(columns="_o"), sd[sd["_o"] > 2].drop(columns="_o")
            st.markdown("**当前信号**（按评级排序：A 可靠 / B 参考 / D 反向提示）")
            if main.empty:
                st.caption("当前没有触发 A / B / D 级信号。")
            else:
                st.dataframe(main, hide_index=True, width="stretch", height=38 + 35 * len(main))
            if not rest.empty:
                with st.expander(f"其余 {len(rest)} 条（C 噪音或当前未触发可检验信号）"):
                    st.dataframe(rest, hide_index=True, width="stretch", height=38 + 35 * len(rest))

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
