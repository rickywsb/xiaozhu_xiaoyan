"""core/intraday.py — 盘中全市场数据（驾驶舱与板块雷达共用一份，进程内缓存）

  base()       当日的收盘矩阵（core.rating.build，约 25 秒；每个收盘日、每组持仓 / 关注只算一次）
  snapshot()   每 BUCKET_MIN 分钟一次：全股票池今天的盘中 K 线 → 盘中预估评分表、盘中预计信号、
               每只股票的今日涨跌 / 预计量比（成交量按日内典型分布折算成全天）
非交易时段返回 None（各页面改用收盘数据）。多个会话同时打开时用锁避免重复下载。
"""

from __future__ import annotations

import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import config
from core import daily_momentum as dm
from core import rating as RT
from core import signal_lab as SL
from core import volume as VOL

BUCKET_MIN = 10

_lock = threading.Lock()
_base: dict = {"key": None, "b": None}
_snap: dict = {"key": None, "data": None}


def now_et() -> datetime:
    return datetime.now(ZoneInfo(config.MARKET_TZ))


def bucket(t: datetime | None = None) -> str:
    t = t or now_et()
    return f"{t:%Y-%m-%d %H}:{t.minute // BUCKET_MIN}"


def base(close_day: str, held: set[str], watch: set[str]) -> dict:
    key = (close_day, tuple(sorted(held)), tuple(sorted(watch)))
    with _lock:
        if _base["key"] != key:
            _base.update(key=key, b=RT.build(set(held), set(watch)))
        return _base["b"]


def snapshot(close_day: str, held: set[str], watch: set[str], names: dict[str, str],
             force: bool = False) -> dict:
    """
    返回 {"tbl": 盘中评分表 | None, "note", "time", "frac", "signals", "breadth50",
          "quotes": DataFrame[ticker, chg, pvr, close, prev_close, today]}。
    tbl 为 None 时（开盘 15 分钟内 / 今天已收盘入库），quotes 仍按最近完整交易日给出。
    """
    t = now_et()
    key = (bucket(t), close_day, tuple(sorted(held)), tuple(sorted(watch)))
    with _lock:
        if not force and _snap["key"] == key and _snap["data"] is not None:
            return _snap["data"]
    b = base(close_day, held, watch)
    frac = VOL.session_fraction(t)
    today = t.date().isoformat()
    out = {"tbl": None, "note": "", "time": t.strftime("%H:%M"), "frac": frac, "signals": {}, "breadth50": None}
    panel = b["panel"]
    vol50 = panel["volume"].tail(50).mean()
    from core.tracker import us_market_status
    status = us_market_status(t)
    if status != "交易中":
        out["note"] = f"美股{status}，显示最近收盘的评分"
        bars = {}
    elif frac is None:
        out["note"] = f"开盘 {VOL.MIN_MINUTES} 分钟内成交量折算误差太大，暂用昨收评分"
        bars = {}
    else:
        bars = RT.today_bars(list(panel["close"].columns) + ["SPY"])
        b2 = RT.intraday(b, bars, today, frac)
        if b2 is None:
            out["note"] = "今天已收盘入库，显示收盘评分"
        else:
            tbl = RT.latest(b2, set(held), set(watch), names)
            spy = dm.fetch_ohlcv_histories(["SPY"], period="2y", complete_bars_only=True).get("SPY")
            spy_c = spy["Close"] if spy is not None else pd.Series(dtype=float)
            if bars.get("SPY", {}).get("date") == today:
                spy_c = pd.concat([spy_c, pd.Series([bars["SPY"]["close"]], index=[pd.Timestamp(today)])])
            out.update(tbl=tbl, signals=SL.intraday_signals(b2, spy_c), breadth50=tbl.attrs.get("breadth50"))
    # 每只股票今日涨跌与预计量比（无盘中 K 线时用最近两根完整日线）
    last_c = panel["close"].ffill().iloc[-1]
    prev_c = panel["close"].ffill().iloc[-2]
    rows = []
    for tk in panel["close"].columns:
        bar = bars.get(tk)
        if bar and bar["date"] == today:
            f = frac if "." not in tk else 1.0
            pvr = (bar["volume"] / f / vol50[tk]) if bar["volume"] and vol50.get(tk) and f else np.nan
            rows.append({"ticker": tk, "close": bar["close"], "prev_close": float(last_c[tk]),
                         "chg": bar["close"] / float(last_c[tk]) - 1 if last_c[tk] else np.nan, "pvr": pvr, "today": True})
        else:
            v = panel["volume"][tk].iloc[-1]
            rows.append({"ticker": tk, "close": float(last_c[tk]), "prev_close": float(prev_c[tk]),
                         "chg": float(last_c[tk] / prev_c[tk] - 1) if prev_c[tk] else np.nan,
                         "pvr": float(v / panel["volume"][tk].iloc[-51:-1].mean()) if v == v else np.nan, "today": False})
    out["quotes"] = pd.DataFrame(rows)
    out["panel_close"] = panel["close"]
    with _lock:
        _snap.update(key=key, data=out)
    return out


def latest() -> dict | None:
    """最近一次 snapshot（不触发计算）。"""
    return _snap["data"]
