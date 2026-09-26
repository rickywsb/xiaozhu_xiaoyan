"""core/sectors.py — 板块雷达：全市场板块强弱 / 轮动 / 宽度 / 成分股下钻

板块池 = 11 个 SPDR 行业 ETF + 9 个主题 ETF + config.CUSTOM_BASKETS 自定义篮子（等权合成）。

  · 强弱榜   复用 daily_momentum 的 calc_metrics + rank_metrics（与持仓同一套打分），
             另附 1/5/20/60 日收益、相对 SPY 强弱、5 个交易日前的排名（按历史价格倒推）
  · 轮动图   x = RS 趋势（60 日相对 SPY 收益），y = RS 动量（20 日相对收益较 10 日前的变化）
             → 领先 / 转弱 / 落后 / 改善 四象限
  · 宽度     S&P 500 成分股（Wikipedia，含 GICS 行业）站上 MA20/MA50、创 20 日新高的比例，
             区分"普涨"与"少数权重股拉动"
  · 下钻     板块成分股的动量排名
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import daily_momentum as dm

MARKET = "SPY"
PERIOD = "1y"

# (key, 名称, 分组, 成分来源)；成分来源：("gics", 行业) / ("sub", [细分行业]) / ("list", [代码])
_ETF_UNIVERSE: list[tuple[str, str, str, tuple]] = [
    ("XLK",  "科技",     "行业", ("gics", "Information Technology")),
    ("XLC",  "通信",     "行业", ("gics", "Communication Services")),
    ("XLY",  "可选消费", "行业", ("gics", "Consumer Discretionary")),
    ("XLP",  "必需消费", "行业", ("gics", "Consumer Staples")),
    ("XLF",  "金融",     "行业", ("gics", "Financials")),
    ("XLV",  "医疗",     "行业", ("gics", "Health Care")),
    ("XLI",  "工业",     "行业", ("gics", "Industrials")),
    ("XLE",  "能源",     "行业", ("gics", "Energy")),
    ("XLB",  "材料",     "行业", ("gics", "Materials")),
    ("XLU",  "公用事业", "行业", ("gics", "Utilities")),
    ("XLRE", "地产",     "行业", ("gics", "Real Estate")),
    ("SMH",  "半导体",   "主题", ("sub", ["Semiconductors", "Semiconductor Materials & Equipment"])),
    ("IGV",  "软件",     "主题", ("sub", ["Application Software", "Systems Software"])),
    ("XBI",  "生物科技", "主题", ("sub", ["Biotechnology"])),
    ("ITA",  "军工",     "主题", ("sub", ["Aerospace & Defense"])),
    ("KRE",  "地区银行", "主题", ("sub", ["Regional Banks"])),
    ("GDX",  "黄金矿业", "主题", ("list", ["NEM", "AEM", "B", "WPM", "FNV", "KGC", "AU", "GFI", "RGLD"])),
    ("COPX", "铜矿",     "主题", ("list", ["FCX", "SCCO", "TECK", "HBM", "ERO"])),
    ("URA",  "铀",       "主题", ("list", ["CCJ", "NXE", "UEC", "DNN", "UUUU", "LEU"])),
    ("TAN",  "太阳能",   "主题", ("list", ["FSLR", "ENPH", "SEDG", "RUN", "NXT", "ARRY", "CSIQ"])),
]

MIN_BREADTH_MEMBERS = 5      # 成分股少于此数不计算宽度
QUADRANTS = {(True, True): "🟢 领先", (True, False): "🟡 转弱",
             (False, False): "🔴 落后", (False, True): "🔵 改善"}


def universe() -> list[dict]:
    """板块池：[{key, name, group, source}]，含 config.CUSTOM_BASKETS。"""
    import config
    out = [{"key": k, "name": n, "group": g, "source": src} for k, n, g, src in _ETF_UNIVERSE]
    for name, members in config.CUSTOM_BASKETS.items():
        out.append({"key": name, "name": name, "group": "自定义", "source": ("list", list(members))})
    return out


# ─── 价格序列 ─────────────────────────────────────────────────────────────────

def _basket_index(closes: dict[str, pd.Series], members: list[str]) -> pd.Series | None:
    """等权篮子指数：成分日收益均值累乘（每日再平衡），起点 = 1。"""
    px = pd.DataFrame({t: closes[t] for t in members if t in closes})
    if px.empty:
        return None
    rets = px.sort_index().pct_change(fill_method=None).mean(axis=1, skipna=True).iloc[1:]
    return (1 + rets.fillna(0)).cumprod()


def sector_series(period: str = PERIOD) -> dict[str, pd.Series]:
    """{板块 key: 价格序列}（ETF 收盘价 / 篮子等权指数），另含 MARKET 基准。"""
    import config
    etfs = [k for k, *_ in _ETF_UNIVERSE] + [MARKET]
    members = sorted({t for ms in config.CUSTOM_BASKETS.values() for t in ms})
    closes = dm.fetch_histories(etfs + members, period=period, usd=True)
    out = {k: closes[k] for k in etfs if k in closes}
    for name, ms in config.CUSTOM_BASKETS.items():
        idx = _basket_index(closes, ms)
        if idx is not None:
            out[name] = idx
    return out


# ─── 强弱榜 + 轮动 ────────────────────────────────────────────────────────────

def _rel_return(s: pd.Series, m: pd.Series, days: int, end_offset: int = 0) -> float | None:
    """截至倒数第 end_offset 根，days 日内 板块收益 − 大盘收益。"""
    both = pd.concat([s, m], axis=1, join="inner").dropna()
    if end_offset:
        both = both.iloc[:-end_offset]
    if len(both) < days + 1:
        return None
    a, b = both.iloc[-(days + 1)], both.iloc[-1]
    return float(b.iloc[0] / a.iloc[0] - b.iloc[1] / a.iloc[1])


def _rrg_point(s: pd.Series, m: pd.Series, offset: int = 0) -> tuple[float | None, float | None]:
    """(RS 趋势, RS 动量)：60 日相对收益，20 日相对收益较 10 日前的变化。"""
    x = _rel_return(s, m, 60, offset)
    r_now = _rel_return(s, m, 20, offset)
    r_prev = _rel_return(s, m, 20, offset + 10)
    y = (r_now - r_prev) if (r_now is not None and r_prev is not None) else None
    return x, y


def _ranked(series: dict[str, pd.Series], meta: dict[str, dict], offset: int = 0) -> pd.DataFrame:
    rows = []
    for key, s in series.items():
        if key == MARKET or key not in meta:
            continue
        c = s.dropna()
        if offset:
            c = c.iloc[:-offset]
        m = dm.calc_metrics(key, meta[key]["name"], c)
        if m:
            rows.append(m)
    return dm.rank_metrics(rows) if rows else pd.DataFrame()


def sector_board(period: str = PERIOD) -> pd.DataFrame:
    """
    板块强弱榜（按 composite 降序）。列：
      key, name, group, rank, rank_5d_ago, rank_chg(正=上升), composite, heat, trend_z,
      ret_1d/5d/20d/60d, rs_20d/rs_60d（相对 SPY）, rrg_x, rrg_y, quadrant, direction
    """
    series = sector_series(period)
    meta = {u["key"]: u for u in universe()}
    board = _ranked(series, meta)
    if board.empty:
        return board
    prev = _ranked(series, meta, offset=5)
    prev_rank = dict(zip(prev["ticker"], prev["rank"])) if not prev.empty else {}

    mkt = series.get(MARKET)
    out = []
    for _, r in board.iterrows():
        key = r["ticker"]
        s = series[key].dropna()
        ret_1d = float(s.iloc[-1] / s.iloc[-2] - 1) if len(s) > 1 else None
        rs20 = _rel_return(s, mkt, 20) if mkt is not None else None
        rs60 = _rel_return(s, mkt, 60) if mkt is not None else None
        x, y = _rrg_point(s, mkt) if mkt is not None else (None, None)
        quad = QUADRANTS[(x > 0, y > 0)] if (x is not None and y is not None) else "—"
        pr = prev_rank.get(key)
        out.append({
            "key": key, "name": meta[key]["name"], "group": meta[key]["group"],
            "rank": int(r["rank"]), "rank_5d_ago": pr,
            "rank_chg": (pr - int(r["rank"])) if pr is not None else None,
            "composite": r["composite"], "heat": r["heat"], "trend_z": r["trend_z"],
            "direction": r["direction"], "ret_1d": ret_1d,
            "ret_5d": r["ret_5d"], "ret_20d": r["ret_20d"], "ret_60d": r["ret_60d"],
            "rs_20d": rs20, "rs_60d": rs60, "rrg_x": x, "rrg_y": y, "quadrant": quad,
        })
    return pd.DataFrame(out)


def rrg_trails(keys: list[str], points: int = 5, step: int = 5,
               period: str = PERIOD) -> pd.DataFrame:
    """轮动图尾迹：每个板块最近 points 个点（间隔 step 个交易日），列 key, name, t(0=最新), x, y。"""
    series = sector_series(period)
    meta = {u["key"]: u for u in universe()}
    mkt = series.get(MARKET)
    rows = []
    if mkt is None:
        return pd.DataFrame(rows)
    for key in keys:
        s = series.get(key)
        if s is None:
            continue
        for i in range(points):
            x, y = _rrg_point(s.dropna(), mkt, i * step)
            if x is not None and y is not None:
                rows.append({"key": key, "name": meta[key]["name"], "t": i, "x": x, "y": y})
    return pd.DataFrame(rows)


# ─── S&P 500 成分股（Wikipedia）───────────────────────────────────────────────
_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15"


def sp500_constituents() -> pd.DataFrame:
    """S&P 500 成分：DataFrame[ticker, name, gics_sector, sub_industry]；ticker 已转 Yahoo 格式（BRK.B→BRK-B）。"""
    html = requests.get(_SP500_URL, headers={"User-Agent": _UA}, timeout=30).text
    for t in pd.read_html(io.StringIO(html)):
        if {"Symbol", "GICS Sector", "GICS Sub-Industry"} <= set(t.columns):
            return pd.DataFrame({
                "ticker": t["Symbol"].astype(str).str.strip().str.upper().str.replace(".", "-", regex=False),
                "name": t.get("Security", t["Symbol"]),
                "gics_sector": t["GICS Sector"],
                "sub_industry": t["GICS Sub-Industry"],
            })
    raise ValueError("Wikipedia 页面中未找到 S&P 500 成分表")


def members_of(key: str, sp500: pd.DataFrame | None) -> dict[str, str]:
    """板块成分 {ticker: 名称}；需要 S&P 500 表而未提供时返回空。"""
    u = {x["key"]: x for x in universe()}.get(key)
    if u is None:
        return {}
    kind, arg = u["source"]
    if kind == "list":
        return {t: t for t in arg}
    if sp500 is None or sp500.empty:
        return {}
    if kind == "gics":
        sub = sp500[sp500["gics_sector"] == arg]
    else:
        sub = sp500[sp500["sub_industry"].isin(arg)]
    return dict(zip(sub["ticker"], sub["name"]))


# ─── 市场宽度 ─────────────────────────────────────────────────────────────────

def _download_chunked(tickers: list[str], period: str, chunk: int = 100) -> dict[str, pd.Series]:
    out: dict[str, pd.Series] = {}
    for i in range(0, len(tickers), chunk):
        out.update(dm.fetch_histories(tickers[i:i + chunk], period=period))
    return out


def breadth(sp500: pd.DataFrame, period: str = "6mo") -> pd.DataFrame:
    """
    各板块宽度：成分股中 站上 MA20 / 站上 MA50 / 创 20 日新高（收盘）的比例。
    成分 < MIN_BREADTH_MEMBERS 的板块不计算。另附全体 S&P 500 一行（key="S&P 500"）。
    """
    import config
    basket_members = sorted({t for ms in config.CUSTOM_BASKETS.values() for t in ms})
    list_members = sorted({t for _, _, _, (kind, arg) in _ETF_UNIVERSE if kind == "list" for t in arg})
    tickers = sorted(set(sp500["ticker"]) | set(basket_members) | set(list_members))
    closes = _download_chunked(tickers, period)

    stat: dict[str, dict] = {}
    for t, c in closes.items():
        c = c.dropna()
        if len(c) < 50:
            continue
        last = float(c.iloc[-1])
        stat[t] = {
            "ma20": last > float(c.tail(20).mean()),
            "ma50": last > float(c.tail(50).mean()),
            "hi20": last >= float(c.tail(20).max()),
        }

    def _agg(key: str, name: str, group: str, members: list[str]) -> dict | None:
        got = [stat[t] for t in members if t in stat]
        if len(got) < MIN_BREADTH_MEMBERS:
            return None
        n = len(got)
        return {"key": key, "name": name, "group": group, "n": n,
                "above_ma20": sum(g["ma20"] for g in got) / n,
                "above_ma50": sum(g["ma50"] for g in got) / n,
                "new_high20": sum(g["hi20"] for g in got) / n}

    rows = [_agg("S&P 500", "标普500全体", "大盘", list(sp500["ticker"]))]
    for u in universe():
        rows.append(_agg(u["key"], u["name"], u["group"], list(members_of(u["key"], sp500))))
    return pd.DataFrame([r for r in rows if r])


# ─── 日报摘要 ─────────────────────────────────────────────────────────────────

def daily_summary(top_n: int = 3) -> dict:
    """给日报的板块轮动摘要：最强 / 最弱板块 + 四象限分布。"""
    board = sector_board()
    if board.empty:
        return {}

    def _row(r) -> dict:
        pct = lambda v: round(float(v) * 100, 1) if v is not None and pd.notna(v) else None
        return {"板块": f"{r['name']}({r['key']})", "20日%": pct(r["ret_20d"]),
                "相对SPY 60日%": pct(r["rs_60d"]), "象限": r["quadrant"],
                "排名变化(5日)": int(r["rank_chg"]) if pd.notna(r["rank_chg"]) else None}

    quads = {q: board.loc[board["quadrant"] == q, "name"].tolist() for q in QUADRANTS.values()}
    risers = board.dropna(subset=["rank_chg"]).sort_values("rank_chg", ascending=False).head(top_n)
    return {
        "口径": "板块强弱=与持仓同一套动量打分(板块间相对排名)；象限按相对SPY的60日强弱与近期变化划分",
        "最强板块": [_row(r) for _, r in board.head(top_n).iterrows()],
        "最弱板块": [_row(r) for _, r in board.tail(top_n).iloc[::-1].iterrows()],
        "排名上升最快": [_row(r) for _, r in risers.iterrows() if r["rank_chg"] > 0],
        "象限分布": {k: v for k, v in quads.items() if v},
    }

