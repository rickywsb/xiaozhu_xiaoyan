"""app.py — 小猪小眼基金公司 · 股票分析软件入口"""

import streamlit as st

st.set_page_config(
    page_title="小猪小眼基金公司",
    page_icon="🐷",
    layout="wide",
    initial_sidebar_state="expanded",
)

pg = st.navigation(
    {
        "投资组合": [
            st.Page("pages/1_Portfolio.py", title="💼 持仓净值", icon="💼"),
            st.Page("pages/5_Options_Review.py", title="🎯 期权复盘", icon="🎯"),
        ],
        "分析工具": [
            st.Page("pages/2_Momentum.py", title="📊 量能健康", icon="📊"),
            st.Page("pages/3_Watchlist.py", title="🔭 Watch List", icon="🔭"),
        ],
        "笔记 & 策略": [
            st.Page("pages/4_Notes.py", title="📓 交易策略笔记", icon="📓"),
        ],
    }
)

pg.run()
