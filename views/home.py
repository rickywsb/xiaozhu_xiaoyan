"""views/home.py — 驾驶舱：此刻的一切（实时净值 / 市场状态 / 持仓盘中评分 / 预警 / 板块 / 日报摘要）"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.ui import page_header, run_section
import config

page_header("驾驶舱", f"{config.market_today():%Y-%m-%d} · 实时净值、市场状态、持仓盘中评分与预警")
run_section("home_overview")
