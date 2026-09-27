"""core/risk.py — 风险仪表盘 + 压力测试

持仓统一整理成一本"账"（book）：股票 / 现金 / 期权，全部美元计价，
期权另算 delta 等效敞口（delta × 标的价 × 100 × 张数），归入标的所属板块。

  · 敞口     各标的 delta 调整后敞口、权重、板块集中度、有效持仓数（1/Σw²）
  · 风险     β（vs SPY / SOXX）、年化波动、历史模拟 VaR / CVaR、最大回撤、风险贡献
             ——用过去 1 年日收益 × 当前敞口回放（期权按 delta 线性近似）
  · 压力测试 因子冲击：基准跌 X% → 各标的按 β 联动，期权用 Black-Scholes 全额重估（可叠加 IV 与时间变化）
             历史情景：把过去真实发生的急跌区间（各标的真实涨跌）套到当前持仓上
"""

from __future__ import annotations

import math
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from core import daily_momentum as dm
from core import options as O

HIST_PERIOD = "2y"
BETA_BENCHES = ("SPY", "SOXX", "QQQ")
LOOKBACK = 252                 # β / 波动 / VaR 回看交易日

# 固定的历史事件情景（区间收盘 → 收盘）
NAMED_EVENTS = [
    ("DeepSeek 冲击", "2025-01-24", "2025-01-27"),
    ("关税冲击（解放日）", "2025-04-02", "2025-04-08"),
]


# ─── 持仓账本 ─────────────────────────────────────────────────────────────────

def _option_quote(contract: str) -> dict | None:
    try:
        q = O.fetch_option(contract)
    except Exception:
        q = None
    return q.as_dict() if q else None


def build_book(portfolio: dict, cache: dict | None = None) -> tuple[pd.DataFrame, dict[str, pd.Series]]:
    """
    返回 (book, closes)：
      book    每个仓位一行：kind(stock/cash/option), ticker, underlying, display, sector,
              qty, price, value（美元市值）, exposure（delta 调整后敞口）,
              期权另有 option_type/strike/expiry/T/iv/delta/mark
      closes  {ticker: 美元收盘价序列}（HIST_PERIOD），供 β / 回放 / 情景使用
    期权报价优先现场抓取（CBOE 延迟行情），失败时退回价格缓存。
    """
    cache = cache or {}
    stocks: dict[str, dict] = {}
    cash = 0.0
    for acc in portfolio.get("accounts", []):
        for pos in acc.get("positions", []):
            t = pos["yf_ticker"].upper()
            if t == config.CASH_TICKER:
                cash += float(pos.get("shares", 0) or 0)
                continue
            s = stocks.setdefault(t, {"display": pos.get("display", t), "sector": pos.get("sector", "其他"),
                                      "qty": 0.0})
            s["qty"] += float(pos.get("shares", 0) or 0)

    options = portfolio.get("options", [])
    unders = {str(o.get("underlying", "")).upper() for o in options if o.get("underlying")}
    tickers = sorted(set(stocks) | unders | set(BETA_BENCHES))
    closes = dm.fetch_histories(tickers, period=HIST_PERIOD, usd=True)

    rows = []
    for t, s in stocks.items():
        c = closes.get(t)
        px = float(c.iloc[-1]) if c is not None and len(c) else None
        val = px * s["qty"] if px else 0.0
        rows.append({"kind": "stock", "ticker": t, "underlying": t, "display": s["display"],
                     "sector": s["sector"], "qty": s["qty"], "price": px, "value": val, "exposure": val})
    if cash:
        rows.append({"kind": "cash", "ticker": "CASH", "underlying": None, "display": "现金",
                     "sector": "现金", "qty": cash, "price": 1.0, "value": cash, "exposure": 0.0})

    cached_opts = cache.get("options") or {}
    today = date.today()
    for o in options:
        contract = str(o.get("contract", "")).strip()
        occ = O.parse_occ(contract) if contract else None
        if not occ:
            continue
        und = str(o.get("underlying") or occ["root"]).upper()
        n = float(o.get("contracts", 1) or 1)
        c = closes.get(und)
        S = float(c.iloc[-1]) if c is not None and len(c) else None
        q = _option_quote(contract) or {}
        cq = cached_opts.get(contract, {})
        mark = o.get("manual_mark") or q.get("mark_price") or cq.get("mark")
        T = max((date.fromisoformat(occ["expiry"]) - today).days, 0) / 365.0
        # IV 优先由当前价格反解：保证"标的不动、时间不动"时模型价 = 市价，情景盈亏从 0 起算
        iv = None
        if mark and S:
            iv = O.implied_vol_from_price(float(mark), S, occ["strike"], T, option_type=occ["option_type"])
        iv = iv or q.get("iv") or cq.get("iv")
        g = O.bs_greeks(S, occ["strike"], T, iv, option_type=occ["option_type"]) if (S and iv) else {}
        delta = g.get("delta", q.get("delta") or cq.get("delta") or 0.0)
        mark = float(mark) if mark else g.get("price")
        rows.append({
            "kind": "option", "ticker": contract, "underlying": und,
            "display": o.get("display", contract), "sector": O.option_sector(o, portfolio),
            "qty": n, "price": mark, "value": (mark or 0.0) * O.CONTRACT_MULTIPLIER * n,
            "exposure": (delta or 0.0) * (S or 0.0) * O.CONTRACT_MULTIPLIER * n,
            "option_type": occ["option_type"], "strike": occ["strike"], "expiry": occ["expiry"],
            "T": T, "iv": iv, "delta": delta, "S": S, "mark": mark,
        })
    return pd.DataFrame(rows), closes


# ─── 收益矩阵与 β ─────────────────────────────────────────────────────────────

def _returns(closes: dict[str, pd.Series], tickers: list[str]) -> pd.DataFrame:
    px = pd.DataFrame({t: closes[t] for t in tickers if t in closes}).sort_index()
    return px.pct_change(fill_method=None).iloc[1:]


def betas(closes: dict[str, pd.Series], tickers: list[str], lookback: int = LOOKBACK) -> pd.DataFrame:
    """各标的相对 BETA_BENCHES 的 β（近 lookback 日，按共同交易日计算）与年化波动。"""
    rets = _returns(closes, list(set(tickers) | set(BETA_BENCHES))).tail(lookback)
    rows = []
    for t in tickers:
        if t not in rets:
            continue
        row = {"ticker": t}
        r = rets[t]
        row["vol"] = float(r.std(ddof=0) * math.sqrt(252)) if r.notna().sum() > 20 else None
        for b in BETA_BENCHES:
            both = pd.concat([r, rets[b]], axis=1).dropna()
            if len(both) > 40 and both.iloc[:, 1].var() > 0:
                row[f"beta_{b}"] = float(both.cov().iloc[0, 1] / both.iloc[:, 1].var())
            else:
                row[f"beta_{b}"] = None
        rows.append(row)
    return pd.DataFrame(rows).set_index("ticker") if rows else pd.DataFrame()


def exposures(book: pd.DataFrame) -> pd.Series:
    """{标的: delta 调整后美元敞口}（股票 + 期权合并到标的）。"""
    b = book[book["kind"] != "cash"]
    return b.groupby("underlying")["exposure"].sum()


# ─── 组合风险指标 ─────────────────────────────────────────────────────────────

def _filled_returns(closes, tickers, beta_tbl, lookback) -> pd.DataFrame:
    """回看期日收益；某标的当天无数据（未上市 / 当地休市）时用 β × SPY 代替。"""
    rets = _returns(closes, list(set(tickers) | {"SPY"})).tail(lookback)
    spy = rets["SPY"]
    out = {}
    for t in tickers:
        r = rets[t] if t in rets else pd.Series(np.nan, index=rets.index)
        b = beta_tbl.loc[t, "beta_SPY"] if (t in beta_tbl.index and pd.notna(beta_tbl.loc[t, "beta_SPY"])) else 1.0
        out[t] = r.fillna(spy * b)
    return pd.DataFrame(out)


def portfolio_risk(book: pd.DataFrame, closes: dict[str, pd.Series],
                   lookback: int = LOOKBACK) -> dict:
    """
    组合层面风险（当前敞口 × 过去 lookback 日真实日收益回放）：
      nav, gross_exposure, leverage, beta_*（敞口加权 / NAV）, vol（年化，占 NAV）,
      var95 / cvar95（1 日，美元）, max_dd（回放净值最大回撤）, sim（回放的日盈亏序列）,
      risk_contrib（各标的对组合波动的贡献占比）, corr（标的相关矩阵）, hhi_n（有效持仓数）
    """
    nav = float(book["value"].sum())
    exp = exposures(book)
    exp = exp[exp.abs() > 1]
    tickers = list(exp.index)
    beta_tbl = betas(closes, tickers, lookback)
    rets = _filled_returns(closes, tickers, beta_tbl, lookback)

    pnl = (rets * exp).sum(axis=1)                 # 每日美元盈亏
    sim_nav = nav + pnl.cumsum()
    dd = sim_nav / sim_nav.cummax() - 1
    q = float(pnl.quantile(0.05))

    cov = rets.cov() * 252
    e = exp.reindex(cov.index).fillna(0).to_numpy()
    port_var = float(e @ cov.to_numpy() @ e)
    port_sd = math.sqrt(port_var) if port_var > 0 else 0.0
    rc = pd.Series(e * (cov.to_numpy() @ e) / port_var if port_var > 0 else 0.0, index=cov.index)

    w = (exp / exp.abs().sum()).abs()
    out = {
        "nav": nav,
        "gross_exposure": float(exp.abs().sum()),
        "leverage": float(exp.sum() / nav) if nav else None,
        "vol": port_sd / nav if nav else None,
        "var95": -q,
        "cvar95": float(-pnl[pnl <= q].mean()) if (pnl <= q).any() else None,
        "max_dd": float(dd.min()),
        "sim": pnl,
        "risk_contrib": rc.sort_values(ascending=False),
        "corr": rets.corr(),
        "hhi_n": float(1 / (w ** 2).sum()) if len(w) else None,
        "betas": beta_tbl,
        "exposure": exp.sort_values(ascending=False),
    }
    for b in BETA_BENCHES:
        col = f"beta_{b}"
        if col in beta_tbl:
            bb = beta_tbl[col].reindex(exp.index).fillna(1.0)
            out[f"beta_{b}"] = float((bb * exp).sum() / nav) if nav else None
    return out


def sector_exposure(book: pd.DataFrame) -> pd.DataFrame:
    """板块：市值 / delta 调整敞口 及其占 NAV 比例（期权按标的归入板块）。"""
    nav = float(book["value"].sum())
    g = book.groupby("sector").agg(value=("value", "sum"), exposure=("exposure", "sum")).reset_index()
    g["value_pct"] = g["value"] / nav
    g["exposure_pct"] = g["exposure"] / nav
    return g.sort_values("exposure", ascending=False).reset_index(drop=True)


# ─── 压力测试 ─────────────────────────────────────────────────────────────────

def _reprice(book: pd.DataFrame, moves: dict[str, float], iv_shift: float = 0.0,
             days: int = 0) -> pd.DataFrame:
    """
    给定各标的涨跌幅 moves，逐仓位重估：股票线性，期权 Black-Scholes 全额重估
    （IV 平移 iv_shift，时间前进 days 天）。返回每仓位 pnl。
    """
    rows = []
    for r in book.itertuples():
        mv = moves.get(r.underlying, 0.0) if r.underlying else 0.0
        if r.kind == "stock":
            pnl = r.value * mv
        elif r.kind == "option" and r.S and r.iv:
            T2 = max(r.T - days / 365.0, 0.0)
            new = O.bs_greeks(r.S * (1 + mv), r.strike, T2, max(r.iv + iv_shift, 0.01),
                              option_type=r.option_type)["price"]
            pnl = (new - (r.mark or 0.0)) * O.CONTRACT_MULTIPLIER * r.qty
        else:
            pnl = 0.0
        rows.append({"ticker": r.ticker, "display": r.display, "kind": r.kind, "sector": r.sector,
                     "underlying": r.underlying, "move": mv, "value": r.value, "pnl": pnl})
    return pd.DataFrame(rows)


def stress_factor(book: pd.DataFrame, beta_tbl: pd.DataFrame, bench: str, shock: float,
                  iv_shift: float = 0.0, days: int = 0) -> pd.DataFrame:
    """基准涨跌 shock → 各标的按 β 联动（无 β 时按 1），期权全额重估。"""
    col = f"beta_{bench}"
    moves = {}
    for u in book["underlying"].dropna().unique():
        b = beta_tbl.loc[u, col] if (u in beta_tbl.index and col in beta_tbl and pd.notna(beta_tbl.loc[u, col])) else 1.0
        moves[u] = b * shock
    return _reprice(book, moves, iv_shift, days)


def _window_move(c: pd.Series | None, d0: pd.Timestamp, d1: pd.Timestamp) -> float | None:
    if c is None:
        return None
    a, b = c[c.index <= d0], c[c.index <= d1]
    if a.empty or b.empty or a.index[-1] < d0 - pd.Timedelta(days=7):
        return None
    return float(b.iloc[-1] / a.iloc[-1] - 1)


def stress_window(book: pd.DataFrame, closes: dict[str, pd.Series], beta_tbl: pd.DataFrame,
                  d0: str, d1: str) -> tuple[pd.DataFrame, int]:
    """
    把 d0 收盘 → d1 收盘 的真实涨跌套到当前持仓；当时未上市的标的用 β × SPY 同期涨跌代替。
    返回 (逐仓位 pnl, 用代理的标的数)。
    """
    t0, t1 = pd.Timestamp(d0), pd.Timestamp(d1)
    spy_mv = _window_move(closes.get("SPY"), t0, t1) or 0.0
    moves, n_proxy = {}, 0
    for u in book["underlying"].dropna().unique():
        mv = _window_move(closes.get(u), t0, t1)
        if mv is None:
            b = beta_tbl.loc[u, "beta_SPY"] if (u in beta_tbl.index and pd.notna(beta_tbl.loc[u, "beta_SPY"])) else 1.0
            mv = b * spy_mv
            n_proxy += 1
        moves[u] = mv
    return _reprice(book, moves), n_proxy


def worst_windows(closes: dict[str, pd.Series], bench: str = "SOXX",
                  horizons: tuple[int, ...] = (5, 20)) -> list[tuple[str, str, str, float]]:
    """基准在历史数据中最差的 N 日区间：[(名称, 起, 止, 基准涨跌)]。"""
    c = closes.get(bench)
    out = []
    if c is None:
        return out
    c = c.dropna()
    for h in horizons:
        r = c / c.shift(h) - 1
        if r.dropna().empty:
            continue
        end = r.idxmin()
        start = c.index[c.index.get_loc(end) - h]
        out.append((f"{bench} 最差 {h} 日", start.date().isoformat(), end.date().isoformat(), float(r.min())))
    return out


def scenario_table(book: pd.DataFrame, closes: dict[str, pd.Series], beta_tbl: pd.DataFrame) -> pd.DataFrame:
    """汇总各预设情景对组合的冲击（美元与占 NAV%）。"""
    nav = float(book["value"].sum())
    rows = []
    for bench, shocks in (("SOXX", (-0.10, -0.20)), ("SPY", (-0.05, -0.10))):
        for s in shocks:
            p = stress_factor(book, beta_tbl, bench, s)
            rows.append({"情景": f"{bench} {s:+.0%}（按 β 联动）", "类型": "因子冲击",
                         "区间": "—", "基准涨跌": s, "组合盈亏": p["pnl"].sum(), "代理标的数": 0})
    p = stress_factor(book, beta_tbl, "SOXX", -0.15, iv_shift=0.10)
    rows.append({"情景": "SOXX -15% 且 IV +10 点", "类型": "因子冲击", "区间": "—", "基准涨跌": -0.15,
                 "组合盈亏": p["pnl"].sum(), "代理标的数": 0})
    windows = worst_windows(closes) + [(n, a, b, _window_move(closes.get("SOXX"), pd.Timestamp(a),
                                                               pd.Timestamp(b))) for n, a, b in NAMED_EVENTS]
    for name, a, b, bm in windows:
        p, n_proxy = stress_window(book, closes, beta_tbl, a, b)
        rows.append({"情景": name, "类型": "历史重演", "区间": f"{a} → {b}", "基准涨跌": bm,
                     "组合盈亏": p["pnl"].sum(), "代理标的数": n_proxy})
    df = pd.DataFrame(rows)
    df["占净值"] = df["组合盈亏"] / nav if nav else None
    return df
