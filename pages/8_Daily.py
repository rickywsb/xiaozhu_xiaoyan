"""pages/8_Daily.py — 📅 每日日报：一键更新价格 + AI 综合汇总

一次点击完成：更新最新持仓价格 → 汇总 持仓结构 / 量能 / 期权 / 资讯 →
交给 LLM 生成一份中文晨报。⚠️ AI 生成，非投资建议。
"""

import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
from core import llm, ai_review, news
from core import options_review as R
from core import accumulation as accum
from core.daily_momentum import score_holdings
from core.price_updater import load_cache, update_all_prices
from core.github_storage import sync_to_github
from core.value_history import append_value, HISTORY_PATH

st.title("📅 每日日报")
st.caption(
    "一键更新最新价格，AI 综合 **持仓结构 · 量能 · 期权 · 资讯**，生成当日晨报。"
    "⚠️ AI 生成、基于历史量价与公开资讯的辅助研究，**非投资建议**。"
)


# ─── 工具 ─────────────────────────────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def _load_portfolio() -> dict:
    return json.loads(config.PORTFOLIO_PATH.read_text(encoding="utf-8"))


def _positions(pf: dict):
    for a in pf.get("accounts", []):
        for p in a.get("positions", []):
            yield p


def _total_nav(pf: dict, cache: dict) -> float:
    """总净值(USD)：股票 = USD 价 × 持股；期权 = 缓存 value。"""
    prices = {k.upper(): v for k, v in (cache.get("prices") or {}).items()}
    nav = 0.0
    for p in _positions(pf):
        t = p["yf_ticker"].upper()
        px = prices.get(t)
        if px:
            nav += float(px) * float(p.get("shares", 0) or 0)
    for od in (cache.get("options") or {}).values():
        if od.get("value"):
            nav += float(od["value"])
    return nav


def _portfolio_block(pf: dict, cache: dict, total_nav: float) -> dict:
    """持仓结构：板块权重 + 最大持仓。"""
    prices = {k.upper(): v for k, v in (cache.get("prices") or {}).items()}
    sector_val: dict[str, float] = {}
    holding_val: list[tuple[str, float]] = []
    for p in _positions(pf):
        t = p["yf_ticker"].upper()
        if t == config.CASH_TICKER:
            sector = "现金"
        else:
            sector = p.get("sector", "其他")
        px = prices.get(t)
        val = float(px) * float(p.get("shares", 0) or 0) if px else 0.0
        sector_val[sector] = sector_val.get(sector, 0.0) + val
        holding_val.append((p.get("display", t), val))
    # 期权归入「期权」板块
    opt_val = sum(float(o["value"]) for o in (cache.get("options") or {}).values() if o.get("value"))
    if opt_val:
        sector_val["期权"] = sector_val.get("期权", 0.0) + opt_val
    weights = ({s: round(v / total_nav * 100, 1) for s, v in sector_val.items()}
               if total_nav > 0 else {})
    top = sorted(holding_val, key=lambda x: x[1], reverse=True)[:8]
    top_pct = [{"名称": n, "占比": round(v / total_nav * 100, 1) if total_nav > 0 else None}
               for n, v in top]
    return {
        "总净值USD": round(total_nav, 0),
        "持仓数": sum(1 for _ in _positions(pf)),
        "板块权重(%)": dict(sorted(weights.items(), key=lambda x: -x[1])),
        "最大持仓": top_pct,
    }


def _momentum_block(pf: dict, acc: pd.DataFrame) -> dict:
    """量能：领涨/领跌 + 吸筹亮点/派发预警。"""
    mom = score_holdings(pf)
    leaders, laggards = [], []
    if not mom.empty:
        for _, r in mom.head(5).iterrows():
            leaders.append({"ticker": r["ticker"], "direction": r["direction"],
                            "momentum": round(float(r["composite"]), 2)})
        for _, r in mom.tail(5).iloc[::-1].iterrows():
            laggards.append({"ticker": r["ticker"], "direction": r["direction"],
                             "momentum": round(float(r["composite"]), 2)})
    accum_hi, distrib = [], []
    if not acc.empty:
        for _, r in acc.iterrows():
            sc = int(r["评分"]) if pd.notna(r["评分"]) else 0
            if sc >= 3:
                accum_hi.append({"ticker": r["代码"], "评分": sc, "判定": r["判定"]})
            elif sc <= -3:
                distrib.append({"ticker": r["代码"], "评分": sc, "判定": r["判定"]})
    return {"领涨": leaders, "领跌": laggards,
            "吸筹亮点": accum_hi[:6], "派发预警": distrib[:6]}


def _options_block(cache: dict) -> dict:
    options = cache.get("options") or {}
    if not options:
        return {}
    total_value = net_delta_notional = total_theta = total_vega = 0.0
    for od in options.values():
        ct = od.get("contracts", 1) or 1
        mult = 100 * ct
        if od.get("value"):
            total_value += od["value"]
        if od.get("delta") is not None and od.get("underlying_price"):
            net_delta_notional += od["delta"] * od["underlying_price"] * mult
        if od.get("theta") is not None:
            total_theta += od["theta"] * mult
        if od.get("vega") is not None:
            total_vega += od["vega"] * mult
    # 时间衰减最快的前 3
    decay = []
    for c, od in options.items():
        dm = R.decay_metrics(od)
        decay.append({"期权": od.get("display", c), "剩余天数": dm["dte"],
                      "Theta每日": round(dm["theta_day_usd"], 1) if dm["theta_day_usd"] is not None else None})
    decay = sorted([d for d in decay if d["Theta每日"] is not None],
                   key=lambda x: x["Theta每日"])[:3]
    # 日间归因（需两天快照，可能为空）
    attr = R.portfolio_attribution(cache)
    attribution = {}
    if attr["totals"] and attr["prev_date"] is not None:
        t = attr["totals"]
        attribution = {
            "对比区间": f"{attr['prev_date']} → {attr['curr_date']}",
            "净变化": round(t["actual"], 0),
            "标的贡献": round(t["delta_pnl"] + t["gamma_pnl"], 0),
            "时间衰减": round(t["theta_pnl"], 1),
            "IV变化": round(t["vega_pnl"], 0),
        }
    return {
        "期权总市值": round(total_value, 0),
        "净Delta敞口USD": round(net_delta_notional, 0),
        "每日Theta损耗USD": round(total_theta, 1),
        "Vega敞口USD每1%IV": round(total_vega, 0),
        "时间衰减最快": decay,
        "归因": attribution,
    }


def _news_block() -> list[dict]:
    out = []
    for theme in ("存储", "光通信", "半导体大盘"):
        for it in news.fetch_theme(theme)[:5]:
            out.append({"theme": theme, "title": it.title, "source": it.source})
    return out


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

cache_now = load_cache() or {}
if cache_now.get("updated_at"):
    st.caption(f"上次价格更新：{cache_now['updated_at']}")

col_go, col_skip = st.columns([2, 1])
run_full = col_go.button("📈 更新价格并生成日报", type="primary", use_container_width=True)
run_skip = col_skip.button("⚡ 用现有价格直接生成", use_container_width=True,
                           help="跳过抓价，用最近一次缓存价格生成日报")


@st.cache_data(show_spinner="🤖 AI 正在撰写日报…", ttl=1800)
def _cached_report(cache_key: str, payload: dict, model: str) -> dict:
    return ai_review.daily_report(payload, model=model)


if run_full or run_skip:
    if run_full:
        with st.spinner("正在获取最新价格…（约 20-30 秒）"):
            cache = update_all_prices(pf)
            st.cache_data.clear()
        failed = cache.get("failed", [])
        if failed:
            st.warning(f"⚠️ 部分获取失败：{', '.join(failed)}")
        else:
            st.success(f"✅ {len(cache.get('prices', {}))} 只全部更新成功")
        # 记录当日净值
        try:
            nav_hist = _total_nav(pf, cache)
            append_value(nav_hist)
            sync_to_github(HISTORY_PATH, "data/portfolio_value_history.csv",
                           "chore: record daily portfolio value")
        except Exception:
            pass
    else:
        cache = load_cache() or {}
        if not cache.get("prices"):
            st.error("暂无缓存价格。请先点击「📈 更新价格并生成日报」。")
            st.stop()

    total_nav = _total_nav(pf, cache)
    acc = accum.scan_holdings(pf)

    payload = {
        "日期": date.today().isoformat(),
        "组合概览": _portfolio_block(pf, cache, total_nav),
        "量能": _momentum_block(pf, acc),
        "期权": _options_block(cache),
        "资讯": _news_block(),
    }

    ck = f"{date.today().isoformat()}|{round(total_nav)}|{model}"
    try:
        rep = _cached_report(ck, payload, model)
    except llm.LLMError as e:
        st.error(f"日报生成失败：{e}")
        st.stop()

    st.divider()
    m1, m2, m3 = st.columns(3)
    m1.metric("📊 总净值 (USD)", f"${total_nav:,.0f}")
    ob = payload["组合概览"]
    m2.metric("📋 持仓数", ob["持仓数"])
    opt_v = payload["期权"].get("期权总市值") if payload["期权"] else None
    m3.metric("🎯 期权市值", f"${opt_v:,.0f}" if opt_v else "—")

    if rep.get("headline"):
        st.markdown(f"## {rep['headline']}")
    if rep.get("market_note"):
        st.info(rep["market_note"])

    # 持仓结构
    port = rep.get("portfolio", {})
    st.markdown("### 💼 持仓结构")
    if port.get("summary"):
        st.write(port["summary"])
    if ob.get("板块权重(%)"):
        st.caption("板块权重(%): " + " · ".join(f"{k} {v}%" for k, v in ob["板块权重(%)"].items()))
    for h in port.get("highlights", []):
        st.markdown(f"- {h}")

    # 量能
    mo = rep.get("momentum", {})
    st.markdown("### 📊 量能")
    if mo.get("summary"):
        st.write(mo["summary"])
    cL, cR = st.columns(2)
    with cL:
        if mo.get("leaders"):
            st.markdown("**🟢 领涨**")
            for x in mo["leaders"]:
                st.markdown(f"- **`{x.get('ticker','')}`**：{x.get('note','')}")
    with cR:
        if mo.get("laggards"):
            st.markdown("**🔴 领跌**")
            for x in mo["laggards"]:
                st.markdown(f"- **`{x.get('ticker','')}`**：{x.get('note','')}")

    # 期权
    st.markdown("### 🎯 期权")
    op = rep.get("options", {})
    st.write(op.get("summary", "无期权持仓。"))

    # 资讯
    nw = rep.get("news", {})
    st.markdown("### 📰 资讯")
    if nw.get("summary"):
        st.write(nw["summary"])
    for h in nw.get("highlights", []):
        st.markdown(f"- {h}")

    # 关注 / 风险
    cA, cB = st.columns(2)
    with cA:
        if rep.get("actions"):
            st.markdown("### 🧭 今日关注")
            for a in rep["actions"]:
                st.markdown(f"- {a}")
    with cB:
        if rep.get("risks"):
            st.markdown("### ⚠️ 风险")
            for rk in rep["risks"]:
                st.markdown(f"- {rk}")

    u = rep.get("_usage", {})
    st.caption(
        f"🤖 {rep.get('_model','')} · {u.get('total_tokens','?')} tokens · "
        f"~${rep.get('_cost_usd',0):.4f} | AI 生成，非投资建议。"
    )
    with st.expander("🔎 查看喂给 AI 的原始数据"):
        st.json(payload)
