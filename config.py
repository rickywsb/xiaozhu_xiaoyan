"""config.py — 全局路径与参数常量"""

from pathlib import Path

# ── 目录 ──────────────────────────────────────────────────────────────────────
APP_DIR       = Path(__file__).resolve().parent
DATA_DIR      = APP_DIR / "data"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"

# ── 数据文件 ──────────────────────────────────────────────────────────────────
PORTFOLIO_PATH   = DATA_DIR / "portfolio.json"
PRICE_CACHE_PATH = DATA_DIR / "price_cache.json"
FX_CACHE_PATH    = DATA_DIR / "fx_cache.json"
WATCHLIST_PATH   = DATA_DIR / "watchlist.json"
NOTES_DIR        = DATA_DIR / "notes"

# ── 关联的历史 xlsx（仅供参考/导出，不作为数据源）────────────────────────────
XLSX_PATH = APP_DIR.parent / "value_update" / "小白小鸡毛基金管理公司.xlsx"

# ── 价格缓存参数 ──────────────────────────────────────────────────────────────
FX_CACHE_TTL_MINUTES = 60   # FX 汇率缓存有效期（分钟）

# ── 板块色彩映射（Plotly color）──────────────────────────────────────────────
SECTOR_COLORS = {
    "光":   "#4C9BE8",
    "存":   "#E8844C",
    "配置": "#4CE87A",
    "半导体": "#B44CE8",
    "其他": "#E8D04C",
    "期权": "#A0A0A0",
    "现金": "#5DE8D0",
}

# 现金仓位的特殊 yf_ticker（price 固定为 1.0 USD，shares = 金额）
CASH_TICKER = "CASH"

# ── 非美元标的的原始货币（yf_ticker → 货币代码；GBp = 英国便士）──────────────
CURRENCY_MAP: dict[str, str] = {
    "3363.TWO":  "TWD",
    "IQE.L":     "GBp",
    "000660.KS": "KRW",
    "7709.HK":   "HKD",
    "XFAB.PA":   "EUR",
    "SIVE.ST":   "SEK",
}

# ── 杠杆产品（yf_ticker → 杠杆倍数）：动量排名时单独标注 ──────────────────────
LEVERAGED_TICKERS: dict[str, int] = {
    "7709.HK": 2,   # CSOP SK Hynix Daily 2x
    "AAOX":    2,   # Tradr 2X Long AAOI Daily ETF
}

# ── 相对强度基准：按持仓 sector 选板块基准，另统一对比大盘 ──────────────────
SECTOR_BENCHMARKS: dict[str, str] = {
    "半导体": "SOXX",
    "存":     "SOXX",
    "光":     "LAZR",   # Tema Photonics & Optical ETF（2026-06-30 上市，历史不足 3 月时 RS 按 1 月算）
    "配置":   "QQQ",
    "其他":   "QQQ",
}
DEFAULT_BENCHMARK = "SPY"      # 未映射板块 / 大盘对比基准

# ── 市场日期：以美东时间为准（云端服务器是 UTC，美东晚 8 点后会跨日）───────────
MARKET_TZ = "America/New_York"


def market_today():
    """当前美东日期。替代 date.today()，避免 UTC 服务器跨日。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(MARKET_TZ)).date()

# ── 确保目录存在 ──────────────────────────────────────────────────────────────
DATA_DIR.mkdir(parents=True, exist_ok=True)
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
