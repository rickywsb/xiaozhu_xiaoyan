"""views/options.py — 期权：复盘 / 情景"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.ui import page_header, section_switch, run_section

page_header("期权", "卖 Put / 买 Call 机会扫描；持仓期权的归因、时间衰减、情景推演与换月")
sid = section_switch({"机会扫描": "finder", "复盘": "review", "情景": "lab"}, key="sw_opt")
run_section({"finder": "option_finder", "review": "options_review", "lab": "option_lab"}[sid])
