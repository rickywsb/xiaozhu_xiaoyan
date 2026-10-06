"""core/sector_live.py — 板块雷达的实时层：成分股（ETF 真实前十大持仓 ∪ S&P 500 行业映射 ∪ 自定义篮子）、
板块盘中统计、成分股指标与龙头候选、每日龙头记录（日后检验）

龙头候选（O'Neil：领先板块里的领先股）——只用已检验 / 可解释的指标，不另造未检验的打分：
  ① 综合评分（5 成分，全市场百分位；已检验）为主排序
  ② 相对板块强度：20 日收益 − 板块 ETF 20 日收益 > 0（跑赢自己的板块）为入选门槛
  ③ 理由里附：距 52 周高、今日相对板块、预计量比、今日 A/B 信号、营收 / EPS 同比、ETF 权重（权重只展示，不参与排序）
每天记录各板块的龙头候选（data/sector_leaders.json），积累几周后检验它们是否跑赢所在板块。
"""

from __future__ import annotations

import json
import time
from datetime import datetime

import pandas as pd

import config
from core import daily_momentum as dm
from core import sectors as sc

LEADERS_PATH = config.DATA_DIR / "sector_leaders.json"
_hold_cache: dict[str, tuple[float, dict]] = {}
_HOLD_TTL = 86400
_last_sync = [0.0]


# ─── 成分股 ───────────────────────────────────────────────────────────────────

def etf_holdings(etf: str) -> dict[str, tuple[str, float]]:
    """ETF 前十大持仓 {ticker: (名称, 权重)}（yfinance funds_data，缓存 1 天；失败返回空）。"""
    hit = _hold_cache.get(etf)
    if hit and time.time() - hit[0] < _HOLD_TTL:
        return hit[1]
    out = {}
    try:
        import yfinance as yf
        th = yf.Ticker(etf).funds_data.top_holdings
        for sym, row in th.iterrows():
            t = str(sym).upper().replace(".", "-")
            out[t] = (str(row.iloc[0]), float(row.iloc[-1]))
    except Exception:
        out = {}
    _hold_cache[etf] = (time.time(), out)
    return out


def members(key: str, sp500: pd.DataFrame | None) -> pd.DataFrame:
    """板块成分：DataFrame[ticker, name, weight(ETF 前十大才有), source]。"""
    base = sc.members_of(key, sp500)
    hold = etf_holdings(key) if key not in config.CUSTOM_BASKETS else {}
    rows = {t: {"ticker": t, "name": n, "weight": None, "source": "行业映射"} for t, n in base.items()}
    for t, (n, w) in hold.items():
        r = rows.setdefault(t, {"ticker": t, "name": n, "weight": None, "source": "ETF 持仓"})
        r["weight"] = w
        if r["name"] == t:
            r["name"] = n
    return pd.DataFrame(list(rows.values()))


# ─── 板块盘中统计 ─────────────────────────────────────────────────────────────

def sector_live_stats(board: pd.DataFrame, mem_of: dict[str, pd.DataFrame], quotes: pd.DataFrame,
                      etf_q: pd.DataFrame) -> pd.DataFrame:
    """
    每个板块：今日涨跌（ETF 实时；自定义篮子 = 成分等权）、成分上涨比例、放量成分数（预计量比 ≥1.5）、
    今日 vs 20 日的背离提示。quotes = core.intraday 的成分股今日数据；etf_q = ETF 实时报价。
    """
    q = quotes.set_index("ticker") if not quotes.empty else pd.DataFrame()
    e = etf_q.set_index("ticker") if not etf_q.empty else pd.DataFrame()
    rows = []
    for r in board.itertuples():
        m = mem_of.get(r.key, pd.DataFrame())
        tick = [t for t in (m["ticker"] if not m.empty else []) if t in q.index]
        chg_m = q.loc[tick, "chg"].dropna() if tick else pd.Series(dtype=float)
        pvr_m = q.loc[tick, "pvr"].dropna() if tick else pd.Series(dtype=float)
        today = float(e.at[r.key, "chg"]) if r.key in e.index and pd.notna(e.at[r.key, "chg"]) else (
            float(chg_m.mean()) if len(chg_m) else None)
        flag = ""
        if today is not None and r.ret_20d == r.ret_20d:
            if r.ret_20d > 0.03 and today < -0.015:
                flag = "强势回调"
            elif r.ret_20d < -0.03 and today > 0.015:
                flag = "弱势反弹"
        rows.append({"key": r.key, "today": today, "up_pct": float((chg_m > 0).mean()) if len(chg_m) else None,
                     "n_members": len(tick), "n_surge": int((pvr_m >= 1.5).sum()), "flag": flag})
    df = pd.DataFrame(rows)
    df["today_rank"] = df["today"].rank(ascending=False, method="min")
    return df


# ─── 成分股指标与龙头候选 ─────────────────────────────────────────────────────

def member_table(key: str, mem: pd.DataFrame, board_row: dict, quotes: pd.DataFrame, panel_close: pd.DataFrame,
                 live: pd.DataFrame, ratings: pd.DataFrame, ratings_close: pd.DataFrame,
                 isig: dict) -> pd.DataFrame:
    """
    成分股一行一只：价格 / 今日（live 实时报价优先）、相对板块今日、20/60 日、相对板块 20 日、距 52 周高、
    预计量比、评分（盘中预估或收盘）及较昨收、营收 / EPS 同比、今日信号、ETF 权重。
    """
    if mem.empty:
        return pd.DataFrame()
    tick = list(mem["ticker"])
    extra = [t for t in tick if t not in panel_close.columns]
    hist = dm.fetch_histories(extra, period="1y") if extra else {}
    q = quotes.set_index("ticker") if not quotes.empty else pd.DataFrame()
    lq = live.set_index("ticker") if live is not None and not live.empty else pd.DataFrame()
    rt = ratings.set_index("ticker") if not ratings.empty else pd.DataFrame()
    rc = ratings_close.set_index("ticker") if not ratings_close.empty else pd.DataFrame()
    etf_today = board_row.get("today")
    rows = []
    for m in mem.itertuples():
        t = m.ticker
        c = panel_close[t].dropna() if t in panel_close.columns else hist.get(t)
        if c is None or len(c) < 25:
            continue
        price = float(lq.at[t, "last"]) if t in lq.index else (float(q.at[t, "close"]) if t in q.index else float(c.iloc[-1]))
        chg = float(lq.at[t, "chg"]) if t in lq.index else (float(q.at[t, "chg"]) if t in q.index else None)
        ret = lambda n: float(price / c.iloc[-n] - 1) if len(c) > n else None
        r20, r60 = ret(20), ret(60)
        hi = float(max(c.tail(252).max(), price))
        sc_ = rt.at[t, "score"] if t in rt.index else None
        sc0 = rc.at[t, "score"] if t in rc.index else None
        g = lambda k: (rc.at[t, k] if t in rc.index and k in rc.columns else None)
        sigs = isig.get(t, [])
        rows.append({
            "ticker": t, "name": m.name, "weight": _num(m.weight), "source": m.source,
            "price": price, "chg": chg, "vs_etf_today": (chg - etf_today) if chg is not None and etf_today is not None else None,
            "ret20": r20, "ret60": r60,
            "rel20": (r20 - board_row["ret_20d"]) if r20 is not None and board_row.get("ret_20d") == board_row.get("ret_20d") else None,
            "rel60": (r60 - board_row["ret_60d"]) if r60 is not None and board_row.get("ret_60d") == board_row.get("ret_60d") else None,
            "off_high": price / hi - 1, "pvr": float(q.at[t, "pvr"]) if t in q.index and q.at[t, "pvr"] == q.at[t, "pvr"] else None,
            "score": _num(sc_), "score_delta": (_num(sc_) - _num(sc0)) if _num(sc_) is not None and _num(sc0) is not None else None,
            "eps_yoy": _num(g("eps_yoy")), "rev_yoy": _num(g("rev_yoy")),
            "signals": " · ".join(f"{'▲' if s['expect'] > 0 else '▼'}{s['signal']} {s['grade']}" for s in sigs),
        })
    return pd.DataFrame(rows)


def _num(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return None if v != v else v


def leaders(mt: pd.DataFrame, n: int = 3) -> pd.DataFrame:
    """龙头候选：跑赢板块（相对 20 日 > 0）里评分最高的 n 只；不足 n 只时按评分补足。附理由。"""
    if mt.empty:
        return mt
    ok = mt[mt["score"].notna()].copy()
    ok["_beat"] = ok["rel20"].fillna(-1) > 0
    ok = ok.sort_values(["_beat", "score", "rel20"], ascending=[False, False, False]).head(n)
    ok["why"] = [_why(r) for r in ok.to_dict("records")]
    return ok.drop(columns="_beat")


def _ok(v) -> bool:
    return v is not None and v == v


def _why(r: dict) -> str:
    p = lambda v: f"{v * 100:+.0f}%"
    d = r.get("score_delta")
    parts = [f"评分 {int(r['score'])}" + (f"（较昨收 {int(d):+d}）" if d is not None and d == d and d else "")]
    if _ok(r.get("rel20")):
        parts.append(f"20 日{'跑赢' if r['rel20'] > 0 else '落后'}板块 {p(r['rel20'])}")
    if _ok(r.get("off_high")):
        parts.append("创 52 周新高" if r["off_high"] > -0.005 else f"距 52 周高 {p(r['off_high'])}")
    if _ok(r.get("vs_etf_today")) and abs(r["vs_etf_today"]) >= 0.005:
        parts.append(f"今日{'强于' if r['vs_etf_today'] > 0 else '弱于'}板块 {r['vs_etf_today'] * 100:+.1f}%")
    if _ok(r.get("pvr")) and r["pvr"] >= 1.5:
        parts.append(f"预计量比 {r['pvr']:.1f}×")
    if _ok(r.get("rev_yoy")):
        parts.append(f"营收同比 {p(r['rev_yoy'])}")
    if r.get("signals"):
        parts.append(r["signals"])
    if _ok(r.get("weight")):
        parts.append(f"ETF 权重 {r['weight'] * 100:.1f}%")
    return " · ".join(parts)


# ─── 每日龙头记录 ─────────────────────────────────────────────────────────────

def log_leaders(day: str, by_sector: dict[str, list[dict]], status: str) -> None:
    """覆盖写入今天各板块的龙头候选；收盘后或每小时最多同步一次 GitHub。"""
    try:
        d = json.loads(LEADERS_PATH.read_text(encoding="utf-8")) if LEADERS_PATH.exists() else {}
    except Exception:
        d = {}
    d[day] = {"time": datetime.now().isoformat(timespec="seconds"), "status": status, "sectors": by_sector}
    LEADERS_PATH.write_text(json.dumps(d, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    if status == "已收盘" or time.time() - _last_sync[0] > 3600:
        _last_sync[0] = time.time()
        try:
            from core.github_storage import sync_to_github
            sync_to_github(LEADERS_PATH, "data/sector_leaders.json", "chore: sector leaders")
        except Exception:
            pass
