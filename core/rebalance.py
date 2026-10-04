"""core/rebalance.py — 情景调仓：面对已知风险事件（如中期选举），按风险收益比给持仓分层、算减仓与期权保护方案

流程
  ① 事件与情景   默认值来自历史（中期选举：1962–2022 共 16 次，选举前 21 个交易日 → 选举后 15 个交易日的 SPY 表现）
                 下跌情景 = 窗口内最大回撤最差四分之一的均值（风险在路径上，不只在终点）
  ② 每只持仓     情景盈亏：下跌情景用「下行 β」（只用 SPY 下跌日估计，抛售时相关性更高），其他情景用普通 β；
                 期权按 Black-Scholes 全额重估（含 IV 变化）
                 期望超额：综合评分所在十分组的历史 20 日超额 + 当前 A/B 信号的历史超额（×0.5，避免重复计算），
                 截断在 ±5%，按事件期长度折算
                 风险收益比 = 情景加权期望盈亏 ÷ |下跌情景亏损|（越低越该先减）
  ③ 角色         核心（你标注，只用期权保护不卖股）/ 进攻 / 持有 / 可减仓 / 防守；各角色最多可减比例不同
  ④ 自动方案     目标「下跌情景亏损 ≤ 净值的 X%」：按风险收益比从低到高减仓，直到达标
                 仍不达标 → 给核心 / 进攻持仓的期权保护（保护性 put、领口）
  ⑤ 期权工具     备兑 call 增厚、保护性 put、领口、深度实值 call 替代正股、卖 put 事后回补（CBOE 实时延迟报价）
期望超额只是历史平均（幅度小，+1~2% / 20 日），风险端（β、压力测试）更可靠：本页的定位是
「用最小的收益代价把风险降下来」，不是预测涨跌。仅供研究，非投资建议。
"""

from __future__ import annotations

import json
import math
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from core import options as O
from core import risk as R

ROLES_PATH = config.DATA_DIR / "position_roles.json"
PLANS_DIR = config.DATA_DIR / "plans"
ROLES = ["核心", "进攻", "持有", "可减仓", "防守"]
MAX_TRIM = {"核心": 0.0, "进攻": 0.25, "持有": 0.5, "可减仓": 1.0, "防守": 0.0}
ROLE_ICON = {"核心": "🏛", "进攻": "🗡", "持有": "⚖️", "可减仓": "✂️", "防守": "🛡"}
SIGNAL_SHRINK = 0.5
ALPHA_CAP = 0.05
MULT = O.CONTRACT_MULTIPLIER


# ─── ① 事件与情景 ─────────────────────────────────────────────────────────────

def election_day(year: int) -> date:
    """美国大选 / 中期选举日：11 月第一个星期一之后的星期二。"""
    n1 = date(year, 11, 1)
    monday = n1 + timedelta(days=(0 - n1.weekday()) % 7)
    return monday + timedelta(days=1)


_hist_cache: dict = {}


def midterm_history(pre: int = 21, post: int = 15) -> pd.DataFrame:
    """1962–2022 每次中期选举：选举前 pre 个交易日 → 选举后 post 个交易日的 S&P 500 表现与窗口内最大回撤。"""
    key = (pre, post)
    if key in _hist_cache:
        return _hist_cache[key]
    import yfinance as yf
    d = yf.download("^GSPC", start="1962-01-01", auto_adjust=True, progress=False)["Close"].squeeze().dropna()
    rows = []
    for y in range(1962, date.today().year, 4):
        e = pd.Timestamp(election_day(y))
        i = int(d.index.searchsorted(e))
        if i - pre < 0 or i + post >= len(d):
            continue
        win = d.iloc[i - pre:i + post + 1]
        rows.append({"年份": y, "选举前": float(d.iloc[i - 1] / d.iloc[i - pre] - 1),
                     "选举后": float(d.iloc[i + post] / d.iloc[i - 1] - 1),
                     "全程": float(d.iloc[i + post] / d.iloc[i - pre] - 1),
                     "期间最大回撤": float((win / win.cummax() - 1).min())})
    df = pd.DataFrame(rows)
    _hist_cache[key] = df
    return df


def default_scenarios(hist: pd.DataFrame) -> list[dict]:
    """
    由历史给出三情景（可在页面上改）：
      下跌      SPY = 窗口最大回撤最差四分之一的均值；概率 = 全程为负的比例
      大涨      SPY = 全程 > +8% 那些年的均值；概率 = 其比例
      震荡上行  SPY = 其余年份全程的中位数；概率 = 余下
    IV：下跌 +8 点、震荡 0、大涨 −3（事件落地后 IV 回落）。
    """
    if hist.empty:
        return [{"情景": "下跌", "SPY": -0.08, "IV": 0.08, "概率": 0.3},
                {"情景": "震荡", "SPY": 0.0, "IV": 0.0, "概率": 0.4},
                {"情景": "上涨", "SPY": 0.06, "IV": -0.03, "概率": 0.3}]
    dd = hist["期间最大回撤"].sort_values()
    down = float(dd.head(max(1, len(dd) // 4)).mean())
    p_down = float((hist["全程"] < 0).mean())
    up_mask = hist["全程"] > 0.08
    up = float(hist.loc[up_mask, "全程"].mean()) if up_mask.any() else 0.06
    p_up = float(up_mask.mean())
    mid = hist.loc[(hist["全程"] >= 0) & ~up_mask, "全程"]
    base = float(mid.median()) if len(mid) else 0.0
    return [{"情景": "下跌", "SPY": round(down, 3), "IV": 0.08, "概率": round(p_down, 2)},
            {"情景": "震荡上行", "SPY": round(base, 3), "IV": 0.0, "概率": round(1 - p_down - p_up, 2)},
            {"情景": "大涨", "SPY": round(up, 3), "IV": -0.03, "概率": round(p_up, 2)}]


def horizon_days(event: date, post_days: int = 15) -> int:
    """今天 → 事件后 post_days 个交易日，按交易日（工作日）计。"""
    today = date.today()
    return max(int(np.busday_count(today, event)) + post_days, 1)


# ─── ② 每只持仓的风险收益 ──────────────────────────────────────────────────────

def downside_betas(closes: dict[str, pd.Series], tickers: list[str], lookback: int = R.LOOKBACK) -> pd.Series:
    """只用 SPY 下跌日估计的 β（抛售时个股联动更强，压力测试更贴近现实）。"""
    rets = R._returns(closes, list(set(tickers) | {"SPY"})).tail(lookback)
    spy = rets["SPY"]
    down = spy < 0
    out = {}
    for t in tickers:
        if t not in rets:
            continue
        both = pd.concat([rets[t], spy], axis=1)[down].dropna()
        if len(both) > 30 and both.iloc[:, 1].var() > 0:
            out[t] = float(both.cov().iloc[0, 1] / both.iloc[:, 1].var())
    return pd.Series(out, dtype=float)


def _alpha20(ticker: str, score_of: dict, dec_excess: dict, sig_excess: dict) -> tuple[float, str]:
    """20 日期望超额与说明。"""
    parts, why = 0.0, []
    s = score_of.get(ticker)
    if s is not None and s == s:
        dec = min(10, max(1, int(math.ceil(float(s) / 10))))
        ex = dec_excess.get(dec)
        if ex is not None:
            parts += ex
            why.append(f"评分 {int(s)}（第 {dec} 组历史 {ex * 100:+.1f}%）")
    for name, ex in sig_excess.get(ticker, []):
        parts += SIGNAL_SHRINK * ex
        why.append(f"{name} {ex * 100:+.1f}%×0.5")
    return float(np.clip(parts, -ALPHA_CAP, ALPHA_CAP)), "；".join(why)


def scenario_moves(book: pd.DataFrame, beta: pd.Series, beta_down: pd.Series, spy_move: float) -> dict[str, float]:
    """SPY 涨跌 → 各标的涨跌：下跌用下行 β，上涨用普通 β；缺失时用 1。"""
    out = {}
    for u in book["underlying"].dropna().unique():
        b = beta_down.get(u) if spy_move < 0 else beta.get(u)
        if b is None or b != b:
            b = beta.get(u) if beta.get(u) == beta.get(u) and beta.get(u) is not None else 1.0
        out[u] = float(b) * spy_move
    return out


def scenario_pnl(book: pd.DataFrame, beta: pd.Series, beta_down: pd.Series, scen: list[dict],
                 days: int = 0) -> dict[str, pd.DataFrame]:
    """{情景名: 逐仓位 pnl 表}。days = 时间前进天数（期权时间损耗）。"""
    return {s["情景"]: R._reprice(book, scenario_moves(book, beta, beta_down, s["SPY"]), s["IV"], days)
            for s in scen}


def holding_table(book: pd.DataFrame, closes: dict, scen: list[dict], hdays: int, ratings: pd.DataFrame,
                  validation: dict | None, lab_summary: pd.DataFrame | None, lab_latest: dict) -> tuple[pd.DataFrame, dict]:
    """
    每个标的（股票与其期权合并）一行：市值、敞口、β、下行 β、评分、各情景盈亏、期望盈亏、风险收益比。
    返回 (表, 上下文{beta, beta_down, pnl 字典})。
    """
    nav = float(book["value"].sum())
    unders = [u for u in book["underlying"].dropna().unique()]
    bt = R.betas(closes, unders)
    beta = bt["beta_SPY"] if "beta_SPY" in bt else pd.Series(dtype=float)
    vol = bt["vol"] if "vol" in bt else pd.Series(dtype=float)
    beta_down = downside_betas(closes, unders)
    cal_days = int(hdays * 7 / 5)
    pnls = scenario_pnl(book, beta, beta_down, scen, days=cal_days)

    score_of = dict(zip(ratings["ticker"], ratings["score"])) if not ratings.empty else {}
    dec_excess = {int(k): v for k, v in ((validation or {}).get("deciles", {}).get("综合", {}).get("deciles", {}) or {}).items()}
    sig_ex = {}
    if lab_summary is not None and not lab_summary.empty:
        exm = dict(zip(lab_summary["信号"], lab_summary["excess"]))
        good = set(lab_summary.loc[lab_summary["grade"].str[:1].isin(["A", "B"]), "信号"])
        for t, names in (lab_latest or {}).items():
            sig_ex[t] = [(n, float(exm[n])) for n in names if n in good and exm.get(n) == exm.get(n)]

    probs = {s["情景"]: s["概率"] for s in scen}
    down_name = min(scen, key=lambda s: s["SPY"])["情景"]
    rows = []
    for u in unders:
        sub = book[book["underlying"] == u]
        stock = sub[sub["kind"] == "stock"]
        val = float(sub["value"].sum())
        exp_ = float(sub["exposure"].sum())
        a20, why = _alpha20(u, score_of, dec_excess, sig_ex)
        a_h = a20 * hdays / 20
        sp = {k: float(v.loc[v["underlying"] == u, "pnl"].sum()) for k, v in pnls.items()}
        exp_pnl = sum(probs[k] * sp[k] for k in sp) + exp_ * a_h
        down = sp[down_name]
        rows.append({
            "ticker": u, "display": str(sub["display"].iloc[0]).split(" ")[0] if len(stock) == 0 else stock["display"].iloc[0],
            "sector": sub["sector"].iloc[0], "shares": float(stock["qty"].sum()) if len(stock) else 0.0,
            "price": float(stock["price"].iloc[0]) if len(stock) and stock["price"].iloc[0] else None,
            "stock_value": float(stock["value"].sum()), "option_value": float(sub.loc[sub["kind"] == "option", "value"].sum()),
            "value": val, "exposure": exp_, "weight": val / nav if nav else None, "exp_weight": exp_ / nav if nav else None,
            "beta": _f(beta.get(u)), "beta_down": _f(beta_down.get(u)), "vol": _f(vol.get(u)),
            "score": _f(score_of.get(u)), "alpha20": a20, "alpha_why": why,
            **{f"pnl_{k}": v for k, v in sp.items()},
            "down_pnl": down, "exp_pnl": exp_pnl,
            "rr": exp_pnl / abs(down) if abs(down) > 1 else None,
            "has_sell_signal": any(ex < 0 for _, ex in sig_ex.get(u, [])),
        })
    df = pd.DataFrame(rows)
    return df, {"beta": beta, "beta_down": beta_down, "pnls": pnls, "down_name": down_name, "nav": nav}


def _f(v):
    return None if v is None or v != v else float(v)


# ─── ③ 角色 ───────────────────────────────────────────────────────────────────

def load_roles() -> dict[str, str]:
    if not ROLES_PATH.exists():
        return {}
    try:
        return json.loads(ROLES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_roles(roles: dict[str, str]) -> Path:
    ROLES_PATH.write_text(json.dumps(roles, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        from core.github_storage import sync_to_github
        sync_to_github(ROLES_PATH, "data/position_roles.json", "chore: update position roles")
    except Exception:
        pass
    return ROLES_PATH


def suggest_role(r: dict, rr_lo: float, rr_hi: float) -> tuple[str, str]:
    """未标注时的建议角色与理由（核心只能由你标注）。"""
    if r["stock_value"] <= 0:
        return "持有", "只有期权仓位"
    if r.get("beta_down") is not None and r["beta_down"] < 0.5 and (r.get("vol") or 1) < 0.25:
        return "防守", f"下行 β {r['beta_down']:.2f}、波动低"
    if r.get("has_sell_signal"):
        return "可减仓", "当前有 A/B 级卖点信号"
    if r.get("score") is not None and r["score"] < 50:
        return "可减仓", f"评分 {int(r['score'])} < 50"
    if (r.get("score") or 0) >= 80:          # 高分股不因风险收益比被列为可减仓，最多「持有」（可减一半）
        if r.get("rr") is not None and r["rr"] >= rr_hi:
            return "进攻", f"评分 {int(r['score'])}、风险收益比靠前"
        return "持有", f"评分 {int(r['score'])} 高，但下行 β 偏大（风险收益比靠后）"
    if r.get("rr") is not None and r["rr"] <= rr_lo:
        return "可减仓", "风险收益比在持仓后三分之一"
    return "持有", "居中"


def assign_roles(tbl: pd.DataFrame, user_roles: dict[str, str]) -> pd.DataFrame:
    tbl = tbl.copy()
    rr = tbl["rr"].dropna()
    lo, hi = (float(rr.quantile(1 / 3)), float(rr.quantile(0.5))) if len(rr) >= 3 else (-np.inf, np.inf)
    sug = [suggest_role(r, lo, hi) for r in tbl.to_dict("records")]
    tbl["role_suggest"] = [s[0] for s in sug]
    tbl["role_why"] = [s[1] for s in sug]
    tbl["role"] = [user_roles.get(t, s[0]) for t, s in zip(tbl["ticker"], sug)]
    tbl["role_user"] = tbl["ticker"].isin(user_roles)
    return tbl


# ─── ④ 自动减仓方案 ───────────────────────────────────────────────────────────

def auto_trims(tbl: pd.DataFrame, nav: float, target_loss: float) -> tuple[dict[str, float], float, float]:
    """
    目标：下跌情景组合亏损 ≤ target_loss × nav。按风险收益比从低到高减正股（受角色上限约束），整股取整。
    返回 ({ticker: 目标股数}, 方案后下跌情景亏损, 还差多少)。
    """
    loss = float(tbl["down_pnl"].sum())
    need = max(0.0, -loss - target_loss * nav)
    targets = {r.ticker: r.shares for r in tbl.itertuples()}
    order = tbl.assign(_rr=tbl["rr"].fillna(0)).sort_values("_rr")
    for r in order.itertuples():
        if need <= 0:
            break
        cap = MAX_TRIM.get(r.role, 0.0)
        if cap <= 0 or r.shares <= 0 or not r.price or r.stock_value <= 0:
            continue
        stock_down = r.down_pnl * (r.stock_value / r.value) if r.value else 0.0      # 期权部分不动
        if stock_down >= 0:
            continue
        per_share = -stock_down / r.shares
        n = min(r.shares * cap, math.ceil(need / per_share))
        n = math.floor(n) if r.shares >= 1 else n
        if n <= 0:
            continue
        targets[r.ticker] = r.shares - n
        need -= n * per_share
    new_loss = -target_loss * nav - max(need, 0.0) if loss < -target_loss * nav else loss
    return targets, new_loss, max(need, 0.0)


# ─── ⑤ 期权工具 ───────────────────────────────────────────────────────────────

def _pick_expiry(opts: list[dict], after: date, max_after: int = 75, min_dte: int = 0) -> str | None:
    exps = sorted({O.parse_occ(o["option"])["expiry"] for o in opts if O.parse_occ(o.get("option", ""))})
    for e in exps:
        d = date.fromisoformat(e)
        if d >= after and (d - date.today()).days >= min_dte and (d - after).days <= max_after:
            return e
    return None


def _nearest(opts: list[dict], expiry: str, kind: str, target_delta: float) -> dict | None:
    best, bd = None, 9.0
    for o in opts:
        m = O.parse_occ(o.get("option", ""))
        if not m or m["expiry"] != expiry or m["option_type"] != kind:
            continue
        d, bid, ask = o.get("delta"), o.get("bid") or 0, o.get("ask") or 0
        if d is None or not ask or (o.get("open_interest") or 0) < 10:
            continue
        diff = abs(abs(d) - target_delta)
        if diff < bd:
            best, bd = {**o, **m, "mid": (bid + ask) / 2 if bid else ask}, diff
    return best


def _leg(o: dict, side: int, n: int, und: str, spot: float) -> dict:
    T = max((date.fromisoformat(o["expiry"]) - date.today()).days, 0) / 365.0
    return {"contract": o["option"], "underlying": und, "option_type": o["option_type"], "strike": o["strike"],
            "expiry": o["expiry"], "side": side, "contracts": n, "mark": float(o["mid"]), "iv": float(o.get("iv") or 0.4),
            "delta": float(o.get("delta") or 0), "T": T, "S": spot}


def overlay_ideas(ticker: str, shares: float, spot: float, event: date, role: str, chain: dict | None,
                  trimmed: float = 0.0) -> list[dict]:
    """
    给一个标的生成期权方案（每个方案 = 若干腿 + 说明）。股数 < 100 时按 1 张（敞口会偏大，说明里注明）。
      核心 / 进攻：保护性 put、领口、备兑 call、深度实值 call 替代正股
      被减仓的：卖 put 事后回补
    """
    if not chain or not chain.get("options") or not spot:
        return []
    opts = chain["options"]
    after = event + timedelta(days=14)
    exp_near = _pick_expiry(opts, after)
    exp_far = _pick_expiry(opts, date.today() + timedelta(days=150), max_after=120)
    n = max(1, int(shares // 100)) if shares > 0 else 0
    note = "" if shares >= 100 else f"（持股 {shares:g} 股不足 100 股，按 1 张计，保护比例 > 100%）"
    ideas = []
    if exp_near and n and role in ("核心", "进攻", "持有"):
        put = _nearest(opts, exp_near, "put", 0.25)
        call = _nearest(opts, exp_near, "call", 0.20)
        if put:
            ideas.append({"kind": "保护性 put", "ticker": ticker, "legs": [_leg(put, +1, n, ticker, spot)],
                          "why": f"买 {n} 张 {put['strike']:g} put（{exp_near}，Δ{put['delta']:.2f}），"
                                 f"事件后到期；跌破 {put['strike'] / spot - 1:+.0%} 以下的损失由 put 承担" + note})
        if put and call:
            ideas.append({"kind": "领口", "ticker": ticker, "legs": [_leg(put, +1, n, ticker, spot), _leg(call, -1, n, ticker, spot)],
                          "why": f"买 {put['strike']:g} put + 卖 {call['strike']:g} call（{exp_near}），"
                                 f"用卖 call 的权利金抵 put 成本；上方收益封顶在 {call['strike'] / spot - 1:+.0%}" + note})
        if call and shares >= 100:
            ideas.append({"kind": "备兑 call", "ticker": ticker, "legs": [_leg(call, -1, n, ticker, spot)],
                          "why": f"卖 {n} 张 {call['strike']:g} call（{exp_near}，Δ{call['delta']:.2f}）收权利金增厚；"
                                 f"涨超 {call['strike'] / spot - 1:+.0%} 部分让出"})
    if exp_far and shares >= 100 and role in ("核心", "进攻"):
        itm = _nearest(opts, exp_far, "call", 0.80)
        if itm:
            k = max(1, round(shares / 100 / max(itm["delta"], 0.5) * itm["delta"]))
            ideas.append({"kind": "实值 call 替代正股", "ticker": ticker, "sell_shares": shares,
                          "legs": [_leg(itm, +1, k, ticker, spot)],
                          "why": f"卖出全部 {shares:g} 股，买 {k} 张 {itm['strike']:g} call（{exp_far}，Δ{itm['delta']:.2f}）："
                                 f"保留约 {k * 100 * itm['delta'] / shares:.0%} 的上涨敞口，最大亏损 = 权利金"})
    if trimmed >= 100:
        exp_re = _pick_expiry(opts, event + timedelta(days=7), max_after=45)
        if exp_re:
            sp = _nearest(opts, exp_re, "put", 0.25)
            if sp:
                k = int(trimmed // 100)
                ideas.append({"kind": "卖 put 回补", "ticker": ticker, "legs": [_leg(sp, -1, k, ticker, spot)],
                              "why": f"减掉的 {trimmed:g} 股：卖 {k} 张 {sp['strike']:g} put（{exp_re}）收权利金，"
                                     f"事件后若跌到 {sp['strike'] / spot - 1:+.0%} 被行权即低位接回（需备足现金）"})
    return ideas


def index_hedge_ideas(chains: dict[str, dict], closes: dict, ctx: dict, scen: list[dict], event: date,
                      need: float) -> list[dict]:
    """
    指数保护：买 QQQ / SOXX / SPY 的 put（事件后到期，Δ≈0.30），张数按「把下跌情景亏损再降 need 美元」估算。
    每个指数一个方案；按单位成本换来的保护排序（保护 ÷ 权利金越高越好）。
    """
    out = []
    down = min(scen, key=lambda s: s["SPY"])
    bt = R.betas(closes, ["QQQ", "SOXX", "SPY"])
    for idx in ("QQQ", "SOXX", "SPY"):
        ch = chains.get(idx)
        if not ch or not ch.get("options") or not ch.get("price") or need <= 0:
            continue
        spot = float(ch["price"])
        exp_ = _pick_expiry(ch["options"], event + timedelta(days=14))
        put = _nearest(ch["options"], exp_, "put", 0.30) if exp_ else None
        if not put:
            continue
        b = ctx["beta_down"].get(idx) if idx in ctx["beta_down"] else (bt.loc[idx, "beta_SPY"] if idx in bt.index else 1.0)
        b = 1.0 if idx == "SPY" else float(b if b == b and b is not None else 1.0)
        leg = _leg(put, +1, 1, idx, spot)
        days = max((event - date.today()).days + 21, 1)
        T2 = max(leg["T"] - days / 365, 0)
        val_down = O.bs_greeks(spot * (1 + b * down["SPY"]), leg["strike"], T2, max(leg["iv"] + down["IV"], 0.01),
                               option_type="put")["price"]
        gain = (val_down - leg["mark"]) * MULT           # 每张在下跌情景的盈利
        if gain <= 0:
            continue
        k = max(1, math.ceil(need / gain))
        leg["contracts"] = k
        cost = k * leg["mark"] * MULT
        out.append({"kind": "指数保护", "ticker": idx, "legs": [leg], "protect": k * gain, "cost": cost,
                    "why": f"买 {k} 张 {idx} {leg['strike']:g} put（{exp_}，Δ{leg['delta']:.2f}），权利金约 ${cost:,.0f}；"
                           f"下跌情景（{idx} 约 {b * down['SPY']:+.1%}）约赚 ${k * gain:,.0f}，"
                           f"每 $1 权利金换 ${k * gain / cost:.1f} 保护。所有持仓都不用卖"})
    return sorted(out, key=lambda x: -x["protect"] / x["cost"])


def frontier(tbl: pd.DataFrame, nav: float, steps: int = 12) -> pd.DataFrame:
    """只靠减仓时：下跌情景亏损上限 vs 剩余期望盈亏（看每多一分保护要放弃多少期望收益）。"""
    cur = -float(tbl["down_pnl"].sum()) / nav
    rows = []
    for lim in np.linspace(cur, max(cur * 0.25, 0.02), steps):
        targets, new_loss, short = auto_trims(tbl, nav, float(lim))
        kept = sum(r.exp_pnl * (targets[r.ticker] / r.shares if r.shares else 1) if r.stock_value else r.exp_pnl
                   for r in tbl.itertuples())
        rows.append({"limit": float(lim), "down_loss": -new_loss / nav, "exp_pnl": kept, "short": short})
    return pd.DataFrame(rows)


# ─── 模拟后的持仓账本 ─────────────────────────────────────────────────────────

def apply_plan(book: pd.DataFrame, targets: dict[str, float], ideas: list[dict]) -> pd.DataFrame:
    """按目标股数与选中的期权方案生成新账本：卖股所得 / 期权收付计入现金（净值不变）。"""
    b = book.copy()
    cash_delta = 0.0
    sells = {i["ticker"]: i["sell_shares"] for i in ideas if i.get("sell_shares")}
    for i, r in b.iterrows():
        if r["kind"] != "stock":
            continue
        tgt = targets.get(r["ticker"], r["qty"])
        if r["ticker"] in sells:
            tgt = min(tgt, r["qty"] - sells[r["ticker"]])
        tgt = max(tgt, 0.0)
        if abs(tgt - r["qty"]) < 1e-9 or not r["price"]:
            continue
        cash_delta += (r["qty"] - tgt) * r["price"]
        b.at[i, "qty"], b.at[i, "value"], b.at[i, "exposure"] = tgt, tgt * r["price"], tgt * r["price"]
    new_rows = []
    for idea in ideas:
        for lg in idea["legs"]:
            q = lg["side"] * lg["contracts"]
            val = lg["mark"] * MULT * q
            cash_delta -= val
            new_rows.append({"kind": "option", "ticker": lg["contract"], "underlying": lg["underlying"],
                             "display": f"{lg['underlying']} {lg['strike']:g} {lg['option_type']}",
                             "sector": b.loc[b["underlying"] == lg["underlying"], "sector"].iloc[0]
                             if (b["underlying"] == lg["underlying"]).any() else "其他",
                             "qty": q, "price": lg["mark"], "value": val, "exposure": lg["delta"] * lg["S"] * MULT * q,
                             "option_type": lg["option_type"], "strike": lg["strike"], "expiry": lg["expiry"],
                             "T": lg["T"], "iv": lg["iv"], "delta": lg["delta"], "S": lg["S"], "mark": lg["mark"]})
    if new_rows:
        b = pd.concat([b, pd.DataFrame(new_rows)], ignore_index=True)
    if cash_delta:
        if (b["kind"] == "cash").any():
            i = b.index[b["kind"] == "cash"][0]
            b.at[i, "qty"] += cash_delta
            b.at[i, "value"] += cash_delta
        else:
            b = pd.concat([b, pd.DataFrame([{"kind": "cash", "ticker": "CASH", "underlying": None, "display": "现金",
                                             "sector": "现金", "qty": cash_delta, "price": 1.0, "value": cash_delta,
                                             "exposure": 0.0}])], ignore_index=True)
    return b


def summary(book: pd.DataFrame, closes: dict, ctx: dict, scen: list[dict], hdays: int,
            alpha_of: dict[str, float]) -> dict:
    """一本账本的关键指标：β、下行 β、VaR、各情景盈亏、期望盈亏、现金占比。"""
    pr = R.portfolio_risk(book, closes)
    nav = pr["nav"]
    exp = R.exposures(book)
    bd = ctx["beta_down"].reindex(exp.index)
    bd = bd.fillna(ctx["beta"].reindex(exp.index)).fillna(1.0)
    pnls = scenario_pnl(book, ctx["beta"], ctx["beta_down"], scen, days=int(hdays * 7 / 5))
    sp = {k: float(v["pnl"].sum()) for k, v in pnls.items()}
    alpha_pnl = float(sum(exp.get(t, 0.0) * alpha_of.get(t, 0.0) * hdays / 20 for t in exp.index))
    cash = float(book.loc[book["kind"] == "cash", "value"].sum())
    return {"nav": nav, "beta": pr.get("beta_SPY"), "beta_down": float((bd * exp).sum() / nav) if nav else None,
            "var95": pr["var95"], "exposure": float(exp.sum()), "cash": cash, "cash_pct": cash / nav if nav else None,
            "scen": sp, "exp_pnl": sum(s["概率"] * sp[s["情景"]] for s in scen) + alpha_pnl}


# ─── 方案保存 ─────────────────────────────────────────────────────────────────

def save_plan(plan: dict) -> Path:
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    path = PLANS_DIR / f"{stamp}.json"
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    try:
        from core.github_storage import sync_to_github
        sync_to_github(path, f"data/plans/{path.name}", "chore: save rebalance plan")
    except Exception:
        pass
    return path


def list_plans() -> list[dict]:
    if not PLANS_DIR.exists():
        return []
    out = []
    for p in sorted(PLANS_DIR.glob("*.json"), reverse=True):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            d["_file"] = p.name
            out.append(d)
        except Exception:
            continue
    return out
