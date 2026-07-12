"""core/ai_review.py — LLM 分析层（基于我们已算好的量化信号做解读，非投资建议）

三个功能：
  • news_digest(items_by_theme)  —— 资讯晨报：主题摘要 + 情绪 + 对持仓的潜在影响
  • portfolio_diagnosis(payload) —— 持仓健康诊断：集中度/赛道暴露/信号背离/风险
  • options_review(payload)      —— 期权复盘：涨跌归因 + 时间衰减 + 进场/抄底信号解读

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


# ─── 功能 C：期权复盘 ─────────────────────────────────────────────────────────

def options_review(
    payload: dict,
    *,
    model: str = llm.DEFAULT_MODEL,
) -> dict:
    """对期权组合做一次自然语言复盘。

    payload 结构（全部由页面用我们已算好的数字构造，LLM 不得臆造）：
      {
        "组合敞口": {期权总市值, 净Delta敞口USD, 每日Theta损耗USD, Vega敞口USD每1%IV},
        "涨跌归因": {对比区间, 净变化, 标的贡献, 时间衰减, IV变化, 残差},   # 可为空
        "逐支归因": [{期权, 板块, 实际变化, 标的Δ, 时间Θ, IV_V, 标的Δ价, ΔIV点}],
        "时间衰减": [{期权, 剩余天数, Theta每日, 日损耗率, 未来7日, 未来30日, 市值}],
        "进场信号": [{期权, 信号, 当前IV, IV_Rank, IV分位, S比K, 剩余天数, 理由}],
      }
    """
    system = _GUARDRAIL + (
        " 你精通期权希腊字母（Delta/Gamma/Theta/Vega）与隐含波动率(IV)分析，"
        "能把'跌的是标的、时间衰减还是 IV 收缩'讲清楚，并结合 IV 分位判断当前买方成本高低。"
    )
    user = (
        "下面是一支半导体组合的**期权持仓**及我们自算的：组合级希腊字母敞口、"
        "日间涨跌归因（把涨跌拆成 标的Δ+Γ / 时间衰减Θ / IV变化V / 残差）、"
        "时间衰减报告、以及基于历史 IV 分位的进场/抄底信号。\n"
        "请做一次中文期权复盘，帮基金经理回答：**现在跌/涨的主因是什么、"
        "时间价值损耗有多快、当前是不是买方进场的好时机**。\n\n"
        f"数据：\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        "字段说明：正的'标的Δ'=因标的上涨而赚；大额负的'IV变化V'=隐含波动率收缩(IV crush)拖累；"
        "'时间衰减Θ'恒为缓慢负损耗；IV_Rank 越低=当前 IV 处于自身历史低位、买方越便宜。\n\n"
        "请严格输出如下 JSON：\n"
        "{\n"
        '  "overview": "3~5 句总览：本期期权组合整体涨跌与主因",\n'
        '  "attribution_read": "归因解读：本期涨跌主要由 标的/时间衰减/IV 哪一块驱动，点名影响最大的期权",\n'
        '  "decay_alert": "时间衰减警示：哪几支被时间吃得最快、临近到期需要注意的",\n'
        '  "entry_read": "进场/抄底解读：结合 IV 分位与到期时间，当前买方成本偏高还是偏低",\n'
        '  "per_option": [\n'
        '    {"option": "期权名", "read": "该期权一句话点评（涨跌主因/衰减/信号）"}\n'
        "  ],\n"
        '  "actions": ["可考虑关注的方向或动作（中性表述，非指令）…"],\n'
        '  "risks": ["需警惕的风险点（如临近到期、IV 高位、集中度）…"]\n'
        "}\n"
        "per_option 覆盖数据里出现的主要期权；无历史 IV 样本时 entry_read 说明'样本不足'。"
    )
    return llm.chat_json(system, user, model=model, max_tokens=1800)


# ─── 功能 D：每日综合日报 ─────────────────────────────────────────────────────

def daily_report(
    payload: dict,
    *,
    model: str = llm.DEFAULT_MODEL,
) -> dict:
    """把当日 持仓结构 / 量能 / 期权 / 资讯 汇成一份中文日报。

    payload 结构（全部由页面用我们已算好的数字构造，LLM 不得臆造）：
      {
        "日期": "2026-07-04",
        "组合概览": {总净值USD, 持仓数, 最大持仓, 板块权重(%)},
        "量能": {领涨: [{ticker, direction, momentum, note?}], 领跌: [...], 吸筹亮点: [...], 派发预警: [...]},
        "期权": {期权总市值, 净Delta敞口USD, 每日Theta损耗USD, Vega敞口, 时间衰减最快: [...], 归因: {}},
        "资讯": [{theme, title, source}],
      }
    """
    system = _GUARDRAIL + (
        " 现在需要你写一份面向基金经理的**每日综合晨报**，要求条理清晰、抓重点、"
        "把'持仓结构 / 量能 / 期权 / 行业资讯'四块串起来，并给出当日值得关注的点。"
    )
    user = (
        "下面是今日更新后的组合数据，涵盖四个板块：持仓结构、量能信号、期权敞口、行业资讯。"
        "请综合成一份中文日报，帮基金经理 1 分钟掌握全局。\n\n"
        f"数据：\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        "请严格输出如下 JSON：\n"
        "{\n"
        '  "headline": "一句话today综述（点出今日最重要的一点）",\n'
        '  "market_note": "结合资讯的行业/市场背景，2~3 句",\n'
        '  "portfolio": {"summary": "持仓结构点评（净值/集中度/板块）", "highlights": ["要点…"]},\n'
        '  "momentum": {"summary": "量能概述", "leaders": [{"ticker": "XX", "note": "为何领涨"}], "laggards": [{"ticker": "XX", "note": "为何走弱"}]},\n'
        '  "options": {"summary": "期权情况点评（敞口/衰减/进场）"},\n'
        '  "news": {"summary": "资讯要点 2~3 句", "highlights": ["关键事件…"]},\n'
        '  "actions": ["今日值得关注的方向或事项（中性表述，非指令）…"],\n'
        '  "risks": ["需警惕的风险点…"]\n'
        "}\n"
        "每一块只用提供的数据，缺数据就说'数据不足'；leaders/laggards 从量能数据里选。"
        "只要'期权'字段包含数字（如期权总市值>0），就必须在 options.summary 中"
        "总结其敞口/时间衰减/归因，**不得**回答'无期权持仓'；"
        "仅当'期权'为空对象 {} 时才说'无期权持仓'。"
    )
    return llm.chat_json(system, user, model=model, max_tokens=2000)


# ─── 功能 E：EMA 量能评分解读 ─────────────────────────────────────────────────

def momentum_review(
    payload: dict,
    *,
    model: str = llm.DEFAULT_MODEL,
) -> dict:
    """对持仓的 EMA 量能评分做一次自然语言解读。

    payload 结构（全部由页面用我们已算好的数字构造，LLM 不得臆造）：
      {
        "EMA参数": "EMA10/20/60",
        "评分口径": "0-100；🟢≥70 / 🟡40-69 / 🔴<40；由 位置/排列/斜率/乖离 加权",
        "分布": {"🟢强": n, "🟡中": n, "🔴弱": n, "平均分": x},
        "个股": [{股票, 量能分, 灯, 状态, 乖离%, 斜率%(5日), 占比%}],
      }
    """
    system = _GUARDRAIL + (
        " 你精通均线趋势分析（EMA 多头/空头排列、斜率、乖离），"
        "能基于 EMA 量能评分判断哪些持仓趋势健康、哪些正在破位转弱、哪些动能在加速，"
        "并提醒乖离过大的追高风险。"
    )
    user = (
        "下面是一支半导体组合**全部股票持仓**的 EMA 量能评分（我们已用"
        "现价/EMA排列/EMA斜率/乖离自算好，分数越高趋势越强）。\n"
        "请做一次中文量能解读，帮基金经理回答：**哪些持仓趋势健康可继续持有、"
        "哪些正在破位转弱需警惕、哪些动能加速值得关注**，并结合仓位占比点出轻重。\n\n"
        f"数据：\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        "字段说明：'量能分'越高越强；🟢≥70 强 / 🟡40-69 中 / 🔴<40 弱；"
        "'乖离%'为现价相对 EMA20 的偏离，正值过大(>15%)有追高风险；"
        "'斜率%'为 EMA20 近 5 日变化，正=上行。\n\n"
        "请严格输出如下 JSON：\n"
        "{\n"
        '  "overview": "3~5 句总览：组合整体量能强弱与结构（结合占比说明轻重）",\n'
        '  "healthy": [{"ticker": "XX", "note": "为何趋势健康（排列/斜率/位置）"}],\n'
        '  "warning": [{"ticker": "XX", "note": "为何需警惕（破位/转弱/空头排列）"}],\n'
        '  "accelerating": [{"ticker": "XX", "note": "动能加速、值得关注的理由"}],\n'
        '  "actions": ["可考虑关注的方向或动作（中性表述，非指令）…"],\n'
        '  "risks": ["需警惕的风险点（如高仓位却转弱、乖离过大追高）…"]\n'
        "}\n"
        "healthy/warning/accelerating 从数据中选取代表性标的即可，不必穷举；"
        "优先点名仓位占比大的持仓。数据不足时相应字段给空数组。"
    )
    return llm.chat_json(system, user, model=model, max_tokens=1800)


def fib_review(
    payload: dict,
    *,
    model: str = llm.DEFAULT_MODEL,
) -> dict:
    """对持仓的 Fibonacci 回撤预警做一次自然语言解读（结合 EMA 量能）。"""
    system = _GUARDRAIL + (
        " 你精通 Fibonacci 回撤与支撑/阻力分析，能结合 EMA 量能趋势，"
        "判断哪些持仓回踩到关键支撑（潜在关注/持有区）、哪些已破位转弱（需警惕），"
        "并说明 Fib 信号与量能是否共振。"
    )
    user = (
        "下面是一支半导体组合中**触发 Fib 回撤预警**的持仓（我们已用近半年波段"
        "高低点自算好回撤位与信号，并附上 EMA 量能分做共振参考）。\n"
        "请做一次中文预警解读，帮基金经理回答：**哪些回踩到关键支撑值得关注、"
        "哪些已破位需警惕、Fib 与量能是否共振**，并结合仓位占比点出轻重。\n\n"
        f"数据：\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        "字段说明：'回撤%'越大代表从波段极值回吐越多；'最近Fib位'如 61.8% 为黄金支撑；"
        "'距最近位%'接近 0 表示正贴该 fib 位；'量能分'越高趋势越强(🟢≥70/🟡40-69/🔴<40)；"
        "Fib 破位 + 量能弱 = 共振看淡，Fib 回踩关键支撑 + 量能仍强 = 共振偏多。\n\n"
        "请严格输出如下 JSON：\n"
        "{\n"
        '  "overview": "3~5 句总览：组合当前有哪些 Fib 预警、整体偏强还是偏弱（结合占比）",\n'
        '  "support_watch": [{"ticker": "XX", "note": "回踩到哪个 fib 支撑、量能是否共振、为何值得关注"}],\n'
        '  "breakdown": [{"ticker": "XX", "note": "为何破位转弱（跌破 78.6%/量能同步走弱）"}],\n'
        '  "actions": ["可考虑关注的方向或动作（中性表述，非指令）…"],\n'
        "support_watch/breakdown 从数据中选取代表性标的即可，不必穷举；"
        "优先点名仓位占比大的持仓。数据不足时相应字段给空数组。"
    )
    return llm.chat_json(system, user, model=model, max_tokens=1600)


def vp_review(
    payload: dict,
    *,
    model: str = llm.DEFAULT_MODEL,
) -> dict:
    """对持仓的 Volume Profile（筹码分布）预警做一次自然语言解读（结合 EMA 量能）。"""
    system = _GUARDRAIL + (
        " 你精通成交量分布（Volume Profile）与筹码分析，理解 POC(成交最密集价位)、"
        "VAH/VAL(价值区上下沿) 的支撑阻力含义，能结合 EMA 量能趋势，"
        "判断哪些持仓放量站上价值区(强势)、哪些跌出价值区(弱势)、哪些回踩 POC 面临变盘，"
        "并说明筹码信号与量能是否共振。"
    )
    user = (
        "下面是一支半导体组合中**触发筹码分布(Volume Profile)预警**的持仓（我们已用近半年"
        "日线成交量按价格分箱自算好 POC/VAH/VAL 与信号，并附 EMA 量能分做共振参考）。\n"
        "请做一次中文预警解读，帮基金经理回答：**哪些放量站上价值区值得关注、"
        "哪些跌出价值区需警惕、哪些回踩 POC 可能变盘、筹码与量能是否共振**，并结合仓位占比点出轻重。\n\n"
        f"数据：\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        "字段说明：'POC'为成交最密集价位(最强支撑/阻力/磁吸)；'VAH/VAL'为价值区上/下沿；"
        "'距POC%'正=现价在 POC 上方；'价值区宽度'越小筹码越集中(突破更有意义)；"
        "'量能分'越高趋势越强(🟢≥70/🟡40-69/🔴<40)；"
        "上破VAH+量能强=偏多共振，跌破VAL+量能弱=偏淡共振，回踩POC=多空决战需观察。\n\n"
        "请严格输出如下 JSON：\n"
        "{\n"
        '  "overview": "3~5 句总览：组合当前筹码结构、有哪些突破/破位/变盘信号（结合占比）",\n'
        '  "breakout": [{"ticker": "XX", "note": "为何放量站上价值区、量能是否共振、关注点"}],\n'
        '  "breakdown": [{"ticker": "XX", "note": "为何跌出价值区转弱、量能是否同步走弱"}],\n'
        '  "at_poc": [{"ticker": "XX", "note": "回踩 POC 面临变盘、上下沿在哪、如何观察"}],\n'
        '  "actions": ["可考虑关注的方向或动作（中性表述，非指令）…"],\n'
        '  "risks": ["需警惕的风险点（如高仓位却跌出价值区、假突破）…"]\n'
        "}\n"
        "breakout/breakdown/at_poc 从数据中选取代表性标的即可，不必穷举；"
        "优先点名仓位占比大的持仓。数据不足时相应字段给空数组。"
    )
    return llm.chat_json(system, user, model=model, max_tokens=1600)
