"""core/value_history.py — 每日持仓总净值历史记录"""

import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

HISTORY_PATH = config.DATA_DIR / "portfolio_value_history.csv"
_COLS = ["date", "total_value"]


def append_value(total_usd: float, note: str = "") -> None:
    """
    追加或更新今日总净值到 CSV。
    同一天多次调用时，用最新值覆盖（upsert by date）。
    """
    today = date.today().isoformat()

    if HISTORY_PATH.exists():
        df = pd.read_csv(HISTORY_PATH, dtype={"date": str})
    else:
        df = pd.DataFrame(columns=_COLS)

    # upsert：删除同日旧行，追加新行
    df = df[df["date"] != today]
    new_row = pd.DataFrame([{"date": today, "total_value": round(total_usd, 2)}])
    df = pd.concat([df, new_row], ignore_index=True)
    df = df.sort_values("date").reset_index(drop=True)
    df.to_csv(HISTORY_PATH, index=False)


def load_history() -> pd.DataFrame:
    """
    加载历史净值，返回 DataFrame。
    columns: date (str), total_value (float)
    空文件或不存在时返回空 DataFrame。
    """
    if not HISTORY_PATH.exists():
        return pd.DataFrame(columns=_COLS)
    df = pd.read_csv(HISTORY_PATH, dtype={"date": str})
    df["total_value"] = pd.to_numeric(df["total_value"], errors="coerce")
    df = df.dropna(subset=["total_value"]).sort_values("date").reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"])
    return df
