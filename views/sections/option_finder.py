"""views/sections/option_finder.py — 期权 · 机会扫描：卖 Put / 买 Call（参考 PutFinder）"""

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core import option_screener as OS
from core import rating as RT
from core import posture, sectors
from core.ui import pz_table, pct, _clean

st.caption("扫描美股期权链（CBOE 延迟行情），找风险收益比好的**卖 Put（现金担保）**与**买 Call**机会。"
           "胜率为 Black-Scholes 模型概率，不是历史胜率；期权历史价格不可得，本页未经回测。仅供研究，非投资建议。")


@st.cache_data(show_spinner=False, ttl=3600)
def _ratings() -> tuple[dict, list[str], float | None]:
    df, _ = RT.load_latest()
    if df.empty:
        return {}, [], None
    top = df[(df["score"] >= RT.TOP_TIER)]["ticker"].tolist()
    return ({r.ticker: {"score": r.score, "action": r.action} for r in df.itertuples()},
            OS.us_only(top)[:60], df.attrs.get("breadth50"))


@st.cache_data(show_spinner="🔍 正在抓取期权链与财报日…（约 10–40 秒）", ttl=1200)
def _scan(universe: tuple[str, ...]) -> tuple[dict, dict]:
    data = OS.fetch_all(list(universe))
    return data, OS.realized_vol(list(data))


@st.cache_data(show_spinner=False, ttl=1800)
def _posture(b50) -> dict:
    return posture.compute(b50, sectors.sector_board())


ratings, top_names, b50 = _ratings()
held, watch, names = RT._portfolio_sets()

# ─── 扫描范围 ─────────────────────────────────────────────────────────────────
c1, c2, c3 = st.columns([2, 2, 1])
src = c1.multiselect("扫描范围", ["持仓", "关注", f"全市场评分 ≥{RT.TOP_TIER}"], default=["持仓", "关注"],
                     help=f"全市场评分 ≥{RT.TOP_TIER} = 强势筛选中的美股（最多 60 只）")
extra = c2.text_input("另外加入代码（逗号分隔）", placeholder="如 TSLA, CRWD")
only_stock = c3.toggle("只看个股", value=True, help="排除 ETF（SOXX、QQQ 等）")
uni = set()
if "持仓" in src:
    uni |= held
if "关注" in src:
    uni |= watch
if any(x.startswith("全市场") for x in src):
    uni |= set(top_names)
uni |= {x.strip().upper() for x in extra.split(",") if x.strip()}
universe = tuple(OS.us_only(uni))
if not universe:
    st.info("请选择扫描范围。")
    st.stop()

data, rv = _scan(universe)
if not data:
    st.error("期权链获取失败（CBOE），请稍后重试。")
    st.stop()
if only_stock:
    data = {t: v for t, v in data.items() if v.get("earnings")}     # ETF 没有财报日

strat = st.segmented_control(" ", ["卖 Put（现金担保）", "买 Call"], default="卖 Put（现金担保）",
                             key="of_strat", label_visibility="collapsed") or "卖 Put（现金担保）"
is_put = strat.startswith("卖")
dflt = OS.PUT_DEFAULTS if is_put else OS.CALL_DEFAULTS

with st.expander("⚙️ 筛选条件", expanded=False):
    f1, f2, f3, f4 = st.columns(4)
    dte = f1.slider("剩余天数", 7, 400, dflt["dte"], key=f"of_dte_{is_put}")
    dl = f2.slider("|delta| 范围", 0.05, 0.95, dflt["delta"], 0.05, key=f"of_delta_{is_put}",
                   help="卖 Put：|delta| ≈ 被行权概率；买 Call：delta 越高越接近持有正股")
    min_oi = f3.number_input("最少未平仓量", 0, 10000, dflt["min_oi"], 50, key=f"of_oi_{is_put}")
    max_sp = f4.slider("最大买卖价差（占中间价）", 0.02, 0.40, dflt["max_spread"], 0.01, key=f"of_sp_{is_put}")
    g1, g2, g3 = st.columns(3)
    skip_earn = g1.toggle("排除到期前有财报的", value=is_put, key=f"of_earn_{is_put}",
                          help="财报前后股价跳空，卖 Put 容易被击穿；买 Call 则面临财报后 IV 骤降")
    min_q = g2.slider("股票评分 ≥", 0, 99, 60 if is_put else 70, key=f"of_q_{is_put}",
                      help="卖 Put 前提是愿意在行权价接股：只看质量好的股票")
    show_all = g3.toggle("显示全部合约（否则每只股票只列最好的一张）", value=False, key=f"of_all_{is_put}")

params = {"dte": dte, "delta": dl, "min_oi": min_oi, "max_spread": max_sp}
df = (OS.screen_puts if is_put else OS.screen_calls)(data, ratings, rv, params)
if not df.empty:
    if skip_earn:
        df = df[~df["before_earn"]]
    df = df[df["quality"].fillna(0) >= min_q]

# ─── 时机 ─────────────────────────────────────────────────────────────────────
allp = OS.screen_puts(data, ratings, rv)
allc = OS.screen_calls(data, ratings, rv)
tm = OS.timing(_posture(b50), allp, allc)
k1, k2 = st.columns(2)
k1.metric("现在适合卖 Put 吗", tm["sell_put"], tm["sell_put_why"], delta_color="off", delta_arrow="off",
          help="市场不在下降趋势，且期权偏贵（波动率溢价高）时卖 Put 更有利")
k2.metric("现在适合买 Call 吗", tm["buy_call"], tm["buy_call_why"], delta_color="off", delta_arrow="off",
          help="市场上升趋势，且期权偏便宜（IV 不高于实际波动）时买 Call 更有利")

if df.empty:
    st.info("没有符合条件的合约，试着放宽筛选条件。")
    st.stop()
res = df if show_all else OS.best_per_ticker(df)
res = res.sort_values("score", ascending=False).head(80)
st.markdown(f"**{len(res)} 个机会**（扫描 {len(data)} 只标的 · 合约 {len(df)} 张）")


def _contract_label(r) -> str:
    return f"{pd.Timestamp(r['expiry']):%m/%d} {r['strike']:g}{'P' if is_put else 'C'}"


def _earn_txt(r) -> str:
    if not r["earnings"]:
        return "—"
    return f"⚠ {r['earnings'][5:]}" if r["before_earn"] else r["earnings"][5:]


def _sub(r) -> str:
    q = f"评分 {int(r['quality'])}" if pd.notna(r["quality"]) else "未评分"
    tag = "持仓" if r["ticker"] in held else ("关注" if r["ticker"] in watch else "")
    earn = f"⚠财报 {r['earnings'][5:]}" if r["before_earn"] else ""
    return " · ".join(x for x in [f"现价 {r['price']:,.2f}", q, r["action"], tag, earn] if x)


rows = []
for _, r in res.iterrows():
    base = {"ticker": r["ticker"], "name": names.get(r["ticker"], r["ticker"]), "sub": _sub(r),
            "contract_txt": _contract_label(r), "dte": int(r["dte"]), "premium": _clean(r["premium"]),
            "pop": pct(r["pop"]), "earn": _earn_txt(r), "score": int(round(r["score"])),
            "price_txt": f"{r['price']:,.2f}"}
    if is_put:
        base.update({"ann": pct(r["ann_yield"]), "be": _clean(r["breakeven"]), "buf": pct(r["buffer"]),
                     "bufs": _clean(r["buffer_sigma"]), "assign": pct(r["assign_prob"]), "vrp": _clean(r["vrp"]),
                     "collateral": _clean(r["collateral"])})
    else:
        base.update({"bem": pct(r["be_move"]), "bes": _clean(r["be_sigma"]), "lev": _clean(r["leverage"]),
                     "theta": pct(r["theta_day"]), "ivrv": _clean(r["iv_rv"]), "rr": _clean(r["rr_1sigma"]),
                     "tv": pct(r["time_value"])})
    rows.append(base)

common_head = [
    {"key": "ticker", "label": "股票", "kind": "stock", "width": "minmax(136px,1.4fr)", "sortable": True},
    {"key": "contract_txt", "label": "合约", "kind": "text", "width": "88px", "sortKey": "dte", "sortable": True},
    {"key": "premium", "label": "权利金", "kind": "num", "decimals": 0, "prefix": "$", "width": "72px",
     "sortable": True, "align": "right"},
]
tail = [
    {"key": "pop", "label": "模型胜率", "kind": "bar", "suffix": "%", "width": "88px", "sortable": True},
    {"key": "score", "label": "综合分", "kind": "score", "width": "56px", "sortable": True},
]
if is_put:
    cols = common_head + [
        {"key": "ann", "label": "年化", "kind": "num", "decimals": 0, "suffix": "%", "width": "56px",
         "sortable": True, "align": "right"},
        {"key": "be", "label": "盈亏平衡", "kind": "num", "decimals": 2, "width": "80px", "align": "right"},
        {"key": "buf", "label": "下跌缓冲", "kind": "num", "decimals": 1, "suffix": "%", "width": "70px",
         "sortable": True, "align": "right"},
        {"key": "bufs", "label": "缓冲 σ", "kind": "num", "decimals": 2, "width": "56px", "sortable": True,
         "align": "right"},
        {"key": "assign", "label": "被行权率", "kind": "num", "decimals": 0, "suffix": "%", "width": "66px",
         "sortable": True, "align": "right"},
        {"key": "vrp", "label": "IV/实际", "kind": "num", "decimals": 2, "suffix": "×", "width": "62px",
         "sortable": True, "align": "right"},
    ] + tail
else:
    cols = common_head + [
        {"key": "bem", "label": "回本需涨", "kind": "num", "decimals": 1, "suffix": "%", "width": "70px",
         "sortable": True, "align": "right"},
        {"key": "bes", "label": "需涨 σ", "kind": "num", "decimals": 2, "width": "56px", "sortable": True,
         "align": "right"},
        {"key": "lev", "label": "杠杆", "kind": "num", "decimals": 1, "suffix": "×", "width": "52px",
         "sortable": True, "align": "right"},
        {"key": "theta", "label": "日损耗", "kind": "num", "decimals": 2, "suffix": "%", "width": "60px",
         "sortable": True, "align": "right"},
        {"key": "ivrv", "label": "IV/实际", "kind": "num", "decimals": 2, "suffix": "×", "width": "62px",
         "sortable": True, "align": "right"},
        {"key": "rr", "label": "+1σ 回报", "kind": "num", "decimals": 2, "suffix": "×", "sign": True,
         "color": True, "width": "72px", "sortable": True, "align": "right"},
    ] + tail
pz_table(rows, cols, key=f"of_tbl_{is_put}", sort="score", min_width=940, empty="没有符合条件的合约")

with st.expander("📖 指标怎么读 & 局限"):
    if is_put:
        st.markdown(
            "- **卖 Put 的逻辑**：收取权利金，承诺在行权价买入股票——只卖**愿意在这个价格持有**的好公司。\n"
            "- **年化** = 权利金 ÷ 行权价 × 365 ÷ 剩余天数（现金担保按行权价占用资金）。\n"
            "- **盈亏平衡** = 行权价 − 权利金；**下跌缓冲** = 股价要跌多少才开始亏；"
            "**缓冲 σ** = 缓冲相当于到期前几个标准差的波动（同样 10% 的缓冲，对高波动股更薄）。\n"
            "- **模型胜率** = 到期时股价高于盈亏平衡点的风险中性概率；**被行权率** ≈ |delta|。\n"
            "- **IV/实际** = 30 日隐含波动 ÷ 20 日实际波动：>1 期权偏贵，卖方收得多（波动率风险溢价）。\n"
            "- **综合分** = 股票质量^0.4 × 收益与缓冲的平衡^0.6 × IV 溢价调整 × 财报闸门——"
            "**看缓冲和质量，不追收益率**；到期前有财报的打五折。\n"
            "- 局限：模型胜率不是历史胜率；急跌时可能被行权并继续下跌（最大亏损 = 行权价 − 权利金）；"
            "延迟行情，下单前以券商实时报价为准。")
    else:
        st.markdown(
            "- **买 Call 的逻辑**：用较少资金获得上涨敞口，最大亏损 = 权利金。\n"
            "- **回本需涨** = （行权价 + 权利金）÷ 现价 − 1；**需涨 σ** = 这个涨幅相当于几个标准差（越小越容易回本）。\n"
            "- **杠杆** = delta × 现价 ÷ 权利金（标的涨 1%，期权约涨几 %）；**日损耗** = 每天时间价值流失占权利金比例。\n"
            "- **IV/实际** < 1 表示期权相对便宜；**+1σ 回报** = 若到期时股价上涨一个标准差，赚回权利金的几倍。\n"
            "- **综合分** = 股票质量^0.5（评分 ≥90 是验证过的强势区间）× 便宜程度 × 回本难度 × 时间损耗 × 财报闸门。\n"
            "- 局限：期限越长回本越容易，但占用权利金越多；财报后 IV 常骤降（IV crush）；延迟行情，下单前以实时报价为准。")
