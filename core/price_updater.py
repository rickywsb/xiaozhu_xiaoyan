"""core/price_updater.py — 抓取最新收盘价并换算为 USD"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

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
}


def fetch_price(yf_ticker: str) -> Optional[float]:
    """获取单只股票最新收盘价（原始货币）。"""
    try:
        hist = yf.Ticker(yf_ticker).history(period="5d")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception:
        pass
    return None


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

    prices: dict[str, float] = {}
    failed: list[str] = []

    for ticker in sorted(tickers):
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

    config.PRICE_CACHE_PATH.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return cache


def load_cache() -> Optional[dict]:
    """读取价格缓存，不存在则返回 None。"""
    if config.PRICE_CACHE_PATH.exists():
        try:
            return json.loads(config.PRICE_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None
