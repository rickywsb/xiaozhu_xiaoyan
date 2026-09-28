"""core/tracker.py — 当日涨跌（盘中看板）+ Day 0 追踪

  · 盘中   一次批量请求取最新日线：今日 K 线收盘 = 最新价，前一根 = 昨收 → 当日涨跌 / 当日盈亏（美元）；
           另用昨日为止的完整日线算出 Fib / 筹码关键价位，盘中价格接近或穿越时预警
  · Day 0  data/day0_tracker.json 只记录基准日与股票名单（及各自加入日）；每日收盘价随时从行情补算，
           哪天没打开 App 也不会断档。之后新加入持仓/关注的股票，从加入当日收盘开始计。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from core import daily_momentum as dm

TRACKER_PATH = config.DATA_DIR / "day0_tracker.json"
BENCHMARKS = ("SPY", "QQQ", "SOXX")
NEAR_LEVEL = 0.01          # 盘中距关键价位 ±1% 视为"接近"


# ─── 股票池 ───────────────────────────────────────────────────────────────────

def universe(portfolio: dict, watchlist: list[str]) -> dict[str, dict]:
    """
    {TICKER: {name, group, sector, shares}}；ticker 统一大写（"Disk" 与 "DISK" 视为同一只）。
    group：持仓 / 关注 / 基准。持仓优先。
    """
    out: dict[str, dict] = {}
    for acc in portfolio.get("accounts", []):
        for pos in acc.get("positions", []):
            t = pos["yf_ticker"].upper()
            if t == config.CASH_TICKER:
                continue
            row = out.setdefault(t, {"name": pos.get("display", t), "group": "持仓",
                                     "sector": pos.get("sector", ""), "shares": 0.0})
            row["shares"] += float(pos.get("shares", 0) or 0)
    for t in watchlist:
        t = t.upper().strip()
        if t and t != config.CASH_TICKER and t not in out:
            out[t] = {"name": t, "group": "关注", "sector": "", "shares": 0.0}
    for b in BENCHMARKS:
        if b not in out:
            out[b] = {"name": b, "group": "基准", "sector": "", "shares": 0.0}
    return out


# ─── 美股交易时段 ─────────────────────────────────────────────────────────────

def us_market_status(now: datetime | None = None) -> str:
    """"交易中" / "盘前" / "已收盘" / "休市"（周末；未处理节假日）。"""
    now = now or datetime.now(ZoneInfo(config.MARKET_TZ))
    if now.weekday() >= 5:
        return "休市"
    t = now.time()
    if t < dtime(9, 30):
        return "盘前"
    if t < dtime(16, 0):
        return "交易中"
    return "已收盘"


# ─── 盘中报价 ─────────────────────────────────────────────────────────────────

def live_quotes(tickers: list[str]) -> pd.DataFrame:
    """
    最新价 / 昨收 / 当日涨跌（本币），一次批量请求、不走缓存。
    列：ticker, last, prev_close, chg, bar_date（最新 K 线日期；美股盘前时仍是上一交易日）。
    """
    data = dm._download(tickers, period="5d", max_age=0)
    rows = []
    for t in tickers:
        df = data.get(t)
        if df is None:
            continue
        c = df["Close"].dropna()
        if len(c) < 2:
            continue
        last, prev = float(c.iloc[-1]), float(c.iloc[-2])
        v = df["Volume"].reindex(c.index) if "Volume" in df else None
        rows.append({"ticker": t, "last": last, "prev_close": prev,
                     "chg": last / prev - 1 if prev else None,
                     "vol": float(v.iloc[-1]) if v is not None and pd.notna(v.iloc[-1]) else None,
                     "bar_date": c.index[-1].date().isoformat()})
    return pd.DataFrame(rows)


def intraday_board(uni: dict[str, dict], quotes: pd.DataFrame, fx_rates: dict[str, float]) -> pd.DataFrame:
    """合并报价与股票池，算持仓当日盈亏（美元，按最新汇率）。"""
    from core.price_updater import to_usd
    if quotes.empty:
        return pd.DataFrame()
    df = quotes.copy()
    df["name"] = df["ticker"].map(lambda t: uni[t]["name"])
    df["group"] = df["ticker"].map(lambda t: uni[t]["group"])
    df["sector"] = df["ticker"].map(lambda t: uni[t]["sector"])
    df["shares"] = df["ticker"].map(lambda t: uni[t]["shares"])
    df["pnl_usd"] = [
        to_usd((r.last - r.prev_close) * r.shares, r.ticker, fx_rates) if r.shares else None
        for r in df.itertuples()
    ]
    df["prev_value_usd"] = [
        to_usd(r.prev_close * r.shares, r.ticker, fx_rates) if r.shares else None
        for r in df.itertuples()
    ]
    # None → NaN，表格才能统一显示为"—"
    for c in ("pnl_usd", "prev_value_usd", "chg"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_values("chg", ascending=False).reset_index(drop=True)


def sector_pnl(board: pd.DataFrame) -> pd.DataFrame:
    """持仓按板块汇总当日盈亏与涨跌幅（盈亏 / 昨日市值）。"""
    h = board[board["group"] == "持仓"].dropna(subset=["pnl_usd"])
    if h.empty:
        return pd.DataFrame()
    g = h.groupby("sector").agg(pnl_usd=("pnl_usd", "sum"), prev_value_usd=("prev_value_usd", "sum"),
                                n=("ticker", "count")).reset_index()
    g["chg"] = g["pnl_usd"] / g["prev_value_usd"]
    return g.sort_values("pnl_usd", ascending=False).reset_index(drop=True)


# ─── 关键价位预警 ─────────────────────────────────────────────────────────────

def key_levels(tickers: list[str]) -> dict[str, list[dict]]:
    """
    用截至昨日的完整日线（本币）算关键价位：
      Fib 38.2/50/61.8/78.6%（上升趋势=支撑，下降趋势=阻力）+ 筹码 POC/VAH/VAL。
    返回 {ticker: [{name, price, kind}]}。
    """
    data = dm.fetch_ohlcv_histories(tickers, complete_bars_only=True)
    out: dict[str, list[dict]] = {}
    for t, df in data.items():
        levels = []
        f = dm.fib_signal(df["Close"])
        if f:
            hi, lo = f["swing_high"], f["swing_low"]
            rng = hi - lo
            for r in dm.FIB_KEY:
                if f["uptrend"]:
                    levels.append({"name": f"Fib {r:.1%}", "price": hi - rng * r, "kind": "支撑"})
                else:
                    levels.append({"name": f"Fib {r:.1%}", "price": lo + rng * r, "kind": "阻力"})
        v = dm.volume_profile(df)
        if v:
            levels += [{"name": "POC", "price": v["poc"], "kind": "筹码"},
                       {"name": "VAH", "price": v["vah"], "kind": "筹码"},
                       {"name": "VAL", "price": v["val"], "kind": "筹码"}]
        out[t] = levels
    return out


# 盘中事件 → 信号成绩单里对应的信号（用于显示该类信号的历史判定）
def _bt_signal(level: str, kind: str, crossed_down: bool, crossed_up: bool) -> str | None:
    if level == "VAL":
        return "筹码 跌破VAL" if crossed_down else "筹码 测试/回踩VAL"
    if level == "VAH":
        return "筹码 上破VAH" if crossed_up else None
    if level == "Fib 78.6%" and kind == "支撑" and crossed_down:
        return "Fib 破位(>78.6%)"
    if level.startswith("Fib") and kind == "支撑":
        return "Fib 贴近关键支撑"
    return None


def level_alerts(board: pd.DataFrame, levels: dict[str, list[dict]],
                 verdicts: dict[str, dict] | None = None) -> pd.DataFrame:
    """当日穿越或距离 ±NEAR_LEVEL 以内的关键价位。列含事件、价位、距离、对应信号的历史判定。"""
    verdicts = verdicts or {}
    rows = []
    for r in board.itertuples():
        for lv in levels.get(r.ticker, []):
            p = lv["price"]
            if not p:
                continue
            dist = r.last / p - 1
            crossed_down = r.prev_close >= p > r.last
            crossed_up = r.prev_close <= p < r.last
            if not (crossed_down or crossed_up or abs(dist) <= NEAR_LEVEL):
                continue
            if crossed_down:
                event = f"跌破 {lv['name']}"
            elif crossed_up:
                event = f"上破 {lv['name']}"
            else:
                event = f"接近 {lv['name']}"
            sig = _bt_signal(lv["name"], lv["kind"], crossed_down, crossed_up)
            v = verdicts.get(sig, {}) if sig else {}
            rows.append({"ticker": r.ticker, "name": r.name, "group": r.group, "event": event,
                         "kind": lv["kind"], "level": p, "last": r.last, "dist": dist,
                         "chg": r.chg, "signal": sig, "verdict": v.get("判定")})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["_crossed"] = ~df["event"].str.startswith("接近")
    df["_absd"] = df["dist"].abs()
    return (df.sort_values(["_crossed", "_absd"], ascending=[False, True])
              .drop(columns=["_crossed", "_absd"]).reset_index(drop=True))


# ─── Day 0 追踪 ───────────────────────────────────────────────────────────────

def last_close_date() -> str | None:
    """最近一个已收盘的美股交易日（以 SPY 完整日线为准）。"""
    d = dm.fetch_ohlcv_histories(["SPY"], period="1mo", complete_bars_only=True).get("SPY")
    return d.index[-1].date().isoformat() if d is not None and not d.empty else None


def load_tracker() -> dict | None:
    if not TRACKER_PATH.exists():
        return None
    try:
        return json.loads(TRACKER_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_tracker(tr: dict) -> Path:
    TRACKER_PATH.write_text(json.dumps(tr, ensure_ascii=False, indent=2), encoding="utf-8")
    return TRACKER_PATH


def ensure_tracker(uni: dict[str, dict], reset: bool = False) -> tuple[dict | None, bool]:
    """
    没有追踪记录（或 reset）时以最近收盘日建立 Day 0；已有则把新股票加入（加入日 = 最近收盘日）。
    返回 (tracker, 是否有变更需要保存)。
    """
    tr = None if reset else load_tracker()
    base = last_close_date()
    if base is None:
        return tr, False
    changed = False
    if tr is None:
        tr = {"day0": base, "created_at": config.market_today().isoformat(), "tickers": {}}
        changed = True
    for t, info in uni.items():
        if t not in tr["tickers"]:
            tr["tickers"][t] = {"added": base, "group": info["group"]}
            changed = True
        elif tr["tickers"][t].get("group") != info["group"]:
            tr["tickers"][t]["group"] = info["group"]
            changed = True
    return tr, changed


def track_history(tr: dict, uni: dict[str, dict]) -> dict:
    """
    Day 0 以来的表现（美元计价）。返回：
      daily    DataFrame[date × ticker] 每日涨跌（仅加入日之后）
      cum      DataFrame[date × ticker] 自加入日累计涨跌
      table    每只一行：名称/分组/加入日/基准价/最新价/累计/同期SPY/超额/上涨天数/交易天数/最大单日涨跌
      day_n    Day 0 以来的美股交易日数
      dates    Day 0 以来的美股交易日
    """
    tickers = list(tr["tickers"])
    day0 = pd.Timestamp(tr["day0"])
    period = "2y" if (pd.Timestamp.now() - day0).days > 330 else "1y"
    closes = dm.fetch_histories(tickers, period=period, usd=True)

    spy = closes.get("SPY")
    us_days = spy.index[spy.index > day0] if spy is not None else pd.DatetimeIndex([])

    daily_cols, cum_cols, rows = {}, {}, []
    for t in tickers:
        c = closes.get(t)
        if c is None or c.empty:
            continue
        added = pd.Timestamp(tr["tickers"][t]["added"])
        base_s = c[c.index <= added]
        if base_s.empty:
            continue
        base = float(base_s.iloc[-1])
        after = c[c.index > added]
        seg = pd.concat([base_s.iloc[-1:], after])
        d = seg.pct_change().iloc[1:]
        daily_cols[t] = d
        cum_cols[t] = after / base - 1
        cum = float(after.iloc[-1] / base - 1) if len(after) else 0.0
        spy_cum = None
        if spy is not None:
            sb = spy[spy.index <= added]
            if not sb.empty:
                spy_cum = float(spy.iloc[-1] / sb.iloc[-1] - 1)
        info = uni.get(t, {"name": t, "group": "已移出"})
        rows.append({
            "ticker": t, "name": info["name"], "group": info["group"] if t in uni else "已移出",
            "added": tr["tickers"][t]["added"], "base": base,
            "last": float(c.iloc[-1]), "cum": cum,
            "spy_cum": spy_cum, "excess": (cum - spy_cum) if spy_cum is not None else None,
            "up_days": int((d > 0).sum()), "n_days": int(len(d)),
            "best_day": float(d.max()) if len(d) else None,
            "worst_day": float(d.min()) if len(d) else None,
        })

    table = pd.DataFrame(rows)
    if not table.empty:
        table = table.sort_values("cum", ascending=False).reset_index(drop=True)
    return {
        "daily": pd.DataFrame(daily_cols).sort_index(),
        "cum": pd.DataFrame(cum_cols).sort_index(),
        "table": table,
        "day_n": int(len(us_days)),
        "dates": us_days,
    }


def holdings_value_since(tr: dict, uni: dict[str, dict], cum: pd.DataFrame) -> pd.Series | None:
    """按**当前**持股数回溯的股票市值累计涨跌（假设期间持仓不变），用于和基准对比。"""
    held = [t for t, i in uni.items() if i["group"] == "持仓" and t in cum.columns]
    if not held or cum.empty:
        return None
    closes = dm.fetch_histories(held, period="1y", usd=True)
    day0 = pd.Timestamp(tr["day0"])
    val = pd.DataFrame({t: closes[t] * uni[t]["shares"] for t in held if t in closes}).sort_index().ffill()
    base = val[val.index <= day0]
    if base.empty:
        return None
    total = val.sum(axis=1)
    b = float(total[total.index <= day0].iloc[-1])
    return total[total.index > day0] / b - 1


# ─── 盘中放量 ─────────────────────────────────────────────────────────────────

def volume_alerts(board: pd.DataFrame, status: str, now_et: datetime,
                  verdicts: dict[str, dict] | None = None, min_vr: float = 1.5) -> tuple[pd.DataFrame, str]:
    """
    盘中（或最近交易日）放量：预计全天量比 = 当前量 ÷ 日内已完成占比 ÷ 前 50 日均量。
    美股交易中按典型日内分布折算；非美股 / 非交易时段按完整日线（占比 = 1）。
    返回 (预警表, 说明文字)。预警表附"预计信号"（放量上涨 / 放量突破 / 放量下跌）及其历史评级。
    """
    from core import volume as vol
    verdicts = verdicts or {}
    frac_us = vol.session_fraction(now_et) if status == "交易中" else 1.0
    if status == "交易中" and frac_us is None:
        return pd.DataFrame(), f"开盘 {vol.MIN_MINUTES} 分钟内成交量折算误差太大，稍后再看。"

    hist = dm.fetch_ohlcv_histories(list(board["ticker"]), complete_bars_only=True)
    rows = []
    for r in board.itertuples():
        h = hist.get(r.ticker)
        if h is None or len(h) < 55 or not r.vol:
            continue
        # 剔除与报价同一天的完整 K 线（收盘后报价日 = 最后一根完整 K 线），得到"之前"的基准
        prior = h[h.index.date.astype(str) < r.bar_date] if len(h) else h
        if len(prior) < 51:
            continue
        avg50 = float(prior["Volume"].tail(50).mean())
        high20 = float(prior["Close"].tail(20).max())
        frac = frac_us if "." not in r.ticker else 1.0
        pvr = vol.projected_vr(r.vol, avg50, frac)
        if pvr is None or pvr < min_vr:
            continue
        sig = None
        if pvr >= vol.SURGE and r.chg >= vol.BIG_MOVE:
            sig = "放量上涨"
        elif r.last > high20 and pvr >= vol.BREAKOUT_VR:
            sig = "放量突破20日高"
        elif pvr >= vol.SURGE and r.chg <= -vol.BIG_MOVE:
            sig = "放量下跌"
        rows.append({"ticker": r.ticker, "name": r.name, "group": r.group, "chg": r.chg, "pvr": pvr,
                     "vol": r.vol, "avg50": avg50, "breakout": r.last > high20, "signal": sig,
                     "grade": verdicts.get(sig, {}).get("评级") if sig else None})
    note = (f"美股按日内典型成交分布折算（已完成约 {frac_us:.0%}），为估算值"
            if status == "交易中" else "按最近一个完整交易日计算")
    df = pd.DataFrame(rows)
    return (df.sort_values("pvr", ascending=False).reset_index(drop=True) if not df.empty else df), note
