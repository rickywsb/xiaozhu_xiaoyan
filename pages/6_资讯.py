"""pages/6_资讯.py — 半导体 / 存储 / 光通信 资讯聚合"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
from core import news
from core import llm, ai_review

st.title("📰 行业资讯")
st.caption(
    "聚合 Google News、行业垂直媒体（Blocks & Files / SEMI / EE Times）、"
    "TrendForce 公开新闻稿 与 yfinance 个股新闻。"
    "⚠️ 本页为**公开资讯聚合**，非卖方深度研报或付费投研数据，仅供辅助了解行业动向。"
)


# ─── 缓存 ─────────────────────────────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def _load_portfolio() -> dict:
    return json.loads(config.PORTFOLIO_PATH.read_text(encoding="utf-8"))


@st.cache_data(show_spinner="📡 正在抓取主题资讯…", ttl=1800)
def _cached_theme(theme_name: str) -> list[dict]:
    return [i.as_dict() for i in news.fetch_theme(theme_name)]


@st.cache_data(show_spinner="📡 正在抓取个股新闻…", ttl=1800)
def _cached_ticker_news(tickers_key: str) -> list[dict]:
    tickers = tickers_key.split("|") if tickers_key else []
    return [i.as_dict() for i in news.fetch_ticker_news(tickers, limit_per=4)]


@st.cache_data(show_spinner="🤖 AI 正在生成晨报…", ttl=1800)
def _cached_digest(cache_key: str, items_by_theme: dict, holdings: list[str]) -> dict:
    """cache_key 变化时重算（用日期+标题数拼成）。"""
    return ai_review.news_digest(items_by_theme, holdings=holdings)


# ─── 渲染工具 ─────────────────────────────────────────────────────────────────

def _fmt_when(dt) -> str:
    if not dt:
        return "时间未知"
    if isinstance(dt, str):
        return dt
    now = datetime.now(timezone.utc)
    delta = now - dt
    days = delta.days
    if days <= 0:
        hrs = int(delta.total_seconds() // 3600)
        return f"{hrs} 小时前" if hrs >= 1 else "刚刚"
    if days == 1:
        return "昨天"
    if days < 7:
        return f"{days} 天前"
    return dt.strftime("%Y-%m-%d")


def _render_items(items: list[dict], empty_msg: str = "暂无资讯（来源可能临时不可用，稍后再试）。"):
    if not items:
        st.info(empty_msg)
        return
    for it in items:
        title = it["title"]
        link = it["link"]
        source = it.get("source") or "未知来源"
        when = _fmt_when(it.get("published"))
        tickers = it.get("tickers") or []
        tag = f" · `{' '.join(tickers)}`" if tickers else ""
        st.markdown(f"**[{title}]({link})**")
        summary = (it.get("summary") or "").strip()
        if summary:
            st.caption(summary)
        st.caption(f"🗞 {source} · 🕒 {when}{tag}")
        st.divider()


def _refresh_button():
    if st.button("🔄 刷新资讯", help="清空缓存并重新抓取（缓存有效期 30 分钟）"):
        _cached_theme.clear()
        _cached_ticker_news.clear()
        st.rerun()


# ─── 页面 ─────────────────────────────────────────────────────────────────────

portfolio = _load_portfolio()
_refresh_button()

theme_names = list(news.THEMES.keys())

# ─── 🤖 AI 晨报 ───────────────────────────────────────────────────────────────
with st.expander("🤖 AI 晨报（LLM 生成的中文要点摘要）", expanded=False):
    st.caption("基于下方各主题资讯，用 OpenAI 生成中文晨报：主题要点 · 情绪 · 对持仓潜在影响。⚠️ AI 生成，非投资建议。")
    if not llm.available():
        st.info("未检测到 OPENAI_API_KEY。请在 Streamlit Secrets / 环境变量 / 本地 openaitoken.txt 配置后重试。")
    elif st.button("📝 生成 AI 晨报", key="gen_digest"):
        items_by_theme = {t: _cached_theme(t) for t in theme_names}
        holdings = news.portfolio_tickers(portfolio)
        cache_key = "|".join(f"{t}:{len(items_by_theme[t])}" for t in theme_names) + f"@{datetime.now(timezone.utc).date()}"
        try:
            digest = _cached_digest(cache_key, items_by_theme, holdings)
        except llm.LLMError as e:
            st.error(f"生成失败：{e}")
        else:
            ov = digest.get("overview")
            if ov:
                st.markdown(f"**📌 总览**\n\n{ov}")
            _sent_emoji = {"利好": "🟢", "中性": "🟡", "利空": "🔴"}
            for th in digest.get("themes", []):
                emj = _sent_emoji.get(th.get("sentiment", ""), "⚪")
                st.markdown(f"**{emj} {th.get('name','')}** · {th.get('sentiment','')}")
                if th.get("summary"):
                    st.write(th["summary"])
                for hl in th.get("highlights", []):
                    st.markdown(f"- {hl}")
            impacts = digest.get("portfolio_impact", [])
            if impacts:
                st.markdown("**💼 对持仓的潜在影响**")
                for im in impacts:
                    st.markdown(f"- `{im.get('ticker','')}`：{im.get('note','')}")
            risks = digest.get("risks", [])
            if risks:
                st.markdown("**⚠️ 风险提示**")
                for r in risks:
                    st.markdown(f"- {r}")
            usage = digest.get("_usage", {})
            if usage:
                st.caption(
                    f"🤖 {digest.get('_model','')} · {usage.get('total_tokens',0)} tokens "
                    f"· ~${digest.get('_cost_usd',0):.4f}"
                )

tab_labels = [f"{news.THEMES[t]['emoji']} {t}" for t in theme_names] + ["💼 我的持仓"]
tabs = st.tabs(tab_labels)

# 主题 tab
for idx, theme_name in enumerate(theme_names):
    with tabs[idx]:
        meta = news.THEMES[theme_name]
        st.markdown(f"### {meta['emoji']} {theme_name}")
        kw_str = " · ".join(meta["keywords"])
        feeds_str = "、".join(name for name, _ in meta["feeds"]) or "（纯关键词）"
        st.caption(f"🔑 关键词：{kw_str}")
        st.caption(f"📡 专属源：{feeds_str}")

        # 该主题在持仓中的相关标的
        related = news.theme_tickers(portfolio, meta.get("sector_tag"))
        if related:
            st.caption(f"📌 你在该赛道的持仓：{'、'.join(related)}")

        items = _cached_theme(theme_name)
        st.caption(f"共 {len(items)} 条 · 按时间倒序")
        _render_items(items)

# 我的持仓 tab
with tabs[-1]:
    st.markdown("### 💼 我的持仓相关新闻")
    st.caption("来自 yfinance，逐票拉取近期新闻（每票取最新 4 条）。")
    all_tickers = news.portfolio_tickers(portfolio)
    default_sel = all_tickers[: min(10, len(all_tickers))]
    sel = st.multiselect(
        "选择要查看的标的（默认前 10 只）",
        options=all_tickers,
        default=default_sel,
    )
    if not sel:
        st.info("请至少选择一只标的。")
    else:
        items = _cached_ticker_news("|".join(sorted(sel)))
        st.caption(f"共 {len(items)} 条 · 按时间倒序")
        _render_items(items, empty_msg="所选标的暂无近期新闻。")
