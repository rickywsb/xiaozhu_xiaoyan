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
        "": [
            st.Page("views/home.py", title="驾驶舱", icon="🧭", url_path="home", default=True),
        ],
        "投资组合": [
            st.Page("views/holdings.py", title="持仓", icon="💼", url_path="holdings"),
            st.Page("views/options.py", title="期权", icon="🎯", url_path="options"),
            st.Page("views/daily.py", title="日报", icon="📅", url_path="daily"),
        ],
        "研究": [
            st.Page("views/market.py", title="市场", icon="🗺️", url_path="market"),
            st.Page("views/signals.py", title="个股信号", icon="📊", url_path="signals"),
            st.Page("views/watch.py", title="关注与笔记", icon="🔭", url_path="watch"),
        ],
    },
    expanded=True,
)

# 切换页面时关闭上一页未关的技术图表弹窗（core.stock_chart）
if st.session_state.get("_last_page") != pg.url_path:
    st.session_state.pop("_chart_open", None)
    st.session_state["_last_page"] = pg.url_path

from core.ui import inject_css
inject_css()

pg.run()
