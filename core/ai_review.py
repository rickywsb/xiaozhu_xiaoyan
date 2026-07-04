"""core/ai_review.py — LLM 分析层（基于我们已算好的量化信号做解读，非投资建议）

两个功能：
  • news_digest(items_by_theme)  —— 资讯晨报：主题摘要 + 情绪 + 对持仓的潜在影响
  • portfolio_diagnosis(payload) —— 持仓健康诊断：集中度/赛道暴露/信号背离/风险

设计原则：
  - Grounding：只喂 LLM 我们算好的数字，禁止其臆造价格/指标。
  - 结构化输出：强制 JSON，前端渲染成卡片，不放任自由发挥。
  - 全部输出中文，并自带"AI 生成、非投资建议"的定位。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import llm

# ─── 通用 system 前言 ─────────────────────────────────────────────────────────

_GUARDRAIL = (
    "你是一名严谨的半导体行业买方研究助理，服务于一支专注存储与光通信的组合。"
    "你只能使用用户提供的数据进行分析，禁止编造任何价格、指标或事实；"
    "无法从数据判断时应明确说'数据不足'。所有输出为中文，"
    "定位为辅助研究，不构成投资建议。严格按要求的 JSON 结构输出，不要额外文字。"
)


# ─── 功能 B：资讯晨报 ─────────────────────────────────────────────────────────

def news_digest(
    items_by_theme: dict[str, list[dict]],
    holdings: list[str] | None = None,
    *,
    per_theme: int = 10,
    model: str = llm.DEFAULT_MODEL,
) -> dict:
    """把聚合到的资讯做成中文晨报。

    items_by_theme: {主题: [{title, source, summary, published(可为字符串)}, …]}
    holdings: 持仓 ticker 列表（用于点评对持仓的潜在影响）
    """
    # 压缩输入：每主题取前 per_theme 条，仅保留标题/来源/摘要
    compact: dict[str, list[dict]] = {}
    for theme, items in items_by_theme.items():
        rows = []
        for it in items[:per_theme]:
            rows.append({
                "标题": it.get("title", ""),
                "来源": it.get("source", ""),
                "摘要": (it.get("summary") or "")[:180],
            })
        compact[theme] = rows

    payload = {
        "主题资讯": compact,
        "我的持仓": holdings or [],
    }

    system = _GUARDRAIL
    user = (
        "下面是按主题聚合的近期行业资讯（存储 / 光通信 / 半导体大盘）。"
        "请生成一份中文晨报，帮助基金经理快速把握行业动向。\n\n"
        f"数据：\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        "请严格输出如下 JSON：\n"
        "{\n"
        '  "overview": "3~5 句总览，点出本轮最值得关注的主线",\n'
        '  "themes": [\n'
        '    {"name": "存储", "sentiment": "利好|中性|利空",\n'
        '     "summary": "该主题 2~3 句要点",\n'
        '     "highlights": ["关键事件1", "关键事件2"]}\n'
        "  ],\n"
        '  "portfolio_impact": [\n'
        '    {"ticker": "MU", "note": "结合资讯说明潜在影响与方向"}\n'
        "  ],\n"
        '  "risks": ["需警惕的风险点1", "..."]\n'
        "}\n"
        "themes 要覆盖全部提供的主题；portfolio_impact 只点评能从资讯合理关联到的持仓。"
    )
    return llm.chat_json(system, user, model=model, max_tokens=1600)


# ─── 功能 A：持仓健康诊断 ─────────────────────────────────────────────────────

def portfolio_diagnosis(
    holdings: list[dict],
    sector_weights: dict[str, float] | None = None,
    news_headlines: list[str] | None = None,
    *,
    model: str = llm.DEFAULT_MODEL,
) -> dict:
    """对整体持仓做健康诊断。

    holdings: [{ticker, display, sector, weight_pct, momentum, direction, accel,
                vol_30d, drawdown_10d, accum_verdict, accum_score, cmf, ud_vol, breakout}]
    sector_weights: {sector: 权重%}
    news_headlines: 若干条近期相关新闻标题（可选，帮助结合基本面）
    """
    payload = {
        "持仓明细": holdings,
        "板块权重(%)": sector_weights or {},
        "近期新闻标题": (news_headlines or [])[:20],
        "字段说明": {
            "momentum": "量能综合得分（Z-score 合成，越高动量越强）",
            "direction": "趋势方向 ↑↑/↑/→/↓/↓↓",
            "accum_verdict": "量价吸筹判定（🟢疑似吸筹/🟡中性/🔴疑似派发，代理信号非真实资金流）",
            "accum_score": "吸筹评分（≥3 吸筹，≤-3 派发）",
            "cmf": "Chaikin 资金流（>0 买盘占优）",
            "ud_vol": "涨跌量比（>1 放量在涨）",
            "breakout": "是否放量突破 20 日新高",
            "vol_30d": "30 日年化波动率",
            "drawdown_10d": "近 10 日回撤",
        },
    }

    system = _GUARDRAIL
    user = (
        "下面是一支半导体组合的持仓与我们自算的量能、主力吸筹（量价代理）信号。"
        "请做一次'持仓健康诊断'，重点识别集中度风险、赛道暴露、以及**信号背离**"
        "（例如：价格在涨但吸筹信号为派发、动量强但资金流转弱、放量突破但波动率过高等）。\n\n"
        f"数据：\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        "请严格输出如下 JSON：\n"
        "{\n"
        '  "overall": "一句话总评",\n'
        '  "health_score": 0到100的整数,\n'
        '  "concentration": {"assessment": "集中度评估", "flags": ["超配项…"]},\n'
        '  "sector_exposure": {"assessment": "赛道暴露评估（结合存储/光通信）"},\n'
        '  "divergences": [{"ticker": "XXX", "issue": "信号背离的具体说明"}],\n'
        '  "strengths": ["组合亮点…"],\n'
        '  "risks": ["主要风险…"],\n'
        '  "watch": ["建议重点关注的标的或事项…"]\n'
        "}\n"
        "divergences 只列真正存在矛盾信号的标的；无背离则返回空数组。"
        "health_score 综合动量、资金流一致性、集中度与风险给出。"
    )
    return llm.chat_json(system, user, model=model, max_tokens=1800)
