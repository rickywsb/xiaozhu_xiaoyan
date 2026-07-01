"""core/snapshots.py — 每日全持仓快照 + 与昨日对比

每次「一键更新价格」时，把当日所有持仓（股票 + 期权）的价格与关键指标
写入 data/snapshots/YYYY-MM-DD.json（同日覆盖）。之后可与"最近一个更早的
快照"对比，得到每支持仓的日间变化（价格 Δ、涨跌 %、期权的 ΔIV/Δdelta 等）。

快照结构：
{
  "date": "2026-06-30",
  "saved_at": "2026-06-30T14:03:00",
  "positions": {
    "MU":                 {"price": 1132.3, "value": 45000.0, "kind": "stock"},
    "GLW270617C00155000": {"price": 133.6, "value": 13360.0, "kind": "option",
                           "iv": 0.94, "delta": 0.85, "theta": -0.09, ...}
  }
}
"""

import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

SNAPSHOTS_DIR = config.SNAPSHOTS_DIR

# 参与日间对比的数值字段（存在才比）
_DIFF_FIELDS = ("price", "value", "iv", "delta", "gamma", "theta", "vega",
                "underlying_price")


def _snapshot_path(d: str) -> Path:
    return SNAPSHOTS_DIR / f"{d}.json"


def save_snapshot(positions: dict[str, dict], snap_date: str | None = None) -> Path:
    """保存当日快照（同日覆盖）。

    positions: {key: {price, value, kind, ...其它指标}}
    key 用股票的 yf_ticker 或期权的 OCC 合约代码。
    """
    d = snap_date or date.today().isoformat()
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": d,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "positions": positions,
    }
    path = _snapshot_path(d)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_snapshot(snap_date: str) -> dict | None:
    """读取指定日期的快照，不存在返回 None。"""
    path = _snapshot_path(snap_date)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def list_snapshot_dates() -> list[str]:
    """返回所有快照日期（升序）。"""
    if not SNAPSHOTS_DIR.exists():
        return []
    dates = []
    for p in SNAPSHOTS_DIR.glob("*.json"):
        dates.append(p.stem)
    return sorted(dates)


def latest_prior_snapshot(before: str | None = None) -> dict | None:
    """返回严格早于 before（默认今天）的最近一个快照。

    自动跳过周末/节假日无数据的日子——只要文件存在就算。
    """
    ref = before or date.today().isoformat()
    prior_dates = [d for d in list_snapshot_dates() if d < ref]
    if not prior_dates:
        return None
    return load_snapshot(prior_dates[-1])


def compute_changes(current: dict[str, dict], prior: dict | None) -> dict[str, dict]:
    """对比当前持仓与上一个快照，逐支持仓算日间变化。

    current: 与 save_snapshot 同结构的 {key: {price, value, ...}}
    prior:   latest_prior_snapshot() 返回的完整快照 dict（含 positions）

    返回 {key: {字段: {"prev","curr","chg","pct"}, "_prev_date": ...}}
    prior 为空（首次、无历史）时，所有 chg/pct 为 None。
    """
    prior_pos = (prior or {}).get("positions", {}) if prior else {}
    prev_date = (prior or {}).get("date") if prior else None
    out: dict[str, dict] = {}

    for key, cur in current.items():
        old = prior_pos.get(key, {})
        row: dict = {"_prev_date": prev_date}
        for f in _DIFF_FIELDS:
            cv = cur.get(f)
            pv = old.get(f)
            entry = {"prev": pv, "curr": cv, "chg": None, "pct": None}
            if isinstance(cv, (int, float)) and isinstance(pv, (int, float)):
                entry["chg"] = cv - pv
                entry["pct"] = ((cv - pv) / pv) if pv else None
            row[f] = entry
        out[key] = row
    return out
