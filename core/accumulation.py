"""core/accumulation.py — 主力/机构 吸筹·派发 信号（量价行为代理）

⚠️ 数据边界说明：
yfinance 只提供日线 OHLCV 与季度机构持仓（13F），**拿不到**真正的 Level-2
逐笔大单 / 主力资金净流入（那属于付费行情）。本模块用公开的量价行为构造一套
**代理信号**，用于提示"疑似机构吸筹/派发"，不等于真实主力资金流向。

信号（全部基于日线 OHLCV）：
  • 量比 vol_ratio      = 最新成交量 / 前 20 日均量（放量程度）
  • CMF(20)            = Chaikin 资金流，衡量收盘位置×量的净流入，>0 买盘占优
  • OBV 斜率           = 能量潮 20 日趋势，上升=持续净流入
  • 涨跌量比 ud_vol    = 20 日内 上涨日总量 / 下跌日总量，>1 放量在涨
  • MFI(14)            = 资金流量指标（含量的 RSI），>80 超买/潜在派发，<20 超卖
  • 放量突破 breakout  = 收盘创 20 日新高 且 量比 > 1.5

综合成 吸筹评分 与 判定：🟢 疑似吸筹 / 🟡 中性 / 🔴 疑似派发。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FETCH_PERIOD = "6mo"


# ─── 数据获取 ─────────────────────────────────────────────────────────────────

def fetch_ohlcv_batch(tickers: list[str], period: str = FETCH_PERIOD) -> dict[str, pd.DataFrame]:
    """批量下载 OHLCV，返回 {ticker: DataFrame[Open,High,Low,Close,Volume]}。"""
    import config
    real = [t for t in tickers if t.upper() != config.CASH_TICKER]
    if not real:
        return {}
    try:
        raw = yf.download(real, period=period, auto_adjust=True,
                          progress=False, threads=True, group_by="column")
    except Exception:
        return {}
    if raw is None or raw.empty:
        return {}

    out: dict[str, pd.DataFrame] = {}
    fields = ["Open", "High", "Low", "Close", "Volume"]
    if len(real) == 1:
        t = real[0]
        df = raw[[c for c in fields if c in raw.columns]].dropna(subset=["Close"])
        if not df.empty:
            out[t] = df
    else:
        for t in real:
            try:
                df = pd.DataFrame({f: raw[f][t] for f in fields if f in raw.columns.get_level_values(0)})
            except Exception:
                continue
            df = df.dropna(subset=["Close"])
            if not df.empty:
                out[t] = df
    return out


# ─── 单指标 ───────────────────────────────────────────────────────────────────

def _cmf(df: pd.DataFrame, period: int = 20) -> float | None:
    """Chaikin Money Flow：收盘位置乘量的净流入占总量比例。"""
    if len(df) < period:
        return None
    h, l, c, v = df["High"], df["Low"], df["Close"], df["Volume"]
    rng = (h - l).replace(0, np.nan)
    mfm = ((c - l) - (h - c)) / rng          # money flow multiplier ∈ [-1,1]
    mfv = (mfm * v).fillna(0)                # money flow volume
    vol_sum = v.rolling(period).sum().iloc[-1]
    if not vol_sum:
        return None
    return float(mfv.rolling(period).sum().iloc[-1] / vol_sum)


def _obv_slope(df: pd.DataFrame, period: int = 20) -> float | None:
    """OBV（能量潮）最近 period 日的归一化斜率（>0 上升=净流入）。"""
    if len(df) < period + 1:
        return None
    c, v = df["Close"], df["Volume"]
    obv = (np.sign(c.diff().fillna(0)) * v).cumsum()
    seg = obv.iloc[-period:]
    x = np.arange(len(seg))
    slope = np.polyfit(x, seg.values, 1)[0]
    denom = abs(seg.mean()) or 1
    return float(slope / denom)          # 相对斜率，去量纲


def _ud_vol_ratio(df: pd.DataFrame, period: int = 20) -> float | None:
    """涨跌量比：period 内 上涨日总量 / 下跌日总量。>1 表示放量在涨。"""
    if len(df) < period + 1:
        return None
    seg = df.iloc[-period:]
    chg = seg["Close"].diff().fillna(0)
    up_vol = seg["Volume"][chg > 0].sum()
    dn_vol = seg["Volume"][chg < 0].sum()
    if not dn_vol:
        return float("inf") if up_vol else None
    return float(up_vol / dn_vol)


def _mfi(df: pd.DataFrame, period: int = 14) -> float | None:
    """Money Flow Index：含成交量的 RSI。>80 超买/潜在派发，<20 超卖/潜在吸筹。"""
    if len(df) < period + 1:
        return None
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    rmf = tp * df["Volume"]
    dtp = tp.diff()
    pos = rmf.where(dtp > 0, 0.0)
    neg = rmf.where(dtp < 0, 0.0)
    pos_s = pos.rolling(period).sum().iloc[-1]
    neg_s = neg.rolling(period).sum().iloc[-1]
    if not neg_s:
        return 100.0 if pos_s else None
    mfr = pos_s / neg_s
    return float(100 - 100 / (1 + mfr))


def _vol_ratio(df: pd.DataFrame, period: int = 20) -> float | None:
    """量比：最新成交量 / 前 period 日均量（不含当日）。"""
    if len(df) < period + 1:
        return None
    prior_avg = df["Volume"].iloc[-(period + 1):-1].mean()
    if not prior_avg:
        return None
    return float(df["Volume"].iloc[-1] / prior_avg)


# ─── 综合信号 ─────────────────────────────────────────────────────────────────

def compute_signals(df: pd.DataFrame) -> dict | None:
    """对单只股票的 OHLCV 计算吸筹/派发代理信号，返回指标 + 评分 + 判定。"""
    if df is None or len(df) < 22:
        return None

    cmf = _cmf(df, 20)
    obv_sl = _obv_slope(df, 20)
    udv = _ud_vol_ratio(df, 20)
    mfi = _mfi(df, 14)
    vr = _vol_ratio(df, 20)
    chg_1d = float(df["Close"].pct_change().iloc[-1]) if len(df) > 1 else None
    high_20 = df["Close"].iloc[-20:].max()
    breakout = bool(df["Close"].iloc[-1] >= high_20 and vr is not None and vr > 1.5)

    score = 0
    reasons: list[str] = []

    if cmf is not None:
        if cmf > 0.05:
            score += 2; reasons.append(f"CMF资金流 +{cmf:.2f}（买盘占优，净流入）")
        elif cmf < -0.05:
            score -= 2; reasons.append(f"CMF资金流 {cmf:.2f}（卖盘占优，净流出）")
    if obv_sl is not None:
        if obv_sl > 0:
            score += 1; reasons.append("OBV 能量潮上行（持续净流入）")
        elif obv_sl < 0:
            score -= 1; reasons.append("OBV 能量潮下行（持续净流出）")
    if udv is not None:
        if udv > 1.2:
            score += 1; reasons.append(f"涨跌量比 {udv:.2f}（放量在涨）")
        elif udv < 0.83:
            score -= 1; reasons.append(f"涨跌量比 {udv:.2f}（放量在跌）")
    if vr is not None and chg_1d is not None:
        if vr > 1.5 and chg_1d > 0:
            score += 1; reasons.append(f"放量上涨（量比 {vr:.1f}× · +{chg_1d:.1%}）")
        elif vr > 1.5 and chg_1d < 0:
            score -= 1; reasons.append(f"放量下跌（量比 {vr:.1f}× · {chg_1d:.1%}）")
    if breakout:
        score += 2; reasons.append("放量突破 20 日新高（疑似主力进场）")
    if mfi is not None:
        if mfi > 80:
            score -= 1; reasons.append(f"MFI {mfi:.0f}（超买，警惕派发）")
        elif mfi < 20:
            score += 1; reasons.append(f"MFI {mfi:.0f}（超卖，潜在吸筹）")

    if score >= 3:
        verdict, emoji = "疑似吸筹", "🟢"
    elif score <= -3:
        verdict, emoji = "疑似派发", "🔴"
    else:
        verdict, emoji = "中性", "🟡"

    return {
        "vol_ratio": vr,
        "cmf": cmf,
        "obv_slope": obv_sl,
        "ud_vol": udv,
        "mfi": mfi,
        "chg_1d": chg_1d,
        "breakout": breakout,
        "score": score,
        "verdict": verdict,
        "emoji": emoji,
        "reasons": reasons,
    }


def scan_holdings(portfolio: dict, period: str = FETCH_PERIOD) -> pd.DataFrame:
    """扫描所有持仓，返回吸筹/派发信号表（按评分降序）。"""
    import config
    ticker_map: dict[str, str] = {}
    for acc in portfolio.get("accounts", []):
        for pos in acc.get("positions", []):
            yf_t = pos["yf_ticker"]
            if yf_t.upper() != config.CASH_TICKER:
                ticker_map[yf_t] = pos.get("display", yf_t)

    data = fetch_ohlcv_batch(list(ticker_map.keys()), period=period)

    rows = []
    for yf_t, display in ticker_map.items():
        sig = compute_signals(data.get(yf_t))
        if not sig:
            continue
        rows.append({
            "股票": display,
            "代码": yf_t,
            "判定": f"{sig['emoji']} {sig['verdict']}",
            "评分": sig["score"],
            "量比": sig["vol_ratio"],
            "CMF": sig["cmf"],
            "涨跌量比": sig["ud_vol"],
            "MFI": sig["mfi"],
            "OBV斜率": sig["obv_slope"],
            "放量突破": "✅" if sig["breakout"] else "",
            "_reasons": "；".join(sig["reasons"]),
        })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("评分", ascending=False).reset_index(drop=True)


# ─── 机构 13F 持仓（季度，真实但滞后）──────────────────────────────────────────

def institutional_summary(ticker: str, top_n: int = 12) -> dict | None:
    """抓取季度 13F 机构持仓概况（真实数据，但按季披露，滞后 1~2 个月）。

    返回：
      inst_pct        机构持股占比（占流通股）
      inst_count      机构家数
      insider_pct     内部人持股占比
      as_of           数据披露日（最近一期）
      net_pct         Top 机构 份额加权的季度环比净增减（>0 净增持，<0 净减持）
      n_up / n_down   Top 机构中 增持 / 减持 家数
      top             Top N 机构明细 DataFrame[机构, 持股占比, 股数, 市值, 季度变化]
    数据缺失时返回 None。
    """
    if ticker.upper() == "CASH":
        return None
    try:
        tk = yf.Ticker(ticker)
    except Exception:
        return None

    inst_pct = inst_count = insider_pct = None
    try:
        mh = tk.major_holders
        if mh is not None and "Value" in mh.columns:
            d = mh["Value"].to_dict()
            inst_pct = d.get("institutionsFloatPercentHeld") or d.get("institutionsPercentHeld")
            inst_count = d.get("institutionsCount")
            insider_pct = d.get("insidersPercentHeld")
    except Exception:
        pass

    top = None
    as_of = None
    net_pct = None
    n_up = n_down = None
    try:
        ih = tk.institutional_holders
        if ih is not None and not ih.empty:
            ih = ih.copy()
            if "Date Reported" in ih.columns:
                as_of = str(pd.to_datetime(ih["Date Reported"]).max().date())
            pc = ih.get("pctChange")
            sh = ih.get("Shares")
            if pc is not None and sh is not None:
                pc = pd.to_numeric(pc, errors="coerce").fillna(0)
                sh = pd.to_numeric(sh, errors="coerce").fillna(0)
                tot = sh.sum()
                # 份额加权的季度净增减（用当期股数近似权重）
                net_pct = float((sh * pc).sum() / tot) if tot else None
                n_up = int((pc > 0).sum())
                n_down = int((pc < 0).sum())
            top = ih.head(top_n).rename(columns={
                "Holder": "机构", "pctHeld": "持股占比", "Shares": "股数",
                "Value": "市值", "pctChange": "季度变化",
            })
            keep = [c for c in ["机构", "持股占比", "股数", "市值", "季度变化"] if c in top.columns]
            top = top[keep].reset_index(drop=True)
    except Exception:
        pass

    if inst_pct is None and top is None:
        return None

    return {
        "inst_pct": inst_pct,
        "inst_count": inst_count,
        "insider_pct": insider_pct,
        "as_of": as_of,
        "net_pct": net_pct,
        "n_up": n_up,
        "n_down": n_down,
        "top": top,
    }
