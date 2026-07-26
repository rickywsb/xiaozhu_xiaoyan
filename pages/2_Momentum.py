"""pages/2_Momentum.py — 量能健康报告 + 技术图表"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
from core.daily_momentum import PERIODS, score_holdings, fetch_histories, calc_metrics, DEFAULT_DECAY, DEFAULT_WINDOW
try:
    from core.daily_momentum import score_holdings_ema, EMA_SPANS
    from core.daily_momentum import fib_alerts, FIB_RATIOS, FIB_LOOKBACK
    from core.daily_momentum import vp_alerts, VP_LOOKBACK, VP_VALUE_AREA
    from core.daily_momentum import relative_strength, RS_BENCHMARK
    from core.daily_momentum import divergence_alerts, MACD_LOOKBACK
    _EMA_AVAILABLE = True
except ImportError:
    # 云端刚更新代码但进程未完全重启时，旧模块可能缺少新函数——优雅降级而非崩溃
    _EMA_AVAILABLE = False
    EMA_SPANS = (10, 20, 60)
    FIB_RATIOS = (0.236, 0.382, 0.5, 0.618, 0.786)
    FIB_LOOKBACK = 120
    VP_LOOKBACK = 120
    VP_VALUE_AREA = 0.70
    RS_BENCHMARK = "SOXX"
    MACD_LOOKBACK = 120
from core.technical_analysis import get_ohlcv, build_candlestick_chart
from core import accumulation as accum
from core import llm, ai_review

# ─── 工具 ─────────────────────────────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def _load_portfolio() -> dict:
    return json.loads(config.PORTFOLIO_PATH.read_text(encoding="utf-8"))


@st.cache_data(show_spinner="⚡ 正在计算量能数据…（首次约 15 秒）", ttl=1800)
def _cached_score(portfolio_hash: str, window: int, decay: float) -> pd.DataFrame:
    """缓存 30 分钟；portfolio_hash 变化时自动失效。"""
    portfolio = json.loads(config.PORTFOLIO_PATH.read_text(encoding="utf-8"))
    return score_holdings(portfolio, window=window, decay=decay)


def _portfolio_hash(portfolio: dict) -> str:
    tickers = sorted(
        pos["yf_ticker"]
        for acc in portfolio.get("accounts", [])
        for pos in acc.get("positions", [])
    )
    return "|".join(tickers)


@st.cache_data(show_spinner=False, ttl=3600)
def _cached_ohlcv(ticker: str, period: str) -> pd.DataFrame:
    return get_ohlcv(ticker, period=period)


@st.cache_data(show_spinner="🏦 正在扫描主力吸筹信号…", ttl=1800)
def _cached_accum(portfolio_hash: str) -> pd.DataFrame:
    portfolio = json.loads(config.PORTFOLIO_PATH.read_text(encoding="utf-8"))
    return accum.scan_holdings(portfolio)


@st.cache_data(show_spinner="🚦 正在计算 EMA 量能评分…", ttl=1800)
def _cached_ema(portfolio_hash: str) -> pd.DataFrame:
    portfolio = json.loads(config.PORTFOLIO_PATH.read_text(encoding="utf-8"))
    return score_holdings_ema(portfolio)


@st.cache_data(show_spinner="🎯 正在计算 Fib 回撤预警…", ttl=1800)
def _cached_fib(portfolio_hash: str, lookback: int) -> pd.DataFrame:
    portfolio = json.loads(config.PORTFOLIO_PATH.read_text(encoding="utf-8"))
    return fib_alerts(portfolio, lookback)


@st.cache_data(show_spinner="📊 正在计算筹码分布预警…", ttl=1800)
def _cached_vp(portfolio_hash: str, lookback: int) -> pd.DataFrame:
    portfolio = json.loads(config.PORTFOLIO_PATH.read_text(encoding="utf-8"))
    return vp_alerts(portfolio, lookback)


@st.cache_data(show_spinner="🏅 正在计算相对强度 RS…", ttl=1800)
def _cached_rs(portfolio_hash: str) -> pd.DataFrame:
    portfolio = json.loads(config.PORTFOLIO_PATH.read_text(encoding="utf-8"))
    return relative_strength(portfolio)


@st.cache_data(show_spinner="⚡ 正在检测 MACD 背驰…", ttl=1800)
def _cached_div(portfolio_hash: str, lookback: int) -> pd.DataFrame:
    portfolio = json.loads(config.PORTFOLIO_PATH.read_text(encoding="utf-8"))
    return divergence_alerts(portfolio, lookback)


@st.cache_data(show_spinner=False, ttl=21600)
def _cached_13f(ticker: str) -> dict | None:
    return accum.institutional_summary(ticker)


def _chart_options(portfolio: dict) -> list[tuple[str, str]]:
    """返回 [(label, ticker), …]：持仓 ∪ 关注列表。"""
    opts: dict[str, str] = {}
    for acc in portfolio.get("accounts", []):
        for pos in acc.get("positions", []):
            t = pos["yf_ticker"].upper()
            name = pos.get("name") or pos.get("display") or t
            opts[t] = f"{name} ({t})"
    # 关注列表
    if config.WATCHLIST_PATH.exists():
        try:
            wl = json.loads(config.WATCHLIST_PATH.read_text(encoding="utf-8"))
            for t in wl.get("watchlist", []):
                tu = t.upper().strip()
                opts.setdefault(tu, f"⭐ {tu}")
        except Exception:
            pass
    return [(label, tic) for tic, label in sorted(opts.items())]


# ─── 颜色工具 ─────────────────────────────────────────────────────────────────
_GREEN = "#26a641"
_RED   = "#d73a4a"
_GRAY  = "#8b949e"

def _accel_color(v):
    if pd.isna(v): return _GRAY
    return _GREEN if v > 0 else _RED


# ─── 页面 ─────────────────────────────────────────────────────────────────────
st.title("📊 量能健康报告")

portfolio = _load_portfolio()

# 侧边栏参数
with st.sidebar:
    st.subheader("⚙️ 参数")
    decay  = st.slider("衰减因子 decay", 0.88, 0.99, DEFAULT_DECAY, 0.01,
                       help="越大→近期权重越集中")
    window = st.slider("回看窗口 window", 20, 60, DEFAULT_WINDOW, 5,
                       help="计算衰减得分的交易日数")
    st.caption("修改参数后点击「⚡ 刷新量能」重新计算。")

# 顶栏
col_title, col_btn = st.columns([5, 1])
with col_btn:
    refresh = st.button("⚡ 刷新量能", type="primary", width="stretch")

if refresh:
    st.cache_data.clear()
    st.rerun()

# 计算
ph = _portfolio_hash(portfolio)
with st.spinner("正在加载量能数据…"):
    df = _cached_score(ph, window, decay)

tab_ema, tab_fib, tab_vp, tab_div, tab_momentum, tab_accum, tab_chart = st.tabs(
    ["🚦 EMA量能", "🎯 Fib预警", "📊 筹码分布", "⚡ 背驰", "📈 量能报告", "🏦 主力吸筹", "🕯 技术图表"])

# ═══════════════════════════════════════════════════════════════════════════════
# EMA 量能评分 TAB （红绿灯 · 0-100 分 · AI 解读）
# ═══════════════════════════════════════════════════════════════════════════════
with tab_ema:
    st.subheader("🚦 EMA 量能评分")
    s_span, m_span, l_span = EMA_SPANS
    st.caption(
        f"基于 **EMA{s_span}/{m_span}/{l_span}** 的趋势打分（0-100）："
        f"位置(现价vsEMA{m_span}) 30% · 排列(多头/空头) 30% · "
        f"斜率(EMA{m_span}近5日) 25% · 乖离(防追高) 15%。"
        f"🟢≥70 强 / 🟡40-69 中 / 🔴<40 弱。仅供辅助研究，非投资建议。"
    )

    ema_df = _cached_ema(ph) if _EMA_AVAILABLE else pd.DataFrame()
    if not _EMA_AVAILABLE:
        st.info("🔄 EMA 量能模块刚更新，云端进程需重启后生效："
                "右下角 **Manage app → ⋮ → Reboot app**（重启后本页即恢复）。")
    elif ema_df.empty:
        st.warning("暂无有效数据，请检查持仓 ticker 或点击「⚡ 刷新量能」重试。")
    else:
        n_strong = int((ema_df["ema_score"] >= 70).sum())
        n_mid    = int(((ema_df["ema_score"] >= 40) & (ema_df["ema_score"] < 70)).sum())
        n_weak   = int((ema_df["ema_score"] < 40).sum())
        avg_score = float(ema_df["ema_score"].mean())
        e1, e2, e3, e4 = st.columns(4)
        e1.metric("🟢 强势 (≥70)", n_strong)
        e2.metric("🟡 中性 (40-69)", n_mid)
        e3.metric("🔴 弱势 (<40)", n_weak)
        e4.metric("组合平均量能分", f"{avg_score:.0f}")

        show_ema = ema_df.copy()
        show_ema["灯"] = show_ema["light"]
        show_ema["乖离%"] = show_ema["dev"] * 100
        show_ema["斜率%(5日)"] = show_ema["slope"] * 100

        # 相对强度 RS（vs SOXX）并入表格
        rs_df = _cached_rs(ph) if _EMA_AVAILABLE else pd.DataFrame()
        has_rs = not rs_df.empty
        if has_rs:
            rs_map = {r["ticker"]: r for _, r in rs_df.iterrows()}
            show_ema["相对强度"] = show_ema["ticker"].map(
                lambda t: rs_map[t]["rs_tag"] if t in rs_map else None)
            show_ema[f"vs{RS_BENCHMARK}%(3月)"] = show_ema["ticker"].map(
                lambda t: rs_map[t]["rs_3m"] * 100 if t in rs_map else None)
            show_ema["RS排名"] = show_ema["ticker"].map(
                lambda t: rs_map[t]["rs_rank"] if t in rs_map else None)

        show_ema = show_ema.rename(columns={
            "display": "股票", "ema_score": "量能分",
            "state": "状态", "price": "现价", "ema_mid": f"EMA{m_span}",
        })
        cols = ["灯", "股票", "量能分", "状态", "现价", f"EMA{m_span}", "乖离%", "斜率%(5日)"]
        if has_rs:
            cols += ["相对强度", f"vs{RS_BENCHMARK}%(3月)", "RS排名"]
        col_cfg = {
            "量能分": st.column_config.ProgressColumn(
                "量能分", format="%d", min_value=0, max_value=100,
                help="0-100，越高趋势越强",
            ),
            "现价": st.column_config.NumberColumn("现价", format="$%.2f"),
            f"EMA{m_span}": st.column_config.NumberColumn(f"EMA{m_span}", format="$%.2f"),
            "乖离%": st.column_config.NumberColumn("乖离%", format="%+.1f%%"),
            "斜率%(5日)": st.column_config.NumberColumn("斜率%(5日)", format="%+.2f%%"),
        }
        if has_rs:
            col_cfg[f"vs{RS_BENCHMARK}%(3月)"] = st.column_config.NumberColumn(
                f"vs{RS_BENCHMARK}%(3月)", format="%+.1f%%",
                help=f"近3月个股收益 − {RS_BENCHMARK} 收益，正=跑赢板块")
            col_cfg["RS排名"] = st.column_config.ProgressColumn(
                "RS排名", format="%d", min_value=0, max_value=100,
                help="组合内相对强度百分位，越高越领涨")
        st.dataframe(
            show_ema[cols],
            column_config=col_cfg,
            width="stretch", hide_index=True,
            height=min(560, 80 + len(show_ema) * 35),
        )
        if has_rs:
            st.caption(f"🏅 **相对强度 RS** = 个股相对 **{RS_BENCHMARK}**(费城半导体) 的强弱："
                       "领涨=跑赢板块 ≥10% / 落后=跑输 ≥10%；RS排名为组合内百分位。")

        warn_ema = ema_df[ema_df["ema_score"] < 40]
        if not warn_ema.empty:
            st.warning(
                "🔴 **量能预警**（分数 <40，多为破位/空头排列）：  \n"
                + "  ".join(f"`{r['display']} {r['ema_score']:.0f}`"
                           for _, r in warn_ema.iterrows())
            )

        # ── 🤖 AI 量能解读 ────────────────────────────────────────────────
        st.divider()
        st.markdown("#### 🤖 AI 量能解读")
        if not llm.available():
            st.info("未检测到 OpenAI Key。在 Streamlit Secrets 配置 `OPENAI_API_KEY` 后即可生成 AI 解读。")
        else:
            ema_ai_model = st.radio(
                "模型档位",
                options=[llm.DEFAULT_MODEL, llm.DEEP_MODEL],
                format_func=lambda m: "gpt-4o-mini（便宜·日常）" if m == llm.DEFAULT_MODEL
                else "gpt-4.1（更强·深度）",
                horizontal=True, key="ema_ai_model",
            )

            def _build_ema_payload() -> dict:
                # 仓位占比：从价格缓存估算股票市值权重
                try:
                    from core.price_updater import load_cache
                    _cache = load_cache()
                    _prices = _cache.get("prices", {}) if _cache else {}
                except Exception:
                    _prices = {}
                weights: dict[str, float] = {}
                for acc in portfolio.get("accounts", []):
                    for pos in acc.get("positions", []):
                        t = pos["yf_ticker"]
                        px_ = _prices.get(t)
                        sh = pos.get("shares")
                        if px_ and sh:
                            weights[t] = weights.get(t, 0.0) + px_ * sh
                tot = sum(weights.values()) or 1.0

                per = []
                for _, r in ema_df.iterrows():
                    row = {
                        "股票": r["display"],
                        "量能分": int(r["ema_score"]),
                        "灯": r["light"],
                        "状态": r["state"],
                        "乖离%": round(r["dev"] * 100, 1),
                        "斜率%(5日)": round(r["slope"] * 100, 2),
                        "占比%": round(weights.get(r["ticker"], 0.0) / tot * 100, 1),
                    }
                    if has_rs and r["ticker"] in rs_map:
                        _rr = rs_map[r["ticker"]]
                        row["相对强度"] = _rr["rs_tag"]
                        row[f"vs{RS_BENCHMARK}%(3月)"] = round(_rr["rs_3m"] * 100, 1)
                        row["RS排名"] = int(_rr["rs_rank"])
                    per.append(row)
                return {
                    "EMA参数": f"EMA{s_span}/{m_span}/{l_span}",
                    "评分口径": "0-100；🟢≥70 / 🟡40-69 / 🔴<40；由 位置/排列/斜率/乖离 加权",
                    "相对强度基准": RS_BENCHMARK if has_rs else None,
                    "分布": {"🟢强": n_strong, "🟡中": n_mid, "🔴弱": n_weak,
                            "平均分": round(avg_score, 1)},
                    "个股": per,
                }

            @st.cache_data(show_spinner="🤖 AI 正在解读量能…", ttl=1800)
            def _cached_ema_review(cache_key: str, payload: dict, model: str) -> dict:
                return ai_review.momentum_review(payload, model=model)

            if st.button("🩺 生成 AI 量能解读", type="primary", key="gen_ema_review"):
                payload = _build_ema_payload()
                ck = f"{ph}|{ema_ai_model}|{round(avg_score)}|{len(ema_df)}"
                try:
                    res = _cached_ema_review(ck, payload, ema_ai_model)
                except llm.LLMError as e:
                    st.error(f"AI 解读失败：{e}")
                    res = None

                if res:
                    if res.get("overview"):
                        st.markdown(f"### {res['overview']}")
                    ch, cw = st.columns(2)
                    with ch:
                        if res.get("healthy"):
                            st.markdown("#### 🟢 趋势健康")
                            for o in res["healthy"]:
                                st.markdown(f"- **{o.get('ticker','')}**：{o.get('note','')}")
                    with cw:
                        if res.get("warning"):
                            st.markdown("#### 🔴 需警惕破位")
                            for o in res["warning"]:
                                st.markdown(f"- **{o.get('ticker','')}**：{o.get('note','')}")
                    if res.get("accelerating"):
                        st.markdown("#### 🚀 动能加速")
                        for o in res["accelerating"]:
                            st.markdown(f"- **{o.get('ticker','')}**：{o.get('note','')}")
                    ca, cr = st.columns(2)
                    with ca:
                        if res.get("actions"):
                            st.markdown("#### 🧭 可关注方向")
                            for a in res["actions"]:
                                st.markdown(f"- {a}")
                    with cr:
                        if res.get("risks"):
                            st.markdown("#### ⚠️ 风险")
                            for rk in res["risks"]:
                                st.markdown(f"- {rk}")
                    u = res.get("_usage", {})
                    st.caption(
                        f"🤖 {res.get('_model','')} · {u.get('total_tokens','?')} tokens · "
                        f"~${res.get('_cost_usd',0):.4f} | AI 生成，非投资建议。"
                    )
                    with st.expander("🔎 查看喂给 AI 的原始数据"):
                        st.json(_build_ema_payload())

# ═══════════════════════════════════════════════════════════════════════════════
# Fib 回撤预警 TAB （波段高低点 · 关键支撑/阻力 · 只列触发预警 · AI 解读）
# ═══════════════════════════════════════════════════════════════════════════════
with tab_fib:
    st.subheader("🎯 Fibonacci 回撤预警")
    st.caption(
        f"从近 **{FIB_LOOKBACK} 交易日（约半年）** 的波段高/低点画 Fib 回撤线"
        f"（{' / '.join(f'{r:.1%}' for r in FIB_RATIOS)}），判断现价所处支撑/阻力带。"
        "**只列出触发预警的持仓**：🔴破位(跌破78.6%) · 🎯贴近关键位(±2%) · 🟢强势贴高。"
        "并附 EMA 量能分做**共振**参考。仅供辅助研究，非投资建议。"
    )

    fib_df = _cached_fib(ph, FIB_LOOKBACK) if _EMA_AVAILABLE else pd.DataFrame()
    if not _EMA_AVAILABLE:
        st.info("🔄 Fib 预警模块刚更新，云端进程需重启后生效："
                "右下角 **Manage app → ⋮ → Reboot app**（重启后本页即恢复）。")
    elif fib_df.empty:
        st.warning("暂无有效数据，请检查持仓 ticker 或点击「⚡ 刷新量能」重试。")
    else:
        # EMA 量能分映射（共振参考）
        ema_map: dict[str, int] = {}
        if _EMA_AVAILABLE:
            try:
                _ema_for_fib = _cached_ema(ph)
                if not _ema_for_fib.empty:
                    ema_map = {r["ticker"]: int(r["ema_score"])
                               for _, r in _ema_for_fib.iterrows()}
            except Exception:
                ema_map = {}

        triggered = fib_df[fib_df["trigger"]].copy()
        n_break = int((triggered["category"] == "破位预警").sum())
        n_near  = int(triggered["category"].str.startswith("贴近").sum()) if not triggered.empty else 0
        n_strong = int((triggered["category"] == "强势贴高").sum())

        f1, f2, f3, f4 = st.columns(4)
        f1.metric("🚨 触发预警", len(triggered))
        f2.metric("🔴 破位 (>78.6%)", n_break)
        f3.metric("🎯 贴近关键位", n_near)
        f4.metric("🟢 强势贴高", n_strong)

        if triggered.empty:
            st.success("✅ 当前无持仓触发 Fib 回撤预警——多数标的处于健康回撤区/趋势中段。")
        else:
            show_fib = triggered.copy()
            show_fib["灯"] = show_fib["fib_light"]
            show_fib["回撤%"] = show_fib["retr"] * 100
            show_fib["距最近位%"] = show_fib["dist"] * 100
            show_fib["量能分"] = show_fib["ticker"].map(ema_map)
            show_fib = show_fib.rename(columns={
                "display": "股票", "category": "预警", "fib_signal": "信号",
                "price": "现价", "nearest_fib": "最近Fib位", "nearest_price": "该位价",
                "swing_high": "波段高", "swing_low": "波段低",
            })
            cols = ["灯", "股票", "预警", "信号", "现价", "最近Fib位", "该位价",
                    "回撤%", "距最近位%", "量能分", "波段高", "波段低"]
            st.dataframe(
                show_fib[cols],
                column_config={
                    "现价": st.column_config.NumberColumn("现价", format="$%.2f"),
                    "该位价": st.column_config.NumberColumn("该位价", format="$%.2f"),
                    "波段高": st.column_config.NumberColumn("波段高", format="$%.2f"),
                    "波段低": st.column_config.NumberColumn("波段低", format="$%.2f"),
                    "回撤%": st.column_config.NumberColumn("回撤%", format="%.1f%%",
                        help="从波段极值回吐的比例，越大回撤越深"),
                    "距最近位%": st.column_config.NumberColumn("距最近位%", format="%+.1f%%",
                        help="现价距最近 Fib 位；接近 0=正贴该位"),
                    "量能分": st.column_config.ProgressColumn("量能分", format="%d",
                        min_value=0, max_value=100, help="EMA 量能分，共振参考"),
                },
                width="stretch", hide_index=True,
                height=min(500, 80 + len(show_fib) * 35),
            )

            break_rows = triggered[triggered["category"] == "破位预警"]
            if not break_rows.empty:
                st.warning(
                    "🔴 **破位预警**（跌破 78.6% 回撤，趋势转弱风险）：  \n"
                    + "  ".join(f"`{r['display']} 回撤{r['retr']*100:.0f}%`"
                               for _, r in break_rows.iterrows())
                )

        with st.expander("📖 怎么读这张预警表"):
            st.markdown(
                "- **波段高/低**：近半年内的最高/最低收盘价，Fib 线以此为锚。\n"
                "- **回撤%**：从波段极值回吐了多少。<23.6% 强势；38.2–61.8% 健康回撤区；"
                ">78.6% 视为破位。\n"
                "- **贴近关键位**：现价落在 38.2/50/61.8/78.6% 附近（±2%），这些是常见"
                "支撑/阻力带，容易出现反应。**61.8%（黄金分割）**最受关注。\n"
                "- **量能分共振**：Fib 回踩关键支撑 + 量能分仍高(🟢) = 偏多共振；"
                "Fib 破位 + 量能分低(🔴) = 偏淡共振。\n"
                "- Fib 高低点选择较主观，请与 EMA 量能、基本面一起看，切勿单独据此操作。"
            )

        # ── 🤖 AI Fib 预警解读 ────────────────────────────────────────────
        st.divider()
        st.markdown("#### 🤖 AI Fib 预警解读")
        if triggered.empty:
            st.caption("当前无触发项，无需 AI 解读。")
        elif not llm.available():
            st.info("未检测到 OpenAI Key。在 Streamlit Secrets 配置 `OPENAI_API_KEY` 后即可生成 AI 解读。")
        else:
            fib_ai_model = st.radio(
                "模型档位",
                options=[llm.DEFAULT_MODEL, llm.DEEP_MODEL],
                format_func=lambda m: "gpt-4o-mini（便宜·日常）" if m == llm.DEFAULT_MODEL
                else "gpt-4.1（更强·深度）",
                horizontal=True, key="fib_ai_model",
            )

            def _build_fib_payload() -> dict:
                # 仓位占比：从价格缓存估算股票市值权重
                try:
                    from core.price_updater import load_cache
                    _cache = load_cache()
                    _prices = _cache.get("prices", {}) if _cache else {}
                except Exception:
                    _prices = {}
                weights: dict[str, float] = {}
                for acc in portfolio.get("accounts", []):
                    for pos in acc.get("positions", []):
                        t = pos["yf_ticker"]
                        px_ = _prices.get(t)
                        sh = pos.get("shares")
                        if px_ and sh:
                            weights[t] = weights.get(t, 0.0) + px_ * sh
                tot = sum(weights.values()) or 1.0

                per = []
                for _, r in triggered.iterrows():
                    per.append({
                        "股票": r["display"],
                        "灯": r["fib_light"],
                        "信号": r["fib_signal"],
                        "类别": r["category"],
                        "回撤%": round(r["retr"] * 100, 1),
                        "最近Fib位": r["nearest_fib"],
                        "距最近位%": round(r["dist"] * 100, 1),
                        "量能分": ema_map.get(r["ticker"]),
                        "占比%": round(weights.get(r["ticker"], 0.0) / tot * 100, 1),
                    })
                return {
                    "波段窗口": f"{FIB_LOOKBACK} 交易日",
                    "口径": "从近半年波段高/低点画 Fib 回撤；retr=回撤进度；"
                            "🔴破位>78.6% / 贴近关键位(±2%) / 🟢强势贴高",
                    "组合概览": {"触发数": len(triggered), "破位数": n_break,
                                "贴近关键位数": n_near, "强势数": n_strong},
                    "触发预警": per,
                }

            @st.cache_data(show_spinner="🤖 AI 正在解读 Fib 预警…", ttl=1800)
            def _cached_fib_review(cache_key: str, payload: dict, model: str) -> dict:
                return ai_review.fib_review(payload, model=model)

            if st.button("🩺 生成 AI Fib 解读", type="primary", key="gen_fib_review"):
                payload = _build_fib_payload()
                ck = f"{ph}|{fib_ai_model}|{len(triggered)}|{n_break}"
                try:
                    res = _cached_fib_review(ck, payload, fib_ai_model)
                except llm.LLMError as e:
                    st.error(f"AI 解读失败：{e}")
                    res = None

                if res:
                    if res.get("overview"):
                        st.markdown(f"### {res['overview']}")
                    cs, cb = st.columns(2)
                    with cs:
                        if res.get("support_watch"):
                            st.markdown("#### 🎯 回踩支撑·关注")
                            for o in res["support_watch"]:
                                st.markdown(f"- **{o.get('ticker','')}**：{o.get('note','')}")
                    with cb:
                        if res.get("breakdown"):
                            st.markdown("#### 🔴 破位·警惕")
                            for o in res["breakdown"]:
                                st.markdown(f"- **{o.get('ticker','')}**：{o.get('note','')}")
                    ca, cr = st.columns(2)
                    with ca:
                        if res.get("actions"):
                            st.markdown("#### 🧭 可关注方向")
                            for a in res["actions"]:
                                st.markdown(f"- {a}")
                    with cr:
                        if res.get("risks"):
                            st.markdown("#### ⚠️ 风险")
                            for rk in res["risks"]:
                                st.markdown(f"- {rk}")
                    u = res.get("_usage", {})
                    st.caption(
                        f"🤖 {res.get('_model','')} · {u.get('total_tokens','?')} tokens · "
                        f"~${res.get('_cost_usd',0):.4f} | AI 生成，非投资建议。"
                    )
                    with st.expander("🔎 查看喂给 AI 的原始数据"):
                        st.json(_build_fib_payload())

# ═══════════════════════════════════════════════════════════════════════════════
# 筹码分布预警 TAB （Volume Profile · POC/VAH/VAL · 只列触发预警 · AI 解读）
# ═══════════════════════════════════════════════════════════════════════════════
with tab_vp:
    st.subheader("📊 筹码分布预警（Volume Profile）")
    st.caption(
        f"按近 **{VP_LOOKBACK} 交易日（约半年）** 的日线成交量分价格区间统计，"
        f"找出 **POC**（成交最密集价位·最强磁吸）与 **VAH/VAL**（包住 {VP_VALUE_AREA:.0%} "
        "成交量的价值区上/下沿）。**只列出现价贴近关键位(±3%)的持仓**："
        "🟢上破VAH · 🟡测试VAH/测试VAL/回踩POC · 🔴跌破VAL。"
        "远离关键位（延伸段/区间中部）不预警。并附 EMA 量能分做**共振**参考。仅供辅助研究，非投资建议。"
    )

    vp_df = _cached_vp(ph, VP_LOOKBACK) if _EMA_AVAILABLE else pd.DataFrame()
    if not _EMA_AVAILABLE:
        st.info("🔄 筹码分布模块刚更新，云端进程需重启后生效："
                "右下角 **Manage app → ⋮ → Reboot app**（重启后本页即恢复）。")
    elif vp_df.empty:
        st.warning("暂无有效数据，请检查持仓 ticker 或点击「⚡ 刷新量能」重试。")
    else:
        # EMA 量能分映射（共振参考）
        ema_map_vp: dict[str, int] = {}
        if _EMA_AVAILABLE:
            try:
                _ema_for_vp = _cached_ema(ph)
                if not _ema_for_vp.empty:
                    ema_map_vp = {r["ticker"]: int(r["ema_score"])
                                  for _, r in _ema_for_vp.iterrows()}
            except Exception:
                ema_map_vp = {}

        triggered_vp = vp_df[vp_df["trigger"]].copy()
        n_up   = int((triggered_vp["category"] == "上破VAH").sum()) if not triggered_vp.empty else 0
        n_down = int((triggered_vp["category"] == "跌破VAL").sum()) if not triggered_vp.empty else 0
        n_watch = len(triggered_vp) - n_up - n_down   # 测试VAH/测试VAL/回踩POC

        v1, v2, v3, v4 = st.columns(4)
        v1.metric("🚨 触发预警", len(triggered_vp))
        v2.metric("🟢 上破VAH", n_up)
        v3.metric("🔴 跌破VAL", n_down)
        v4.metric("🟡 测试/回踩", n_watch)

        if triggered_vp.empty:
            st.success("✅ 当前无持仓触发筹码预警——多数标的远离关键筹码位（延伸段或区间中部）。")
        else:
            show_vp = triggered_vp.copy()
            show_vp["灯"] = show_vp["vp_light"]
            show_vp["距POC%"] = show_vp["d_poc"] * 100
            show_vp["价值区宽度%"] = show_vp["va_width"] * 100
            show_vp["量能分"] = show_vp["ticker"].map(ema_map_vp)
            show_vp = show_vp.rename(columns={
                "display": "股票", "category": "预警", "vp_signal": "信号",
                "price": "现价", "poc": "POC", "vah": "VAH", "val": "VAL",
            })
            cols = ["灯", "股票", "预警", "信号", "现价", "POC", "VAH", "VAL",
                    "距POC%", "价值区宽度%", "量能分"]
            st.dataframe(
                show_vp[cols],
                column_config={
                    "现价": st.column_config.NumberColumn("现价", format="$%.2f"),
                    "POC": st.column_config.NumberColumn("POC", format="$%.2f",
                        help="成交最密集价位（最强支撑/阻力/磁吸）"),
                    "VAH": st.column_config.NumberColumn("VAH", format="$%.2f",
                        help="价值区上沿"),
                    "VAL": st.column_config.NumberColumn("VAL", format="$%.2f",
                        help="价值区下沿"),
                    "距POC%": st.column_config.NumberColumn("距POC%", format="%+.1f%%",
                        help="现价相对 POC；正=上方"),
                    "价值区宽度%": st.column_config.NumberColumn("价值区宽度%", format="%.1f%%",
                        help="价值区宽度（相对 POC）；越小筹码越集中"),
                    "量能分": st.column_config.ProgressColumn("量能分", format="%d",
                        min_value=0, max_value=100, help="EMA 量能分，共振参考"),
                },
                width="stretch", hide_index=True,
                height=min(500, 80 + len(show_vp) * 35),
            )

            down_rows = triggered_vp[triggered_vp["category"] == "跌破VAL"]
            if not down_rows.empty:
                st.warning(
                    "🔴 **跌出价值区**（跌破 VAL，筹码支撑失守风险）：  \n"
                    + "  ".join(f"`{r['display']} ${r['price']:.2f}<VAL${r['val']:.2f}`"
                               for _, r in down_rows.iterrows())
                )

        with st.expander("📖 怎么读这张筹码预警表"):
            st.markdown(
                "- **POC（控制点）**：近半年成交量最大的价位，是最强的支撑/阻力与磁吸位，"
                "价格常反复回踩。\n"
                "- **VAH / VAL（价值区上/下沿）**：包住约 70% 成交量的区间边界。区间内=公允震荡；"
                "**上破 VAH**=多头掌控、有效放量走强；**跌破 VAL**=筹码支撑失守、转弱。\n"
                "- **回踩 POC（±2%）**：多空成本交汇的决策区，容易变盘——需结合量能看方向。\n"
                "- **价值区宽度**：越窄说明筹码越集中，一旦突破意义越大；越宽说明分散、突破可信度低。\n"
                "- **量能分共振**：上破VAH + 量能🟢=偏多共振；跌破VAL + 量能🔴=偏淡共振。\n"
                "- 与 EMA(趋势)、Fib(几何回撤) 一起看效果最好，切勿单独据此操作。"
            )

        # ── 🤖 AI 筹码预警解读 ────────────────────────────────────────────
        st.divider()
        st.markdown("#### 🤖 AI 筹码预警解读")
        if triggered_vp.empty:
            st.caption("当前无触发项，无需 AI 解读。")
        elif not llm.available():
            st.info("未检测到 OpenAI Key。在 Streamlit Secrets 配置 `OPENAI_API_KEY` 后即可生成 AI 解读。")
        else:
            vp_ai_model = st.radio(
                "模型档位",
                options=[llm.DEFAULT_MODEL, llm.DEEP_MODEL],
                format_func=lambda m: "gpt-4o-mini（便宜·日常）" if m == llm.DEFAULT_MODEL
                else "gpt-4.1（更强·深度）",
                horizontal=True, key="vp_ai_model",
            )

            def _build_vp_payload() -> dict:
                # 仓位占比：从价格缓存估算股票市值权重
                try:
                    from core.price_updater import load_cache
                    _cache = load_cache()
                    _prices = _cache.get("prices", {}) if _cache else {}
                except Exception:
                    _prices = {}
                weights: dict[str, float] = {}
                for acc in portfolio.get("accounts", []):
                    for pos in acc.get("positions", []):
                        t = pos["yf_ticker"]
                        px_ = _prices.get(t)
                        sh = pos.get("shares")
                        if px_ and sh:
                            weights[t] = weights.get(t, 0.0) + px_ * sh
                tot = sum(weights.values()) or 1.0

                per = []
                for _, r in triggered_vp.iterrows():
                    per.append({
                        "股票": r["display"],
                        "灯": r["vp_light"],
                        "信号": r["vp_signal"],
                        "类别": r["category"],
                        "现价": r["price"],
                        "POC": r["poc"], "VAH": r["vah"], "VAL": r["val"],
                        "距POC%": round(r["d_poc"] * 100, 1),
                        "价值区宽度%": round(r["va_width"] * 100, 1),
                        "量能分": ema_map_vp.get(r["ticker"]),
                        "占比%": round(weights.get(r["ticker"], 0.0) / tot * 100, 1),
                    })
                return {
                    "回看窗口": f"{VP_LOOKBACK} 交易日",
                    "口径": "近半年日线成交量按价格分箱；POC=成交最密集价位；"
                            "VAH/VAL=价值区上下沿；贴近关键位(±3%)才预警：🟢上破VAH / "
                            "🔴跌破VAL / 🟡测试VAH·测试VAL·回踩POC",
                    "组合概览": {"触发数": len(triggered_vp), "上破VAH": n_up,
                                "跌破VAL": n_down, "测试/回踩": n_watch},
                    "触发预警": per,
                }

            @st.cache_data(show_spinner="🤖 AI 正在解读筹码分布…", ttl=1800)
            def _cached_vp_review(cache_key: str, payload: dict, model: str) -> dict:
                return ai_review.vp_review(payload, model=model)

            if st.button("🩺 生成 AI 筹码解读", type="primary", key="gen_vp_review"):
                payload = _build_vp_payload()
                ck = f"{ph}|{vp_ai_model}|{len(triggered_vp)}|{n_up}|{n_down}"
                try:
                    res = _cached_vp_review(ck, payload, vp_ai_model)
                except llm.LLMError as e:
                    st.error(f"AI 解读失败：{e}")
                    res = None

                if res:
                    if res.get("overview"):
                        st.markdown(f"### {res['overview']}")
                    cu, cd = st.columns(2)
                    with cu:
                        if res.get("breakout"):
                            st.markdown("#### 🟢 上破价值区·关注")
                            for o in res["breakout"]:
                                st.markdown(f"- **{o.get('ticker','')}**：{o.get('note','')}")
                    with cd:
                        if res.get("breakdown"):
                            st.markdown("#### 🔴 跌出价值区·警惕")
                            for o in res["breakdown"]:
                                st.markdown(f"- **{o.get('ticker','')}**：{o.get('note','')}")
                    if res.get("at_poc"):
                        st.markdown("#### 🟡 回踩 POC·变盘观察")
                        for o in res["at_poc"]:
                            st.markdown(f"- **{o.get('ticker','')}**：{o.get('note','')}")
                    ca, cr = st.columns(2)
                    with ca:
                        if res.get("actions"):
                            st.markdown("#### 🧭 可关注方向")
                            for a in res["actions"]:
                                st.markdown(f"- {a}")
                    with cr:
                        if res.get("risks"):
                            st.markdown("#### ⚠️ 风险")
                            for rk in res["risks"]:
                                st.markdown(f"- {rk}")
                    u = res.get("_usage", {})
                    st.caption(
                        f"🤖 {res.get('_model','')} · {u.get('total_tokens','?')} tokens · "
                        f"~${res.get('_cost_usd',0):.4f} | AI 生成，非投资建议。"
                    )
                    with st.expander("🔎 查看喂给 AI 的原始数据"):
                        st.json(_build_vp_payload())

# ═══════════════════════════════════════════════════════════════════════════════
# MACD 背驰预警 TAB （缠论"背驰"·顶背驰/底背驰·反转早期预警·AI 解读）
# ═══════════════════════════════════════════════════════════════════════════════
with tab_div:
    st.subheader("⚡ MACD 背驰预警")
    st.caption(
        f"按近 **{MACD_LOOKBACK} 交易日** 的日线自算 MACD(12/26/9)，检测价格枢轴与 DIF 的**背驰**："
        "🔴 **顶背驰**=价创新高但 MACD 动能走弱(涨势或衰竭) · "
        "🟢 **底背驰**=价创新低但 MACD 动能转强(跌势或衰竭)。"
        "**只列出最新枢轴在近 30 根内的有效背驰**，并附 EMA 量能分 / 相对强度做**共振**参考。"
        "背驰是概率性早期信号，需价格/量能确认，非投资建议。"
    )

    div_df = _cached_div(ph, MACD_LOOKBACK) if _EMA_AVAILABLE else pd.DataFrame()
    if not _EMA_AVAILABLE:
        st.info("🔄 背驰模块刚更新，云端进程需重启后生效："
                "右下角 **Manage app → ⋮ → Reboot app**（重启后本页即恢复）。")
    elif div_df.empty:
        st.warning("暂无有效数据，请检查持仓 ticker 或点击「⚡ 刷新量能」重试。")
    else:
        # EMA 量能分 & 相对强度映射（共振参考）
        ema_map_div: dict[str, int] = {}
        rs_map_div: dict[str, str] = {}
        try:
            _ema_for_div = _cached_ema(ph)
            if not _ema_for_div.empty:
                ema_map_div = {r["ticker"]: int(r["ema_score"])
                               for _, r in _ema_for_div.iterrows()}
        except Exception:
            ema_map_div = {}
        try:
            _rs_for_div = _cached_rs(ph)
            if not _rs_for_div.empty:
                rs_map_div = {r["ticker"]: r["rs_tag"] for _, r in _rs_for_div.iterrows()}
        except Exception:
            rs_map_div = {}

        triggered_div = div_df[div_df["trigger"]].copy()
        n_top = int((triggered_div["signal"] == "顶背驰").sum()) if not triggered_div.empty else 0
        n_bot = int((triggered_div["signal"] == "底背驰").sum()) if not triggered_div.empty else 0

        d1, d2, d3 = st.columns(3)
        d1.metric("🚨 触发背驰", len(triggered_div))
        d2.metric("🔴 顶背驰", n_top)
        d3.metric("🟢 底背驰", n_bot)

        if triggered_div.empty:
            st.success("✅ 当前无持仓触发背驰——多数标的价格与 MACD 动能同步，暂无明显反转预警。")
        else:
            show_div = triggered_div.copy()
            show_div["灯"] = show_div["div_light"]
            show_div["量能分"] = show_div["ticker"].map(ema_map_div)
            show_div["相对强度"] = show_div["ticker"].map(rs_map_div)
            show_div = show_div.rename(columns={
                "display": "股票", "signal": "背驰", "note": "说明",
                "price": "现价", "dif": "DIF", "dea": "DEA",
                "macd_hist": "MACD柱", "macd_state": "MACD状态",
            })
            cols = ["灯", "股票", "背驰", "说明", "现价", "DIF", "DEA",
                    "MACD柱", "MACD状态", "量能分", "相对强度"]
            st.dataframe(
                show_div[cols],
                column_config={
                    "现价": st.column_config.NumberColumn("现价", format="$%.2f"),
                    "DIF": st.column_config.NumberColumn("DIF", format="%.3f",
                        help="MACD 快线（EMA12−EMA26）"),
                    "DEA": st.column_config.NumberColumn("DEA", format="%.3f",
                        help="MACD 慢线（DIF 的 EMA9）"),
                    "MACD柱": st.column_config.NumberColumn("MACD柱", format="%+.3f",
                        help="(DIF−DEA)×2；柱缩短=动能衰减"),
                    "量能分": st.column_config.ProgressColumn("量能分", format="%d",
                        min_value=0, max_value=100, help="EMA 量能分，共振参考"),
                },
                width="stretch", hide_index=True,
                height=min(500, 80 + len(show_div) * 35),
            )

            top_rows = triggered_div[triggered_div["signal"] == "顶背驰"]
            if not top_rows.empty:
                st.warning(
                    "🔴 **顶背驰**（价创新高但动能走弱，警惕见顶回落）：  \n"
                    + "  ".join(f"`{r['display']} ${r['price']:.2f}`"
                               for _, r in top_rows.iterrows())
                )

        with st.expander("📖 怎么读这张背驰预警表"):
            st.markdown(
                "- **背驰（缠论精髓）**：价格与 MACD 动能「走反」，是趋势可能反转的**早期**信号。\n"
                "- **🔴 顶背驰**：价格创新高，但 DIF 未能同步创新高（且 DIF>0）——上涨「后劲不足」，"
                "警惕见顶回落，尤其配合量能转弱/相对强度落后。\n"
                "- **🟢 底背驰**：价格创新低，但 DIF 未能同步创新低（且 DIF<0）——下跌「动能枯竭」，"
                "关注见底企稳，尤其配合相对强度回升。\n"
                "- **DIF / DEA / MACD柱**：DIF 是快线、DEA 是慢线，柱=（DIF−DEA)×2；柱由长转短=动能衰减。\n"
                "- **MACD状态**：金叉/死叉 + 零轴上/下方，判断当前多空强弱位置。\n"
                "- **共振**：顶背驰 + 量能🔴 + 相对落后 = 偏空共振；底背驰 + 相对领涨 = 偏多共振。\n"
                "- 背驰是**概率性**信号，可能「钝化」后继续单边，务必等价格/量能确认，切勿单独据此操作。"
            )

        # ── 🤖 AI 背驰预警解读 ────────────────────────────────────────────
        st.divider()
        st.markdown("#### 🤖 AI 背驰预警解读")
        if triggered_div.empty:
            st.caption("当前无触发项，无需 AI 解读。")
        elif not llm.available():
            st.info("未检测到 OpenAI Key。在 Streamlit Secrets 配置 `OPENAI_API_KEY` 后即可生成 AI 解读。")
        else:
            div_ai_model = st.radio(
                "模型档位",
                options=[llm.DEFAULT_MODEL, llm.DEEP_MODEL],
                format_func=lambda m: "gpt-4o-mini（便宜·日常）" if m == llm.DEFAULT_MODEL
                else "gpt-4.1（更强·深度）",
                horizontal=True, key="div_ai_model",
            )

            def _build_div_payload() -> dict:
                # 仓位占比：从价格缓存估算股票市值权重
                try:
                    from core.price_updater import load_cache
                    _cache = load_cache()
                    _prices = _cache.get("prices", {}) if _cache else {}
                except Exception:
                    _prices = {}
                weights: dict[str, float] = {}
                for acc in portfolio.get("accounts", []):
                    for pos in acc.get("positions", []):
                        t = pos["yf_ticker"]
                        px_ = _prices.get(t)
                        sh = pos.get("shares")
                        if px_ and sh:
                            weights[t] = weights.get(t, 0.0) + px_ * sh
                tot = sum(weights.values()) or 1.0

                per = []
                for _, r in triggered_div.iterrows():
                    per.append({
                        "股票": r["display"],
                        "灯": r["div_light"],
                        "背驰": r["signal"],
                        "说明": r["note"],
                        "现价": r["price"],
                        "DIF": r["dif"], "DEA": r["dea"], "MACD柱": r["macd_hist"],
                        "MACD状态": r["macd_state"],
                        "量能分": ema_map_div.get(r["ticker"]),
                        "相对强度": rs_map_div.get(r["ticker"]),
                        "占比%": round(weights.get(r["ticker"], 0.0) / tot * 100, 1),
                    })
                return {
                    "回看窗口": f"{MACD_LOOKBACK} 交易日",
                    "MACD参数": "12/26/9",
                    "口径": "价格枢轴 vs DIF 背驰；顶背驰=价新高但DIF不新高(DIF>0)；"
                            "底背驰=价新低但DIF不新低(DIF<0)；仅列最新枢轴在近30根内的有效背驰",
                    "组合概览": {"触发数": len(triggered_div), "顶背驰": n_top, "底背驰": n_bot},
                    "触发预警": per,
                }

            @st.cache_data(show_spinner="🤖 AI 正在解读背驰…", ttl=1800)
            def _cached_div_review(cache_key: str, payload: dict, model: str) -> dict:
                return ai_review.divergence_review(payload, model=model)

            if st.button("🩺 生成 AI 背驰解读", type="primary", key="gen_div_review"):
                payload = _build_div_payload()
                ck = f"{ph}|{div_ai_model}|{len(triggered_div)}|{n_top}|{n_bot}"
                try:
                    res = _cached_div_review(ck, payload, div_ai_model)
                except llm.LLMError as e:
                    st.error(f"AI 解读失败：{e}")
                    res = None

                if res:
                    if res.get("overview"):
                        st.markdown(f"### {res['overview']}")
                    ct, cb = st.columns(2)
                    with ct:
                        if res.get("top_div"):
                            st.markdown("#### 🔴 顶背驰·警惕见顶")
                            for o in res["top_div"]:
                                st.markdown(f"- **{o.get('ticker','')}**：{o.get('note','')}")
                    with cb:
                        if res.get("bottom_div"):
                            st.markdown("#### 🟢 底背驰·关注见底")
                            for o in res["bottom_div"]:
                                st.markdown(f"- **{o.get('ticker','')}**：{o.get('note','')}")
                    ca, cr = st.columns(2)
                    with ca:
                        if res.get("actions"):
                            st.markdown("#### 🧭 可关注方向")
                            for a in res["actions"]:
                                st.markdown(f"- {a}")
                    with cr:
                        if res.get("risks"):
                            st.markdown("#### ⚠️ 风险")
                            for rk in res["risks"]:
                                st.markdown(f"- {rk}")
                    u = res.get("_usage", {})
                    st.caption(
                        f"🤖 {res.get('_model','')} · {u.get('total_tokens','?')} tokens · "
                        f"~${res.get('_cost_usd',0):.4f} | AI 生成，非投资建议。"
                    )
                    with st.expander("🔎 查看喂给 AI 的原始数据"):
                        st.json(_build_div_payload())

# ═══════════════════════════════════════════════════════════════════════════════
# 技术图表 TAB （独立于量能数据，先渲染以避免量能 st.stop 影响）
# ═══════════════════════════════════════════════════════════════════════════════
with tab_accum:
    st.subheader("🏦 主力/机构 吸筹·派发 信号")
    st.caption("基于日线量价行为的**代理信号**：CMF 资金流 / OBV 能量潮 / 量比 / "
               "涨跌量比 / MFI / 放量突破。⚠️ yfinance 无 Level-2 逐笔大单数据，"
               "本页为行为推断，非真实主力资金流向，仅供辅助判断。")

    accum_df = _cached_accum(ph)
    if accum_df.empty:
        st.warning("暂无有效数据，请检查持仓 ticker 或稍后重试。")
    else:
        n_buy = int((accum_df["评分"] >= 3).sum())
        n_sell = int((accum_df["评分"] <= -3).sum())
        n_neu = len(accum_df) - n_buy - n_sell
        m1, m2, m3 = st.columns(3)
        m1.metric("🟢 疑似吸筹", n_buy)
        m2.metric("🟡 中性", n_neu)
        m3.metric("🔴 疑似派发", n_sell)

        st.dataframe(
            accum_df.drop(columns=["_reasons"]).style.format({
                "量比": "{:.2f}", "CMF": "{:+.3f}", "涨跌量比": "{:.2f}",
                "MFI": "{:.0f}", "OBV斜率": "{:+.4f}",
            }, na_rep="—"),
            width="stretch", hide_index=True,
        )

        st.caption("**评分逻辑**：CMF>0.05 +2 / OBV上行 +1 / 涨跌量比>1.2 +1 / "
                   "放量上涨 +1 / 放量突破新高 +2 / MFI<20 +1；反向对称扣分。"
                   "评分 ≥3 判定吸筹，≤-3 判定派发。")

        with st.expander("📋 各股信号解读"):
            for _, r in accum_df.iterrows():
                st.markdown(f"**{r['股票']} ({r['代码']})** — {r['判定']}（评分 {r['评分']}）")
                if r["_reasons"]:
                    for rs in str(r["_reasons"]).split("；"):
                        if rs:
                            st.markdown(f"- {rs}")

    # ── 机构 13F 持仓（季度真实数据，作为参考）──────────────────────────────
    st.divider()
    st.subheader("🏛️ 机构 13F 持仓（季度参考）")
    st.caption("来自 SEC 13F 披露的**真实**机构持仓，但按季申报、滞后约 1~2 个月，"
               "适合看中长期机构态度，与上方实时量价信号互补。")

    accum_options = _chart_options(portfolio)
    if accum_options:
        labels_13f = [o[0] for o in accum_options]
        sel_label_13f = st.selectbox("选择标的查看机构持仓", labels_13f, index=0, key="inst_ticker")
        sel_13f_ticker = dict((l, t) for l, t in accum_options)[sel_label_13f]

        with st.spinner(f"加载 {sel_13f_ticker} 机构持仓…"):
            info = _cached_13f(sel_13f_ticker)

        if not info:
            st.info(f"暂无 {sel_13f_ticker} 的机构持仓数据（部分海外/小盘股 yfinance 无 13F）。")
        else:
            g1, g2, g3, g4 = st.columns(4)
            g1.metric("机构持股占比", f"{info['inst_pct']:.1%}" if info["inst_pct"] else "—")
            g2.metric("机构家数", f"{int(info['inst_count']):,}" if info["inst_count"] else "—")
            if info["net_pct"] is not None:
                arrow = "🟢 净增持" if info["net_pct"] > 0 else ("🔴 净减持" if info["net_pct"] < 0 else "持平")
                g3.metric("Top机构季度净增减", f"{info['net_pct']:+.1%}", delta=arrow,
                          help="Top 机构按持股份额加权的季度环比增减仓；>0=整体加仓")
            else:
                g3.metric("Top机构季度净增减", "—")
            g4.metric("数据截至", info["as_of"] or "—",
                      help=f"增持 {info['n_up']} 家 / 减持 {info['n_down']} 家"
                           if info["n_up"] is not None else "")

            if info["top"] is not None and not info["top"].empty:
                st.dataframe(
                    info["top"].style.format({
                        "持股占比": "{:.2%}", "股数": "{:,.0f}",
                        "市值": "${:,.0f}", "季度变化": "{:+.1%}",
                    }, na_rep="—"),
                    width="stretch", hide_index=True,
                )
                st.caption("「季度变化」为各机构相对上一季的持股变动；+100% 通常代表新建仓或翻倍。")

# ═══════════════════════════════════════════════════════════════════════════════
with tab_chart:
    st.subheader("🕯 个股技术分析")
    options = _chart_options(portfolio)
    if not options:
        st.info("暂无可选标的，请先在「投资组合」或「关注列表」中添加股票。")
    else:
        c1, c2 = st.columns([3, 2])
        with c1:
            labels = [o[0] for o in options]
            sel_label = st.selectbox("选择标的", labels, index=0, key="tech_ticker")
            sel_tech_ticker = dict((l, t) for l, t in options)[sel_label]
        with c2:
            period_label = st.radio(
                "周期", ["1M", "3M", "6M", "1Y"], index=2,
                horizontal=True, key="tech_period",
            )
        period_map = {"1M": "1mo", "3M": "3mo", "6M": "6mo", "1Y": "1y"}

        ind_cols = st.columns(4)
        show_vol  = ind_cols[0].checkbox("成交量", value=True, key="t_vol")
        show_rsi  = ind_cols[1].checkbox("RSI", value=True, key="t_rsi")
        show_macd = ind_cols[2].checkbox("MACD", value=False, key="t_macd")
        show_boll = ind_cols[3].checkbox("布林带", value=False, key="t_boll")

        with st.spinner(f"加载 {sel_tech_ticker} K线数据…"):
            ohlcv = _cached_ohlcv(sel_tech_ticker, period_map[period_label])

        if ohlcv.empty or len(ohlcv) < 5:
            st.warning(f"暂无 {sel_tech_ticker} 的历史 K 线数据。")
        else:
            fig = build_candlestick_chart(
                ohlcv, sel_tech_ticker,
                mas=[5, 20, 60],
                show_volume=show_vol,
                show_rsi=show_rsi,
                show_macd=show_macd,
                show_bollinger=show_boll,
            )
            st.plotly_chart(fig, width="stretch")

            # 简要快照
            last = ohlcv.iloc[-1]
            prev = ohlcv.iloc[-2]
            chg_pct = (last["Close"] / prev["Close"] - 1) * 100
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("最新收盘", f"${last['Close']:,.2f}", delta=f"{chg_pct:+.2f}%")
            k2.metric("当日最高", f"${last['High']:,.2f}")
            k3.metric("当日最低", f"${last['Low']:,.2f}")
            k4.metric("成交量", f"{last['Volume']:,.0f}")

# ═══════════════════════════════════════════════════════════════════════════════
# 量能报告 TAB
# ═══════════════════════════════════════════════════════════════════════════════
with tab_momentum:
    if df.empty:
        st.warning("数据不足，请检查持仓 ticker 是否正确，或点击「⚡ 刷新量能」重试。")
        st.stop()

    n_ok = len(df)
    n_total = sum(len(a["positions"]) for a in portfolio.get("accounts", []))
    st.caption(f"📅 基于缓存（30分钟内复用） | {n_ok}/{n_total} 只有效数据 | "
               f"decay={decay}  window={window}")

    st.divider()

    # ═══════════════════════════════════════════════════════════════════════════
    # ① 多周期收益热力图
    # ═══════════════════════════════════════════════════════════════════════════
    st.subheader("① 多周期收益热力图")
    st.caption("颜色：🟢 正收益 / 🔴 负收益。按综合得分由高到低排列。")

    period_cols = [f"ret_{p}d" for p in PERIODS]
    heat_df = df[["display"] + period_cols].copy()
    heat_df = heat_df.set_index("display")
    heat_df.columns = [f"{p}D" for p in PERIODS]

    # 转为百分比
    heat_pct = heat_df.multiply(100)

    # 颜色范围：对称区间
    abs_max = float(heat_pct.abs().max().max())
    abs_max = max(abs_max, 1.0)

    fig_heat = px.imshow(
        heat_pct,
        color_continuous_scale="RdYlGn",
        zmin=-abs_max, zmax=abs_max,
        aspect="auto",
        text_auto=".1f",
        labels={"color": "收益%"},
    )
    fig_heat.update_traces(textfont_size=11)
    fig_heat.update_layout(
        xaxis_title=None,
        yaxis_title=None,
        coloraxis_colorbar=dict(title="收益%", thickness=12),
        margin=dict(t=10, b=10, l=10, r=80),
        height=max(320, n_ok * 22),
    )
    st.plotly_chart(fig_heat, width="stretch")

    st.divider()

    # ═══════════════════════════════════════════════════════════════════════════
    # ② 综合动量得分排名
    # ═══════════════════════════════════════════════════════════════════════════
    st.subheader("② 综合动量得分排名")
    st.caption("横向柱：综合得分 (z-score)。颜色=动量方向：🟢加速 / 🔴减速。右标=趋势箭头。")

    rank_df = df[["display", "composite", "accel", "direction", "avg_r5", "avg_r20"]].copy()
    rank_df = rank_df.sort_values("composite")   # plotly horizontal bar: bottom=low

    colors = [_accel_color(v) for v in rank_df["accel"]]
    labels = [f"{d} {r5*100:+.2f}%"
              for d, r5 in zip(rank_df["direction"], rank_df["avg_r5"])]

    fig_rank = go.Figure(go.Bar(
        x=rank_df["composite"],
        y=rank_df["display"],
        orientation="h",
        marker_color=colors,
        text=labels,
        textposition="outside",
        hovertemplate=(
            "<b>%{y}</b><br>"
            "综合得分: %{x:.3f}<br>"
            "<extra></extra>"
        ),
    ))
    fig_rank.add_vline(x=0, line_color="rgba(128,128,128,0.5)", line_width=1)

    # 警告区阴影：底部 20%
    threshold = float(rank_df["composite"].quantile(0.20))
    fig_rank.add_vrect(
        x0=rank_df["composite"].min() - 0.1, x1=threshold,
        fillcolor="rgba(255,200,0,0.08)", line_width=0,
        annotation_text="⚠️ 预警区", annotation_position="top left",
    )

    fig_rank.update_layout(
        xaxis_title="综合得分 (z-score)",
        yaxis_title=None,
        margin=dict(t=10, b=10, l=10, r=120),
        height=max(320, n_ok * 22),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(showgrid=True, gridcolor="rgba(128,128,128,0.15)"),
    )
    st.plotly_chart(fig_rank, width="stretch")

    # 预警提示
    warn_df = df[df["composite"] < threshold]
    if not warn_df.empty:
        st.warning(
            "⚠️ **动量预警**（综合得分后 20%）：  \n"
            + "  ".join(f"`{r['display']}`" for _, r in warn_df.iterrows())
        )

    st.divider()

    # ═══════════════════════════════════════════════════════════════════════════
    # ③ 个股详情卡片
    # ═══════════════════════════════════════════════════════════════════════════
    st.subheader("③ 个股详情")
    st.caption("点击展开查看 60 日价格走势 + 动量加速度。")

    # 按得分分组展示（强→中→弱）
    top_n    = max(1, n_ok // 3)
    strong   = df.head(top_n)
    moderate = df.iloc[top_n: top_n * 2]
    weak     = df.tail(n_ok - top_n * 2)

    for group_label, group_df in [("🟢 强势", strong), ("🟡 中性", moderate), ("🔴 弱势 / 预警", weak)]:
        if group_df.empty:
            continue
        st.markdown(f"**{group_label}**")
        cols = st.columns(min(4, len(group_df)))
        for col_idx, (_, row) in enumerate(group_df.iterrows()):
            with cols[col_idx % len(cols)]:
                accel_str = f"{row['accel']*100:+.3f}%/d" if pd.notna(row['accel']) else "N/A"
                badge = row['direction']
                delta_color = "normal" if row['accel'] > 0 else "inverse"
                st.metric(
                    label=f"{badge} {row['display']}",
                    value=f"${row['latest_close']:,.2f}",
                    delta=accel_str,
                    delta_color=delta_color,
                )

    st.divider()

    # 展开个股走势
    selected = st.selectbox(
        "🔍 查看个股走势详情",
        options=df["display"].tolist(),
        index=0,
    )

    sel_row = df[df["display"] == selected].iloc[0]
    sel_ticker = sel_row["ticker"]

    with st.spinner(f"加载 {selected} 历史数据…"):
        hist = fetch_histories([sel_ticker], period="3mo")
        close = hist.get(sel_ticker)

    if close is not None and len(close) >= 5:
        close_df = close.reset_index()
        close_df.columns = ["date", "close"]
        close_df["ma20"] = close_df["close"].rolling(20, min_periods=5).mean()
        # 5日动量加速度（滚动）
        close_df["r5"]  = close_df["close"].pct_change(5).rolling(1).mean()
        close_df["r20"] = close_df["close"].pct_change(20).rolling(1).mean()
        close_df["accel_roll"] = (close_df["r5"] - close_df["r20"]) * 100

        fig_detail = go.Figure()
        # 收盘价
        fig_detail.add_trace(go.Scatter(
            x=close_df["date"], y=close_df["close"],
            name="收盘价", line=dict(color="#4C9BE8", width=2),
        ))
        # MA20
        fig_detail.add_trace(go.Scatter(
            x=close_df["date"], y=close_df["ma20"],
            name="MA20", line=dict(color="#E8844C", width=1.5, dash="dot"),
        ))
        # 动量加速度（右轴）
        fig_detail.add_trace(go.Bar(
            x=close_df["date"], y=close_df["accel_roll"],
            name="动量加速度%", yaxis="y2",
            marker_color=close_df["accel_roll"].apply(
                lambda v: "rgba(38,166,65,0.5)" if v >= 0 else "rgba(215,58,74,0.5)"
            ),
        ))
        fig_detail.update_layout(
            title=f"{selected}  ({sel_ticker})  方向: {sel_row['direction']}",
            yaxis=dict(title="价格 (USD)"),
            yaxis2=dict(title="加速度 (5d−20d) %", overlaying="y", side="right",
                        showgrid=False),
            legend=dict(orientation="h", y=1.08),
            hovermode="x unified",
            margin=dict(t=60, b=20, l=20, r=60),
            height=420,
            plot_bgcolor="rgba(0,0,0,0)",
            xaxis=dict(showgrid=True, gridcolor="rgba(128,128,128,0.15)"),
        )
        st.plotly_chart(fig_detail, width="stretch")
    else:
        st.info(f"暂无 {selected} 的历史价格数据。")

    # 指标明细
    with st.expander(f"📋 {selected} 指标明细"):
        metrics = {
            "最新收盘价": f"${sel_row['latest_close']:,.2f}",
            "综合得分 (z)": f"{sel_row['composite']:.4f}",
            "动量加速度/日": f"{sel_row['accel']*100:+.4f}%",
            "趋势方向": sel_row["direction"],
            "5日均日收益": f"{sel_row['avg_r5']*100:+.3f}%",
            "10日均日收益": f"{sel_row['avg_r10']*100:+.3f}%" if 'avg_r10' in sel_row else "N/A",
            "20日均日收益": f"{sel_row['avg_r20']*100:+.3f}%",
            "30日年化波动": f"{sel_row['vol_30d']*100:.1f}%" if pd.notna(sel_row.get('vol_30d')) else "N/A",
            "10日最大回撤": f"{sel_row['drawdown_10d']*100:.2f}%" if pd.notna(sel_row.get('drawdown_10d')) else "N/A",
            "距MA20偏离": f"{sel_row['ma20_dev']*100:+.2f}%" if pd.notna(sel_row.get('ma20_dev')) else "N/A",
        }
        st.table(pd.DataFrame(list(metrics.items()), columns=["指标", "值"]))
