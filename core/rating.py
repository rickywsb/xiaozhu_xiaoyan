"""core/rating.py — 综合评分（0–99，全市场百分位）+ 操作倾向 + 量能状态 + 回测验证

股票池 = 选股器股票池（S&P 500 ∪ Nasdaq-100 ∪ 自定义篮子 ∪ 持仓 / 关注，约 540 只）。
全部指标在「日期 × 股票」矩阵上向量化计算，每天在全市场横截面取百分位：

  趋势 T  IBD 式相对强度（3/6/9/12 月收益加权）、距 52 周高、站上 MA50 / MA200
  量能 V  50 日涨跌量比（上涨日量 / 下跌日量）、近 20 日 A 级放量信号次数（放量上涨 / 放量突破 20 日高）、
          近 25 日派发日数（跌 ≥0.2% 且量大于前一日，越少越好）
  板块 S  所属板块（SPDR 行业 / 主题 ETF / 自定义篮子）60 日相对 SPY 收益在板块间的百分位
  健康 H  EMA10/20/60 量能分（与量能健康页同口径）、距 20 日高点的回撤（以 ATR 计，越小越好）

综合 = Σ 权重 × 成分，再取百分位 → 1–99。
定位：**强势筛选**——历史检验显示超额集中在 ≥90 分（前 10%），90 分以下只作描述性排名，不代表好坏。
验证：按分数分十组，看随后 20 个交易日相对股票池平均的超额是否单调；
      头尾组差用互不重叠的样本日（每 20 个交易日）计算 t 值；前后两段分别检验稳定性。
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from core import daily_momentum as dm

PERIOD = "2y"
# 四项等权：回测样本（约 22 个独立期）太少，不据此挑"最优"权重，避免过拟合；每周重跑验证后再议
WEIGHTS = {"趋势": 0.25, "量能": 0.25, "板块": 0.25, "健康": 0.25}
TOP_TIER = 90             # 强势筛选门槛：验证显示超额集中在前 10%
HORIZON = 20
RATING_PATH = config.DATA_DIR / "rating_latest.json"
VALIDATION_PATH = config.DATA_DIR / "rating_validation.json"


# ─── 数据 ─────────────────────────────────────────────────────────────────────

def load_panel(tickers: list[str], period: str = PERIOD, chunk: int = 100, progress=None) -> dict[str, pd.DataFrame]:
    """{"close","high","low","volume","close_usd"}：日期 × 股票 矩阵（剔除盘中未收完的 K 线）。"""
    data: dict[str, pd.DataFrame] = {}
    for i in range(0, len(tickers), chunk):
        if progress:
            progress(i, len(tickers))
        data.update(dm.fetch_ohlcv_histories(tickers[i:i + chunk], period=period, complete_bars_only=True))
    panel = {f.lower(): pd.DataFrame({t: d[f] for t, d in data.items() if f in d}).sort_index()
             for f in ("Close", "High", "Low", "Volume")}
    fx = dm._fx_per_usd({config.CURRENCY_MAP[t] for t in data if t in config.CURRENCY_MAP}, period)
    panel["close_usd"] = pd.DataFrame({t: dm._to_usd(d["Close"], t, fx) for t, d in data.items()}).sort_index()
    # 只保留过半股票有数据的交易日：美股盘中时亚洲股票已收盘，否则最后一天只剩几只亚洲股票参与排名
    close = panel["close"]
    good = close.index[close.notna().sum(axis=1) >= 0.5 * close.shape[1]]
    return {k: v.reindex(good) for k, v in panel.items()}


def sector_strength(sector_keys: set[str], period: str = PERIOD) -> pd.DataFrame:
    """板块 key × 日期 的 60 日相对 SPY 收益，在板块间取百分位（0–1）。"""
    from core import sectors as sc
    series = sc.sector_series(period)
    spy = series.get(sc.MARKET)
    if spy is None:
        return pd.DataFrame()
    rel = {}
    for k in sector_keys:
        s = series.get(k)
        if s is None:
            continue
        both = pd.concat([s, spy], axis=1, join="inner").dropna()
        rel[k] = (both.iloc[:, 0] / both.iloc[:, 0].shift(60)) - (both.iloc[:, 1] / both.iloc[:, 1].shift(60))
    return pd.DataFrame(rel).sort_index().rank(axis=1, pct=True)


# ─── 特征 ─────────────────────────────────────────────────────────────────────

def features(panel: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    c, h, lo, v = panel["close"], panel["high"], panel["low"], panel["volume"].fillna(0)
    r = c.pct_change(fill_method=None)
    ret = lambda n: c / c.shift(n) - 1

    # 趋势：IBD 式 RS（缺长期数据时按可用权重归一）
    parts = [(0.4, ret(63)), (0.2, ret(126)), (0.2, ret(189)), (0.2, ret(252))]
    num = sum(w * x.fillna(0) for w, x in parts)
    den = sum(w * x.notna() for w, x in parts)
    rs = (num / den).where(den >= 0.4)
    ma50, ma200 = c.rolling(50).mean(), c.rolling(200).mean()
    hi252 = c.rolling(252, min_periods=120).max()

    # 量能
    up_v = v.where(r > 0, 0).rolling(50).sum()
    dn_v = v.where(r < 0, 0).rolling(50).sum()
    ud50 = up_v / dn_v.replace(0, np.nan)
    vr = v / v.shift(1).rolling(50).mean()
    hi20_prev = c.shift(1).rolling(20).max()
    a_sig = ((vr >= 2) & (r >= 0.03)) | ((c > hi20_prev) & (vr >= 1.5))
    dist_day = (r <= -0.002) & (v > v.shift(1))

    # 健康：与 daily_momentum.ema_momentum 同口径的 EMA 量能分（10/20/60）
    e10, e20, e60 = (c.ewm(span=s, adjust=False).mean() for s in (10, 20, 60))
    dev = c / e20 - 1
    pos = (50 + dev / 0.05 * 50).clip(0, 100)
    align = ((e10 > e20).astype(float) + (e20 > e60).astype(float)) / 2 * 100
    slope = (50 + (e20 / e20.shift(5) - 1) / 0.03 * 50).clip(0, 100)
    devs = (100 - (dev - 0.08).clip(lower=0) / 0.12 * 100).clip(0, 100)
    ema_score = 0.30 * pos + 0.30 * align + 0.25 * slope + 0.15 * devs
    tr = pd.concat({"a": h - lo, "b": (h - c.shift()).abs(), "c": (lo - c.shift()).abs()}).groupby(level=1).max()
    tr = tr.reindex(c.index)
    atr = tr.rolling(14).mean()
    dd_atr = (c.rolling(20).max() - c) / atr

    valid = c.notna() & (c.notna().cumsum() >= 60)      # 至少 60 根 K 线才参与评分
    return {
        "rs": rs, "dist_hi": c / hi252 - 1,
        "above50": (c > ma50).astype(float).where(ma50.notna()),
        "above200": (c > ma200).astype(float).where(ma200.notna()),
        "ud50": ud50, "vol_sig20": a_sig.astype(float).rolling(20).sum(),
        "dist25": dist_day.astype(float).rolling(25).sum(),
        "ema_score": ema_score, "dd_atr": dd_atr, "vr": vr, "ret1": r,
        "valid": valid,
    }


def _pct(df: pd.DataFrame, valid: pd.DataFrame) -> pd.DataFrame:
    return df.where(valid).rank(axis=1, pct=True)


def components(feat: dict[str, pd.DataFrame], sector_of: dict[str, str | None],
               sec_pct: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """四个成分（0–1，全市场横截面）。"""
    ok = feat["valid"]
    T = pd.concat([_pct(feat["rs"], ok), _pct(feat["dist_hi"], ok),
                   feat["above50"].where(ok), feat["above200"].where(ok)], keys=range(4)).groupby(level=1).mean()
    V = pd.concat([_pct(feat["ud50"], ok), _pct(feat["vol_sig20"], ok),
                   1 - _pct(feat["dist25"], ok)], keys=range(3)).groupby(level=1).mean()
    H = pd.concat([_pct(feat["ema_score"], ok), 1 - _pct(feat["dd_atr"], ok)], keys=range(2)).groupby(level=1).mean()
    idx, cols = feat["rs"].index, feat["rs"].columns
    sp = sec_pct.reindex(idx).ffill() if not sec_pct.empty else pd.DataFrame(index=idx)
    S = pd.DataFrame({t: sp[k] if (k and k in sp) else pd.Series(0.5, index=idx) for t, k in
                      ((t, sector_of.get(t)) for t in cols)}, index=idx).where(ok)
    return {"趋势": T.reindex(idx), "量能": V.reindex(idx), "板块": S, "健康": H.reindex(idx)}


def composite(comp: dict[str, pd.DataFrame], weights: dict[str, float] = WEIGHTS) -> pd.DataFrame:
    """加权合成后取全市场百分位 → 1–99 分。"""
    total = sum(weights[k] * comp[k] for k in weights)
    return (total.rank(axis=1, pct=True) * 98 + 1).round()


# ─── 操作倾向与量能状态 ───────────────────────────────────────────────────────

def health_label(score: float, score_5d: float | None, above50: float | None, ema_score: float | None,
                 dist25: float | None, held: bool = True) -> str:
    """
    持仓：持有 / 注意 / 考虑减仓；关注：可关注 / 等待 / 回避。
      考虑减仓：评分 < 40；或跌破 MA50 且 EMA 量能分 < 40（A 级看空信号）
      注意    ：评分 40–59；或 5 日内降 ≥10 分；或跌破 MA50；或近 25 日派发日 ≥6
    """
    if not held:
        if score >= 70 and above50:
            return "可关注"
        return "回避" if score < 40 else "等待"
    if score < 40 or (above50 == 0 and ema_score is not None and ema_score < 40):
        return "考虑减仓"
    if (score < 60 or (score_5d is not None and score_5d - score >= 10) or above50 == 0
            or (dist25 is not None and dist25 >= 6)):
        return "注意"
    return "持有"


def volume_state(vr: float | None, ret1: float | None, ud50: float | None, vol_sig20: float | None,
                 dist25: float | None, vr5: float | None) -> str:
    """放量吸筹 / 放量派发 / 缩量整理 / 正常。"""
    if vr is not None and ret1 is not None and vr >= 1.5 and ret1 <= -0.02:
        return "放量派发"
    if dist25 is not None and dist25 >= 6 and (ud50 or 1) < 1:
        return "放量派发"
    if (vr is not None and ret1 is not None and vr >= 1.5 and ret1 >= 0.02) or \
            ((vol_sig20 or 0) >= 1 and (ud50 or 0) >= 1.2):
        return "放量吸筹"
    if vr5 is not None and vr5 < 0.8:
        return "缩量整理"
    return "正常"


# ─── 回测验证 ─────────────────────────────────────────────────────────────────

def _grade(spread: float, t: float | None, rho: float, h1: float | None, h2: float | None) -> str:
    both = h1 is not None and h2 is not None and h1 > 0 and h2 > 0
    if spread > 0 and t is not None and t >= 2 and rho >= 0.7 and both:
        return "A 可靠"
    if spread > 0 and both and ((t is not None and t >= 1.5) or rho >= 0.6):
        return "B 参考"
    if spread < 0 and t is not None and t <= -2:
        return "D 反向"
    return "C 噪音"


def _spearman(a: pd.Series, b: pd.Series) -> float:
    """Spearman 秩相关 = 排名后的 Pearson 相关（不依赖 scipy）。"""
    return float(a.rank().reset_index(drop=True).corr(b.rank().reset_index(drop=True)))


def evaluate(score: pd.DataFrame, close_usd: pd.DataFrame, horizon: int = HORIZON,
             step: int = 5, start_min_names: int = 200) -> dict:
    """
    十分组检验：每 step 个交易日取一个样本日，按分数分十组，统计随后 horizon 日相对股票池平均的超额。
    返回 deciles（各组平均超额）、spread（头−尾）、t（非重叠样本）、rho（组号与超额的 Spearman）、
    ic（日度秩相关均值）、前后两段 spread、grade。
    """
    px = close_usd.reindex(score.index).ffill(limit=3)
    fwd = px.shift(-horizon) / px - 1
    ex = fwd.sub(fwd.mean(axis=1), axis=0)
    dates = [d for d in score.index[::step]
             if score.loc[d].notna().sum() >= start_min_names and ex.loc[d].notna().sum() >= start_min_names]
    rows, ics = [], []
    for d in dates:
        s, e = score.loc[d], ex.loc[d]
        m = s.notna() & e.notna()
        if m.sum() < start_min_names:
            continue
        dec = pd.qcut(s[m].rank(method="first"), 10, labels=False) + 1
        g = e[m].groupby(dec).mean()
        rows.append(g.rename(d))
        ics.append(_spearman(s[m], e[m]))
    if not rows:
        return {}
    tab = pd.DataFrame(rows)
    spread_s = tab[10] - tab[1]
    nonoverlap = spread_s.iloc[::max(1, horizon // step)]
    t = float(nonoverlap.mean() / (nonoverlap.std(ddof=1) / math.sqrt(len(nonoverlap)))) \
        if len(nonoverlap) > 2 and nonoverlap.std(ddof=1) > 0 else None
    means = tab.mean()
    rho = _spearman(pd.Series(means.index, dtype=float), pd.Series(means.values))
    half = len(spread_s) // 2
    h1 = float(spread_s.iloc[:half].mean()) if half else None
    h2 = float(spread_s.iloc[half:].mean()) if half else None
    spread = float(spread_s.mean())
    return {
        "deciles": {int(k): float(v) for k, v in means.items()},
        "spread": spread, "t": t, "rho": rho,
        "ic": float(np.nanmean(ics)), "h1": h1, "h2": h2,
        "n_dates": len(tab), "n_nonoverlap": len(nonoverlap),
        "start": str(tab.index[0].date()), "end": str(tab.index[-1].date()),
        "grade": _grade(spread, t, rho, h1, h2),
    }


def evaluate_mask(mask: pd.DataFrame, close_usd: pd.DataFrame, horizon: int = HORIZON,
                  step: int = 5, min_names: int = 200) -> dict:
    """
    把布尔矩阵（如 评分 ≥90、操作倾向=考虑减仓）当作信号：样本日内被选中股票随后 horizon 日
    相对股票池平均的超额；非重叠样本 t 值；前后两段；胜率（跑赢同日中位数）。
    """
    px = close_usd.reindex(mask.index).ffill(limit=3)
    fwd = px.shift(-horizon) / px - 1
    ex = fwd.sub(fwd.mean(axis=1), axis=0)
    med = fwd.sub(fwd.median(axis=1), axis=0)
    vals, hits, counts = {}, [], []
    for d in mask.index[::step]:
        if ex.loc[d].notna().sum() < min_names:
            continue
        m = mask.loc[d].fillna(False).astype(bool) & ex.loc[d].notna()
        if m.sum() < 5:
            continue
        vals[d] = float(ex.loc[d][m].mean())
        hits.append(float((med.loc[d][m] > 0).mean()))
        counts.append(int(m.sum()))
    if len(vals) < 4:
        return {}
    ser = pd.Series(vals)
    no = ser.iloc[::max(1, horizon // step)]
    t = float(no.mean() / (no.std(ddof=1) / math.sqrt(len(no)))) if len(no) > 2 and no.std(ddof=1) > 0 else None
    half = len(ser) // 2
    return {"excess": float(ser.mean()), "t": t, "h1": float(ser.iloc[:half].mean()),
            "h2": float(ser.iloc[half:].mean()), "hit": float(np.mean(hits)), "avg_n": float(np.mean(counts)),
            "n_dates": len(ser), "start": str(ser.index[0].date()), "end": str(ser.index[-1].date())}


def label_masks(score: pd.DataFrame, feat: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """与 health_label（持仓规则）同口径的向量化版本，用于对全市场检验操作倾向标签。"""
    s5 = score.shift(5)
    a50, ema, d25 = feat["above50"], feat["ema_score"], feat["dist25"]
    reduce_ = (score < 40) | ((a50 == 0) & (ema < 40))
    watch_ = ~reduce_ & ((score < 60) | ((s5 - score) >= 10) | (a50 == 0) | (d25 >= 6))
    hold_ = ~reduce_ & ~watch_ & score.notna()
    return {"持有": hold_, "注意": watch_ & score.notna(), "考虑减仓": reduce_ & score.notna()}


# ─── 主流程 ───────────────────────────────────────────────────────────────────

def build(holdings: set[str], watch: set[str], period: str = PERIOD, progress=None) -> dict:
    """下载股票池并计算：特征、成分、综合分。返回中间结果（供验证与最新评分使用）。"""
    from core import screener as S
    uni = S.build_universe(holdings, watch)
    tickers = list(uni["ticker"])
    panel = load_panel(tickers, period, progress=progress)
    sector_of = dict(zip(uni["ticker"], uni["sector_key"]))
    sec_pct = sector_strength({k for k in sector_of.values() if k}, period)
    feat = features(panel)
    comp = components(feat, sector_of, sec_pct)
    score = composite(comp)
    return {"universe": uni, "panel": panel, "feat": feat, "comp": comp, "score": score}


def validate(b: dict) -> dict:
    """
    验证报告：
      deciles   综合分与各成分的十分组检验
      top_tier  评分 ≥90 / ≥80 作为信号
      labels    操作倾向标签（对全市场套用持仓规则）
    """
    cu = b["panel"]["close_usd"]
    deciles = {"综合": evaluate(b["score"], cu)}
    for k, df in b["comp"].items():
        deciles[k] = evaluate(df.rank(axis=1, pct=True) * 98 + 1, cu)
    top = {f"≥{th}": evaluate_mask(b["score"] >= th, cu) for th in (TOP_TIER, 80)}
    labels = {k: evaluate_mask(m, cu) for k, m in label_masks(b["score"], b["feat"]).items()}
    return {"weights": WEIGHTS, "horizon": HORIZON, "deciles": deciles, "top_tier": top, "labels": labels,
            "n_tickers": int(b["score"].shape[1])}


def save_validation(v: dict, run_date: str) -> Path:
    VALIDATION_PATH.write_text(json.dumps({"run_date": run_date, **v}, ensure_ascii=False, indent=1),
                               encoding="utf-8")
    return VALIDATION_PATH


def load_validation() -> dict | None:
    if not VALIDATION_PATH.exists():
        return None
    try:
        return json.loads(VALIDATION_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def latest(b: dict, holdings: set[str], watch: set[str], names: dict[str, str] | None = None) -> pd.DataFrame:
    """最近一个交易日的全市场评分表（含成分贡献、5 日前评分、操作倾向、量能状态）。"""
    names = names or {}
    score, feat, comp = b["score"], b["feat"], b["comp"]
    d = score.index[-1]
    d5 = score.index[-6] if len(score) > 5 else None
    vol_df = b["panel"]["volume"]
    vr5 = vol_df.tail(5).mean() / vol_df.shift(1).rolling(50).mean().iloc[-1]
    uni = b["universe"].set_index("ticker")
    rows = []
    for t in score.columns:
        s = score.at[d, t]
        if pd.isna(s):
            continue
        g = lambda k: (float(feat[k].at[d, t]) if pd.notna(feat[k].at[d, t]) else None)
        s5 = float(score.at[d5, t]) if d5 is not None and pd.notna(score.at[d5, t]) else None
        held = t in holdings
        contrib = {k: round((float(comp[k].at[d, t]) - 0.5) * WEIGHTS[k] * 200, 1)
                   if pd.notna(comp[k].at[d, t]) else None for k in WEIGHTS}
        rows.append({
            "ticker": t, "name": names.get(t) or (uni.at[t, "name"] if t in uni.index else t),
            "sector": uni.at[t, "sector_key"] if t in uni.index else None,
            "score": int(s), "score_5d": int(s5) if s5 is not None else None,
            **{f"c_{k}": v for k, v in contrib.items()},
            "action": health_label(s, s5, g("above50"), g("ema_score"), g("dist25"), held=held),
            "volume_state": volume_state(g("vr"), g("ret1"), g("ud50"), g("vol_sig20"), g("dist25"),
                                         float(vr5[t]) if pd.notna(vr5.get(t)) else None),
            "vr": g("vr"), "ret1": g("ret1"), "ema_score": g("ema_score"),
            "above50": g("above50"), "dist25": g("dist25"),      # 保存以便按当前持仓 / 关注重算操作倾向
            "group": "持仓" if held else ("关注" if t in watch else ""),
        })
    df = pd.DataFrame(rows).sort_values("score", ascending=False).reset_index(drop=True)
    df.attrs["as_of"] = str(d.date())
    ok = feat["valid"].loc[d]
    df.attrs["breadth50"] = float(feat["above50"].loc[d][ok].mean())      # 市场宽度（供市场状态使用）
    df.attrs["breadth200"] = float(feat["above200"].loc[d][ok].mean())
    return df


def save_latest(df: pd.DataFrame, as_of: str) -> Path:
    meta = {k: v for k, v in df.attrs.items() if k != "as_of"}
    RATING_PATH.write_text(json.dumps({"as_of": as_of, "meta": meta,
                                       "rows": json.loads(df.to_json(orient="records", force_ascii=False))},
                                      ensure_ascii=False), encoding="utf-8")
    return RATING_PATH


def load_latest() -> tuple[pd.DataFrame, str | None]:
    if not RATING_PATH.exists():
        return pd.DataFrame(), None
    try:
        d = json.loads(RATING_PATH.read_text(encoding="utf-8"))
        df = pd.DataFrame(d["rows"])
        df.attrs.update(d.get("meta") or {})
        return df, d.get("as_of")
    except Exception:
        return pd.DataFrame(), None


def _portfolio_sets() -> tuple[set[str], set[str], dict[str, str]]:
    from core import watchlist as wl
    pf = json.loads(config.PORTFOLIO_PATH.read_text(encoding="utf-8"))
    names = {p["yf_ticker"].upper(): p.get("display", p["yf_ticker"])
             for a in pf.get("accounts", []) for p in a.get("positions", [])
             if p["yf_ticker"].upper() != config.CASH_TICKER}
    held = set(names)
    watch = {t for t in wl.load() if t != config.CASH_TICKER} - held
    return held, watch, names


def get_ratings(force: bool = False, progress=None) -> tuple[pd.DataFrame, str | None]:
    """
    最新全市场评分表：data/rating_latest.json 已是最近收盘日的就直接用；否则重新计算（约 1 分钟），
    保存并同步 GitHub。持仓 / 关注分组按当前持仓与关注列表实时刷新。
    """
    from core.tracker import last_close_date
    held, watch, names = _portfolio_sets()
    df, as_of = load_latest()
    if not force and not df.empty and as_of and as_of >= (last_close_date() or ""):
        df["group"] = ["持仓" if t in held else ("关注" if t in watch else "") for t in df["ticker"]]
        if {"above50", "dist25"} <= set(df.columns):
            # 持仓 / 关注可能在评分计算之后变动：按当前归属重算操作倾向（持仓用 持有/注意/考虑减仓）
            num = lambda v: None if v is None or v != v else float(v)
            df["action"] = [health_label(r.score, num(r.score_5d), num(r.above50), num(r.ema_score), num(r.dist25),
                                         held=r.ticker in held) for r in df.itertuples()]
        for i, t in enumerate(df["ticker"]):
            if t in names:
                df.at[i, "name"] = names[t]
        return df, as_of
    b = build(held, watch, progress=progress)
    df = latest(b, held, watch, names)
    as_of = df.attrs.get("as_of")
    try:
        from core.github_storage import sync_to_github
        path = save_latest(df, as_of)
        sync_to_github(path, "data/rating_latest.json", "chore: update ratings")
    except Exception:
        pass
    return df, as_of
