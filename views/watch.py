"""views/watch.py — 关注与笔记：Watch List / 策略笔记"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.ui import page_header, section_switch, run_section

page_header("关注与笔记", "关注列表动量扫描与周度变化；每周交易策略笔记")
sid = section_switch({"Watch List": "watchlist", "策略笔记": "notes"}, key="sw_watch")
run_section("watchlist" if sid == "watchlist" else "notes")
