"""views/home.py — 驾驶舱：总览 / 盘中"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.ui import page_header, section_switch, run_section
import config

page_header("驾驶舱", f"{config.market_today():%Y-%m-%d} · 市场状态、组合概况与持仓评分")
sid = section_switch({"总览": "overview", "盘中": "live"}, key="sw_home")
run_section("home_overview") if sid == "overview" else run_section("live", SECTION="live")
