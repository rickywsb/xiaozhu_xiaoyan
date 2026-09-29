"""views/sections/13_Screener.py — 🔎 选股器：S&P 500 + Nasdaq-100 技术面筛选"""

import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config
from core import screener as S
from core import watchlist
from core.stock_chart import clickable_table, click_hint

st.caption(
    "股票池 = S&P 500 ∪ Nasdaq-100 ∪ 自定义篮子 ∪ 持仓/关注（排除杠杆/反向产品），约 540 只。"
    "**RS 评级**：IBD 式相对强度（3/6/9/12 月收益加权）在股票池内的百分位 1-99。"
    "象限来自🧭板块雷达。基于已收盘日线，仅供辅助研究，非投资建议。"
)


def _holdings() -> set[str]:
    pf = json.loads(config.PORTFOLIO_PATH.read_text(encoding="utf-8"))
    return {p["yf_ticker"].upper() for a in pf.get("accounts", []) for p in a.get("positions", [])}


@st.cache_data(show_spinner="🔎 正在扫描约 540 只股票…（首次约 15-60 秒）", ttl=21600)
def _scan(held: tuple[str, ...], watch: tuple[str, ...]) -> pd.DataFrame:
    return S.scan(S.build_universe(set(held), set(watch)))


held = _holdings()
watch = set(watchlist.load())

if "screener_on" not in st.session_state:
    st.session_state["screener_on"] = False
if not st.session_state["screener_on"]:
    if st.button("🔎 开始扫描", type="primary"):
        st.session_state["screener_on"] = True
        st.rerun()
    st.stop()

c_info, c_btn = st.columns([5, 1])
if c_btn.button("🔄 重新扫描", width="stretch"):
    _scan.clear()
    st.rerun()
df = _scan(tuple(sorted(held)), tuple(sorted(watch)))
if df.empty:
    st.error("扫描失败（行情或成分股列表获取失败），请稍后重试。")
    st.stop()
c_info.caption(f"已扫描 {len(df)} 只 ｜ 数据截至最近一个完整交易日 ｜ 结果缓存 6 小时")

# ─── 条件 ─────────────────────────────────────────────────────────────────────
preset_names = list(S.PRESETS) + ["🧰 自定义"]
preset = st.radio("策略", preset_names, horizontal=True)
if preset in S.PRESETS:
    st.caption(f"**{preset}**：{S.PRESETS[preset]['desc']}")
    if S.PRESETS[preset].get("caution"):
        st.warning(S.PRESETS[preset]["caution"])

with st.expander("⚙️ 筛选条件", expanded=preset == "🧰 自定义"):
    c1, c2, c3 = st.columns(3)
    min_rs = c1.slider("RS 评级 ≥", 0, 99, 0 if preset in S.PRESETS else 80)
    max_dist = c2.slider("距 52 周高不超过 %", 1, 100, 100 if preset in S.PRESETS else 15)
    min_dvol = c3.number_input("日均成交额 ≥（百万美元）", 0, 5000, 20, 10)
    c4, c5, c6, c7 = st.columns(4)
    need_ma50 = c4.toggle("站上 MA50", value=preset == "🧰 自定义")
    need_ma200 = c5.toggle("站上 MA200", value=False)
    need_brk = c6.toggle("突破 20 日高点", value=False)
    min_vr = c7.slider("量比 ≥", 0.0, 5.0, 0.0, 0.25)
    c8, c9, c10 = st.columns([2, 2, 1])
    sectors = c8.multiselect("板块", sorted(df["sector_name"].dropna().replace("", pd.NA).dropna().unique()))
    quads = c9.multiselect("所属板块象限", ["🟢 领先", "🔵 改善", "🟡 转弱", "🔴 落后"])
    excl = c10.toggle("排除已持仓", value=True)

res = S.apply_filters(
    df, preset if preset in S.PRESETS else None,
    min_rs=min_rs, max_dist_high=max_dist / 100, need_ma50=need_ma50, need_ma200=need_ma200,
    need_breakout=need_brk, min_vol_ratio=min_vr, min_dollar_vol=min_dvol * 1e6,
    sectors=sectors or None, quadrants=quads or None, exclude=held if excl else None,
)

st.markdown(f"**筛选结果：{len(res)} 只**")
if res.empty:
    st.info("没有符合条件的股票，试着放宽条件。")
    st.stop()

show = res.copy()
show["标记"] = ["💼持仓" if t in held else ("⭐关注" if t in watch else "") for t in show["ticker"]]
for c in ("ret_20d", "ret_63d", "dist_high52", "atr_pct", "trend_6_1"):
    show[c] = show[c] * 100
show["dollar_vol"] = show["dollar_vol"] / 1e6
show["breakout20"] = show["breakout20"].map({True: "✅", False: ""})
click_hint()
clickable_table(
    show[["ticker", "name", "标记", "sector_name", "quadrant", "rs", "ret_20d", "ret_63d", "trend_6_1",
          "dist_high52", "vol_ratio", "breakout20", "atr_pct", "dollar_vol", "last"]].rename(columns={
        "ticker": "代码", "name": "名称", "sector_name": "板块", "quadrant": "板块象限", "rs": "RS",
        "ret_20d": "20日%", "ret_63d": "3月%", "trend_6_1": "6-1月%", "dist_high52": "距52周高%",
        "vol_ratio": "量比", "breakout20": "20日突破", "atr_pct": "ATR%", "dollar_vol": "成交额$M",
        "last": "收盘价",
    }),
    column_config={
        "RS": st.column_config.ProgressColumn(format="%d", min_value=1, max_value=99),
        "20日%": st.column_config.NumberColumn(format="%+.1f%%"),
        "3月%": st.column_config.NumberColumn(format="%+.1f%%"),
        "6-1月%": st.column_config.NumberColumn(format="%+.1f%%"),
        "距52周高%": st.column_config.NumberColumn(format="%+.1f%%"),
        "量比": st.column_config.NumberColumn(format="%.2f", help="最近交易日成交量 / 前 50 日均量"),
        "ATR%": st.column_config.NumberColumn(format="%.1f%%", help="14 日平均真实波幅占股价，越大波动越大"),
        "成交额$M": st.column_config.NumberColumn(format="%.0f", help="近 50 日日均成交额中位数（百万美元）"),
        "收盘价": st.column_config.NumberColumn(format="%.2f"),
    },
    width="stretch", hide_index=True, height=min(760, 80 + len(show) * 35),
    tickers=list(show["ticker"]), names=list(show["name"]), key="scr_tbl",
)

c_sel, c_add = st.columns([4, 1])
cands = [t for t in res["ticker"] if t not in watch]
picks = c_sel.multiselect("加入 Watch List", cands, placeholder="选择要关注的股票")
if c_add.button("⭐ 加入", disabled=not picks, width="stretch"):
    watchlist.add(picks)
    st.success(f"已加入：{', '.join(picks)}")

st.caption("预设策略尚未经过历史检验；可以之后用📐信号成绩单的方法检验它们在你的股票池里是否有效。")
