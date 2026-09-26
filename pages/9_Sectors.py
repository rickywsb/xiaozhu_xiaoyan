"""pages/9_Sectors.py — 🧭 板块雷达：全市场板块强弱 / 轮动 / 宽度 / 成分股下钻"""

import json
import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
from core import sectors as sc
from core.daily_momentum import score_ticker_list
from core.github_storage import sync_to_github

st.title("🧭 板块雷达")
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


@st.cache_data(show_spinner="🔍 正在给成分股打分…", ttl=1800)
def _members_scored(key: str) -> pd.DataFrame:
    members = sc.members_of(key, _sp500())
    return score_ticker_list(list(members), labels=members) if members else pd.DataFrame()


def _load_json_list(path: Path, field: str) -> list[str]:
    if not path.exists():
        return []
    try:
        return [t.upper().strip() for t in json.loads(path.read_text(encoding="utf-8")).get(field, [])]
    except Exception:
        return []


def _holdings() -> set[str]:
    if not config.PORTFOLIO_PATH.exists():
        return set()
    pf = json.loads(config.PORTFOLIO_PATH.read_text(encoding="utf-8"))
    return {p["yf_ticker"].upper() for a in pf.get("accounts", []) for p in a.get("positions", [])}


def _pct(v):
    return v * 100 if v is not None and pd.notna(v) else None


# ─── 顶栏 ─────────────────────────────────────────────────────────────────────

_, c_btn = st.columns([5, 1])
if c_btn.button("🔄 刷新", type="primary", width="stretch"):
    _board.clear()
    _trails.clear()
    _members_scored.clear()
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

tab_rank, tab_rrg, tab_breadth, tab_drill = st.tabs(
    ["🏆 强弱榜", "🔄 轮动图", "🌊 市场宽度", "🔍 板块下钻"])

# ═══════════════════════════════════════════════════════════════════════════════
# 强弱榜
# ═══════════════════════════════════════════════════════════════════════════════
with tab_rank:
    groups = st.multiselect("分组", ["行业", "主题", "自定义"], default=["行业", "主题", "自定义"],
                            key="rank_groups")
    show = board[board["group"].isin(groups)].copy()

    def _chg(v):
        if pd.isna(v):
            return "🆕"
        v = int(v)
        return f"↑{v}" if v > 0 else (f"↓{-v}" if v < 0 else "→")

    show["5日排名变化"] = show["rank_chg"].apply(_chg)
    for c in ("ret_1d", "ret_5d", "ret_20d", "ret_60d", "rs_60d"):
        show[c] = show[c].apply(_pct)
    st.dataframe(
        show[["rank", "5日排名变化", "name", "key", "group", "composite", "direction",
              "ret_1d", "ret_5d", "ret_20d", "ret_60d", "rs_60d", "quadrant"]].rename(columns={
            "rank": "排名", "name": "板块", "key": "代码", "group": "分组", "composite": "综合",
            "direction": "方向", "ret_1d": "1日%", "ret_5d": "5日%", "ret_20d": "20日%",
            "ret_60d": "60日%", "rs_60d": "vs SPY 60日%", "quadrant": "象限",
        }),
        column_config={
            "综合": st.column_config.NumberColumn(format="%+.2f", help="板块间 z 分，越高越强"),
            "1日%": st.column_config.NumberColumn(format="%+.1f%%"),
            "5日%": st.column_config.NumberColumn(format="%+.1f%%"),
            "20日%": st.column_config.NumberColumn(format="%+.1f%%"),
            "60日%": st.column_config.NumberColumn(format="%+.1f%%"),
            "vs SPY 60日%": st.column_config.NumberColumn(format="%+.1f%%",
                                                         help="板块 60 日收益 − SPY 60 日收益"),
        },
        width="stretch", hide_index=True,
        height=min(860, 80 + len(show) * 35),
    )
    st.caption("5日排名变化：与 5 个交易日前（按历史价格倒推）相比；自定义篮子为成分等权日收益累乘，"
               "成分在 config.CUSTOM_BASKETS 中调整。")

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
            for c in ("above_ma20", "above_ma50", "new_high20", "ret_20d"):
                br[c] = br[c].apply(_pct)
            st.dataframe(
                br[["判读", "name", "key", "group", "n", "above_ma20", "above_ma50", "new_high20",
                    "ret_20d"]].rename(columns={
                    "name": "板块", "key": "代码", "group": "分组", "n": "成分数",
                    "above_ma20": "站上MA20", "above_ma50": "站上MA50",
                    "new_high20": "创20日新高", "ret_20d": "板块20日%",
                }),
                column_config={
                    "站上MA20": st.column_config.ProgressColumn(format="%.0f%%", min_value=0, max_value=100),
                    "站上MA50": st.column_config.ProgressColumn(format="%.0f%%", min_value=0, max_value=100),
                    "创20日新高": st.column_config.ProgressColumn(format="%.0f%%", min_value=0, max_value=100),
                    "板块20日%": st.column_config.NumberColumn(format="%+.1f%%"),
                },
                width="stretch", hide_index=True,
                height=min(860, 80 + len(br) * 35),
            )

# ═══════════════════════════════════════════════════════════════════════════════
# 板块下钻
# ═══════════════════════════════════════════════════════════════════════════════
with tab_drill:
    opts = {f"{r['rank']}. {r['name']} ({r['key']}) {r['quadrant']}": r["key"] for _, r in board.iterrows()}
    label = st.selectbox("选择板块", list(opts), index=0)
    key = opts[label]
    mem = _members_scored(key)
    if mem.empty:
        st.info("该板块暂无成分数据（S&P 500 成分表获取失败或成分为空）。")
    else:
        held = _holdings()
        watch = set(_load_json_list(config.WATCHLIST_PATH, "watchlist"))
        mem = mem.copy()
        mem["标记"] = [("💼持仓 " if t.upper() in held else "") + ("⭐关注" if t.upper() in watch else "")
                     for t in mem["ticker"]]
        for c in ("ret_5d", "ret_20d", "trend_6_1"):
            mem[c] = mem[c].apply(_pct)
        st.dataframe(
            mem[["rank", "ticker", "display", "标记", "composite", "heat", "trend_z",
                 "ret_5d", "ret_20d", "trend_6_1", "direction"]].rename(columns={
                "rank": "排名", "ticker": "代码", "display": "名称", "composite": "综合",
                "heat": "短期热度", "trend_z": "中期趋势", "ret_5d": "5日%", "ret_20d": "20日%",
                "trend_6_1": "6-1月%", "direction": "方向",
            }),
            column_config={
                "综合": st.column_config.NumberColumn(format="%+.2f", help="板块内相对排名"),
                "短期热度": st.column_config.NumberColumn(format="%+.2f"),
                "中期趋势": st.column_config.NumberColumn(format="%+.2f"),
                "5日%": st.column_config.NumberColumn(format="%+.1f%%"),
                "20日%": st.column_config.NumberColumn(format="%+.1f%%"),
                "6-1月%": st.column_config.NumberColumn(format="%+.1f%%"),
            },
            width="stretch", hide_index=True,
            height=min(700, 80 + len(mem) * 35),
        )

        # 一键加入关注列表
        candidates = [t for t in mem["ticker"] if t.upper() not in watch]
        c_sel, c_add = st.columns([4, 1])
        picks = c_sel.multiselect("加入 Watch List", candidates, key=f"add_wl_{key}",
                                  placeholder="选择要关注的成分股")
        if c_add.button("⭐ 加入", disabled=not picks, width="stretch"):
            new_list = sorted(watch | {p.upper() for p in picks})
            config.WATCHLIST_PATH.write_text(
                json.dumps({"watchlist": new_list, "last_modified": config.market_today().isoformat()},
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            sync_to_github(config.WATCHLIST_PATH, "data/watchlist.json", "feat: update watchlist via UI")
            st.success(f"已加入：{', '.join(picks)}")
