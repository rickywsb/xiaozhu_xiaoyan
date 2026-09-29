"""views/daily.py — 日报：今日日报 / 行业资讯"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.ui import page_header, section_switch, run_section

page_header("日报", "一键更新价格并生成 AI 晨报；行业资讯聚合")
sid = section_switch({"今日日报": "report", "行业资讯": "news"}, key="sw_daily")
run_section("daily" if sid == "report" else "news")
