"""app.py — 小猪小眼基金公司 · 股票分析软件入口"""

import importlib
import sys
from pathlib import Path

import streamlit as st


def _pyc_source_mtime(mod) -> int | None:
    """模块 .pyc 头里记录的源文件 mtime（即内存中这份代码编译时的源文件版本）。"""
    try:
        head = Path(mod.__cached__).read_bytes()[:16]
    except (OSError, TypeError, AttributeError):
        return None
    if len(head) < 16 or int.from_bytes(head[4:8], "little") != 0:   # 非 mtime 型 pyc
        return None
    return int.from_bytes(head[8:12], "little")


def _reload_stale_modules() -> None:
    """
    Streamlit Cloud 拉取新代码后只重跑脚本，已 import 的 config / core.* 仍是旧版本，
    页面 import 新函数时报 ImportError（以前只能手动 Reboot）。
    判定过期：源文件 mtime 与首次记录时不同；首次见到的模块则对比 .pyc 头里的源 mtime。
    任一过期就按依赖顺序重载全部 config / core.*（config 先行，core 两轮，
    修正模块间 from-import 留下的旧引用）。
    """
    seen: dict[str, float] = getattr(sys, "_facai_module_mtimes", None) or {}
    sys._facai_module_mtimes = seen
    mods = {n: m for n, m in list(sys.modules.items())
            if (n == "config" or n.startswith("core.")) and getattr(m, "__file__", None)}
    stale = False
    for name, mod in mods.items():
        try:
            mtime = Path(mod.__file__).stat().st_mtime
        except OSError:
            continue
        if name in seen:
            stale |= seen[name] != mtime
        else:
            pyc_mtime = _pyc_source_mtime(mod)
            stale |= pyc_mtime is not None and pyc_mtime != (int(mtime) & 0xFFFFFFFF)
        seen[name] = mtime
    if not stale:
        return
    order = (["config"] if "config" in mods else []) + sorted(n for n in mods if n != "config")
    for name in order + order[1:]:
        try:
            importlib.reload(sys.modules[name])
        except Exception:
            pass
        mod = sys.modules.get(name)
        if mod is not None and getattr(mod, "__file__", None):
            seen[name] = Path(mod.__file__).stat().st_mtime


_reload_stale_modules()

st.set_page_config(
    page_title="小猪小眼基金公司",
    page_icon="🐷",
    layout="wide",
    initial_sidebar_state="expanded",
)

# 页面放在 views/ 而不是 pages/：Streamlit 会自动扫描 pages/ 目录，服务刚重启时若首个请求是
# 子页面链接（如 /Portfolio），会绕过本文件回退成"文件名导航 + 窄布局"。
pg = st.navigation(
    {
        "投资组合": [
            st.Page("views/8_Daily.py", title="每日日报", icon="📅"),
            st.Page("views/10_Live.py", title="盘中看板", icon="📡"),
            st.Page("views/1_Portfolio.py", title="持仓净值", icon="💼"),
            st.Page("views/11_Risk.py", title="风险仪表盘", icon="🛡️"),
            st.Page("views/5_Options_Review.py", title="期权复盘", icon="🎯"),
            st.Page("views/12_OptionLab.py", title="期权情景", icon="🧮"),
            st.Page("views/7_AI_Review.py", title="AI 持仓诊断", icon="🩺"),
        ],
        "分析工具": [
            st.Page("views/9_Sectors.py", title="板块雷达", icon="🧭"),
            st.Page("views/13_Screener.py", title="选股器", icon="🔎"),
            st.Page("views/2_Momentum.py", title="量能健康", icon="📊"),
            st.Page("views/3_Watchlist.py", title="Watch List", icon="🔭"),
            st.Page("views/6_News.py", title="行业资讯", icon="📰"),
        ],
        "笔记 & 策略": [
            st.Page("views/4_Notes.py", title="交易策略笔记", icon="📓"),
        ],
    }
)

# 切换页面时关闭上一页未关的技术图表弹窗（core.stock_chart）
if st.session_state.get("_last_page") != pg.url_path:
    st.session_state.pop("_chart_open", None)
    st.session_state["_last_page"] = pg.url_path

pg.run()
