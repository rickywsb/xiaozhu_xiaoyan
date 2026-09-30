"""core/option_screener.py — 期权机会扫描：卖 Put（现金担保）/ 买 Call（参考 PutFinder）

数据：CBOE 免费延迟期权链（每张合约 bid/ask/IV/delta/theta/OI + 标的现价与 iv30），
      yfinance 日线算实际波动率，yfinance 日历取财报日，core.rating 的综合评分作股票质量。

卖 Put：硬筛选（DTE、delta、未平仓量、价差）→ 指标（年化收益、盈亏平衡、下跌缓冲及其 σ 倍数、
        模型胜率）→ 评分 = 质量^0.4 × 性价比^0.6 × 波动率溢价调整 × 财报闸门
买 Call：硬筛选 → 指标（回本需涨幅及其 σ 倍数、杠杆、每日时间损耗、IV/实际波动、+1σ 回报倍数）
        → 评分 = 质量^0.5 × 便宜程度^0.25 × 回本难度^0.25 × 时间损耗调整 × 财报闸门
胜率为 Black-Scholes 风险中性概率（模型估计），不是历史胜率；期权历史价格不可得，无法回测。
"""

from __future__ import annotations

import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from core import daily_momentum as dm
from core.options import _norm_cdf, parse_occ

_CHAIN_TTL = 1200
_chains: dict[str, tuple[float, dict | None]] = {}
_earn: dict[str, tuple[float, str | None]] = {}

PUT_DEFAULTS = {"dte": (20, 50), "delta": (0.10, 0.35), "min_oi": 100, "max_spread": 0.15, "min_bid": 0.10}
CALL_DEFAULTS = {"dte": (60, 180), "delta": (0.40, 0.75), "min_oi": 100, "max_spread": 0.12, "min_bid": 0.20}


# ─── 数据 ─────────────────────────────────────────────────────────────────────

def chain(root: str) -> dict | None:
    """CBOE 期权链原始数据：{price, iv30, options: [...]}；失败返回 None。缓存 20 分钟。"""
    root = root.upper()
    hit = _chains.get(root)
    if hit and time.time() - hit[0] < _CHAIN_TTL:
        return hit[1]
    out = None
    for host in ("cdn.cboe.com", "www.cboe.com"):
        try:
            r = requests.get(f"https://{host}/api/global/delayed_quotes/options/{root}.json", timeout=15,
                             headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                continue
            d = r.json().get("data", {})
            out = {"price": d.get("current_price") or d.get("close"), "iv30": (d.get("iv30") or 0) / 100 or None,
                   "options": d.get("options", [])}
            break
        except Exception:
            continue
    _chains[root] = (time.time(), out)
    return out


def earnings_date(ticker: str) -> str | None:
    """下一个财报日（yfinance 日历），缓存 12 小时。"""
    hit = _earn.get(ticker)
    if hit and time.time() - hit[0] < 43200:
        return hit[1]
    out = None
    try:
        import logging
        import yfinance as yf
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)      # ETF 没有财报，避免刷 404 日志
        cal = yf.Ticker(ticker).calendar
        ed = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if ed:
            ds = sorted(pd.Timestamp(x).date() for x in ed)
            fut = [d for d in ds if d >= date.today()]
            out = (fut[0] if fut else ds[-1]).isoformat()
    except Exception:
        out = None
    _earn[ticker] = (time.time(), out)
    return out


def realized_vol(tickers: list[str], window: int = 20) -> dict[str, float]:
    closes = dm.fetch_histories(tickers, period="6mo")
    out = {}
    for t, c in closes.items():
        r = np.log(c).diff().dropna().tail(window)
        if len(r) >= window // 2:
            out[t] = float(r.std(ddof=0) * math.sqrt(252))
    return out


def fetch_all(tickers: list[str], workers: int = 8) -> dict[str, dict]:
    """并发抓取期权链 + 财报日。"""
    us = [t.upper() for t in tickers if "." not in t and not t.startswith("^")]
    with ThreadPoolExecutor(workers) as ex:
        chains = dict(zip(us, ex.map(chain, us)))
        earns = dict(zip(us, ex.map(earnings_date, us)))
    return {t: {**c, "earnings": earns.get(t)} for t, c in chains.items() if c and c.get("options")}


# ─── 合约筛选与指标 ───────────────────────────────────────────────────────────

def _contracts(root: str, ch: dict, kind: str, p: dict) -> pd.DataFrame:
    today = date.today()
    rows = []
    for o in ch["options"]:
        occ = parse_occ(o.get("option", ""))
        if not occ or occ["option_type"] != kind or occ["root"] != root:
            continue
        dte = (date.fromisoformat(occ["expiry"]) - today).days
        if not (p["dte"][0] <= dte <= p["dte"][1]):
            continue
        bid, ask, delta = o.get("bid") or 0.0, o.get("ask") or 0.0, o.get("delta")
        if bid < p["min_bid"] or ask <= bid or delta is None:
            continue
        if not (p["delta"][0] <= abs(delta) <= p["delta"][1]):
            continue
        mid = (bid + ask) / 2
        oi = o.get("open_interest") or 0
        if oi < p["min_oi"] or (ask - bid) / mid > p["max_spread"]:
            continue
        rows.append({"contract": o["option"], "expiry": occ["expiry"], "dte": dte, "strike": occ["strike"],
                     "bid": bid, "ask": ask, "mid": mid, "iv": o.get("iv") or None, "delta": delta,
                     "theta": o.get("theta"), "oi": oi, "volume": o.get("volume") or 0,
                     "spread": (ask - bid) / mid})
    return pd.DataFrame(rows)


def _prob_above(S: float, K: float, sigma: float, T: float, r: float = 0.045) -> float | None:
    """风险中性下到期价格高于 K 的概率 N(d2)。"""
    if not (S and K and sigma and T) or sigma <= 0 or T <= 0:
        return None
    d2 = (math.log(S / K) + (r - 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return _norm_cdf(d2)


def _quality(t: str, ratings: dict) -> tuple[float, int | None, str]:
    r = ratings.get(t)
    if not r:
        return 0.5, None, ""
    q = r["score"] / 100
    if r.get("action") in ("考虑减仓", "回避"):
        q *= 0.6
    return q, int(r["score"]), r.get("action") or ""


def screen_puts(data: dict[str, dict], ratings: dict, rv: dict[str, float], p: dict | None = None) -> pd.DataFrame:
    p = {**PUT_DEFAULTS, **(p or {})}
    out = []
    for t, ch in data.items():
        S = ch["price"]
        df = _contracts(t, ch, "put", p)
        if df.empty or not S:
            continue
        q, score, action = _quality(t, ratings)
        vrp = (ch["iv30"] / rv[t]) if ch.get("iv30") and rv.get(t) else None
        vrp_adj = float(np.clip(0.85 + 0.15 * ((vrp or 1) - 1) / 0.5, 0.7, 1.15))
        earn = ch.get("earnings")
        for r in df.itertuples():
            T = r.dte / 365
            be = r.strike - r.mid
            sig = r.iv or ch.get("iv30") or 0.4
            buf = 1 - be / S
            buf_sigma = math.log(S / be) / (sig * math.sqrt(T)) if be > 0 else None
            ann = r.mid / r.strike * 365 / r.dte
            pop = _prob_above(S, be, sig, T)
            before_earn = bool(earn and earn <= r.expiry)
            y = float(np.clip(ann / 0.30, 0, 1.5))
            b = float(np.clip((buf_sigma or 0) / 1.0, 0, 1.5))
            fit = math.sqrt(y * b) / 1.5
            sc = 100 * (q ** 0.4) * (fit ** 0.6) * vrp_adj * (0.5 if before_earn else 1.0)
            out.append({"ticker": t, "contract": r.contract, "expiry": r.expiry, "dte": r.dte, "strike": r.strike,
                        "price": S, "mid": r.mid, "bid": r.bid, "ask": r.ask, "premium": r.mid * 100,
                        "collateral": r.strike * 100, "ann_yield": ann, "breakeven": be, "buffer": buf,
                        "buffer_sigma": buf_sigma, "pop": pop, "assign_prob": abs(r.delta), "delta": r.delta,
                        "iv": sig, "iv30": ch.get("iv30"), "rv20": rv.get(t), "vrp": vrp, "oi": r.oi,
                        "spread": r.spread, "earnings": earn, "before_earn": before_earn,
                        "quality": score, "action": action, "score": float(np.clip(sc, 0, 100))})
    return pd.DataFrame(out)


def screen_calls(data: dict[str, dict], ratings: dict, rv: dict[str, float], p: dict | None = None) -> pd.DataFrame:
    p = {**CALL_DEFAULTS, **(p or {})}
    out = []
    for t, ch in data.items():
        S = ch["price"]
        df = _contracts(t, ch, "call", p)
        if df.empty or not S:
            continue
        q, score, action = _quality(t, ratings)
        earn = ch.get("earnings")
        for r in df.itertuples():
            T = r.dte / 365
            sig = r.iv or ch.get("iv30") or 0.4
            be = r.strike + r.mid
            be_move = be / S - 1
            be_sigma = math.log(be / S) / (sig * math.sqrt(T))
            lev = r.delta * S / r.mid
            theta_day = abs(r.theta or 0) / r.mid
            intrinsic = max(S - r.strike, 0)
            s1 = S * math.exp(sig * math.sqrt(T))                    # 到期时 +1σ
            rr1 = (max(s1 - r.strike, 0) - r.mid) / r.mid
            pop = _prob_above(S, be, sig, T)
            iv_rv = sig / rv[t] if rv.get(t) else None
            cheap = float(np.clip(1.25 - 0.25 * (iv_rv or 1), 0.5, 1.1)) / 1.1
            ease = float(np.clip(1 - 0.6 * max(be_sigma, 0), 0.15, 1))
            theta_adj = float(np.clip(1 - theta_day * 20, 0.5, 1))
            before_earn = bool(earn and earn <= r.expiry)
            sc = 100 * (q ** 0.5) * (cheap ** 0.25) * (ease ** 0.25) * theta_adj * (0.7 if before_earn else 1.0)
            out.append({"ticker": t, "contract": r.contract, "expiry": r.expiry, "dte": r.dte, "strike": r.strike,
                        "price": S, "mid": r.mid, "bid": r.bid, "ask": r.ask, "premium": r.mid * 100,
                        "breakeven": be, "be_move": be_move, "be_sigma": be_sigma, "leverage": lev,
                        "theta_day": theta_day, "time_value": (r.mid - intrinsic) / r.mid, "rr_1sigma": rr1,
                        "pop": pop, "delta": r.delta, "iv": sig, "rv20": rv.get(t), "iv_rv": iv_rv,
                        "oi": r.oi, "spread": r.spread, "earnings": earn, "before_earn": before_earn,
                        "quality": score, "action": action, "score": float(np.clip(sc, 0, 100))})
    return pd.DataFrame(out)


def best_per_ticker(df: pd.DataFrame) -> pd.DataFrame:
    """每个标的只保留评分最高的一张合约，按评分排序。"""
    if df.empty:
        return df
    return (df.sort_values("score", ascending=False).groupby("ticker", as_index=False).first()
              .sort_values("score", ascending=False).reset_index(drop=True))


# ─── 时机判断 ─────────────────────────────────────────────────────────────────

def timing(posture: dict, puts: pd.DataFrame, calls: pd.DataFrame) -> dict:
    """
    卖 Put：市场非下降趋势，且期权偏贵（VIX 分位高 / 波动率溢价中位数 >1.1）更有利。
    买 Call：市场上升趋势，且期权偏便宜（VIX 分位低 / IV 相对实际波动率不高）更有利。
    """
    items = (posture or {}).get("items", {})
    trend = items.get("趋势", {}).get("score", 50)
    vix_score = items.get("波动", {}).get("score", 50)            # 越高 = VIX 越低
    vrp_med = float(puts.drop_duplicates("ticker")["vrp"].median()) if not puts.empty and puts["vrp"].notna().any() else None
    ivrv_med = float(calls.drop_duplicates("ticker")["iv_rv"].median()) if not calls.empty and calls["iv_rv"].notna().any() else None

    sp = 0
    sp += 1 if trend >= 40 else -1
    sp += 1 if vix_score <= 50 else 0
    sp += 1 if (vrp_med or 1) >= 1.1 else (-1 if (vrp_med or 1) < 0.9 else 0)
    bc = 0
    bc += 1 if trend >= 60 else (-1 if trend < 40 else 0)
    bc += 1 if vix_score >= 60 else (-1 if vix_score < 35 else 0)
    bc += 1 if (ivrv_med or 1) <= 1.0 else (-1 if (ivrv_med or 1) > 1.3 else 0)
    lab = lambda x: "🟢 有利" if x >= 2 else ("🟡 一般" if x >= 0 else "🔴 不利")
    return {
        "sell_put": lab(sp), "buy_call": lab(bc), "vrp_med": vrp_med, "ivrv_med": ivrv_med,
        "trend": trend, "vix_score": vix_score,
        "sell_put_why": f"趋势 {trend:.0f} · VIX 分项 {vix_score:.0f}（低=期权贵）· 波动率溢价中位数 "
                        + (f"{vrp_med:.2f}" if vrp_med else "—"),
        "buy_call_why": f"趋势 {trend:.0f} · VIX 分项 {vix_score:.0f}（高=期权便宜）· IV/实际波动中位数 "
                        + (f"{ivrv_med:.2f}" if ivrv_med else "—"),
    }


def us_only(tickers) -> list[str]:
    """CBOE 只有美国上市期权：去掉带交易所后缀的、指数、现金，以及杠杆 / 反向产品。"""
    return sorted({t.upper() for t in tickers if t and "." not in t and not t.startswith("^")
                   and t.upper() != config.CASH_TICKER and t.upper() not in config.LEVERAGED_TICKERS})
