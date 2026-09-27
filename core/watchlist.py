"""core/watchlist.py — 关注列表读写（写入后同步 GitHub）"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config


def load() -> list[str]:
    if not config.WATCHLIST_PATH.exists():
        return []
    try:
        d = json.loads(config.WATCHLIST_PATH.read_text(encoding="utf-8"))
        return [t.upper().strip() for t in d.get("watchlist", [])]
    except Exception:
        return []


def add(tickers: list[str]) -> list[str]:
    """把 tickers 加入关注列表并同步 GitHub，返回新列表。"""
    from core.github_storage import sync_to_github
    new_list = sorted(set(load()) | {t.upper().strip() for t in tickers if t.strip()})
    config.WATCHLIST_PATH.write_text(
        json.dumps({"watchlist": new_list, "last_modified": config.market_today().isoformat()},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    sync_to_github(config.WATCHLIST_PATH, "data/watchlist.json", "feat: update watchlist via UI")
    return new_list
