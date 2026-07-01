"""core/price_updater.py — 抓取最新价格（实时/收盘）并换算为 USD"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
from core.fx import get_fx_rates
# 原始货币映射（yf_ticker → 原始货币代码）
# GBp = 英国便士（需 ÷100 转 GBP 再乘汇率）
_CURRENCY_MAP: dict[str, str] = {
    "3363.TWO": "TWD",
    "IQE.L":    "GBp",
    "000660.KS": "KRW",
    "7709.HK":  "HKD",
    "XFAB.PA":  "EUR",
    "SIVE.ST":  "SEK",
}


def _extract_last_close(hist) -> float | None:
    """从 yfinance 历史 DataFrame 取最后一个有效收盘价。"""
    try:
        if hist is None or hist.empty:
            return None
        closes = hist["Close"].dropna()
        if len(closes):
            return float(closes.iloc[-1])
    except Exception:
        pass
    return None


def fetch_price(yf_ticker: str, retries: int = 2) -> float | None:
    """获取单只股票最新收盘价（原始货币），带重试。
    特殊 ticker：CASH → 固定返回 1.0（美元现金，shares = 金额）。
    """
    if yf_ticker.upper() == config.CASH_TICKER:
        return 1.0
    for attempt in range(retries + 1):
        try:
            hist = yf.Ticker(yf_ticker).history(period="5d")
            price = _extract_last_close(hist)
            if price is not None:
                return price
        except Exception:
            pass
        if attempt < retries:
            time.sleep(0.5 * (attempt + 1))  # 退避，缓解限流
    return None


def _batch_fetch(tickers: list[str]) -> dict[str, float]:
    """一次性批量下载所有 ticker（原始货币收盘价）。
    返回 {ticker: close}；失败的 ticker 不在返回值中。
    一个请求替代 N 个请求，大幅减少 Yahoo 限流（HTTP 429）。
    """
    out: dict[str, float] = {}
    if not tickers:
        return out
    try:
        data = yf.download(
            tickers, period="5d", interval="1d",
            group_by="ticker", auto_adjust=False,
            threads=True, progress=False,
        )
    except Exception:
        return out
    if data is None or data.empty:
        return out

    # 单 ticker 时列不是 MultiIndex
    if len(tickers) == 1:
        price = _extract_last_close(data)
        if price is not None:
            out[tickers[0]] = price
        return out

    for t in tickers:
        try:
            sub = data[t]
        except Exception:
            continue
        price = _extract_last_close(sub)
        if price is not None:
            out[t] = price
    return out


def _fetch_live_map(tickers: list[str]) -> dict[str, float]:
    """批量获取实时价 fast_info['lastPrice']（原始货币）。
    盘中 → 当前价；收盘后 → 当日收盘价。解决美股盘中只能拿到昨收的问题。
    返回 {ticker: price}；失败的不在返回值中。
    """
    out: dict[str, float] = {}
    if not tickers:
        return out
    try:
        tobj = yf.Tickers(" ".join(tickers))
    except Exception:
        return out
    for t in tickers:
        try:
            v = tobj.tickers[t].fast_info["lastPrice"]
            if v is not None and float(v) > 0:
                out[t] = float(v)
        except Exception:
            pass
    return out


def to_usd(raw: float, yf_ticker: str, fx_rates: dict[str, float]) -> float:
    """将原始价格换算为 USD。"""
    currency = _CURRENCY_MAP.get(yf_ticker)
    if currency is None:
        return raw                                        # 已是 USD
    if currency == "GBp":
        return (raw / 100.0) * fx_rates.get("GBP", 1.0) # 便士→英镑→USD
    rate = fx_rates.get(currency)
    return raw * rate if rate else raw


def update_all_prices(portfolio: dict) -> dict:
    """
    遍历 portfolio["accounts"] 中所有持仓，获取最新 USD 价格。

    返回 cache dict，同时写入 config.PRICE_CACHE_PATH：
    {
        "updated_at": "...",
        "fx_rates": {...},
        "prices": { yf_ticker: usd_price, ... },
        "failed": [ yf_ticker, ... ]
    }
    """
    fx_rates = get_fx_rates(force_refresh=True)  # 更新时强制刷新汇率

    # 收集所有唯一 yf_ticker
    tickers: set[str] = set()
    for account in portfolio.get("accounts", []):
        for pos in account.get("positions", []):
            tickers.add(pos["yf_ticker"])

    # 现金单独处理（固定 1.0），其余批量抓取
    cash_tickers = {t for t in tickers if t.upper() == config.CASH_TICKER}
    real_tickers = sorted(tickers - cash_tickers)

    prices: dict[str, float] = {}
    failed: list[str] = []

    for t in cash_tickers:
        prices[t] = 1.0

    # ① 实时价优先（盘中=当前价，收盘后=今日收盘）
    live_map = _fetch_live_map(real_tickers)

    # ② 实时价缺失的，用批量历史收盘兑底（一个请求）
    missing = [t for t in real_tickers if t not in live_map]
    close_map = _batch_fetch(missing) if missing else {}

    # ③ 仍缺失的逐个重试兑底
    for ticker in real_tickers:
        raw = live_map.get(ticker)
        if raw is None:
            raw = close_map.get(ticker)
        if raw is None:
            raw = fetch_price(ticker)
        if raw is not None:
            prices[ticker] = round(to_usd(raw, ticker, fx_rates), 2)
        else:
            failed.append(ticker)

    cache = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "fx_rates": fx_rates,
        "prices": prices,
        "failed": failed,
    }

    # ④ 期权：抓价 + Black-Scholes 希腊字母 + 手动覆盖
    from core.options import fetch_option, resolve_option_value
    from core.snapshots import save_snapshot

    options_out: dict[str, dict] = {}
    for o in portfolio.get("options", []):
        contract = str(o.get("contract", "")).strip()
        if not contract:
            continue
        contracts = float(o.get("contracts", 1) or 1)
        manual = o.get("manual_mark")
        try:
            q = fetch_option(contract)
        except Exception:
            q = None
        r = resolve_option_value(q, contracts=contracts, manual_mark=manual)
        options_out[contract] = {
            "display":          o.get("display", contract),
            "sector":           o.get("sector", "期权"),
            "contracts":        contracts,
            "mark":             r["mark"],
            "value":            r["value"],
            "source":           r["source"],
            "flagged":          r["flagged"],
            "deviation":        r["deviation"],
            "fetched_mark":     r["fetched_mark"],
            "iv":               q.iv if q else None,
            "delta":            q.delta if q else None,
            "gamma":            q.gamma if q else None,
            "theta":            q.theta if q else None,
            "vega":             q.vega if q else None,
            "underlying_price": q.underlying_price if q else None,
            "last_price":       q.last_price if q else None,
            "bid":              q.bid if q else None,
            "ask":              q.ask if q else None,
            "days_to_expiry":   q.days_to_expiry if q else None,
        }
    cache["options"] = options_out

    # ⑤ 归档当日全持仓快照（股票 + 期权），供日间对比
    snap_positions: dict[str, dict] = {}
    for account in portfolio.get("accounts", []):
        for pos in account.get("positions", []):
            t = pos["yf_ticker"]
            p = prices.get(t)
            sh = pos.get("shares")
            snap_positions[t] = {
                "price": p,
                "value": round(p * sh, 2) if (p and sh) else None,
                "kind": "stock",
            }
    for contract, od in options_out.items():
        snap_positions[contract] = {
            "price": od["mark"],
            "value": od["value"],
            "kind": "option",
            "iv": od["iv"],
            "delta": od["delta"],
            "gamma": od["gamma"],
            "theta": od["theta"],
            "vega": od["vega"],
            "underlying_price": od["underlying_price"],
        }
    try:
        save_snapshot(snap_positions)
    except Exception:
        pass

    config.PRICE_CACHE_PATH.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return cache


def load_cache() -> dict | None:
    """读取价格缓存，不存在则返回 None。"""
    if config.PRICE_CACHE_PATH.exists():
        try:
            return json.loads(config.PRICE_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None
