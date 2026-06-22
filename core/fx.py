"""core/fx.py — FX 汇率获取，带 60 分钟文件缓存"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import yfinance as yf

# 确保 config 可以 import（无论从哪个目录运行）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

# 需要的货币对（非 USD → USD 方向）
_FX_PAIRS: dict[str, str] = {
    "TWD": "TWDUSD=X",
    "GBP": "GBPUSD=X",
    "EUR": "EURUSD=X",
    "KRW": "KRWUSD=X",
    "HKD": "HKDUSD=X",
}


def get_fx_rates(force_refresh: bool = False) -> dict[str, float]:
    """
    返回各货币兑 USD 的最新汇率 dict，例如 {"TWD": 0.0316, "GBP": 1.321, ...}
    60 分钟内复用文件缓存，不重复请求 yfinance。
    """
    cache_path = config.FX_CACHE_PATH

    # 尝试读取有效缓存
    if not force_refresh and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            cached_at = datetime.fromisoformat(cached["cached_at"])
            ttl = timedelta(minutes=config.FX_CACHE_TTL_MINUTES)
            if datetime.now() - cached_at < ttl:
                return cached["rates"]
        except Exception:
            pass

    # 重新拉取
    rates: dict[str, float] = {}
    for currency, symbol in _FX_PAIRS.items():
        try:
            hist = yf.Ticker(symbol).history(period="5d")
            if not hist.empty:
                rates[currency] = float(hist["Close"].iloc[-1])
        except Exception:
            pass

    # 写缓存
    try:
        cache_path.write_text(
            json.dumps({"cached_at": datetime.now().isoformat(timespec="seconds"),
                        "rates": rates}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass

    return rates
