"""views/sections/option_flow.py — 期权 · 情绪与异动：P/C 比、IV、偏斜、异动合约（持仓 + 关注），每日快照待检验"""

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core import option_flow as F
from core import rating as RT
from core import tracker as tk
from core.stock_chart import click_hint
from core.ui import pz_table, pct, _clean

hist_days = sorted(F.load_history().get("days", {}))
st.caption(
    "持仓与关注里的美股，读 CBOE 延迟期权链（与机会扫描同一数据源）。"
    f"**这些指标尚未经过历史检验**：每天自动保存快照（已积累 **{len(hist_days)}** 个交易日），"
    "满约 60 个交易日后用信号实验室同样的方法评级，评级前只作参考、不进主预警。"
    "成交量不区分买方还是卖方主动——「看涨异动」也可能是有人在卖 call。仅供研究，非投资建议。")
click_hint()


@st.cache_data(show_spinner="📡 正在读取期权链…（约 10–20 秒）", ttl=1200)
def _scan(universe: tuple[str, ...]) -> pd.DataFrame:
    return F.scan(list(universe))


held, watch, names = RT._portfolio_sets()
universe = tuple(sorted(held | watch))
c1, c2 = st.columns([5, 1])
if c2.button("🔄 重新读取", width="stretch"):
    _scan.clear()
df = _scan(universe)
if df.empty:
    st.error("期权链获取失败（CBOE 可能临时限流），请稍后重试。")
    st.stop()
status = tk.us_market_status()
F.set_latest(df)
F.save_snapshot(df, status)
c1.caption(f"{len(df)} 只标的 ｜ 读取于 {df.attrs.get('time', '')[11:16]} ｜ "
           + ("盘中成交量仍在累积，收盘后的快照最完整" if status == "交易中" else
              "非交易时段：成交量为最近一个交易日" if status != "已收盘" else "已收盘：今日快照已保存"))

# 与自身历史比较（快照满 20 天后才有）
pc_pct = F.own_percentile("pc_vol", df.set_index("ticker")["pc_vol"])
iv_pct = F.own_percentile("iv30", df.set_index("ticker")["iv30"])

rows = []
for r in df.to_dict("records"):
    t = r["ticker"]
    top = r["unusual"][0] if r["unusual"] else None
    top_txt = (f"{top['type'].upper()} {top['strike']:g} {top['expiry'][5:]}（{top['dte']}天）"
               f" ${top['premium'] / 1e6:.1f}M · 量/仓 {top['vol_oi']:.1f}×" if top and top.get("vol_oi")
               else (f"{top['type'].upper()} {top['strike']:g} {top['expiry'][5:]} ${top['premium'] / 1e6:.1f}M" if top else ""))
    rows.append({"ticker": t, "name": names.get(t, t), "sub": "持仓" if t in held else "关注",
                 "pc_vol": _clean(r["pc_vol"]), "pc_pct": pct(pc_pct.get(t)),
                 "pc_oi": _clean(r["pc_oi"]), "iv30": pct(r["iv30"]), "iv_pct": pct(iv_pct.get(t)),
                 "skew": pct(r["skew"]), "activity": _clean(r["activity"]),
                 "n_unusual": r["n_unusual"], "net": pct(r["net_unusual"]), "tilt": r["tilt"], "top": top_txt})
cols = [
    {"key": "ticker", "label": "标的", "kind": "stock", "width": "minmax(110px,1fr)", "sortable": True},
    {"key": "pc_vol", "label": "P/C 量比", "kind": "num", "decimals": 2, "width": "74px", "align": "right", "sortable": True},
    {"key": "pc_pct", "label": "自身分位", "kind": "num", "decimals": 0, "suffix": "%", "width": "70px",
     "align": "right", "sortable": True},
    {"key": "pc_oi", "label": "P/C 持仓比", "kind": "num", "decimals": 2, "width": "80px", "align": "right", "sortable": True},
    {"key": "iv30", "label": "IV30", "kind": "num", "decimals": 1, "suffix": "%", "width": "66px", "align": "right",
     "sortable": True},
    {"key": "skew", "label": "偏斜", "kind": "num", "decimals": 1, "suffix": "%", "sign": True, "width": "66px",
     "align": "right", "sortable": True},
    {"key": "activity", "label": "成交/持仓", "kind": "num", "decimals": 2, "width": "76px", "align": "right",
     "sortable": True},
    {"key": "n_unusual", "label": "异动数", "kind": "num", "decimals": 0, "width": "60px", "align": "right", "sortable": True},
    {"key": "net", "label": "异动净额", "kind": "num", "decimals": 1, "suffix": "%", "sign": True, "color": True,
     "width": "76px", "align": "right", "sortable": True},
    {"key": "tilt", "label": "倾向", "kind": "text", "colors": {"看涨异动": "#1F6B3E", "看跌异动": "#8E2E22"}, "width": "76px"},
    {"key": "top", "label": "最大异动合约", "kind": "small", "width": "minmax(200px,2fr)"},
]
pz_table(rows, cols, key="flow_tbl", sort="n_unusual", min_width=1080)
st.caption("P/C 量比：put 成交 ÷ call 成交（>1 偏谨慎）；自身分位：在该标的自己的历史快照里的位置（满 20 天后显示）。"
           "偏斜：约 30 天、|Δ|≈0.25 的 put IV − call IV（越大 = 下跌保护越贵）。"
           "异动净额：（call 异动权利金 − put 异动权利金）÷ 当日总权利金。")

# ─── 异动合约明细 ─────────────────────────────────────────────────────────────
allu = [u for r in df["unusual"] for u in r]
with st.expander(f"🔎 异动合约明细（{len(allu)} 张）", expanded=bool(allu)):
    st.caption("条件：剩余 ≥2 天到期、当日成交 ≥ 500 张、成交 ÷ 未平仓 ≥ 2（多为新开仓）、"
               "权利金 ≥ max($10 万, 该标的当日总权利金 1%)。未平仓为上一交易日数据。")
    pz_table([{"ticker": u["ticker"], "name": names.get(u["ticker"], u["ticker"]),
               "sub": f"{u['type'].upper()} {u['strike']:g} · {u['expiry']}",
               "type": "看涨" if u["type"] == "call" else "看跌", "dte": u["dte"], "volume": u["volume"],
               "oi": u["oi"], "vol_oi": _clean(u["vol_oi"]), "premium": u["premium"] / 1e6,
               "iv": pct(u["iv"]), "otm": pct(u["otm"])} for u in sorted(allu, key=lambda u: -u["premium"])],
             [{"key": "ticker", "label": "合约", "kind": "stock", "width": "minmax(150px,1.3fr)", "sortable": True},
              {"key": "type", "label": "", "kind": "pill", "width": "56px"},
              {"key": "dte", "label": "天数", "kind": "num", "decimals": 0, "width": "56px", "align": "right", "sortable": True},
              {"key": "volume", "label": "成交", "kind": "num", "decimals": 0, "width": "76px", "align": "right", "sortable": True},
              {"key": "oi", "label": "未平仓", "kind": "num", "decimals": 0, "width": "76px", "align": "right"},
              {"key": "vol_oi", "label": "量/仓", "kind": "num", "decimals": 1, "suffix": "×", "width": "64px",
               "align": "right", "sortable": True},
              {"key": "premium", "label": "权利金", "kind": "num", "decimals": 2, "prefix": "$", "suffix": "M",
               "width": "84px", "align": "right", "sortable": True},
              {"key": "iv", "label": "IV", "kind": "num", "decimals": 1, "suffix": "%", "width": "64px", "align": "right"},
              {"key": "otm", "label": "行权价距现价", "kind": "num", "decimals": 1, "suffix": "%", "sign": True,
               "width": "96px", "align": "right"}],
             key="flow_unusual", sort="premium", empty="今天没有符合条件的异动合约", min_width=860)
