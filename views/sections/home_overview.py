"""views/sections/home_overview.py — 驾驶舱：实时净值 / 市场状态 / 持仓评分（盘中预估）/ 预警 / 板块 / 日报摘要

驾驶舱 = 「此刻」：一个页面看完今天。盘中每 60 秒刷新报价与净值，全市场评分每 10 分钟按盘中 K 线重算一次（预估）。
持仓页 = 「账本」：持仓编辑、净值历史、风险、Day 0、AI 诊断。
"""

import io
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config
from core import rating as RT
from core import posture
from core import sectors as sc
from core import signal_backtest as sbt
from core import signal_lab as SL
from core import tracker as tk
from core import risk as RK
from core import volume as VOL
from core import daily_momentum as dm
from core.enrich import enrich
from core.fx import get_fx_rates
from core.price_updater import load_cache
from core.stock_chart import click_hint
from core.ui import pz_table, pct, _clean, VOL_COLORS

PRICE_REFRESH = 60          # 秒：报价 / 净值 / 预警
SCORE_REFRESH_MIN = 10      # 分钟：全市场评分盘中重算


# ─── 数据（缓存）──────────────────────────────────────────────────────────────

@st.cache_data(show_spinner="🧮 正在计算全市场评分…（每个交易日首次约 1 分钟）", ttl=3600)
def _ratings(day_key: str) -> tuple[pd.DataFrame, str | None, dict]:
    df, as_of = RT.get_ratings()
    return df, as_of, dict(df.attrs)


@st.cache_resource(show_spinner="🧮 准备全市场矩阵（盘中评分用，每个交易日首次约 1 分钟）…", ttl=86400, max_entries=2)
def _base(close_day: str, held: tuple[str, ...], watch: tuple[str, ...]) -> dict:
    return RT.build(set(held), set(watch))


@st.cache_data(show_spinner="📡 正在按盘中 K 线重算全市场评分（约 20 秒）…", ttl=SCORE_REFRESH_MIN * 60 + 60, max_entries=4)
def _intraday(bucket: str, close_day: str, held: tuple[str, ...], watch: tuple[str, ...],
              names_json: str) -> tuple[pd.DataFrame | None, dict]:
    """盘中预估评分表 + 盘中预计信号。bucket 每 10 分钟变一次（缓存键）。"""
    now_et = datetime.now(ZoneInfo(config.MARKET_TZ))
    frac = VOL.session_fraction(now_et)
    if frac is None:
        return None, {"note": f"开盘 {VOL.MIN_MINUTES} 分钟内成交量折算误差太大，暂用昨收评分"}
    b = _base(close_day, held, watch)
    bars = RT.today_bars(list(b["panel"]["close"].columns) + ["SPY"])
    today = now_et.date().isoformat()
    b2 = RT.intraday(b, bars, today, frac)
    if b2 is None:
        return None, {"note": "今天已收盘入库，显示收盘评分"}
    tbl = RT.latest(b2, set(held), set(watch), json.loads(names_json))
    spy = dm.fetch_ohlcv_histories(["SPY"], period="2y", complete_bars_only=True).get("SPY")
    spy_c = spy["Close"] if spy is not None else pd.Series(dtype=float)
    if bars.get("SPY", {}).get("date") == today:
        spy_c = pd.concat([spy_c, pd.Series([bars["SPY"]["close"]], index=[pd.Timestamp(today)])])
    sig = SL.intraday_signals(b2, spy_c)
    n_today = sum(1 for t in b["panel"]["close"].columns if bars.get(t, {}).get("date") == today)
    return tbl, {"time": now_et.strftime("%H:%M"), "frac": frac, "n_today": n_today, "signals": sig,
                 "breadth50": tbl.attrs.get("breadth50")}


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
    stocks = book[book["kind"] == "stock"]
    return {"nav": pr["nav"], "var95": pr["var95"], "beta_SPY": pr.get("beta_SPY"),
            "top_rc": top_rc, "leverage": pr["leverage"],
            "stock_vals": dict(zip(stocks["ticker"].str.upper(), stocks["value"].astype(float)))}


@st.cache_data(show_spinner=False, ttl=15)
def _quotes(tickers: tuple[str, ...]) -> pd.DataFrame:
    return tk.live_quotes(list(tickers))


@st.cache_data(show_spinner=False, ttl=3600)
def _fx() -> dict:
    return get_fx_rates()


@st.cache_data(show_spinner=False, ttl=3600)
def _levels(tickers: tuple[str, ...]) -> dict:
    return tk.key_levels(list(tickers))


@st.cache_data(show_spinner="📋 正在汇总持仓评分…", ttl=1800)
def _holdings_table(tickers: tuple[str, ...], ratings_json: str, names_json: str) -> pd.DataFrame:
    return enrich(list(tickers), pd.read_json(io.StringIO(ratings_json)), None, json.loads(names_json))


def live_nav(risk: dict, board: pd.DataFrame) -> tuple[float, int]:
    """实时净值 = 风险簿净值（期权 CBOE 延迟报价 + 现金 + 股票缓存价）里的股票部分换成最新报价。返回 (净值, 用到实时价的股票数)。"""
    nav = risk["nav"]
    n = 0
    if board.empty:
        return nav, 0
    for r in board[board["group"] == "持仓"].itertuples():
        if pd.isna(r.prev_value_usd) or pd.isna(r.pnl_usd) or r.ticker not in risk["stock_vals"]:
            continue
        nav += (r.prev_value_usd + r.pnl_usd) - risk["stock_vals"][r.ticker]
        n += 1
    return nav, n


# ─── 基础数据 ─────────────────────────────────────────────────────────────────

pf = json.loads(config.PORTFOLIO_PATH.read_text(encoding="utf-8"))
names = {p["yf_ticker"].upper(): p.get("display", p["yf_ticker"])
         for a in pf.get("accounts", []) for p in a.get("positions", []) if p["yf_ticker"].upper() != config.CASH_TICKER}
sectors_of = {p["yf_ticker"].upper(): p.get("sector", "") for a in pf.get("accounts", []) for p in a.get("positions", [])}
from core import watchlist as _wl
watch = [t for t in _wl.load() if t not in names and t != config.CASH_TICKER]
held_t = list(names)
uni = tk.universe(pf, watch)
pf_json = json.dumps(pf, sort_keys=True)
ratings_close, as_of, meta = _ratings(config.market_today().isoformat())
click_hint()

_PCT = lambda k, lab, w="76px", d=2: {"key": k, "label": lab, "kind": "num", "decimals": d, "sign": True,
                                      "color": True, "suffix": "%", "width": w, "sortable": True, "align": "right"}


def _sub(name, ticker, *extra) -> str:
    return " · ".join(x for x in [name if name and str(name).upper() != ticker else "", *extra] if x)


status = tk.us_market_status()
is_live = status == "交易中"


@st.fragment(run_every=PRICE_REFRESH if is_live else None)
def cockpit():
    now_et = datetime.now(ZoneInfo(config.MARKET_TZ))

    # ── 状态行 ──
    c_stat, c_b1, c_b2 = st.columns([5, 1, 1])
    badge = {"交易中": "🟢 美股交易中", "盘前": "🟡 美股盘前", "已收盘": "⚪ 美股已收盘", "休市": "⚪ 美股休市"}[status]
    if c_b1.button("🔄 刷新报价", width="stretch", help="立即刷新报价与实时净值"):
        _quotes.clear()
        st.rerun(scope="fragment")
    if c_b2.button("🧮 重算评分", width="stretch", disabled=not is_live,
                   help=f"立即按盘中 K 线重算全市场评分（否则每 {SCORE_REFRESH_MIN} 分钟自动一次）"):
        _intraday.clear()
        st.rerun(scope="fragment")

    # ── 盘中评分（预估）──
    intr, imeta = None, {}
    if is_live:
        bucket = f"{now_et:%Y-%m-%d %H}:{now_et.minute // SCORE_REFRESH_MIN}"
        close_day = tk.last_close_date() or ""
        try:
            intr, imeta = _intraday(bucket, close_day, tuple(sorted(held_t)), tuple(sorted(watch)), json.dumps(names))
        except Exception as e:                      # 盘中重算失败不影响其余部分
            imeta = {"note": f"盘中评分暂不可用（{type(e).__name__}），显示昨收评分"}
    ratings = intr if intr is not None else ratings_close
    score_txt = (f"评分 = 盘中预估（美东 {imeta['time']} 重算，成交量已完成约 {imeta['frac']:.0%}，"
                 f"每 {SCORE_REFRESH_MIN} 分钟更新）" if intr is not None
                 else f"评分 = 收盘 {as_of}" + (f"（{imeta['note']}）" if imeta.get("note") else ""))
    c_stat.caption(f"{badge} ｜ 报价更新于美东 {now_et:%H:%M:%S}"
                   + (f"（每 {PRICE_REFRESH} 秒自动刷新）" if is_live else "") + f" ｜ {score_txt}")

    # ── 市场状态 ──
    post = _posture(imeta.get("breadth50") if intr is not None else meta.get("breadth50"))
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
            f'<span style="font-size:12px;color:#9A968C;letter-spacing:1px">市场状态{"（盘中）" if intr is not None else ""}</span>'
            f'<span style="font-size:26px;font-weight:700">{post["headline"]}</span>'
            f'<span style="font-size:14px;color:#E8A84C">{post["stance"]}</span></div>'
            f'<div style="flex-grow:1;display:grid;grid-template-columns:repeat({len(post["items"])},minmax(0,1fr));gap:18px">'
            f'{items}</div></div>')

    # ── 实时净值 ──
    quotes = _quotes(tuple(sorted(set(uni) | {"SPY"})))
    board = tk.intraday_board(uni, quotes, _fx()) if not quotes.empty else pd.DataFrame()
    risk = _risk(pf_json)
    nav, n_live = live_nav(risk, board)
    k = st.columns(4)
    if not board.empty:
        h = board[board["group"] == "持仓"]
        pnl, prev = float(h["pnl_usd"].sum()), float(h["prev_value_usd"].sum())
        k[0].metric("净值（实时）", f"${nav:,.0f}", f"{pnl / (nav - pnl) * 100:+.2f}% 今日" if nav - pnl else None,
                    help=f"股票按最新报价（{n_live} 只）、期权按 CBOE 延迟报价、加现金。只显示，不写入净值历史"
                         "（净值历史仍由持仓页「一键更新价格」记录）。")
        k[1].metric("今日盈亏（股票）", f"{'+' if pnl >= 0 else '-'}${abs(pnl):,.0f}",
                    f"{pnl / prev * 100:+.2f}% · 涨 {int((h['chg'] > 0).sum())} / 跌 {int((h['chg'] < 0).sum())}")
    else:
        k[0].metric("净值", f"${nav:,.0f}", help="报价获取失败，按缓存价")
    k[2].metric("1 日 VaR 95%", f"${risk['var95']:,.0f}",
                f"{risk['var95'] / risk['nav'] * 100:.1f}% 净值 · 风险贡献第一 {risk['top_rc']}",
                delta_color="off", delta_arrow="off")
    k[3].metric("组合 β vs SPY", f"{risk['beta_SPY']:.2f}", f"总敞口 {risk['leverage']:.2f}× 净值",
                delta_color="off", delta_arrow="off")

    # ── 派发日警示 ──
    _lab = SL.load_summary() or {}
    _mkt = _lab.get("market") or {}
    if _mkt.get("active"):
        st.warning(f"⚠️ **大盘派发日偏多**：SPY 近 25 日出现 {_mkt.get('current_count')} 个派发日（放量下跌，≥5 个即警戒）。"
                   + (f"历史上警戒出现后 SPY 20 日平均 {_mkt['fwd_after'] * 100:+.1f}%，平常 {_mkt['fwd_all'] * 100:+.1f}%"
                      f"（样本 {_mkt['n']} 次，仅供参考）。" if _mkt.get("fwd_after") is not None else "")
                   + "新开仓宜谨慎、优先看持仓的卖点信号。")

    # ── 持仓（评分 + 实时盈亏）──
    st.markdown("#### 持仓")
    val = RT.load_validation() or {}
    tt = (val.get("top_tier") or {}).get(f"≥{RT.TOP_TIER}", {})
    st.caption(
        f"评分 = 全市场约 {val.get('n_tickers', 540)} 只中的百分位；**强势筛选**：历史上 ≥{RT.TOP_TIER} 分之后 20 日"
        + (f"平均跑赢 {tt['excess'] * 100:+.1f}%（前后两段都成立）" if tt else "表现待验证")
        + f"，{RT.TOP_TIER} 分以下只作排名参考。"
        + ("盘中评分旁的箭头 = 较昨收变化；「预计」信号要收盘才确认。" if intr is not None else "评分旁箭头 = 较 5 日前。")
        + "点击任意一行打开个股详情。")
    base = _holdings_table(tuple(held_t), ratings_close.to_json(), json.dumps(names))
    close_score = dict(zip(ratings_close["ticker"], ratings_close["score"])) if not ratings_close.empty else {}
    ir = ratings.set_index("ticker") if intr is not None else None
    bq = board.set_index("ticker") if not board.empty else pd.DataFrame()
    isig = imeta.get("signals", {}) if intr is not None else {}
    total_val = nav or 1.0
    rows, alerts = [], []
    for r in base.to_dict("records"):
        t = r["ticker"]
        price, chg, pnl_t, w = r.get("price"), r.get("chg"), None, None
        if t in bq.index:
            q = bq.loc[t]
            price, chg, pnl_t = float(q["last"]), q["chg"], _clean(q["pnl_usd"])
            if pd.notna(q["prev_value_usd"]) and pd.notna(q["pnl_usd"]):
                w = (q["prev_value_usd"] + q["pnl_usd"]) / total_val
        score, delta, action, vstate = r.get("score"), r.get("d5"), r.get("action"), r.get("volume_state")
        if ir is not None and t in ir.index:
            score, action, vstate = ir.at[t, "score"], ir.at[t, "action"], ir.at[t, "volume_state"]
            cs = close_score.get(t)
            delta = (score - cs) if cs is not None and pd.notna(cs) else None
            if delta is not None and delta <= -15:
                alerts.append({"ticker": t, "type": "评分骤降", "detail": f"盘中评分 {int(score)}（昨收 {int(cs)}，{int(delta)}）",
                               "chg": chg})
        sig_txt = r.get("signals") or ""
        pre = isig.get(t, [])
        if pre:
            sig_txt = " · ".join(f"{'▲' if s['expect'] > 0 else '▼'}{s['signal']} {s['grade']}（预计）" for s in pre[:2]) \
                      + (f" ｜ {sig_txt}" if sig_txt else "")
            for s in pre:
                if s["expect"] < 0:
                    alerts.append({"ticker": t, "type": "卖点", "detail": f"{s['signal']}（{s['grade']} 级，盘中预计，收盘确认）",
                                   "chg": chg})
        rows.append({"ticker": t, "name": r["name"], "sub": _sub(r["name"], t, sectors_of.get(t, "")),
                     "price_txt": f"{price:,.2f}" if isinstance(price, (int, float)) and price == price else None,
                     "chg": pct(chg), "pnl": pnl_t, "weight": pct(w),
                     "score": int(score) if _clean(score) is not None else None,
                     "d5": int(delta) if _clean(delta) is not None else None,
                     "spark": [x for x in (r.get("spark") or []) if x == x],
                     "action": action or "", "volume_state": vstate or "", "signals": sig_txt})
    cols = [
        {"key": "ticker", "label": "股票", "kind": "stock", "width": "minmax(130px,1.3fr)", "sortable": True},
        {"key": "price_txt", "label": "价格 · 当日", "kind": "price", "chg": "chg", "width": "100px",
         "sortable": True, "sortKey": "chg"},
        {"key": "pnl", "label": "今日盈亏", "kind": "num", "decimals": 0, "sign": True, "color": True, "prefix": "$",
         "width": "86px", "sortable": True, "align": "right"},
        {"key": "weight", "label": "仓位", "kind": "bar", "max": 20, "suffix": "%", "decimals": 1, "width": "104px",
         "sortable": True},
        {"key": "score", "label": "盘中评分" if intr is not None else "评分", "kind": "score", "delta": "d5",
         "width": "96px", "sortable": True},
        {"key": "spark", "label": "20 日走势", "kind": "spark", "width": "92px"},
        {"key": "action", "label": "操作倾向", "kind": "pill", "width": "88px"},
        {"key": "volume_state", "label": "量能", "kind": "text", "colors": VOL_COLORS, "width": "72px"},
        {"key": "signals", "label": "可靠信号", "kind": "small", "width": "minmax(150px,1.8fr)"},
    ]
    pz_table(rows, cols, key="home_hold", sort="score", min_width=1080)

    # ── 预警（一个列表）──
    if not board.empty:
        held_b = board[board["group"] == "持仓"]
        res_bt = sbt.load_result()
        vm_bt = sbt.verdict_map(res_bt["summary"]) if res_bt else {}
        va, vnote = tk.volume_alerts(board[board["group"] != "基准"], status, now_et, vm_bt)
        for r in va.to_dict("records"):
            s = r["signal"] if isinstance(r["signal"], str) else ""
            g = r["grade"] if isinstance(r["grade"], str) and r["grade"] else ""
            alerts.append({"ticker": r["ticker"], "type": "放量",
                           "detail": f"预计量比 {r['pvr']:.1f}×" + (f"，{s}（{g[0]}）" if s and g else (f"，{s}" if s else ""))
                                     + ("，突破 20 日高" if r["breakout"] and "突破" not in s else "")
                                     + ("" if r["group"] == "持仓" else "（关注）"),
                           "chg": r["chg"],
                           # 降噪：持仓放量都看；关注股只看 A / B 级信号
                           "reliable": r["group"] == "持仓" or g[:1] in ("A", "B")})
        lv = tk.level_alerts(held_b, _levels(tuple(held_b["ticker"])), vm_bt)
        for r in lv.to_dict("records"):
            vd = r.get("verdict") if isinstance(r.get("verdict"), str) else ""
            alerts.append({"ticker": r["ticker"], "type": "价位",
                           "detail": f"{r['event']} {r['kind']} {r['level']:,.2f}（距 {r['dist'] * 100:+.1f}%）"
                                     + (f"，该类信号历史 {vd}" if vd else "，该类信号未检验"),
                           "chg": r["chg"],
                           # 降噪：只有历史检验有效（✅）的价位进主列表
                           "reliable": "✅" in vd})
        order = {"卖点": 0, "评分骤降": 1, "放量": 2, "价位": 3}

        def _merge(items: list[dict]) -> list[dict]:
            """同一只股票合并成一行：类型取最重要的，说明依次拼接。"""
            by: dict[str, list[dict]] = {}
            for a in sorted(items, key=lambda a: order.get(a["type"], 9)):
                by.setdefault(a["ticker"], []).append(a)
            out = [{"ticker": t, "name": names.get(t, t), "sub": _sub(names.get(t), t), "type": xs[0]["type"],
                    "detail": "；".join(x["detail"] for x in xs), "chg": pct(xs[0]["chg"]), "_o": order.get(xs[0]["type"], 9)}
                   for t, xs in by.items()]
            return sorted(out, key=lambda a: (a["_o"], -abs(a["chg"] or 0)))

        main = _merge([a for a in alerts if a.get("reliable", True)])
        rest = _merge([a for a in alerts if not a.get("reliable", True)])
        acols = [{"key": "ticker", "label": "股票", "kind": "stock", "width": "minmax(120px,1fr)"},
                 {"key": "type", "label": "类型", "kind": "pill", "width": "84px"},
                 {"key": "detail", "label": "说明", "kind": "small", "width": "minmax(280px,3.2fr)"},
                 _PCT("chg", "当日")]
        st.markdown(f"#### 🔔 预警（{len(main)}）")
        st.caption(f"每只股票一行。卖点 / 评分骤降：来自盘中预估评分，收盘确认；放量：{vnote}；"
                   "价位：今日穿越或 ±1% 内的 Fib / 筹码价位，只列历史检验有效（✅）的。")
        pz_table(main, acols, key="home_alerts", empty="暂无需要关注的预警 👍", min_width=640)
        if rest:
            with st.expander(f"低可靠度提醒 {len(rest)} 条（历史检验偏弱 / 反向 / 未检验的价位、关注股的 C/D 级放量）"):
                pz_table(rest, acols, key="home_alerts_rest", min_width=640)

        # ── 板块当日盈亏 ──
        st.session_state["_home_sector_pnl"] = tk.sector_pnl(board).to_dict("records")


cockpit()

# ─── 板块盈亏 / 轮动 / 日报摘要（一行三栏，不随报价刷新）────────────────────────
c_sp, c_rot, c_ai = st.columns([1, 1.15, 1], gap="large")
with c_sp:
    st.markdown("#### 板块当日盈亏")
    sp = pd.DataFrame(st.session_state.get("_home_sector_pnl") or [])
    if not sp.empty:
        fig = go.Figure(go.Bar(x=sp["pnl_usd"], y=sp["sector"], orientation="h",
                               marker_color=["#1F7A45" if v >= 0 else "#B3362A" for v in sp["pnl_usd"]],
                               text=[f"${v:+,.0f}" for v in sp["pnl_usd"]], textposition="outside", cliponaxis=False,
                               hovertemplate="%{y}: $%{x:+,.0f}<extra></extra>"))
        lo, hi = min(0.0, float(sp["pnl_usd"].min())), max(0.0, float(sp["pnl_usd"].max()))
        pad = (hi - lo) * 0.45 or 1.0
        fig.update_layout(height=max(160, 40 + 30 * len(sp)), margin=dict(t=6, b=6, l=6, r=6),
                          xaxis=dict(range=[lo - (pad if lo < 0 else 0), hi + pad], showgrid=False),
                          yaxis=dict(autorange="reversed"), plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)")
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
        st.caption("刷新页面更新；股票部分，按最新汇率折美元。")

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
