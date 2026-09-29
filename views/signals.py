"""views/signals.py — 个股信号（原「量能健康」）"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.ui import page_header, run_section

page_header("个股信号", "信号总览、放量预警、各类技术信号与信号成绩单")
run_section("momentum")
