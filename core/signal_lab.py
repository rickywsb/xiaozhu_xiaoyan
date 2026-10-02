"""core/signal_lab.py — 全市场买卖点信号实验室（O'Neil 体系为主，约 540 只股票向量化）

每个信号 = 「日期 × 股票」布尔矩阵，只取首次触发日（前一日未触发）。检验：
  · 超额      触发后 20 日个股收益 − 同日全市场平均
  · 胜率      跑赢（买点）/ 跑输（卖点）同日全市场中位数的比例
  · t 值      按每 20 个交易日分块取均值后计算（同段行情的事件不独立，避免高估显著性）
  · 前后两段  回看区间对半检验稳定性
  · 回撤差    卖点额外看：触发后 20 日最大回撤 − 同日全市场平均（越负 = 越能帮你躲开下跌）
  · 每日触发  全市场平均每天触发次数（噪音指标）
评级沿用信号成绩单：A 可靠 / B 参考 / C 噪音 / D 反向；只有 A / B 上线展示。
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
from core import signal_backtest as sbt

HORIZON = 20
LAB_PATH = config.DATA_DIR / "signal_lab.json"
LATEST_PATH = config.DATA_DIR / "signal_lab_latest.json"

# (信号名, 分组, 预期方向, 说明)
SIGNALS: list[tuple[str, str, int, str]] = [
    ("放量创52周新高", "O'Neil 买点", +1, "收盘突破前 252 日最高收盘，量比 ≥1.5"),
    ("平台突破", "O'Neil 买点", +1, "前 25 日振幅 ≤15% 的横盘后，放量（≥1.4）突破平台高点，站上 MA50"),
    ("VCP 收缩突破", "O'Neil 买点", +1, "ATR10/ATR50 <0.75 的波动收缩后放量突破 20 日高，且 MA50 > MA200"),
    ("财报跳空 Power Gap", "O'Neil 买点", +1, "跳空高开 ≥5%、量比 ≥3、收阳且守住 4% 以上涨幅"),
    ("RS 线领先新高", "O'Neil 买点", +1, "相对 SPY 强弱线创 52 周新高、股价尚未创新高（低于前高 2% 以上），站上 MA50"),
    ("首次回踩 50 日线", "O'Neil 买点", +1, "上升趋势中 30 日来首次缩量回踩 MA50（±2%）并收在其上"),
    ("上升趋势短线超卖", "均值回归买点", +1, "站上 MA200 且 2 日 RSI <10（Connors）"),
    ("放量上涨", "放量（对照）", +1, "量比 ≥2 且涨 ≥3%"),
    ("放量突破20日高", "放量（对照）", +1, "收盘突破前 20 日最高收盘且量比 ≥1.5"),
    ("放量跌破 50 日线", "O'Neil 卖点", -1, "收盘由上向下跌破 MA50，量比 ≥1.5"),
    ("高潮顶", "O'Neil 卖点", -1, "15 日涨 ≥25%，且当日是这段最大单日涨幅（≥4%）、量比 ≥1.5"),
    ("ATR 追踪止损", "卖点", -1, "收盘跌破 近 22 日最高收盘 − 3×ATR22（吊灯止损）"),
    ("评分大幅下滑", "卖点", -1, "综合评分 5 日前 ≥90，今日 <70"),
    ("相对强弱转弱", "卖点", -1, "RS 百分位从近 20 日 ≥80 跌破 50"),
    ("EMA 量能转弱", "卖点（对照）", -1, "EMA 量能分跌破 40"),
]
_EXPECT = {n: d for n, _, d, _ in SIGNALS}
_GROUP = {n: g for n, g, _, _ in SIGNALS}
_DESC = {n: x for n, _, _, x in SIGNALS}


# ─── 信号矩阵 ─────────────────────────────────────────────────────────────────

def _atr(h, lo, c, n):
    tr = pd.concat({"a": h - lo, "b": (h - c.shift()).abs(), "c": (lo - c.shift()).abs()}).groupby(level=1).max()
    return tr.reindex(c.index).rolling(n).mean()


def signal_masks(panel: dict[str, pd.DataFrame], feat: dict[str, pd.DataFrame],
                 score: pd.DataFrame, spy: pd.Series) -> dict[str, pd.DataFrame]:
    """各信号的「状态」矩阵（True = 当日满足条件）。"""
    c, h, lo, o = panel["close"], panel["high"], panel["low"], panel["open"]
    v = panel["volume"].fillna(0)
    r = c.pct_change(fill_method=None)
    vr = v / v.shift(1).rolling(50).mean()
    ma50, ma200 = c.rolling(50).mean(), c.rolling(200).mean()
    hi20p = c.shift(1).rolling(20).max()
    hi25p, lo25p = c.shift(1).rolling(25).max(), c.shift(1).rolling(25).min()
    hi252p = c.shift(1).rolling(252, min_periods=200).max()
    atr10, atr50, atr22 = _atr(h, lo, c, 10), _atr(h, lo, c, 50), _atr(h, lo, c, 22)

    # RS 线（相对 SPY）
    spy_a = spy.reindex(c.index).ffill()
    rsl = c.div(spy_a, axis=0)
    rsl_hi = rsl.shift(1).rolling(252, min_periods=200).max()

    # 2 日 RSI
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=0.5, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=0.5, adjust=False).mean()
    rsi2 = 100 - 100 / (1 + up / dn.replace(0, np.nan))

    above50 = c > ma50
    touch50 = (lo <= ma50 * 1.02) & (c >= ma50)
    rs_pct = feat["rs"].where(feat["valid"]).rank(axis=1, pct=True)

    return {
        "放量创52周新高": (c > hi252p) & (vr >= 1.5),
        "平台突破": ((hi25p / lo25p - 1) <= 0.15) & (c > hi25p) & (vr >= 1.4) & above50,
        "VCP 收缩突破": ((atr10 / atr50).shift(1) < 0.75) & (c > hi20p) & (vr >= 1.5) & (ma50 > ma200) & above50,
        "财报跳空 Power Gap": (o >= c.shift(1) * 1.05) & (vr >= 3) & (c >= o) & (c >= c.shift(1) * 1.04),
        "RS 线领先新高": (rsl > rsl_hi) & (c < hi252p * 0.98) & above50,
        "首次回踩 50 日线": (touch50 & (vr < 1.0) & (ma50 > ma50.shift(10))
                         & (above50.astype(float).shift(1).rolling(30).min() == 1)
                         & (touch50.astype(float).shift(1).rolling(30).max() == 0)),
        "上升趋势短线超卖": (c > ma200) & (rsi2 < 10),
        "放量上涨": (vr >= 2) & (r >= 0.03),
        "放量突破20日高": (c > hi20p) & (vr >= 1.5),
        "放量跌破 50 日线": (c < ma50) & (c.shift(1) >= ma50.shift(1)) & (vr >= 1.5),
        "高潮顶": ((c / c.shift(15) - 1) >= 0.25) & (r >= r.rolling(15).max()) & (r >= 0.04) & (vr >= 1.5),
        "ATR 追踪止损": c < (c.rolling(22).max() - 3 * atr22),
        "评分大幅下滑": (score.shift(5) >= 90) & (score < 70),
        "相对强弱转弱": (rs_pct < 0.5) & (rs_pct.shift(1).rolling(20).max() >= 0.8),
        "EMA 量能转弱": feat["ema_score"] < 40,
    }


def onsets(mask: pd.DataFrame) -> pd.DataFrame:
    m = mask.fillna(False).astype(bool)
    return m & ~m.shift(1, fill_value=False)


# ─── 检验 ─────────────────────────────────────────────────────────────────────

def _forward(close_usd: pd.DataFrame, horizon: int = HORIZON) -> dict[str, pd.DataFrame]:
    px = close_usd.ffill(limit=3)
    fwd = px.shift(-horizon) / px - 1
    fmin = px[::-1].rolling(horizon, min_periods=horizon // 2).min()[::-1].shift(-1)
    dd = fmin / px - 1
    return {"ex": fwd.sub(fwd.mean(axis=1), axis=0), "med": fwd.sub(fwd.median(axis=1), axis=0),
            "dd": dd.sub(dd.mean(axis=1), axis=0)}


def evaluate(ev: pd.DataFrame, fw: dict[str, pd.DataFrame], expect: int, horizon: int = HORIZON) -> dict:
    # pandas 3 的 stack 不再自动丢弃 NaN：未触发（或无前瞻收益）的格子要显式去掉
    ex = fw["ex"].where(ev).stack().dropna()
    if ex.empty:
        return {"n": 0, "verdict": "⚪ 样本不足", "grade": "⚪ 样本不足"}
    med = fw["med"].where(ev).stack().dropna()
    dd = fw["dd"].where(ev).stack().dropna()
    dates = ex.index.get_level_values(0)
    pos = pd.Series(np.arange(len(fw["ex"].index)), index=fw["ex"].index)
    block = pos.reindex(dates).to_numpy() // horizon
    blk = pd.Series(ex.to_numpy()).groupby(block).mean()
    t = float(blk.mean() / (blk.std(ddof=1) / math.sqrt(len(blk)))) if len(blk) > 2 and blk.std(ddof=1) > 0 else None
    mid = fw["ex"].index[len(fw["ex"].index) // 2]
    h1, h2 = ex[dates < mid], ex[dates >= mid]
    n_days = int(fw["ex"].dropna(how="all").shape[0])
    out = {
        "n": int(len(ex)), "n_tickers": int(ex.index.get_level_values(1).nunique()),
        "per_day": len(ex) / max(n_days, 1),
        "excess": float(ex.mean()), "hit": float(((med * expect) > 0).mean()), "t": t,
        "h1": float(h1.mean()) if len(h1) >= 5 else None, "h2": float(h2.mean()) if len(h2) >= 5 else None,
        "dd_diff": float(dd.mean()) if len(dd) else None,
    }
    out["verdict"] = sbt._verdict(out["n"], out["excess"], t, expect)
    out["grade"] = sbt.grade(out["verdict"], expect, out["h1"], out["h2"])
    return out


def market_distribution(spy_ohlcv: pd.DataFrame, horizon: int = HORIZON) -> dict:
    """大盘派发日累积：SPY 25 日内 ≥5 个派发日（跌 ≥0.2% 且量大于前一日）首次出现后，SPY 的 20 日收益 vs 平常。"""
    c, v = spy_ohlcv["Close"], spy_ohlcv["Volume"]
    r = c.pct_change()
    dist = ((r <= -0.002) & (v > v.shift(1))).astype(int).rolling(25).sum()
    sig = dist >= 5
    on = sig & ~sig.shift(1, fill_value=False)
    fwd = c.shift(-horizon) / c - 1
    base = fwd.dropna()
    ev = fwd[on].dropna()
    return {"n": int(len(ev)), "fwd_after": float(ev.mean()) if len(ev) else None,
            "fwd_all": float(base.mean()), "excess": (float(ev.mean() - base.mean()) if len(ev) else None),
            "current_count": int(dist.iloc[-1]) if len(dist) else None, "active": bool(sig.iloc[-1]) if len(sig) else False}


def run(b: dict) -> dict:
    """
    b = core.rating.build() 的结果。返回 {summary（每个信号一行）, market（派发日）,
    latest（最近交易日各股票触发的 A/B 信号）, recent（近 130 日 A/B 信号事件，供 K 线标记）}。
    """
    spy_df = dm.fetch_ohlcv_histories(["SPY"], period="2y", complete_bars_only=True).get("SPY")
    spy = spy_df["Close"] if spy_df is not None else pd.Series(dtype=float)
    masks = signal_masks(b["panel"], b["feat"], b["score"], spy)
    fw = _forward(b["panel"]["close_usd"])
    rows, evs = [], {}
    for name, mask in masks.items():
        ev = onsets(mask)
        evs[name] = ev
        res = evaluate(ev, fw, _EXPECT[name])
        rows.append({"信号": name, "分组": _GROUP[name], "预期": "看多" if _EXPECT[name] > 0 else "看空",
                     "说明": _DESC[name], **res})
    summary = pd.DataFrame(rows)
    order = {"A 可靠": 0, "B 参考": 1, "D 反向": 2, "C 噪音": 3, "⚪ 样本不足": 4}
    summary["_o"] = summary["grade"].map(order)
    summary = summary.sort_values(["_o", "分组"]).drop(columns="_o").reset_index(drop=True)

    good = set(summary.loc[summary["grade"].str[:1].isin(["A", "B"]), "信号"])
    grade_of = dict(zip(summary["信号"], summary["grade"]))
    last = b["score"].index[-1]
    latest: dict[str, list[str]] = {}
    recent: list[dict] = []
    cut = b["score"].index[max(0, len(b["score"].index) - 130)]
    for name in good:
        ev = evs[name]
        for t in ev.columns[ev.loc[last].fillna(False).to_numpy(dtype=bool)]:
            latest.setdefault(t, []).append(name)
        st_ = ev.loc[ev.index >= cut].stack()
        for (d, t), on in st_[st_.fillna(False).astype(bool)].items():
            if on:
                recent.append({"date": d.date().isoformat(), "ticker": t, "signal": name,
                               "grade": grade_of[name][0], "expect": _EXPECT[name]})
    mkt = market_distribution(spy_df) if spy_df is not None else {}
    return {"summary": summary, "market": mkt, "latest": latest, "recent": recent, "as_of": str(last.date())}


# ─── 持久化 ───────────────────────────────────────────────────────────────────

def save(res: dict, run_date: str) -> tuple[Path, Path]:
    s = res["summary"]
    LAB_PATH.write_text(json.dumps({"run_date": run_date, "as_of": res["as_of"], "market": res["market"],
                                    "summary": json.loads(s.to_json(orient="records", force_ascii=False))},
                                   ensure_ascii=False, indent=1), encoding="utf-8")
    LATEST_PATH.write_text(json.dumps({"as_of": res["as_of"], "latest": res["latest"], "recent": res["recent"]},
                                      ensure_ascii=False), encoding="utf-8")
    return LAB_PATH, LATEST_PATH


def load_summary() -> dict | None:
    if not LAB_PATH.exists():
        return None
    try:
        d = json.loads(LAB_PATH.read_text(encoding="utf-8"))
        d["summary"] = pd.DataFrame(d["summary"])
        return d
    except Exception:
        return None


def load_latest() -> dict:
    if not LATEST_PATH.exists():
        return {"as_of": None, "latest": {}, "recent": []}
    try:
        return json.loads(LATEST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"as_of": None, "latest": {}, "recent": []}


# ─── 降噪展示 ─────────────────────────────────────────────────────────────────

# 包含关系去重：出现更强信号时隐去被它包含的弱信号（降噪）
SUBSUMES = {
    "放量创52周新高": {"放量突破20日高"},
    "财报跳空 Power Gap": {"放量上涨", "放量突破20日高", "放量创52周新高"},
    "放量上涨": {"放量突破20日高"},
}


def ticker_signals(ticker: str) -> list[dict]:
    """某只股票最近交易日触发的 A / B 级信号（按评级、再按卖点优先排序）。"""
    lab = load_summary()
    lt = load_latest()
    if not lab:
        return []
    g = dict(zip(lab["summary"]["信号"], lab["summary"]["grade"]))
    ex = dict(zip(lab["summary"]["信号"], lab["summary"]["excess"]))
    names = set(lt.get("latest", {}).get(ticker.upper(), []))
    hidden = set().union(*(SUBSUMES.get(n, set()) for n in names)) if names else set()
    out = [{"signal": n, "grade": g.get(n, "")[:1], "expect": _EXPECT.get(n, 1), "excess": ex.get(n)}
           for n in names - hidden]
    return sorted(out, key=lambda x: (x["grade"], x["expect"]))


def main_signal_text(ticker: str, max_n: int = 1) -> str:
    """降噪：只给评级最高的 max_n 个，其余写 +N。"""
    sigs = ticker_signals(ticker)
    if not sigs:
        return ""
    head = " · ".join(f"{'▲' if s['expect'] > 0 else '▼'}{s['signal']} {s['grade']}" for s in sigs[:max_n])
    return head + (f" +{len(sigs) - max_n}" if len(sigs) > max_n else "")


def ticker_recent(ticker: str) -> list[dict]:
    """近 130 日该股票的 A / B 级信号事件（K 线标记用）。"""
    return [e for e in load_latest().get("recent", []) if e["ticker"] == ticker.upper()]
