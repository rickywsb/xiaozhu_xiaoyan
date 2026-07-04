"""pages/7_AI_Review.py — 🩺 AI 持仓健康诊断

消费我们已算好的量能评分 + 主力吸筹信号 + 板块权重 + 近期资讯，交给 LLM 做
一次组合级体检：集中度、赛道暴露、信号背离、风险与关注项。⚠️ AI 生成，非投资建议。
"""

import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
from core import llm, ai_review, news
from core.daily_momentum import score_holdings
from core import accumulation as accum
from core.price_updater import load_cache

st.title("🩺 AI 持仓健康诊断")
st.caption(
    "综合 量能评分 · 主力吸筹（量价代理）· 板块权重 · 近期资讯，由 OpenAI 生成组合体检。"
    "⚠️ AI 生成、基于历史量价数据的辅助研究，**非投资建议**。"
)


# ─── 缓存 ─────────────────────────────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def _load_portfolio() -> dict:
    return json.loads(config.PORTFOLIO_PATH.read_text(encoding="utf-8"))


@st.cache_data(show_spinner="⚡ 计算量能评分…", ttl=1800)
def _cached_momentum(pf_hash: str) -> pd.DataFrame:
    return score_holdings(_load_portfolio())


@st.cache_data(show_spinner="🏦 扫描吸筹信号…", ttl=1800)
def _cached_accum(pf_hash: str) -> pd.DataFrame:
    return accum.scan_holdings(_load_portfolio())


@st.cache_data(show_spinner="📡 抓取资讯标题…", ttl=1800)
def _cached_headlines() -> list[str]:
    out: list[str] = []
    for theme in ("存储", "光通信"):
        for it in news.fetch_theme(theme)[:8]:
            out.append(f"[{theme}] {it.title}")
    return out


@st.cache_data(show_spinner="🩺 AI 正在诊断…", ttl=1800)
def _cached_diagnosis(cache_key: str, holdings: list, sectors: dict, headlines: list, model: str) -> dict:
    return ai_review.portfolio_diagnosis(holdings, sector_weights=sectors,
                                         news_headlines=headlines, model=model)


def _pf_hash(pf: dict) -> str:
    return "|".join(sorted(
        p["yf_ticker"] for a in pf.get("accounts", []) for p in a.get("positions", [])
    ))


# ─── 数据组装 ─────────────────────────────────────────────────────────────────

def _build_holdings(pf: dict, mom: pd.DataFrame, acc: pd.DataFrame, usd_prices: dict):
    """把三路数据拼成 LLM 输入的 holdings 列表 + 板块权重。

    市值权重用 price_cache 里**已换算成 USD** 的价格（与持仓净值页一致），
    避免用动量模块的原生货币收盘价导致外币持仓（韩元/港元）被严重高估。
    """
    # 每个 ticker 的 shares / sector / display
    meta: dict[str, dict] = {}
    for a in pf.get("accounts", []):
        for p in a.get("positions", []):
            t = p["yf_ticker"].upper()
            if t == config.CASH_TICKER:
                continue
            meta[t] = {"display": p.get("display", t), "sector": p.get("sector", "其他"),
                       "shares": float(p.get("shares", 0) or 0)}

    mom_by = {r["ticker"].upper(): r for _, r in mom.iterrows()} if not mom.empty else {}
    acc_by = {r["代码"].upper(): r for _, r in acc.iterrows()} if not acc.empty else {}
    prices_by = {k.upper(): v for k, v in (usd_prices or {}).items()}

    # 板块市值权重（shares × USD 价）
    sector_val: dict[str, float] = {}
    total_val = 0.0
    for t, m in meta.items():
        px = float(prices_by[t]) if t in prices_by and prices_by[t] else 0.0
        val = px * m["shares"]
        m["_val"] = val
        total_val += val
        sector_val[m["sector"]] = sector_val.get(m["sector"], 0.0) + val
    sector_weights = ({s: round(v / total_val * 100, 1) for s, v in sector_val.items()}
                      if total_val > 0 else {})

    holdings = []
    for t, m in meta.items():
        mo = mom_by.get(t)
        ac = acc_by.get(t)
        weight = round(m["_val"] / total_val * 100, 1) if total_val > 0 else None
        holdings.append({
            "ticker": t,
            "display": m["display"],
            "sector": m["sector"],
            "weight_pct": weight,
            "momentum": round(float(mo["composite"]), 3) if mo is not None else None,
            "direction": mo["direction"] if mo is not None else None,
            "accel": round(float(mo["accel"]), 5) if mo is not None and pd.notna(mo["accel"]) else None,
            "vol_30d": round(float(mo["vol_30d"]), 3) if mo is not None and pd.notna(mo["vol_30d"]) else None,
            "drawdown_10d": round(float(mo["drawdown_10d"]), 3) if mo is not None and pd.notna(mo["drawdown_10d"]) else None,
            "accum_verdict": ac["判定"] if ac is not None else None,
            "accum_score": int(ac["评分"]) if ac is not None else None,
            "cmf": round(float(ac["CMF"]), 3) if ac is not None and pd.notna(ac["CMF"]) else None,
            "ud_vol": round(float(ac["涨跌量比"]), 2) if ac is not None and pd.notna(ac["涨跌量比"]) else None,
            "breakout": bool(ac["放量突破"] == "✅") if ac is not None else False,
        })
    # 按权重降序，方便阅读
    holdings.sort(key=lambda h: (h["weight_pct"] or 0), reverse=True)
    return holdings, sector_weights


# ─── 页面 ─────────────────────────────────────────────────────────────────────

pf = _load_portfolio()

if not llm.available():
    st.warning(
        "未检测到 OPENAI_API_KEY。请在 Streamlit Secrets（部署）、环境变量，"
        "或本地 openaitoken.txt（开发）中配置后刷新。"
    )
    st.stop()

model_label = st.radio(
    "模型档位", ["gpt-4o-mini（便宜·日常）", "gpt-4.1（更强·深度）"],
    horizontal=True, index=0,
)
model = llm.DEEP_MODEL if model_label.startswith("gpt-4.1") else llm.DEFAULT_MODEL
use_news = st.checkbox("结合近期资讯标题（存储 / 光通信）", value=True)

if st.button("🩺 生成持仓诊断", type="primary"):
    h = _pf_hash(pf)
    mom = _cached_momentum(h)
    acc = _cached_accum(h)
    cache = load_cache() or {}
    holdings, sectors = _build_holdings(pf, mom, acc, cache.get("prices", {}))
    headlines = _cached_headlines() if use_news else []

    if not holdings:
        st.error("未能组装持仓数据（量能/吸筹计算可能失败），请稍后重试。")
        st.stop()

    cache_key = f"{h}@{model}@news={use_news}@n={len(holdings)}"
    try:
        diag = _cached_diagnosis(cache_key, holdings, sectors, headlines, model)
    except llm.LLMError as e:
        st.error(f"诊断失败：{e}")
        st.stop()

    # ── 渲染 ──
    score = diag.get("health_score")
    c1, c2 = st.columns([1, 3])
    with c1:
        if isinstance(score, (int, float)):
            st.metric("组合健康分", f"{int(score)}/100")
    with c2:
        if diag.get("overall"):
            st.markdown(f"### {diag['overall']}")

    conc = diag.get("concentration", {})
    if conc:
        st.markdown("#### 📦 集中度")
        st.write(conc.get("assessment", ""))
        for f in conc.get("flags", []):
            st.markdown(f"- ⚠️ {f}")

    sec = diag.get("sector_exposure", {})
    if sec:
        st.markdown("#### 🧭 赛道暴露")
        st.write(sec.get("assessment", ""))
    if sectors:
        st.caption("板块权重(%): " + " · ".join(f"{k} {v}%" for k, v in sorted(sectors.items(), key=lambda x: -x[1])))

    divs = diag.get("divergences", [])
    if divs:
        st.markdown("#### 🔀 信号背离")
        for d in divs:
            st.markdown(f"- **`{d.get('ticker','')}`**：{d.get('issue','')}")

    colA, colB = st.columns(2)
    with colA:
        if diag.get("strengths"):
            st.markdown("#### ✅ 亮点")
            for s in diag["strengths"]:
                st.markdown(f"- {s}")
    with colB:
        if diag.get("risks"):
            st.markdown("#### ⚠️ 风险")
            for r in diag["risks"]:
                st.markdown(f"- {r}")

    if diag.get("watch"):
        st.markdown("#### 👀 重点关注")
        for w in diag["watch"]:
            st.markdown(f"- {w}")

    usage = diag.get("_usage", {})
    if usage:
        st.caption(
            f"🤖 {diag.get('_model','')} · {usage.get('total_tokens',0)} tokens "
            f"· ~${diag.get('_cost_usd',0):.4f}　|　AI 生成，非投资建议。"
        )

    with st.expander("🔎 查看喂给 AI 的原始数据"):
        st.json({"holdings": holdings, "sector_weights": sectors,
                 "headlines": headlines})
