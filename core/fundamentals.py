"""core/fundamentals.py — SEC EDGAR 基本面（季度 EPS / 营收）→ 评分的「基本面」成分（O'Neil CAN SLIM 的 C 与 A）

数据：SEC EDGAR XBRL frames（政府数据，可自由使用）——一个请求拿到全市场某个会计科目某一季度的值。
  EPS   EarningsPerShareDiluted（缺则 Basic）
  营收  Revenues / RevenueFromContractWithCustomerExcludingAssessedTax / SalesRevenueNet / RevenuesNetOfInterestExpense
  Q4    多数公司不单独申报第四季度：Q4 = 全年 − Q1 − Q2 − Q3
时点（避免前视）：季度结束后 45 天才视为可用（Q4 / 年报 75 天），比法定截止日更保守。
  frames 给的是最新申报值（含后来的重述），这一点仍有轻微前视，影响很小。
覆盖：在美申报 10-Q / 10-K 的公司；外国发行人（20-F，IFRS）与非美股没有数据 → 成分记为中性 0.5。

因子（每天在全市场横截面取百分位）：
  eps_yoy    最新季度 EPS 同比（上年同期 ≤0 而本季 >0 记为 +100%；两者都 ≤0 记缺失）
  rev_yoy    最新季度营收同比
  eps_accel  EPS 同比的变化（本季同比 − 上季同比，加速为正）
SEC 访问须在请求头声明联系邮箱：st.secrets / 环境变量 SEC_USER_AGENT（不写进代码仓库）。上限 10 次/秒。
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config

FUND_PATH = config.DATA_DIR / "fundamentals.json"
EPS_TAGS = [("EarningsPerShareDiluted", "USD-per-shares"), ("EarningsPerShareBasic", "USD-per-shares")]
REV_TAGS = [("Revenues", "USD"), ("RevenueFromContractWithCustomerExcludingAssessedTax", "USD"),
            ("SalesRevenueNet", "USD"), ("RevenuesNetOfInterestExpense", "USD")]
LAG_Q, LAG_Q4 = 45, 75
FIRST_YEAR = 2023
_MIN_INTERVAL = 0.13            # ≈ 7.7 次/秒，低于 SEC 10 次/秒上限
_last_call = [0.0]


# ─── SEC 访问 ─────────────────────────────────────────────────────────────────

def _user_agent() -> str:
    ua = os.environ.get("SEC_USER_AGENT")
    if not ua:
        try:
            import streamlit as st
            ua = st.secrets.get("SEC_USER_AGENT")
        except Exception:
            ua = None
    if not ua:
        try:
            import tomllib
            ua = tomllib.loads((config.BASE_DIR / ".streamlit" / "secrets.toml").read_text()).get("SEC_USER_AGENT")
        except Exception:
            ua = None
    if not ua:
        raise RuntimeError("未配置 SEC_USER_AGENT（SEC 要求请求头带联系邮箱，如 \"name you@example.com\"）")
    return ua


def _get(url: str) -> dict | None:
    for attempt in range(3):
        wait = _MIN_INTERVAL - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()
        try:
            r = requests.get(url, headers={"User-Agent": _user_agent(), "Accept-Encoding": "gzip, deflate"}, timeout=30)
        except requests.RequestException:
            time.sleep(1 + attempt)
            continue
        if r.status_code == 404:
            return None
        if r.status_code == 200:
            return r.json()
        time.sleep(2 + 2 * attempt)            # 429 / 403 / 5xx：退避重试
    return None


def cik_map() -> dict[str, int]:
    d = _get("https://www.sec.gov/files/company_tickers.json") or {}
    return {v["ticker"].upper(): int(v["cik_str"]) for v in d.values()}


def _frame(tag: str, unit: str, period: str) -> dict[int, dict]:
    d = _get(f"https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/{unit}/{period}.json")
    return {x["cik"]: {"val": x["val"], "end": x["end"]} for x in (d or {}).get("data", [])}


def _merged(tags, period: str) -> dict[int, dict]:
    """多个候选科目按优先级合并：每家公司取第一个有值的科目。"""
    out: dict[int, dict] = {}
    for tag, unit in tags:
        for cik, v in _frame(tag, unit, period).items():
            out.setdefault(cik, v)
    return out


# ─── 下载 → 季度表 ────────────────────────────────────────────────────────────

def _quarters(today: date) -> list[tuple[int, int]]:
    out = []
    for y in range(FIRST_YEAR, today.year + 1):
        for q in range(1, 5):
            end = date(y, 3 * q, 28)
            if end + timedelta(days=LAG_Q - 10) <= today:
                out.append((y, q))
    return out


def download(tickers: list[str], progress=None) -> pd.DataFrame:
    """
    股票池的季度 EPS / 营收表（长表）：ticker, quarter(YYYYQn), end, avail, eps, rev。
    约 100 个请求（每个 0.1–0.3 MB），1–2 分钟。
    """
    today = date.today()
    cm = cik_map()
    want = {cm[t.upper()]: t.upper() for t in tickers if t.upper() in cm}
    qs = _quarters(today)
    years = sorted({y for y, q in qs if (y, 4) in qs or date(y, 12, 31) + timedelta(days=LAG_Q4 - 15) <= today})
    raw: dict[tuple[str, str], dict] = {}
    jobs = [(f"CY{y}Q{q}", "q") for y, q in qs] + [(f"CY{y}", "y") for y in years]
    for i, (period, kind) in enumerate(jobs):
        if progress:
            progress(i, len(jobs))
        raw[(period, "eps")] = _merged(EPS_TAGS, period)
        raw[(period, "rev")] = _merged(REV_TAGS, period)
    rows = []
    for cik, t in want.items():
        for y, q in qs + [(y, 4) for y in years if (y, 4) not in qs]:
            p = f"CY{y}Q{q}"
            rec = {"ticker": t, "quarter": f"{y}Q{q}"}
            for f in ("eps", "rev"):
                v = raw.get((p, f), {}).get(cik)
                if v is None and q == 4:                   # Q4 = 全年 − 前三季
                    a = raw.get((f"CY{y}", f), {}).get(cik)
                    parts = [raw.get((f"CY{y}Q{k}", f), {}).get(cik) for k in (1, 2, 3)]
                    if a and all(parts):
                        v = {"val": a["val"] - sum(x["val"] for x in parts), "end": a["end"]}
                rec[f] = v["val"] if v else None
                if v and "end" not in rec:
                    rec["end"] = v["end"]
            if rec.get("eps") is None and rec.get("rev") is None:
                continue
            end = pd.Timestamp(rec.get("end") or date(y, 3 * q, 28))
            rec["end"] = end.date().isoformat()
            rec["avail"] = (end + pd.Timedelta(days=LAG_Q4 if q == 4 else LAG_Q)).date().isoformat()
            rows.append(rec)
    return pd.DataFrame(rows)


def save(df: pd.DataFrame) -> Path:
    FUND_PATH.write_text(json.dumps({"updated": date.today().isoformat(),
                                     "rows": json.loads(df.to_json(orient="records"))},
                                    ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return FUND_PATH


def load() -> tuple[pd.DataFrame, str | None]:
    if not FUND_PATH.exists():
        return pd.DataFrame(), None
    try:
        d = json.loads(FUND_PATH.read_text(encoding="utf-8"))
        return pd.DataFrame(d["rows"]), d.get("updated")
    except Exception:
        return pd.DataFrame(), None


# ─── 因子 ─────────────────────────────────────────────────────────────────────

def _growth(cur: pd.Series, prev: pd.Series, lo: float, hi: float, turnaround: float | None) -> pd.Series:
    g = (cur - prev) / prev.abs()
    if turnaround is not None:
        g = g.where(prev > 0, np.where((prev <= 0) & (cur > 0), turnaround, np.nan))
    return g.clip(lo, hi)


def quarterly_factors(q: pd.DataFrame) -> pd.DataFrame:
    """每只股票每个季度：eps_yoy / rev_yoy / eps_accel（按季度排序后与 4 个季度前比较）。"""
    if q.empty:
        return q
    q = q.sort_values(["ticker", "quarter"]).copy()
    g = q.groupby("ticker")
    eps4, rev4 = g["eps"].shift(4), g["rev"].shift(4)
    q["eps_yoy"] = _growth(q["eps"], eps4, -1.0, 3.0, 1.0)
    q["rev_yoy"] = _growth(q["rev"], rev4, -0.5, 2.0, None).where(rev4 > 0)
    q["eps_accel"] = (q["eps_yoy"] - q.groupby("ticker")["eps_yoy"].shift(1)).clip(-2, 2)
    return q


def factor_matrices(q: pd.DataFrame, index: pd.DatetimeIndex, columns) -> dict[str, pd.DataFrame]:
    """把季度因子按「可用日」铺到 日期 × 股票 矩阵上（之后向前填充到下一季可用）。"""
    f = quarterly_factors(q)
    out = {}
    for k in ("eps_yoy", "rev_yoy", "eps_accel"):
        m = pd.DataFrame(index=index, columns=list(columns), dtype=float)
        if not f.empty:
            for t, sub in f.dropna(subset=[k]).groupby("ticker"):
                if t not in m.columns:
                    continue
                s = pd.Series(sub[k].to_numpy(dtype=float), index=pd.to_datetime(sub["avail"])).sort_index()
                s = s[~s.index.duplicated(keep="last")]
                m[t] = s.reindex(index.union(s.index)).ffill().reindex(index)
        out[k] = m
    return out


def component(mats: dict[str, pd.DataFrame], valid: pd.DataFrame, parts=("eps_yoy", "rev_yoy", "eps_accel")) -> pd.DataFrame:
    """基本面成分 F（0–1）：所选因子全市场百分位的平均；没有数据的股票记中性 0.5。"""
    pct = [mats[k].where(valid).rank(axis=1, pct=True) for k in parts]
    F = pd.concat(pct, keys=range(len(pct))).groupby(level=1).mean().reindex(valid.index)
    return F.fillna(0.5).where(valid)


def coverage(mats: dict[str, pd.DataFrame]) -> float:
    m = mats["eps_yoy"]
    return float(m.iloc[-1].notna().mean()) if len(m) else 0.0
