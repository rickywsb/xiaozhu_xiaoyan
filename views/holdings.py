"""views/holdings.py — 持仓：净值 / 风险 / Day 0 追踪 / AI 诊断"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.ui import page_header, section_switch, run_section

page_header("持仓", "净值与编辑、风险与压力测试、情景调仓、Day 0 以来表现、AI 诊断")
sid = section_switch({"净值": "nav", "风险": "risk", "情景调仓": "rebalance", "Day 0 追踪": "day0", "AI 诊断": "ai"},
                     key="sw_hold")
{"nav": lambda: run_section("portfolio"), "risk": lambda: run_section("risk"),
 "rebalance": lambda: run_section("rebalance"),
 "day0": lambda: run_section("live", SECTION="day0"), "ai": lambda: run_section("ai_review")}[sid]()
