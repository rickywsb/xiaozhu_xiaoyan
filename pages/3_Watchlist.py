"""pages/3_Watchlist.py — 潜力 Watch List（Phase 4）"""

import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
from core.daily_momentum import (
    DEFAULT_DECAY, DEFAULT_WINDOW, PERIODS,
    score_ticker_list,
)
from core.github_storage import sync_to_github

# ─── 工具 ─────────────────────────────────────────────────────────────────────
SNAPSHOT_DIR = config.DATA_DIR / "snapshots"
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)


def _load_watchlist() -> list[str]:
    if config.WATCHLIST_PATH.exists():
        d = json.loads(config.WATCHLIST_PATH.read_text(encoding="utf-8"))
        return [t.upper().strip() for t in d.get("watchlist", [])]
    return []


def _save_watchlist(tickers: list[str]):
    config.WATCHLIST_PATH.write_text(
        json.dumps({"watchlist": tickers, "last_modified": date.today().isoformat()},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    sync_to_github(
        config.WATCHLIST_PATH, "data/watchlist.json",
        "feat: update watchlist via UI",
    )


def _load_portfolio_tickers() -> set[str]:
    if not config.PORTFOLIO_PATH.exists():
        return set()
    p = json.loads(config.PORTFOLIO_PATH.read_text(encoding="utf-8"))
    return {pos["yf_ticker"].upper()
            for acc in p.get("accounts", [])
            for pos in acc.get("positions", [])}


@st.cache_data(show_spinner="⚡ 正在扫描动量数据…（~15 秒）", ttl=1800)
def _cached_scan(wl_key: str, window: int, decay: float) -> pd.DataFrame:
    tickers = wl_key.split("|")
    return score_ticker_list(tickers, window=window, decay=decay)


def _wl_key(tickers: list[str]) -> str:
    return "|".join(sorted(set(tickers)))


def _list_snapshots() -> list[Path]:
    return sorted(SNAPSHOT_DIR.glob("*.csv"), reverse=True)


def _save_snapshot(df: pd.DataFrame):
    today = date.today().isoformat()
    path = SNAPSHOT_DIR / f"{today}.csv"
    cols = ["rank", "ticker", "display", "composite", "accel", "direction",
            "ret_5d", "ret_10d", "ret_20d", "ret_60d", "latest_close", "vol_30d"]
    save_cols = [c for c in cols if c in df.columns]
    df[save_cols].to_csv(path, index=False)
    return path


def _load_snapshot(path: Path) -> pd.DataFrame:
    return pd.read_csv(path)


def _diff_snapshots(curr: pd.DataFrame, prev: pd.DataFrame,
                    top_n: int = 25) -> pd.DataFrame:
    """
    Compare two snapshots by rank.
    Returns a merged DataFrame with rank_change column.
    """
    curr2 = curr[["ticker", "display", "composite", "direction"]].copy()
    curr2["rank_curr"] = range(1, len(curr2) + 1)
    prev2 = prev[["ticker", "composite"]].copy()
    prev2 = prev2.rename(columns={"composite": "composite_prev"})
    prev2["rank_prev"] = range(1, len(prev2) + 1)

    merged = curr2.merge(prev2[["ticker", "rank_prev", "composite_prev"]],
                         on="ticker", how="left")
    merged["rank_change"] = merged["rank_prev"] - merged["rank_curr"]  # positive = climbed
    merged["composite_chg"] = merged["composite"] - merged.get("composite_prev", merged["composite"])
    merged["in_top_n_curr"] = merged["rank_curr"] <= top_n
    merged["in_top_n_prev"] = merged["rank_prev"] <= top_n
    merged["is_new_entry"] = merged["in_top_n_curr"] & (~merged["in_top_n_prev"].fillna(False))
    merged["is_exit"] = (~merged["in_top_n_curr"]) & merged["in_top_n_prev"].fillna(False)

    # rank change arrow
    def _arrow(v):
        if pd.isna(v): return "🆕"
        if v > 5:  return f"↑↑ +{int(v)}"
        if v > 0:  return f"↑ +{int(v)}"
        if v < -5: return f"↓↓ {int(v)}"
        if v < 0:  return f"↓ {int(v)}"
        return "→ 0"
    merged["排名变化"] = merged["rank_change"].apply(_arrow)

    return merged


# ─── 颜色 ─────────────────────────────────────────────────────────────────────
_GREEN = "#26a641"
_RED   = "#d73a4a"
_GOLD  = "#E8A84C"
_BLUE  = "#4C9BE8"
_GRAY  = "#8b949e"


def _accel_color(v):
    if pd.isna(v): return _GRAY
    return _GREEN if v > 0 else _RED


# ═══════════════════════════════════════════════════════════════════════════════
# 页面主体
# ═══════════════════════════════════════════════════════════════════════════════
st.title("🔭 潜力 Watch List")

watchlist = _load_watchlist()
portfolio_tickers = _load_portfolio_tickers()

# 侧栏参数
with st.sidebar:
    st.subheader("⚙️ 参数")
    decay  = st.slider("衰减因子 decay",  0.88, 0.99, DEFAULT_DECAY, 0.01)
    window = st.slider("回看窗口 window", 20,   60,   DEFAULT_WINDOW, 5)
    top_n  = st.slider("Top N 告警阈值",  10,   50,   25, 5)
    st.caption(f"当前 Watch List: **{len(watchlist)}** 只")

tab_scan, tab_track, tab_edit = st.tabs(["📊 扫描排名", "📈 周度追踪", "✏️ 编辑列表"])


# ═══════════════════════════════════════════════════════════════════════════════
# Tab 1 — 扫描排名
# ═══════════════════════════════════════════════════════════════════════════════
with tab_scan:
    col_info, col_btn = st.columns([5, 1])
    with col_btn:
        if st.button("⚡ 运行扫描", type="primary", width="stretch"):
            st.cache_data.clear()
            st.rerun()

    if not watchlist:
        st.warning("Watch List 为空，请在「✏️ 编辑列表」Tab 添加 ticker。")
        st.stop()

    wk = _wl_key(watchlist)
    df = _cached_scan(wk, window, decay)

    if df.empty:
        st.warning("数据不足，请检查 ticker 是否正确，或点击「⚡ 运行扫描」重试。")
        st.stop()

    n_ok = len(df)
    st.caption(f"✅ {n_ok}/{len(watchlist)} 只有效 | decay={decay} window={window} | 缓存 30 分钟")

    # 颜色分类
    df["_type"] = df["ticker"].apply(
        lambda t: "已持仓" if t.upper() in portfolio_tickers else "Watch List"
    )

    # ─ Top 5 Metric 卡片 ─────────────────────────────────────────────────────
    st.subheader("🏆 Top 5")
    top5 = df.head(5)
    cols5 = st.columns(5)
    for i, (_, row) in enumerate(top5.iterrows()):
        with cols5[i]:
            badge = "🟡" if row["ticker"].upper() in portfolio_tickers else "🔵"
            st.metric(
                label=f"{badge} {row['display']}",
                value=f"${row['latest_close']:,.2f}",
                delta=f"{row['direction']} {row['accel']*100:+.3f}%/d",
                delta_color="normal" if row["accel"] > 0 else "inverse",
            )

    st.divider()

    # ─ 散点图：综合得分 × 动量加速度 ─────────────────────────────────────────
    st.subheader("① 综合得分 × 动量加速度 散点图")
    st.caption("🟡 = 已持仓，🔵 = Watch List。右上角 = 强势加速。")

    color_map = {"已持仓": _GOLD, "Watch List": _BLUE}
    fig_scatter = px.scatter(
        df,
        x="composite",
        y=df["accel"] * 100,
        text="display",
        color="_type",
        color_discrete_map=color_map,
        hover_data={"display": True, "composite": ":.3f",
                    "direction": True, "latest_close": True, "_type": False},
        labels={"composite": "综合得分 (z-score)", "y": "动量加速度 (%/日)"},
    )
    fig_scatter.add_hline(y=0, line_color="rgba(128,128,128,0.4)", line_width=1)
    fig_scatter.add_vline(x=0, line_color="rgba(128,128,128,0.4)", line_width=1)
    fig_scatter.update_traces(
        textposition="top center",
        marker=dict(size=10, line=dict(width=1, color="rgba(0,0,0,0.3)")),
    )
    fig_scatter.update_layout(
        height=500,
        legend_title_text="",
        margin=dict(t=20, b=20, l=20, r=20),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(showgrid=True, gridcolor="rgba(128,128,128,0.15)"),
        yaxis=dict(showgrid=True, gridcolor="rgba(128,128,128,0.15)"),
    )
    st.plotly_chart(fig_scatter, width="stretch")

    st.divider()

    # ─ 排名柱图 ──────────────────────────────────────────────────────────────
    st.subheader("② 综合动量得分排名")

    rank_df = df.sort_values("composite")
    bar_colors = [
        _GOLD if t.upper() in portfolio_tickers else _accel_color(a)
        for t, a in zip(rank_df["ticker"], rank_df["accel"])
    ]
    bar_labels = [f"{d} {r5*100:+.2f}%"
                  for d, r5 in zip(rank_df["direction"], rank_df["avg_r5"])]

    fig_rank = go.Figure(go.Bar(
        x=rank_df["composite"],
        y=rank_df["display"],
        orientation="h",
        marker_color=bar_colors,
        text=bar_labels,
        textposition="outside",
        hovertemplate="<b>%{y}</b><br>综合得分: %{x:.3f}<extra></extra>",
    ))
    fig_rank.add_vline(x=0, line_color="rgba(128,128,128,0.4)", line_width=1)

    threshold = float(rank_df["composite"].quantile(0.20))
    fig_rank.add_vrect(
        x0=float(rank_df["composite"].min()) - 0.1, x1=threshold,
        fillcolor="rgba(255,200,0,0.08)", line_width=0,
        annotation_text="⚠️ 预警区", annotation_position="top left",
    )
    fig_rank.update_layout(
        xaxis_title="综合得分 (z-score)",
        yaxis_title=None,
        margin=dict(t=10, b=10, l=10, r=120),
        height=max(350, n_ok * 22),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(showgrid=True, gridcolor="rgba(128,128,128,0.15)"),
    )
    st.plotly_chart(fig_rank, width="stretch")

    # 预警
    warn_df = df[df["composite"] < threshold]
    if not warn_df.empty:
        st.warning(
            "⚠️ **动量预警**（后 20%）：  \n"
            + "  ".join(f"`{r['display']}`" for _, r in warn_df.iterrows())
        )

    st.divider()

    # ─ 明细表 ────────────────────────────────────────────────────────────────
    st.subheader("③ 明细排名表")
    disp_cols = ["display", "ticker", "_type", "composite", "direction",
                 "avg_r5", "avg_r20", "vol_30d", "drawdown_10d", "latest_close"]
    disp_cols = [c for c in disp_cols if c in df.columns]
    show = df[disp_cols].copy()
    show.columns = [{"display":"名称","ticker":"Ticker","_type":"类型",
                     "composite":"综合分","direction":"方向",
                     "avg_r5":"5日均收益","avg_r20":"20日均收益",
                     "vol_30d":"30日波动","drawdown_10d":"10日回撤",
                     "latest_close":"最新价"}.get(c, c) for c in show.columns]
    # 格式化百分比列
    for col in ["5日均收益","20日均收益","30日波动","10日回撤"]:
        if col in show.columns:
            show[col] = show[col].map(lambda v: f"{v*100:+.2f}%" if pd.notna(v) else "N/A")
    st.dataframe(show, width="stretch", hide_index=True)


# ═══════════════════════════════════════════════════════════════════════════════
# Tab 2 — 周度追踪
# ═══════════════════════════════════════════════════════════════════════════════
with tab_track:
    st.subheader("📈 排名快照 & 周度变化")

    snap_files = _list_snapshots()

    col_save, col_spacer = st.columns([2, 5])
    with col_save:
        if st.button("💾 保存今日快照", width="stretch"):
            wk2 = _wl_key(watchlist)
            df2 = _cached_scan(wk2, window, decay)
            if df2.empty:
                st.warning("请先在「📊 扫描排名」运行扫描，再保存快照。")
            else:
                p = _save_snapshot(df2)
                st.success(f"✅ 快照已保存：{p.name}")
                snap_files = _list_snapshots()

    if len(snap_files) == 0:
        st.info("尚无历史快照，点击「💾 保存今日快照」生成第一份。")
    elif len(snap_files) == 1:
        st.info(f"只有一份快照（{snap_files[0].stem}），再次运行并保存后可比较变化。")
        curr_snap = _load_snapshot(snap_files[0])
        st.dataframe(curr_snap, width="stretch", hide_index=True)
    else:
        snap_names = [f.stem for f in snap_files]
        col_a, col_b = st.columns(2)
        with col_a:
            curr_name = st.selectbox("当前（较新）", snap_names, index=0, key="snap_curr")
        with col_b:
            prev_name = st.selectbox("对比（较旧）", snap_names, index=1, key="snap_prev")

        if curr_name == prev_name:
            st.warning("请选择两个不同的快照。")
        else:
            curr_snap = _load_snapshot(SNAPSHOT_DIR / f"{curr_name}.csv")
            prev_snap = _load_snapshot(SNAPSHOT_DIR / f"{prev_name}.csv")

            diff = _diff_snapshots(curr_snap, prev_snap, top_n=top_n)

            # Top N 进出告警
            entries = diff[diff["is_new_entry"] == True]
            exits   = diff[diff["is_exit"] == True]
            if not entries.empty:
                st.success(f"🆕 **新进 Top {top_n}**：" +
                           "  ".join(f"`{r['display']}`" for _, r in entries.iterrows()))
            if not exits.empty:
                st.error(f"📉 **跌出 Top {top_n}**：" +
                         "  ".join(f"`{r['display']}`" for _, r in exits.iterrows()))

            st.divider()

            # 排名变化柱图
            st.subheader(f"排名变化（{prev_name} → {curr_name}）")
            diff_valid = diff[diff["rank_change"].notna()].sort_values("rank_change")

            bar_clr = [_GREEN if v > 0 else (_GRAY if v == 0 else _RED)
                       for v in diff_valid["rank_change"]]
            fig_diff = go.Figure(go.Bar(
                x=diff_valid["rank_change"],
                y=diff_valid["display"],
                orientation="h",
                marker_color=bar_clr,
                text=diff_valid["排名变化"],
                textposition="outside",
            ))
            fig_diff.add_vline(x=0, line_color="rgba(128,128,128,0.4)", line_width=1)
            fig_diff.update_layout(
                xaxis_title="排名变化（正=上升）",
                yaxis_title=None,
                height=max(300, len(diff_valid) * 22),
                margin=dict(t=10, b=10, l=10, r=100),
                plot_bgcolor="rgba(0,0,0,0)",
                paper_bgcolor="rgba(0,0,0,0)",
            )
            st.plotly_chart(fig_diff, width="stretch")

            # 完整对比表
            with st.expander("📋 完整对比表"):
                show_diff = diff[["rank_curr", "display", "ticker", "排名变化",
                                  "composite", "direction"]].copy()
                show_diff.columns = ["当前排名", "名称", "Ticker", "排名变化", "综合分", "方向"]
                st.dataframe(show_diff, width="stretch", hide_index=True)

        # 历史快照列表
        with st.expander("🗂 历史快照文件"):
            for f in snap_files:
                st.caption(f.name)


# ═══════════════════════════════════════════════════════════════════════════════
# Tab 3 — 编辑列表
# ═══════════════════════════════════════════════════════════════════════════════
with tab_edit:
    st.subheader("✏️ 编辑 Watch List")
    st.caption("直接编辑下方表格，添加/删除 ticker，保存后点「⚡ 运行扫描」重新计算。")

    edit_df = pd.DataFrame({"Ticker": watchlist})
    edited = st.data_editor(
        edit_df,
        num_rows="dynamic",
        width="stretch",
        column_config={"Ticker": st.column_config.TextColumn("YF Ticker", width="large")},
        hide_index=True,
    )

    # 当前持仓一键加入
    st.caption("快速添加：将持仓 ticker 加入 Watch List")
    port_not_in_wl = sorted(portfolio_tickers - set(watchlist))
    if port_not_in_wl:
        if st.button(f"➕ 把全部持仓加入 Watch List（{len(port_not_in_wl)} 只）"):
            new_tickers = sorted(set(watchlist) | portfolio_tickers)
            _save_watchlist(new_tickers)
            st.cache_data.clear()
            st.success(f"✅ 已添加 {len(port_not_in_wl)} 只")
            st.rerun()
    else:
        st.caption("✅ 所有持仓已在 Watch List 中")

    st.divider()

    if st.button("💾 保存 Watch List", type="primary"):
        new_list = [t.strip().upper() for t in edited["Ticker"].dropna().tolist() if t.strip()]
        new_list = list(dict.fromkeys(new_list))  # 去重保序
        _save_watchlist(new_list)
        st.cache_data.clear()
        st.success(f"✅ 已保存 {len(new_list)} 只 ticker")
        st.rerun()
