# 🐷 小猪小眼基金公司 · 股票分析软件

> 专为私人基金打造的可视化持仓管理 & 量能分析 Dashboard

---

## 功能概览

| 页面 | 功能 | 状态 |
|---|---|---|
| 📊 持仓净值 | 一键更新价格、编辑持仓、板块饼图、Top15 市值榜 | ✅ |
| 📈 量能健康报告 | 多周期热力图、动量得分排名、个股走势卡片 | ✅ |
| 🔭 潜力 Watch List | 全宇宙动量扫描、散点图、周度排名 diff | 🚧 |

---

## 快速启动

### 1. 安装依赖

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. 运行

```bash
streamlit run app.py
```

浏览器自动打开 `http://localhost:8501`

---

## 项目结构

```
fund_app/
├── app.py                   # Streamlit 多页入口
├── config.py                # 全局路径 & 常量
├── requirements.txt
│
├── data/
│   ├── portfolio.json       # 持仓配置（手动维护）
│   ├── price_cache.json     # 最新价格缓存（自动生成）
│   └── fx_cache.json        # 汇率缓存（自动生成）
│
├── core/
│   ├── price_updater.py     # yfinance 抓价 + 多货币→USD
│   ├── daily_momentum.py    # 动量评分引擎（衰减加权 + z-score）
│   └── fx.py                # 汇率缓存层
│
└── pages/
    ├── 1_Portfolio.py       # 持仓净值页
    ├── 2_Momentum.py        # 量能健康报告页
    └── 3_Watchlist.py       # Watch List（开发中）
```

---

## 持仓配置

编辑 `data/portfolio.json` 或直接在 UI「✏️ 编辑持仓」Tab 操作：

```json
{
  "accounts": [{
    "name": "Consolidated",
    "positions": [
      { "display": "NVDA", "yf_ticker": "NVDA", "shares": 141.0, "sector": "半导体" },
      { "display": "现金", "yf_ticker": "CASH", "shares": 50000.0, "sector": "现金" }
    ]
  }],
  "manual_values": {
    "GLW Call": 6055.31
  }
}
```

**支持的货币**：USD / TWD / GBp / KRW / HKD / EUR（自动换算成 USD）

---

## 量能评分原理

综合得分 = `0.55 × Z(衰减加权收益) + 0.45 × Z(多周期加权收益) − 0.08 × Z(波动率)`

**衰减因子 `decay`（默认 0.94）**：控制近期日收益的权重，越小→越强调最近几天，越大→趋势更平滑。

**回看窗口 `window`（默认 40 交易日）**：参与衰减计算的历史天数。

趋势方向信号 = `avg_return_5d − avg_return_20d`，映射为 ↑↑ / ↑ / → / ↓ / ↓↓

---

## 技术栈

- **Python 3.11+** · **Streamlit ≥1.35** · **Plotly** · **yfinance** · **pandas**

---

## 开发文档

详见 [DEVELOPER.md](DEVELOPER.md)
