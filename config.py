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

# ── 确保目录存在 ──────────────────────────────────────────────────────────────
DATA_DIR.mkdir(parents=True, exist_ok=True)
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
