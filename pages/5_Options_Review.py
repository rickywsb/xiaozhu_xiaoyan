"""pages/5_Options_Review.py — 期权复盘：涨跌归因 · 时间衰减 · 抄底信号

回答三个问题：
  1. 现在（相较上一交易日）跌的是哪一块？标的跌 / 时间衰减 / IV 收缩？
  2. 每支期权每天在被时间吃掉多少钱（theta 损耗）？还剩多少天？
  3. 现在是不是买入的好时机？（基于历史 IV 分位的抄底信号）
"""

import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
from core.price_updater import load_cache
from core import options_review as R
from core.options import parse_occ
from core import llm, ai_review

st.title("🎯 期权复盘")
st.caption("涨跌归因 · 时间衰减 · 抄底/进场信号 —— 仅辅助判断，非投资建议")

cache = load_cache()
options = (cache or {}).get("options", {})

if not options:
    st.info("暂无期权数据。请到「💼 持仓净值」页点击「🔄 一键更新价格」，抓取期权行情与希腊字母后再来复盘。")
    st.stop()

updated_at = (cache or {}).get("updated_at", "—")
st.caption(f"数据更新时间：{updated_at}")

# ─── ① 组合级希腊字母敞口 ──────────────────────────────────────────────────────
st.subheader("① 组合级期权敞口")

net_delta_shares = 0.0     # 等效股数（Σ delta×100×张数）
net_delta_notional = 0.0   # delta 折算美元敞口（Σ delta×标的价×100×张数）
total_theta = 0.0          # 每日时间损耗 $
total_vega = 0.0           # 每 1% IV 变化的 $ 敞口
total_value = 0.0
for c, od in options.items():
    ct = od.get("contracts", 1) or 1
    mult = 100 * ct
    d = od.get("delta"); th = od.get("theta"); ve = od.get("vega")
    S = od.get("underlying_price"); val = od.get("value")
    if val:
        total_value += val
    if d is not None:
        net_delta_shares += d * mult
        if S:
            net_delta_notional += d * S * mult
    if th is not None:
        total_theta += th * mult
    if ve is not None:
        total_vega += ve * mult

c1, c2, c3, c4 = st.columns(4)
c1.metric("期权总市值", f"${total_value:,.0f}")
c2.metric("净 Delta 敞口", f"${net_delta_notional:,.0f}",
          help="所有期权 delta 折算成标的美元敞口（标的每涨跌 1%，期权组合约变动的金额）")
c3.metric("每日 Theta 损耗", f"${total_theta:,.1f}/日",
          help="持有一天因时间流逝损失的时间价值（负值），组合级合计")
c4.metric("Vega 敞口", f"${total_vega:,.0f} / 1%IV",
          help="隐含波动率每变化 1 个百分点，期权组合变动的金额")

# ─── ② 涨跌归因：跌的是哪一块 ─────────────────────────────────────────────────
st.subheader("② 涨跌归因 · 现在跌的是哪一块")

attr = R.portfolio_attribution(cache)
if not attr["totals"] or attr["prev_date"] is None:
    st.info("还没有足够的历史快照做日间归因（需要至少两天含期权的快照）。"
            "每天点一次「🔄 一键更新价格」，积累后这里会自动显示：本期涨跌里"
            "**标的变动 / 时间衰减 / IV 变化**各占多少。")
else:
    st.caption(f"对比区间：{attr['prev_date']} → {attr['curr_date']}")
    t = attr["totals"]
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("期权总变化", f"${t['actual']:,.0f}")
    m2.metric("标的贡献 (Δ+Γ)", f"${(t['delta_pnl'] + t['gamma_pnl']):,.0f}",
              help="标的价格变动带来的盈亏（一阶 delta + 二阶 gamma）")
    m3.metric("时间衰减 (Θ)", f"${t['theta_pnl']:,.0f}",
              help="时间流逝造成的损耗，通常为负")
    m4.metric("IV 变化 (V)", f"${t['vega_pnl']:,.0f}",
              help="隐含波动率变动带来的盈亏；负值=IV 收缩(IV crush)拖累")
    m5.metric("残差", f"${t['residual']:,.0f}", help="高阶项/利率/估计误差")

    # 组合级瀑布图：从上一日到今日，逐块拆解
    wf = go.Figure(go.Waterfall(
        orientation="v",
        measure=["relative", "relative", "relative", "relative", "total"],
        x=["标的 Δ+Γ", "时间衰减 Θ", "IV 变化 V", "残差", "净变化"],
        y=[t["delta_pnl"] + t["gamma_pnl"], t["theta_pnl"], t["vega_pnl"],
           t["residual"], t["actual"]],
        connector={"line": {"color": "#888"}},
        decreasing={"marker": {"color": "#e15759"}},
        increasing={"marker": {"color": "#59a14f"}},
        totals={"marker": {"color": "#4e79a7"}},
    ))
    wf.update_layout(title="组合期权涨跌归因（本期）", height=360,
                     margin=dict(l=10, r=10, t=40, b=10))
    st.plotly_chart(wf, use_container_width=True)

    # 逐支明细
    rows = []
    for c, r in attr["rows"].items():
        rows.append({
            "期权": r["display"],
            "板块": r.get("sector", ""),
            "实际变化": r["actual"],
            "标的 Δ": r["delta_pnl"],
            "Gamma Γ": r["gamma_pnl"],
            "时间 Θ": r["theta_pnl"],
            "IV V": r["vega_pnl"],
            "残差": r["residual"],
            "标的Δ价": r["dS"],
            "ΔIV(点)": r["d_iv_pts"],
        })
    df_attr = pd.DataFrame(rows)
    st.dataframe(
        df_attr.style.format({
            "实际变化": "${:,.0f}", "标的 Δ": "${:,.0f}", "Gamma Γ": "${:,.1f}",
            "时间 Θ": "${:,.1f}", "IV V": "${:,.0f}", "残差": "${:,.0f}",
            "标的Δ价": "{:+.2f}", "ΔIV(点)": "{:+.2f}",
        }, na_rep="—"),
        use_container_width=True, hide_index=True,
    )
    st.caption("解读：**IV V 为大额负数** = 主要因隐含波动率收缩（IV crush）而跌；"
               "**标的 Δ 为负** = 因标的下跌而跌；**时间 Θ** 恒为缓慢负向损耗。")

# ─── ③ 时间衰减报告 ───────────────────────────────────────────────────────────
st.subheader("③ 时间衰减报告 · 每天被吃掉多少")

decay_rows = []
for c, od in options.items():
    dm = R.decay_metrics(od)
    decay_rows.append({
        "期权": od.get("display", c),
        "剩余天数": dm["dte"],
        "Theta/日": dm["theta_day_usd"],
        "日损耗率": dm["theta_pct"],
        "未来7日≈": dm["decay_7d"],
        "未来30日≈": dm["decay_30d"],
        "当前市值": od.get("value"),
    })
df_decay = pd.DataFrame(decay_rows).sort_values("Theta/日")
st.dataframe(
    df_decay.style.format({
        "Theta/日": "${:,.2f}", "日损耗率": "{:.2%}",
        "未来7日≈": "${:,.0f}", "未来30日≈": "${:,.0f}", "当前市值": "${:,.0f}",
    }, na_rep="—"),
    use_container_width=True, hide_index=True,
)
st.caption("Theta/日 越负、日损耗率越高，说明该期权被时间吃得越快；"
           "临近到期（剩余天数少）时衰减会加速，「未来N日」为按当前速度的线性粗估。")

# ─── ④ 抄底 / 进场信号 ────────────────────────────────────────────────────────
st.subheader("④ 抄底 / 进场信号 · 基于历史 IV 分位")

sig_rows = []
has_iv_hist = False
for c, od in options.items():
    stt = R.iv_stats(c, od.get("iv"))
    mny = R.moneyness_of(c, od.get("underlying_price"))
    iv_rank = stt["iv_rank"] if stt else None
    if stt:
        has_iv_hist = True
    sig = R.entry_signal(iv_rank, od.get("days_to_expiry"), mny)
    sig_rows.append({
        "期权": od.get("display", c),
        "信号": f"{sig['emoji']} {sig['level']}",
        "当前IV": od.get("iv"),
        "IV Rank": iv_rank,
        "IV分位": stt["iv_percentile"] if stt else None,
        "样本天数": stt["n"] if stt else 0,
        "S/K": mny,
        "剩余天数": od.get("days_to_expiry"),
        "_reasons": "；".join(sig["reasons"]),
    })
df_sig = pd.DataFrame(sig_rows)
st.dataframe(
    df_sig.drop(columns=["_reasons"]).style.format({
        "当前IV": "{:.1%}", "IV Rank": "{:.0%}", "IV分位": "{:.0%}", "S/K": "{:.2f}",
    }, na_rep="—"),
    use_container_width=True, hide_index=True,
)
if not has_iv_hist:
    st.info("IV Rank / 分位需要历史快照积累（每天点一次「🔄 一键更新价格」）。"
            "目前含期权的快照不足两天，先展示信号的其余维度（到期时间、价内外）。")

with st.expander("📋 各期权信号解读"):
    for row in sig_rows:
        st.markdown(f"**{row['期权']}** — {row['信号']}")
        if row["_reasons"]:
            for rs in row["_reasons"].split("；"):
                st.markdown(f"- {rs}")

# ─── ⑤ IV 历史走势 ────────────────────────────────────────────────────────────
iv_series = {c: R.iv_history(c) for c in options}
iv_series = {c: s for c, s in iv_series.items() if len(s) >= 2}
if iv_series:
    st.subheader("⑤ 隐含波动率(IV) 历史走势")
    fig = go.Figure()
    for c, s in iv_series.items():
        disp = options[c].get("display", c)
        fig.add_trace(go.Scatter(
            x=[d for d, _ in s], y=[v for _, v in s],
            mode="lines+markers", name=disp,
        ))
    fig.update_layout(height=360, yaxis_tickformat=".0%",
                      margin=dict(l=10, r=10, t=30, b=10),
                      legend=dict(orientation="h"))
    st.plotly_chart(fig, use_container_width=True)
    st.caption("IV 处于自身历史低位时买入期权更便宜（抄底成本低）；高位时买方偏贵。")


# ─── ⑥ AI 期权复盘 ────────────────────────────────────────────────────────────
st.subheader("⑥ 🤖 AI 期权复盘")

if not llm.available():
    st.info("未检测到 OpenAI Key。在 Streamlit Secrets 配置 `OPENAI_API_KEY` 后即可生成 AI 复盘。")
else:
    ai_model = st.radio(
        "模型档位",
        options=[llm.DEFAULT_MODEL, llm.DEEP_MODEL],
        format_func=lambda m: "gpt-4o-mini（便宜·日常）" if m == llm.DEFAULT_MODEL
        else "gpt-4.1（更强·深度）",
        horizontal=True, key="opt_ai_model",
    )

    def _build_options_payload() -> dict:
        exposure = {
            "期权总市值": round(total_value, 0),
            "净Delta敞口USD": round(net_delta_notional, 0),
            "每日Theta损耗USD": round(total_theta, 1),
            "Vega敞口USD每1%IV": round(total_vega, 0),
        }
        attribution = {}
        if attr["totals"] and attr["prev_date"] is not None:
            t = attr["totals"]
            attribution = {
                "对比区间": f"{attr['prev_date']} → {attr['curr_date']}",
                "净变化": round(t["actual"], 0),
                "标的贡献ΔΓ": round(t["delta_pnl"] + t["gamma_pnl"], 0),
                "时间衰减Θ": round(t["theta_pnl"], 1),
                "IV变化V": round(t["vega_pnl"], 0),
                "残差": round(t["residual"], 0),
            }
        per_attr = []
        if attr["totals"] and attr["prev_date"] is not None:
            for c, r in attr["rows"].items():
                per_attr.append({
                    "期权": r["display"], "板块": r.get("sector", ""),
                    "实际变化": round(r["actual"], 0),
                    "标的Δ": round(r["delta_pnl"], 0),
                    "时间Θ": round(r["theta_pnl"], 1),
                    "IV_V": round(r["vega_pnl"], 0),
                    "标的Δ价": round(r["dS"], 2) if r.get("dS") is not None else None,
                    "ΔIV点": round(r["d_iv_pts"], 2) if r.get("d_iv_pts") is not None else None,
                })
        decay = [{
            "期权": d["期权"], "剩余天数": d["剩余天数"],
            "Theta每日": d["Theta/日"], "日损耗率": d["日损耗率"],
            "未来7日": d["未来7日≈"], "未来30日": d["未来30日≈"],
            "市值": d["当前市值"],
        } for d in decay_rows]
        signals = [{
            "期权": s["期权"], "信号": s["信号"], "当前IV": s["当前IV"],
            "IV_Rank": s["IV Rank"], "IV分位": s["IV分位"],
            "S比K": s["S/K"], "剩余天数": s["剩余天数"], "理由": s["_reasons"],
        } for s in sig_rows]
        return {
            "组合敞口": exposure,
            "涨跌归因": attribution,
            "逐支归因": per_attr,
            "时间衰减": decay,
            "进场信号": signals,
        }

    @st.cache_data(show_spinner="🤖 AI 正在复盘期权…", ttl=1800)
    def _cached_options_review(cache_key: str, payload: dict, model: str) -> dict:
        return ai_review.options_review(payload, model=model)

    if st.button("🩺 生成 AI 期权复盘", type="primary", key="gen_opt_review"):
        payload = _build_options_payload()
        ck = f"{updated_at}|{ai_model}|{len(options)}"
        try:
            res = _cached_options_review(ck, payload, ai_model)
        except llm.LLMError as e:
            st.error(f"AI 复盘失败：{e}")
            res = None

        if res:
            if res.get("overview"):
                st.markdown(f"### {res['overview']}")
            if res.get("attribution_read"):
                st.markdown("#### 🧮 涨跌归因")
                st.write(res["attribution_read"])
            cda, cea = st.columns(2)
            with cda:
                if res.get("decay_alert"):
                    st.markdown("#### ⏳ 时间衰减警示")
                    st.write(res["decay_alert"])
            with cea:
                if res.get("entry_read"):
                    st.markdown("#### 🎯 进场/抄底")
                    st.write(res["entry_read"])
            if res.get("per_option"):
                st.markdown("#### 📋 逐支点评")
                for o in res["per_option"]:
                    st.markdown(f"- **{o.get('option','')}**：{o.get('read','')}")
            cac, crc = st.columns(2)
            with cac:
                if res.get("actions"):
                    st.markdown("#### 🧭 可关注方向")
                    for a in res["actions"]:
                        st.markdown(f"- {a}")
            with crc:
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
                st.json(_build_options_payload())
