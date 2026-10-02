"""core/option_flow.py — 期权情绪与异动（CBOE 延迟期权链，与机会扫描同一数据源）+ 每日快照

每只美股标的一行：
  P/C 量比 / P/C 持仓比  当日 put 成交量 ÷ call 成交量；put 未平仓 ÷ call 未平仓
  IV30                  CBOE 给出的 30 日隐含波动率
  偏斜 skew              20–45 天到期、|delta|≈0.25 的 put IV − call IV（正 = 下跌保护更贵 = 偏谨慎）
  活跃度                 当日总成交 ÷ 总未平仓
  异动合约               剩余 ≥2 天到期、当日成交 ≥ 500、成交 / 未平仓 ≥ 2（多为新开仓）、
                        权利金 ≥ max($10 万, 该标的当日总权利金的 1%)
  异动倾向               异动权利金 call ≥ 2 倍 put、≥ $25 万，且净额 ≥ 当日总权利金的 8% → 看涨异动，反之看跌异动
注意：成交量不区分买方还是卖方主动，「看涨异动」也可能是有人在卖 call。这些指标**尚未经过历史检验**：
每天保存快照（data/option_flow_history.json），积累约 60 个交易日后再用信号实验室同样的方法评级。
"""

from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from core import option_screener as OS
from core.options import parse_occ

HISTORY_PATH = config.DATA_DIR / "option_flow_history.json"
MIN_VOL, MIN_VOL_OI, MIN_PREMIUM = 500, 2.0, 100_000
MIN_PREM_SHARE = 0.01        # 单张异动合约权利金 ≥ 该标的当日总权利金的 1%（大票、小票各按自身体量）
TILT_SHARE = 0.08            # 异动倾向：call − put 异动权利金 ≥ 当日总权利金的 8%
MIN_DTE = 2                  # 排除 0DTE / 1DTE：日内炒作的成交天然远超未平仓，不代表新建仓
TILT_RATIO, TILT_MIN = 2.0, 250_000
SCAN_EVERY = 3600            # 后台扫描最短间隔（秒）
SYNC_EVERY = 3600            # 快照同步 GitHub 最短间隔（秒）

_last: dict = {"time": 0.0, "df": None, "running": False}
_lock = threading.Lock()


# ─── 单只标的汇总 ─────────────────────────────────────────────────────────────

def summarize(ticker: str, ch: dict, today: date | None = None) -> dict | None:
    opts = ch.get("options") or []
    if not opts:
        return None
    today = today or config.market_today()
    spot = ch.get("price")
    cv = pv = coi = poi = prem_total = 0.0
    unusual, skew_c, skew_p = [], [], []
    for o in opts:
        m = parse_occ(o.get("option", ""))
        if not m:
            continue
        vol, oi = float(o.get("volume") or 0), float(o.get("open_interest") or 0)
        is_call = m["option_type"] == "call"
        if is_call:
            cv, coi = cv + vol, coi + oi
        else:
            pv, poi = pv + vol, poi + oi
        dte = (date.fromisoformat(m["expiry"]) - today).days
        iv, delta = o.get("iv") or 0, o.get("delta")
        if 20 <= dte <= 45 and iv > 0 and delta is not None:
            (skew_c if is_call else skew_p).append((abs(abs(delta) - 0.25), iv))
        if dte < MIN_DTE or vol <= 0:
            continue
        bid, ask = o.get("bid") or 0, o.get("ask") or 0
        mid = (bid + ask) / 2 if bid and ask else (o.get("last_trade_price") or 0)
        prem = vol * mid * 100
        prem_total += prem
        if vol >= MIN_VOL and (oi == 0 or vol / oi >= MIN_VOL_OI) and prem >= MIN_PREMIUM:
                unusual.append({"ticker": ticker, "contract": o["option"], "type": m["option_type"],
                                "strike": m["strike"], "expiry": m["expiry"], "dte": dte, "volume": vol, "oi": oi,
                                "vol_oi": vol / oi if oi else None, "premium": prem, "iv": iv or None,
                                "delta": delta, "otm": (m["strike"] / spot - 1) if spot else None})
    unusual = [u for u in unusual if u["premium"] >= MIN_PREM_SHARE * prem_total]
    skew = None
    if skew_c and skew_p:
        skew = min(skew_p)[1] - min(skew_c)[1]
    uc = sum(u["premium"] for u in unusual if u["type"] == "call")
    up = sum(u["premium"] for u in unusual if u["type"] == "put")
    tilt = ""
    net = (uc - up) / prem_total if prem_total else 0.0
    if uc >= TILT_MIN and uc >= TILT_RATIO * up and net >= TILT_SHARE:
        tilt = "看涨异动"
    elif up >= TILT_MIN and up >= TILT_RATIO * uc and -net >= TILT_SHARE:
        tilt = "看跌异动"
    return {"ticker": ticker, "price": spot, "iv30": ch.get("iv30"),
            "call_vol": cv, "put_vol": pv, "pc_vol": pv / cv if cv else None,
            "call_oi": coi, "put_oi": poi, "pc_oi": poi / coi if coi else None,
            "activity": (cv + pv) / (coi + poi) if coi + poi else None, "skew": skew,
            "premium_total": prem_total, "unusual_call_prem": uc, "unusual_put_prem": up, "net_unusual": net,
            "n_unusual": len(unusual), "tilt": tilt,
            "unusual": sorted(unusual, key=lambda u: -u["premium"])[:8]}


def scan(tickers: list[str], workers: int = 4) -> pd.DataFrame:
    us = OS.us_only(tickers)
    with ThreadPoolExecutor(workers) as ex:
        chains = dict(zip(us, ex.map(OS.chain, us)))
    rows = [s for t, c in chains.items() if c and (s := summarize(t, c))]
    df = pd.DataFrame(rows)
    df.attrs["time"] = datetime.now().isoformat(timespec="seconds")
    return df


# ─── 快照 ─────────────────────────────────────────────────────────────────────

def load_history() -> dict:
    if not HISTORY_PATH.exists():
        return {"days": {}}
    try:
        return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"days": {}}


def save_snapshot(df: pd.DataFrame, status: str) -> None:
    """
    覆盖写入今天的快照（同一天多次扫描以最后一次为准，收盘后的那次最完整）。
    只记汇总指标与前 3 个异动合约，文件保持小；每小时最多同步一次 GitHub。
    """
    if df.empty or status not in ("交易中", "已收盘"):
        return
    hist = load_history()
    day = config.market_today().isoformat()
    keep = ["price", "iv30", "pc_vol", "pc_oi", "activity", "skew", "call_vol", "put_vol", "premium_total",
            "unusual_call_prem", "unusual_put_prem", "net_unusual", "n_unusual", "tilt"]
    rows = {}
    for r in df.to_dict("records"):
        rows[r["ticker"]] = {k: (round(r[k], 5) if isinstance(r[k], float) else r[k]) for k in keep if r.get(k) is not None}
        rows[r["ticker"]]["top"] = [{k: u[k] for k in ("contract", "volume", "oi", "premium")} for u in r["unusual"][:3]]
    hist.setdefault("days", {})[day] = {"time": datetime.now().isoformat(timespec="seconds"), "status": status,
                                        "rows": rows}
    HISTORY_PATH.write_text(json.dumps(hist, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    if time.time() - hist.get("_synced", 0) >= SYNC_EVERY or status == "已收盘":
        try:
            from core.github_storage import sync_to_github
            sync_to_github(HISTORY_PATH, "data/option_flow_history.json", "chore: option flow snapshot")
        except Exception:
            pass


def history_frame(field: str) -> pd.DataFrame:
    """快照里某个指标的 日期 × 标的 矩阵（供与自身历史比较、日后评级）。"""
    days = load_history().get("days", {})
    return pd.DataFrame({d: {t: v.get(field) for t, v in x["rows"].items()} for d, x in days.items()}).T.sort_index()


def own_percentile(field: str, current: pd.Series, min_days: int = 20) -> pd.Series:
    """当前值在该标的自身历史里的百分位（历史不足 min_days 天时为 NaN）。"""
    h = history_frame(field)
    out = {}
    for t, v in current.items():
        if t not in h.columns or v is None or v != v:
            continue
        s = h[t].dropna()
        if len(s) >= min_days:
            out[t] = float((s < v).mean())
    return pd.Series(out, dtype=float)


# ─── 后台扫描（驾驶舱不阻塞）──────────────────────────────────────────────────

def run_background(tickers: list[str], status: str) -> None:
    """距上次扫描 ≥ SCAN_EVERY 时，起一个后台线程扫描并存快照；驾驶舱只读 latest()。"""
    if status not in ("交易中", "已收盘"):
        return
    with _lock:
        if _last["running"] or time.time() - _last["time"] < SCAN_EVERY:
            return
        _last["running"] = True

    def _work():
        try:
            df = scan(tickers)
            save_snapshot(df, status)
            _last.update(df=df, time=time.time())
        finally:
            _last["running"] = False

    threading.Thread(target=_work, daemon=True).start()


def set_latest(df: pd.DataFrame) -> None:
    _last.update(df=df, time=time.time())


def latest() -> pd.DataFrame | None:
    return _last["df"]
