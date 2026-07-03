"""core/options_review.py — 期权复盘：涨跌归因 + 时间衰减 + 抄底/进场信号

围绕"帮我们看懂现在跌的是哪一块、什么时候是买入的好时机"来设计，全部
复用 data/snapshots/*.json 里已存的每日 iv/delta/gamma/theta/vega/underlying_price。

三大能力：
  1. 涨跌归因 (attribute_change)
     把某支期权某日的价格变化拆成 4 块，用上一日的希腊字母做一阶泰勒展开：
        ΔV ≈ delta·ΔS + ½·gamma·ΔS² + theta·Δt + vega·ΔIV(百分点)
     从而回答"今天这个期权跌的钱里，标的下跌占多少、时间衰减占多少、
     IV 收缩占多少"。剩余项 residual 归为高阶/rho/估计误差。

  2. 时间衰减报告 (decay_metrics)
     每日 theta 损耗（$ 与占市值 %）、剩余到期天数、按当前速度的短期损耗估算。

  3. 抄底/进场信号 (iv_stats + entry_signal)
     基于历史快照的 IV 分布算 IV Rank / IV Percentile：
        IV 处于历史低位 → 期权便宜，利于买方抄底；
        IV 处于历史高位 → 期权贵，买方不划算（更适合卖方/等回落）。

约定（与 core/options.py 保持一致）：
  theta = 每日；vega = 每 1% IV 变化；iv = 小数（0.886 表示 88.6%）。
"""

from __future__ import annotations

from datetime import datetime

from core.options import parse_occ, CONTRACT_MULTIPLIER
from core import snapshots


# ─── 涨跌归因 ────────────────────────────────────────────────────────────────

def _num(v):
    return v if isinstance(v, (int, float)) else None


def _days_between(d1: str | None, d2: str | None) -> int:
    """两个快照日期相差的自然日数（theta 按自然日衰减）。缺失时默认 1。"""
    try:
        a = datetime.strptime(d1, "%Y-%m-%d").date()
        b = datetime.strptime(d2, "%Y-%m-%d").date()
        return max((b - a).days, 1)
    except Exception:
        return 1


def attribute_change(
    prev: dict,
    curr: dict,
    prev_date: str | None = None,
    curr_date: str | None = None,
    contracts: float = 1.0,
) -> dict | None:
    """把单支期权的日间价格变化拆成 delta/gamma/theta/vega/residual（合约级 $）。

    prev / curr 为快照里的 position dict，需含 price 及上一日的
    delta/gamma/theta/vega/iv/underlying_price（用 prev 的希腊字母展开）。
    返回各分量的美元金额（已 ×100×张数），无法计算时返回 None。
    """
    p0, p1 = _num(prev.get("price")), _num(curr.get("price"))
    if p0 is None or p1 is None:
        return None

    mult = CONTRACT_MULTIPLIER * (contracts or 1)
    actual_ps = p1 - p0                       # 每股实际变化
    actual = actual_ps * mult

    delta = _num(prev.get("delta"))
    gamma = _num(prev.get("gamma"))
    theta = _num(prev.get("theta"))
    vega = _num(prev.get("vega"))
    S0, S1 = _num(prev.get("underlying_price")), _num(curr.get("underlying_price"))
    iv0, iv1 = _num(prev.get("iv")), _num(curr.get("iv"))

    dS = (S1 - S0) if (S0 is not None and S1 is not None) else None
    d_iv_pts = ((iv1 - iv0) * 100) if (iv0 is not None and iv1 is not None) else None
    dt = _days_between(prev_date, curr_date)

    delta_pnl = (delta * dS) if (delta is not None and dS is not None) else None
    gamma_pnl = (0.5 * gamma * dS * dS) if (gamma is not None and dS is not None) else None
    theta_pnl = (theta * dt) if theta is not None else None
    vega_pnl = (vega * d_iv_pts) if (vega is not None and d_iv_pts is not None) else None

    # 已建模部分（转合约级）
    modeled_ps = sum(x for x in (delta_pnl, gamma_pnl, theta_pnl, vega_pnl) if x is not None)
    residual_ps = actual_ps - modeled_ps

    def _c(x):
        return round(x * mult, 2) if x is not None else None

    return {
        "actual":     round(actual, 2),
        "delta_pnl":  _c(delta_pnl),
        "gamma_pnl":  _c(gamma_pnl),
        "theta_pnl":  _c(theta_pnl),
        "vega_pnl":   _c(vega_pnl),
        "residual":   _c(residual_ps),
        "dS":         round(dS, 4) if dS is not None else None,
        "d_iv_pts":   round(d_iv_pts, 3) if d_iv_pts is not None else None,
        "dt_days":    dt,
    }


def portfolio_attribution(cache: dict, contracts_map: dict[str, float] | None = None) -> dict:
    """对所有期权做日间归因，并汇总组合级各分量。

    从最近一个更早的快照 vs 当前缓存里的期权做对比。
    返回 {"rows": {contract: 归因dict + display}, "totals": {分量: 合计}, "prev_date": ...}
    """
    options = (cache or {}).get("options", {})
    # 拿最近两个"不同日期"的快照对比：当前=最新快照日，上一日=严格早于它的最近快照。
    # （缓存对应的就是最新快照，故当前值用缓存最新的希腊字母/价格。）
    dates = snapshots.list_snapshot_dates()
    curr_date = dates[-1] if dates else None
    prior = snapshots.latest_prior_snapshot(before=curr_date) if curr_date else None
    prior_pos = (prior or {}).get("positions", {})
    prev_date = (prior or {}).get("date")

    rows: dict[str, dict] = {}
    totals = {"actual": 0.0, "delta_pnl": 0.0, "gamma_pnl": 0.0,
              "theta_pnl": 0.0, "vega_pnl": 0.0, "residual": 0.0}
    has_any = False

    for contract, od in options.items():
        prev = prior_pos.get(contract)
        if not prev:
            continue
        curr = {
            "price": od.get("mark"),
            "delta": od.get("delta"), "gamma": od.get("gamma"),
            "theta": od.get("theta"), "vega": od.get("vega"),
            "iv": od.get("iv"), "underlying_price": od.get("underlying_price"),
        }
        contracts = (contracts_map or {}).get(contract) or od.get("contracts", 1)
        attr = attribute_change(prev, curr, prev_date, curr_date, contracts)
        if not attr:
            continue
        attr["display"] = od.get("display", contract)
        attr["sector"] = od.get("sector", "期权")
        rows[contract] = attr
        for k in totals:
            v = attr.get(k)
            if v is not None:
                totals[k] += v
                has_any = True

    return {
        "rows": rows,
        "totals": {k: round(v, 2) for k, v in totals.items()} if has_any else None,
        "prev_date": prev_date,
        "curr_date": curr_date,
    }


# ─── 时间衰减报告 ────────────────────────────────────────────────────────────

def decay_metrics(opt: dict) -> dict:
    """单支期权的时间衰减指标（基于当前缓存的 theta / value / DTE）。

    theta_day_usd     每日时间价值损耗（$，合约级，负值）
    theta_pct         每日损耗占当前市值的比例
    dte               剩余到期天数
    decay_7d/30d      按当前 theta 线性估算的未来 7/30 日损耗（仅供参考，theta 会加速）
    """
    theta = _num(opt.get("theta"))
    value = _num(opt.get("value"))
    contracts = opt.get("contracts", 1) or 1
    dte = opt.get("days_to_expiry")

    theta_day = (theta * CONTRACT_MULTIPLIER * contracts) if theta is not None else None
    theta_pct = (theta_day / value) if (theta_day is not None and value) else None
    decay_7d = (theta_day * min(7, dte)) if (theta_day is not None and dte) else None
    decay_30d = (theta_day * min(30, dte)) if (theta_day is not None and dte) else None

    return {
        "theta_day_usd": round(theta_day, 2) if theta_day is not None else None,
        "theta_pct":     theta_pct,
        "dte":           dte,
        "decay_7d":      round(decay_7d, 2) if decay_7d is not None else None,
        "decay_30d":     round(decay_30d, 2) if decay_30d is not None else None,
    }


# ─── 抄底 / 进场信号 ──────────────────────────────────────────────────────────

def iv_history(contract: str) -> list[tuple[str, float]]:
    """遍历所有历史快照，收集该合约的 (日期, IV) 序列（升序，仅含有效 IV）。"""
    out: list[tuple[str, float]] = []
    for d in snapshots.list_snapshot_dates():
        snap = snapshots.load_snapshot(d)
        pos = (snap or {}).get("positions", {}).get(contract)
        if pos and isinstance(pos.get("iv"), (int, float)) and pos["iv"] > 0:
            out.append((d, float(pos["iv"])))
    return out


def iv_stats(contract: str, current_iv: float | None = None) -> dict | None:
    """基于历史快照计算 IV Rank / IV Percentile 等分布指标。

    iv_rank       (当前 - 区间最低) / (区间最高 - 区间最低)，0~1
    iv_percentile 历史上低于/等于当前 IV 的天数比例，0~1
    需要至少 2 个历史数据点，否则返回 None。
    """
    hist = iv_history(contract)
    if current_iv is not None and current_iv > 0:
        # 把当前值并入序列（避免今日尚未快照时缺失）
        if not hist or hist[-1][1] != current_iv:
            hist = hist + [("_now", float(current_iv))]
    ivs = [v for _, v in hist]
    if len(ivs) < 2:
        return None
    cur = current_iv if (current_iv and current_iv > 0) else ivs[-1]
    lo, hi = min(ivs), max(ivs)
    rank = (cur - lo) / (hi - lo) if hi > lo else 0.5
    pct = sum(1 for v in ivs if v <= cur) / len(ivs)
    return {
        "current": round(cur, 4),
        "min": round(lo, 4),
        "max": round(hi, 4),
        "mean": round(sum(ivs) / len(ivs), 4),
        "iv_rank": round(rank, 3),
        "iv_percentile": round(pct, 3),
        "n": len(ivs),
    }


def entry_signal(iv_rank: float | None, dte: int | None, moneyness: float | None) -> dict:
    """综合 IV Rank / 剩余期限 / 价内外程度，给一个买方视角的启发式信号。

    moneyness = 标的价 / 行权价（call 视角：>1 价内，<1 价外）。
    返回 {"level": "抄底良机/中性/偏贵", "emoji": ..., "reasons": [...]}。
    纯启发式，仅辅助判断，不构成投资建议。
    """
    reasons: list[str] = []
    score = 0  # 越高越利于买入

    if iv_rank is not None:
        if iv_rank <= 0.3:
            score += 2
            reasons.append(f"IV 处于历史低位（Rank {iv_rank:.0%}）→ 期权便宜，买方成本低")
        elif iv_rank >= 0.7:
            score -= 2
            reasons.append(f"IV 处于历史高位（Rank {iv_rank:.0%}）→ 期权偏贵，买方吃亏")
        else:
            reasons.append(f"IV 居中（Rank {iv_rank:.0%}）")

    if dte is not None:
        if dte < 30:
            score -= 1
            reasons.append(f"仅剩 {dte} 天到期 → 时间价值衰减快，谨慎")
        elif dte > 180:
            score += 1
            reasons.append(f"剩余 {dte} 天 → 时间充裕，衰减慢")

    if moneyness is not None:
        if moneyness < 0.9:
            reasons.append(f"深度价外（S/K={moneyness:.2f}）→ 杠杆高但胜率低")
        elif moneyness > 1.1:
            reasons.append(f"较深价内（S/K={moneyness:.2f}）→ 更像持股，时间价值占比低")

    if score >= 2:
        level, emoji = "抄底良机", "🟢"
    elif score <= -2:
        level, emoji = "偏贵/观望", "🔴"
    else:
        level, emoji = "中性", "🟡"

    return {"level": level, "emoji": emoji, "score": score, "reasons": reasons}


def moneyness_of(contract: str, underlying_price: float | None) -> float | None:
    """由 OCC 合约代码解析行权价，计算 moneyness = 标的价 / 行权价。"""
    parsed = parse_occ(contract)
    if not parsed or not underlying_price:
        return None
    k = parsed.get("strike")
    return (underlying_price / k) if k else None
