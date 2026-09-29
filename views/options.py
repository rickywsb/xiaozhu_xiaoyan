"""views/options.py — 期权：复盘 / 情景"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.ui import page_header, section_switch, run_section

page_header("期权", "涨跌归因、时间衰减、IV；以及「如果……会怎样」的情景推演与换月")
sid = section_switch({"复盘": "review", "情景": "lab"}, key="sw_opt")
run_section("options_review" if sid == "review" else "option_lab")
