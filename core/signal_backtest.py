"""core/signal_backtest.py — 信号历史检验（信号成绩单）

把 Momentum 页的各套信号放回过去逐日重算（只用当日及以前的数据，无未来函数），
统计信号**首次出现**后 5 / 20 个交易日的表现：

  · 超额收益 = 个股前瞻收益 − 同日股票池平均前瞻收益
    （横截面去均值：扣掉大盘涨跌与"池子本身涨得多"的影响，只看信号的选股能力）
  · 胜率     = 前瞻收益跑赢同日股票池**中位数**的事件占比（看空信号以跑输为胜）；
               收益分布右偏，用中位数使"无效信号"的胜率约为 50%
  · t 值     = 平均超额 / 标准误（事件间有重叠与聚集，仅作参考）

杠杆 / 反向产品（config.LEVERAGED_TICKERS）不参与检验。
局限：用当前持仓 + 关注列表回看，存在幸存者偏差——结果适合**比较信号之间谁更靠谱**，
不代表未来收益。
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import daily_momentum as dm
from core.accumulation import compute_signals

BT_PERIOD   = "2y"
WARMUP      = 150          # 前 150 根 K 线只做指标预热（Fib/筹码/MACD 回看 120，6-1 月趋势需 147）
INPUT_BARS  = 252          # 每日重算时只喂最近 1 年 K 线——与页面 FETCH_PERIOD="1y" 同口径，也快得多
HORIZONS    = (5, 20)
MIN_EVENTS  = 15           # 少于此数判"样本不足"
T_EFFECTIVE = 1.5          # |t| 超过此值且方向正确判"有效"

# 信号定义：(名称, 分组, 预期方向 +1 看多 / -1 看空)
SIGNALS: list[tuple[str, str, int]] = [
    ("EMA量能 🟢强(≥70)",   "EMA量能", +1),
    ("EMA量能 🔴弱(<40)",   "EMA量能", -1),
    ("Fib 强势贴高",         "Fib回撤", +1),
    ("Fib 贴近关键支撑",     "Fib回撤", +1),
    ("Fib 破位(>78.6%)",     "Fib回撤", -1),
    ("筹码 上破VAH",         "筹码分布", +1),
    ("筹码 测试/回踩VAL",    "筹码分布", +1),
    ("筹码 跌破VAL",         "筹码分布", -1),
    ("MACD 底背驰",          "背驰", +1),
    ("MACD 顶背驰",          "背驰", -1),
    ("吸筹 疑似吸筹",        "主力吸筹", +1),
    ("吸筹 疑似派发",        "主力吸筹", -1),
    ("综合动量 前20%",       "综合动量", +1),
    ("综合动量 后20%",       "综合动量", -1),
]
_EXPECT = {name: d for name, _, d in SIGNALS}
_GROUP  = {name: g for name, g, _ in SIGNALS}


# ─── 单只股票逐日信号 ─────────────────────────────────────────────────────────

def _day_signals(ohlcv: pd.DataFrame) -> set[str]:
    """用截至当日的 OHLCV 计算当日处于激活状态的信号集合（调用与页面相同的函数）。"""
    on: set[str] = set()
    close = ohlcv["Close"]

    e = dm.ema_momentum(close)
    if e:
        if e["ema_score"] >= 70:
            on.add("EMA量能 🟢强(≥70)")
        elif e["ema_score"] < 40:
            on.add("EMA量能 🔴弱(<40)")

    f = dm.fib_signal(close)
    if f and f["trigger"]:
        cat = f["category"]
        if cat == "强势贴高":
            on.add("Fib 强势贴高")
        elif cat == "破位预警":
            on.add("Fib 破位(>78.6%)")
        elif cat.endswith("支撑"):
            on.add("Fib 贴近关键支撑")

    v = dm.vp_signal(ohlcv)
    if v and v["trigger"]:
        cat = v["category"]
        if cat == "上破VAH":
            on.add("筹码 上破VAH")
        elif cat == "跌破VAL":
            on.add("筹码 跌破VAL")
        elif cat == "测试VAL":
            on.add("筹码 测试/回踩VAL")

    m = dm.macd_divergence(close)
    if m and m["trigger"]:
        on.add("MACD 底背驰" if m["signal"] == "底背驰" else "MACD 顶背驰")

    a = compute_signals(ohlcv.tail(60))
    if a:
        if a["verdict"] == "疑似吸筹":
            on.add("吸筹 疑似吸筹")
        elif a["verdict"] == "疑似派发":
            on.add("吸筹 疑似派发")
    return on


def _ticker_panel(ticker: str, ohlcv: pd.DataFrame, usd_close: pd.Series) -> tuple[list, list]:
    """
    返回 (信号行, 动量原料行)：
      信号行   [date, ticker, signal]  每日激活的非横截面信号
      原料行   [date, ticker, heat_a, heat_b, trend_ra]  供按日横截面计算综合动量
    """
    sig_rows, mom_rows = [], []
    df = ohlcv.dropna(subset=["Close"])
    usd = usd_close.reindex(df.index).ffill()
    n = len(df)
    for i in range(WARMUP, n):
        lo = max(0, i + 1 - INPUT_BARS)
        sub = df.iloc[lo: i + 1]
        d = sub.index[-1]
        for s in _day_signals(sub):
            sig_rows.append((d, ticker, s))
        m = dm.calc_metrics(ticker, ticker, usd.iloc[lo: i + 1])
        if m:
            mom_rows.append((d, ticker, m["heat_a"], m["heat_b"], m["trend_ra"]))
    return sig_rows, mom_rows


def _momentum_signals(mom: pd.DataFrame) -> pd.DataFrame:
    """按日横截面复现 composite（与 _score_frame 同口径），取前 / 后 20%。"""
    rows = []
    for d, g in mom.groupby("date"):
        if len(g) < 10:
            continue
        heat = dm._zscore(0.55 * dm._zscore(g["heat_a"]) + 0.45 * dm._zscore(g["heat_b"]))
        trend = dm._zscore(g["trend_ra"])
        comp = np.where(trend.notna(), 0.5 * heat + 0.5 * trend, heat)
        pct = pd.Series(comp, index=g.index).rank(pct=True)
        for t, p in zip(g["ticker"], pct):
            if p >= 0.8:
                rows.append((d, t, "综合动量 前20%"))
            elif p <= 0.2:
                rows.append((d, t, "综合动量 后20%"))
    return pd.DataFrame(rows, columns=["date", "ticker", "signal"])


# ─── 前瞻收益与统计 ───────────────────────────────────────────────────────────

def _forward_excess(closes: dict[str, pd.Series]) -> tuple[dict[int, pd.DataFrame], dict[int, pd.DataFrame]]:
    """
    前瞻 h 日收益（美元计价）的横截面比较，返回两组 {h: DataFrame[date × ticker]}：
      excess  减同日股票池平均（算平均超额）
      vs_med  减同日股票池中位数（算胜率）
    """
    px = pd.DataFrame(closes).sort_index().ffill(limit=3)
    excess, vs_med = {}, {}
    for h in HORIZONS:
        fwd = px.shift(-h) / px - 1
        excess[h] = fwd.sub(fwd.mean(axis=1), axis=0)
        vs_med[h] = fwd.sub(fwd.median(axis=1), axis=0)
    return excess, vs_med


def _onsets(sig: pd.DataFrame, trading_days: pd.DatetimeIndex) -> pd.DataFrame:
    """只保留信号"首次出现"的日子：前一交易日（该股）未激活同一信号。"""
    if sig.empty:
        return sig
    prev_day = pd.Series(trading_days[:-1], index=trading_days[1:])
    sig = sig.copy()
    sig["prev"] = sig["date"].map(prev_day)
    active = set(zip(sig["date"], sig["ticker"], sig["signal"]))
    keep = [(p, t, s) not in active for p, t, s in zip(sig["prev"], sig["ticker"], sig["signal"])]
    return sig[keep].drop(columns="prev")


def _verdict(n: int, excess: float | None, t: float | None, expect: int) -> str:
    if n < MIN_EVENTS or excess is None or t is None:
        return "⚪ 样本不足"
    if excess * expect > 0:
        return "✅ 有效" if abs(t) >= T_EFFECTIVE else "🟡 偏弱"
    return "❌ 反向" if abs(t) >= T_EFFECTIVE else "🟡 无效"


def run_backtest(tickers: list[str], period: str = BT_PERIOD, progress=None) -> dict:
    """
    对 tickers 做信号历史检验。返回：
      summary  每个信号一行：分组/预期方向/事件数/覆盖股票数/5日与20日平均超额/胜率/t值/判定
      events   逐事件明细：date, ticker, signal, ex_5d, ex_20d
      meta     回看区间、股票数
    progress: 可选回调 progress(done, total, ticker)，供页面显示进度。
    """
    import config
    # 排除杠杆 / 反向产品：反向产品的信号逻辑与正股相反，杠杆产品会放大横截面超额
    real = sorted({t for t in tickers
                   if t.upper() != config.CASH_TICKER and t.upper() not in config.LEVERAGED_TICKERS})
    ohlcv = dm.fetch_ohlcv_histories(real, period=period, complete_bars_only=True)
    fx = dm._fx_per_usd({config.CURRENCY_MAP[t] for t in ohlcv if t in config.CURRENCY_MAP}, period)
    closes = {t: dm._to_usd(df["Close"], t, fx) for t, df in ohlcv.items()}

    sig_rows, mom_rows = [], []
    for k, (t, df) in enumerate(ohlcv.items()):
        if progress:
            progress(k, len(ohlcv), t)
        if len(df) <= WARMUP + 5:
            continue
        s, m = _ticker_panel(t, df, closes[t])
        sig_rows += s
        mom_rows += m

    sig = pd.DataFrame(sig_rows, columns=["date", "ticker", "signal"])
    mom = pd.DataFrame(mom_rows, columns=["date", "ticker", "heat_a", "heat_b", "trend_ra"])
    if not mom.empty:
        sig = pd.concat([sig, _momentum_signals(mom)], ignore_index=True)

    days = pd.DatetimeIndex(sorted(pd.DataFrame(closes).index))
    events = _onsets(sig, days)

    excess, vs_med = _forward_excess(closes)
    keys = pd.MultiIndex.from_arrays([events["date"], events["ticker"]])
    for h in HORIZONS:
        events[f"ex_{h}d"] = excess[h].stack().reindex(keys).to_numpy()
    events["med_20d"] = vs_med[20].stack().reindex(keys).to_numpy()

    rows = []
    for name, group, expect in SIGNALS:
        ev = events[events["signal"] == name]
        row = {"信号": name, "分组": group, "预期": "看多" if expect > 0 else "看空",
               "事件数": int(ev["ex_20d"].notna().sum()), "股票数": int(ev["ticker"].nunique())}
        for h in HORIZONS:
            x = ev[f"ex_{h}d"].dropna()
            row[f"{h}日超额"] = float(x.mean()) if len(x) else None
        x20 = ev["ex_20d"].dropna()
        n = len(x20)
        # 胜率按预期方向计：看空信号以"跑输池子中位数"为胜
        m20 = ev["med_20d"].dropna()
        row["胜率"] = float(((m20 * expect) > 0).mean()) if len(m20) else None
        sd = float(x20.std(ddof=1)) if n > 1 else 0.0
        row["t值"] = float(x20.mean() / (sd / math.sqrt(n))) if sd > 0 else None
        row["判定"] = _verdict(n, row["20日超额"], row["t值"], expect)
        rows.append(row)

    summary = pd.DataFrame(rows)
    first = days[WARMUP] if len(days) > WARMUP else (days[0] if len(days) else None)
    meta = {
        "start": first.date().isoformat() if first is not None else None,
        "end":   days[-1].date().isoformat() if len(days) else None,
        "n_tickers": len(ohlcv),
        "n_events": int(events["ex_20d"].notna().sum()),
    }
    return {"summary": summary, "events": events, "meta": meta}


def verdict_map(summary: pd.DataFrame) -> dict[str, dict]:
    """{信号名: {判定, 胜率, 20日超额, 事件数}}——供页面和 AI 解读引用。"""
    if summary is None or summary.empty:
        return {}
    return {
        r["信号"]: {
            "判定": r["判定"],
            "胜率": round(r["胜率"], 2) if pd.notna(r["胜率"]) else None,
            "20日平均超额%": round(r["20日超额"] * 100, 2) if pd.notna(r["20日超额"]) else None,
            "事件数": int(r["事件数"]),
        }
        for _, r in summary.iterrows()
    }


# ─── 结果持久化（data/signal_backtest.json，经 GitHub 同步，重启不丢）────────────

def _path():
    import config
    return config.DATA_DIR / "signal_backtest.json"


def save_result(res: dict, run_date: str) -> Path:
    """只存汇总与元信息（逐事件明细太大，不入库）。"""
    import json
    payload = {"run_date": run_date, "meta": res["meta"],
               "summary": json.loads(res["summary"].to_json(orient="records", force_ascii=False))}
    p = _path()
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def load_result() -> dict | None:
    """读取上次检验结果：{run_date, meta, summary(DataFrame)}；没有则 None。"""
    import json
    p = _path()
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        d["summary"] = pd.DataFrame(d["summary"])
        return d
    except Exception:
        return None
