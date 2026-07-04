# 小猪小眼基金公司 · 股票分析软件 — 开发者文档

> **版本**: v0.7  
> **日期**: 2026-06-22  
> **状态**: 🔨 Phase 6 设计中

---

## 1. 项目背景与目标

### 1.1 来源模块

| 现有脚本 | 功能 | 本项目对应模块 |
|---|---|---|
| `value_update/update_prices.py` | yfinance 抓价 + 多货币转 USD + 更新 xlsx | `core/price_updater.py` |
| `new/daily_top10.py` | 指数衰减 + 多周期合成日动量 Top 10 | `core/daily_momentum.py` |
| `new/weekly_tracker.py` | 周度快照对比、排名变化、Excel 报告 | `core/weekly_tracker.py` |

### 1.2 核心需求

1. **持仓净值看板** — 一键更新所有持仓最新价格，计算每个账户及总净值，非 USD 自动换算。持仓数量 & ticker 为**纯手动维护**（因仓位分布在多个券商账户）。
2. **量能健康报告** — 对当前持仓每一只股票给出动量 overview（多周期收益、动量得分、趋势方向），可视化展示强弱。
3. **潜力 Watch List** — 从全宇宙筛选出量能加速的股票，结合自定义篮子，给出买入候选排名。
4. **可视化优先** — 采用 Web 式 Dashboard，拒绝纯 CLI，支持图表、热力图、排名榜等。

---

## 2. 技术栈决策

### 2.1 前端框架：Streamlit ✅

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| **Streamlit** | Python 原生、热重载、组件丰富、部署简单 | 交互有延迟 | ✅ 选用 |
| Dash/Plotly | 更灵活 | 需要写回调，开发成本高 | ❌ |
| PyQt/tkinter | 本地桌面 | UI 老旧，开发繁琐 | ❌ |
| Jupyter | 快速 PoC | 不适合日常使用 | ❌ |

### 2.2 核心依赖

```
streamlit          >= 1.35
yfinance           >= 1.3
openpyxl           >= 3.1
pandas             >= 2.0
numpy              >= 1.26
plotly             >= 5.22       # 交互图表
streamlit-aggrid   >= 0.3        # 可编辑表格（持仓编辑用）
```

---

## 3. 文件夹结构

```
fund_app/
│
├── DEVELOPER.md             ← 本文档
├── requirements.txt         ← 依赖清单
├── app.py                   ← Streamlit 主入口（多页应用）
├── config.py                ← 全局路径 & 参数常量
│
├── data/
│   ├── portfolio.json       ← 持仓配置（手动维护，多账户）
│   ├── price_cache.json     ← 最新价格缓存（每次 update 后写入）
│   └── watchlist.json       ← 自定义 watch list
│
├── core/
│   ├── __init__.py
│   ├── price_updater.py     ← 抓价 + FX 换算（迁移自 update_prices.py）
│   ├── daily_momentum.py    ← 日动量评分（迁移自 daily_top10.py）
│   ├── weekly_tracker.py    ← 周度快照对比（迁移自 weekly_tracker.py）
│   └── fx.py                ← 汇率缓存层（避免重复请求）
│
├── pages/
│   ├── 1_Portfolio.py       ← 页面：持仓净值
│   ├── 2_Momentum.py        ← 页面：量能健康报告
│   └── 3_Watchlist.py       ← 页面：潜力 Watch List
│
└── utils/
    ├── charts.py            ← 可复用 Plotly 图表函数
    └── formatters.py        ← 数字/颜色/标签格式化
```

---

## 4. 数据模型

### 4.1 `portfolio.json` — 持仓配置（纯手动维护）

```json
{
  "accounts": [
    {
      "name": "MooMoo",
      "broker": "moomoo",
      "positions": [
        { "ticker": "NVDA",  "display": "NVDA",       "shares": 141.0,  "note": "" },
        { "ticker": "MU",    "display": "MU",          "shares": 39.32,  "note": "=33+6.32" },
        { "ticker": "3363.TWO", "display": "3363 Foci","shares": 20.0,  "note": "TWD→USD" }
      ]
    },
    {
      "name": "Joint Wros",
      "broker": "schwab",
      "positions": [
        { "ticker": "MSFT", "display": "MSFT", "shares": 145.09, "note": "" }
      ]
    }
  ],
  "manual_prices": {
    "GLW Call":      6055.31,
    "MARVEL CALL":   18500.0,
    "NOK Call 2026 12": 1775.0
  },
  "last_modified": "2026-06-21"
}
```

**设计原则**:
- `shares` 和 `ticker` 均由用户手动编辑 JSON 或通过 UI 界面修改
- `manual_prices` 存放期权等无法自动抓取的仓位价值（直接输入市值）
- 多账户聚合展示时按 ticker 合并

### 4.2 `price_cache.json` — 价格缓存

```json
{
  "updated_at": "2026-06-21T17:48:56",
  "fx_rates": {
    "TWD": 0.031622, "GBP": 1.321161, "EUR": 1.146132,
    "KRW": 0.000653, "HKD": 0.127598
  },
  "prices": {
    "NVDA": 210.69,
    "MU":   1133.99,
    "IQE.L_usd": 0.67,
    "3363.TWO_usd": 22.51
  }
}
```

---

## 5. 功能规格

### 5.1 页面 1 — 持仓净值 (`Portfolio`)

#### 布局设计

```
┌─────────────────────────────────────────────────────────┐
│  💼 持仓净值    最后更新: 2026-06-21 17:48  [ 🔄 一键更新 ] │
├──────────────┬──────────────┬──────────────┬────────────┤
│ 总净值         │ 今日变化       │ 最大单仓       │ 账户数量     │
│ $XXX,XXX     │ +$X,XXX (+X%)│ NVDA 12.5%  │ 5          │
├─────────────────────────────────────────────────────────┤
│ [账户筛选: All ▼]  [排序: 市值 ▼]  [显示: 全部 ▼]         │
├─────────────────────────────────────────────────────────┤
│                   持仓明细表                               │
│ Stock │ Shares │ Price │ MktVal │ Weight │ Acct │ Chg   │
│ ...   │  手动  │ 自动  │ 自动   │  自动  │ 手动 │ 颜色  │
├─────────────────────────────────────────────────────────┤
│         [饼图: 板块分布]   [柱图: 账户净值对比]            │
└─────────────────────────────────────────────────────────┘
```

#### 核心功能
- **🔄 一键更新**: 调用 `core/price_updater.py`，刷新所有价格 + FX，写入 `price_cache.json` 并同步 xlsx
- **可编辑表格**: 用 `streamlit-aggrid` 让用户直接在 UI 修改 shares / 添加 ticker，保存回 `portfolio.json`
- **颜色编码**: 涨绿跌红，行级背景
- **饼图**: Plotly pie chart，按板块（光、存储、半导体、其他）分组
- **持仓权重条形图**: 横向 bar，显示各票占比

#### 数据流
```
portfolio.json → [聚合] → DataFrame → [merge price_cache] → 展示表格
                                                          → Plotly 图表
[🔄 点击] → core/price_updater.py → price_cache.json → 刷新页面
```

---

### 5.2 页面 2 — 量能健康报告 (`Momentum`)

#### 布局设计

```
┌─────────────────────────────────────────────────────────┐
│  📊 量能健康报告    [ ⚡ 刷新量能数据 ]   运算时间: ~45s    │
├─────────────────────────────────────────────────────────┤
│  量能热力图（仅当前持仓）                                  │
│  X轴: 时间周期(5d/10d/20d/60d)                           │
│  Y轴: 股票名称                                            │
│  颜色: 收益率强弱（绿强红弱）                              │
├─────────────────────────────────────────────────────────┤
│  综合动量得分排名（当前持仓）                              │
│  横向 bar chart，颜色=得分分位，附 5d/20d 动量方向箭头    │
├─────────────────────────────────────────────────────────┤
│  个股详情卡片（点击展开）                                  │
│  [NVDA ↑↑] [MU ↑] [LITE →] [COHR ↓] ...               │
│  → 展开: 价格走势 + 动量加速度时序图                      │
└─────────────────────────────────────────────────────────┘
```

#### 核心功能
- **量能热力图**: `plotly.express.imshow`，收益率矩阵（行=ticker，列=时间周期）
- **持仓动量得分榜**: 仅对 `portfolio.json` 中的 ticker 跑 `daily_momentum.py` 的评分逻辑
- **趋势方向信号**: `acc = 5d_avg_return - 20d_avg_return`，正=加速🟢，负=减速🔴
- **个股卡片**: 用 `st.expander`，展示近 60 日收盘价折线图 + 动量加速度曲线
- **警告高亮**: 若持仓股票动量得分排名 < 底部 20%，显示黄色预警

#### 数据来源
- 调用 `core/daily_momentum.py` 的 `calculate_daily_metrics()`
- 价格数据来自 yfinance（独立请求，不依赖 price_cache）

---

### 5.3 页面 3 — 潜力 Watch List (`Watchlist`)

#### 布局设计

```
┌─────────────────────────────────────────────────────────┐
│  🔭 潜力 Watch List    [ 🚀 运行全宇宙筛选 ]   ~3-5 min    │
├─────────────────────────────────────────────────────────┤
│  筛选参数 (侧边栏):                                       │
│  - Universe: [QQQ] [SPY] [Optical] [AI Infra] [Custom] │
│  - Top N: 25   Decay: 0.94   Window: 40                 │
├─────────────────────────────────────────────────────────┤
│  📈 动量加速榜 Top 25                                     │
│  综合分 + 加速度 双维度散点图                              │
│  X轴: 综合动量得分  Y轴: 动量加速度  大小: 成交量          │
│  颜色: 是否已持仓（金色=已持仓，蓝=watch，灰=其他）        │
├─────────────────────────────────────────────────────────┤
│  本周排名变化 (周度 Tracker)                              │
│  ▲ 新进 Top25   ▼ 跌出 Top25   📍 已持仓变化              │
├─────────────────────────────────────────────────────────┤
│  自定义 Watch List 状态表                                 │
│  [LITE ▲3] [MU ▲1] [AXTI ▼2] ...                       │
└─────────────────────────────────────────────────────────┘
```

#### 核心功能
- **全宇宙扫描**: 调用 `core/weekly_tracker.py`（或 `daily_momentum.py`）对配置的 universe 跑完整筛选
- **散点图**: Plotly scatter，`x=Composite_Score`，`y=Momentum_Acceleration`，区分持仓/watchlist/其他
- **周度 diff**: 对比 `data/snapshots/` 最近两个快照，高亮排名变化
- **进出 Top N 告警**: 自动标注新进/跌出的 ticker
- **导出**: 一键导出当日 Top 25 CSV / Excel

---

## 6. 开发路线图

### Phase 1：基础骨架 ✅ 已完成 (2026-06-21)

- [x] `requirements.txt` — 依赖清单
- [x] `config.py` — 路径常量 + 板块颜色映射
- [x] `data/portfolio.json` — 35 个持仓初始化（从 xlsx 迁移）
- [x] `core/price_updater.py` — 重构 update_prices.py，输出写 `price_cache.json`
- [x] `core/fx.py` — FX 缓存模块（60 分钟复用）
- [x] `app.py` — Streamlit 多页入口（三页导航）
- [x] `pages/1_Portfolio.py` — 持仓表格 + 一键更新 + 板块饼图 + Top15 柱图

### Phase 2：持仓编辑器 ✅ 已完成 (2026-06-21)

- [x] 板块饼图 + Top 15 市值柱状图
- [x] `pages/1_Portfolio.py` — Tab 切换：📊 概览 / ✏️ 编辑持仓
- [x] `st.data_editor` 可编辑表格（`num_rows="dynamic"` 支持增删行）
  - 列：显示名 / YF Ticker / 持股数 / 板块（下拉+自定义） / 货币（下拉） / 备注
  - 操作：勾选删除 / 底部「+」新增行
- [x] 手动价值编辑器（期权等 → 直接填市值）
- [x] 💾 保存持仓配置 → 写回 `portfolio.json`
- [x] 🚀 保存 + 获取最新价格 → 保存后立即 fetch + rerun
- [x] **hotfix**: 占比 ProgressColumn 改百分制 (×100)，标签正确显示 16.0%
- [x] **hotfix**: 饼图 `textposition="auto"` + `uniformtext_mode="hide"` + 右侧图例
- [x] **hotfix**: 支持现金仓位（YF Ticker=`CASH`，price=1.0，shares=金额）

### Phase 3：量能健康报告 ✅ 已完成 (2026-06-21)

**目标**：仅对当前持仓（~35只）跑动量评分，页面响应 < 15s。

- [x] `core/daily_momentum.py` — 从 `new/daily_top10.py` 提取纯函数库
  - `fetch_histories(tickers, period)` → `dict[ticker, pd.Series]`（批量下载，自动跳过 CASH）
  - `_avg_return(close, days)` / `_decay_return(close, window, decay)` / `_total_return(close, days)`
  - `calc_metrics(ticker, display, close, window, decay)` → dict（单股全量指标）
  - `score_holdings(portfolio, window, decay)` → `pd.DataFrame`（Z-score 综合排序）
  - 趋势信号：`accel = avg_r5 - avg_r20`，映射 ↑↑/↑/→/↓/↓↓
- [x] `pages/2_Momentum.py` — 三区块布局：
  - **① 多周期收益热力图**：`plotly.imshow`，行=ticker（按综合分排序），列=5D/10D/20D/60D，`RdYlGn`，对称色阶
  - **② 综合动量得分排名**：横向柱状图，颜色=加速(绿)/减速(红)，底部20%黄色预警区阴影
  - **③ 预警提示**：`st.warning` 列出得分后20%的持仓 ticker
- [x] 个股详情：metric 卡片（强/中/弱分组）+ 下拉选股 → 60日收盘价+MA20+动量加速度双轴图
- [x] 「⚡ 刷新量能」按钮 + 30分钟 `st.cache_data` 缓存
- [x] 侧边栏参数：`decay` 滑块 (0.88–0.99) + `window` 滑块 (20–60)

### Phase 4：Watch List 扫描器 ✅ 已完成 (2026-06-21)

- [x] `data/watchlist.json` — 默认 31 只 ticker 初始化
- [x] `core/daily_momentum.py` — 新增 `score_ticker_list(tickers, labels, window, decay)` 平铺列表评分函数
- [x] `pages/3_Watchlist.py` — 三 Tab 布局：
  - **📊 扫描排名** Tab：Top5 卡片 + 散点图（综合分×动量加速，已持仓金色）+ 排名柱图 + 明细表
  - **📈 周度追踪** Tab：保存当日快照 `data/snapshots/YYYY-MM-DD.csv`，对比任意两日快照，排名变化柱图 + Top N 进出告警
  - **✏️ 编辑列表** Tab：`st.data_editor` 增删 ticker + 一键把全部持仓加入 Watch List
- [x] 30 分钟 `st.cache_data` 缓存，与 Phase 3 和谐统一参数

### Phase 5：云端持久化 ✅ 已完成 (2026-06-21)

**目标**：Streamlit Cloud 文件系统临时，用户编辑持仓后自动写回 GitHub repo，实现跨会话持久化。

**方案**：GitHub Contents API（无需外部数据库，利用已有 git repo）

- [x] `core/github_storage.py` — GitHub Contents API 封装
  - `get_file_sha(remote_path, token, repo)` → 获取文件 SHA（PUT 时必须提供）
  - `save_json_to_github(local_path, remote_path, commit_msg, *, token, repo)` → 写回 GitHub
  - `sync_to_github(local_path, remote_path, commit_msg)` → 便捷封装，自动读 `st.secrets`
  - 本地运行时无 token → 静默跳过，仅保存本地（不影响开发体验）
- [x] `pages/1_Portfolio.py` — 保存按钮调用 `sync_to_github`，结果显示 GitHub 同步状态
- [x] `pages/3_Watchlist.py` — `_save_watchlist` 自动同步 `data/watchlist.json`
- [x] `requirements.txt` — 添加 `requests>=2.31`
- [x] `runtime.txt` — 改为 `python-3.11` 格式
- [x] 修复所有文件的 `from __future__ import annotations`（Python 3.14 兼容）

**Streamlit Cloud 配置步骤**：
```
App Settings → Secrets → 添加：
GITHUB_TOKEN = "ghp_xxxxxxxxxxxxxxxxxxxx"
GITHUB_REPO  = "rickywsb/xiaozhu_xiaoyan"
```

**数据流**：
```
用户点击「💾 保存」
  → 写本地 data/portfolio.json
  → GitHub API PUT /repos/{repo}/contents/data/portfolio.json
  → 自动 commit → Streamlit Cloud 检测 push → 重部署（~1 min）
```

---

### Phase 6：净值趋势 + 个股技术图表 🔨 设计中 (2026-06-22)

---

#### 6A — 每日持仓总额变化趋势

**目标**：自动记录每次价格更新时的总净值，形成历史曲线，直观看出资金增减趋势。

**设计原则**：
- 不需要成本价/盈亏，只追踪**总市值快照**
- Schema 不破坏（不修改 `portfolio.json` 结构）
- 未来可扩展个股成本记录，但当前只存总值

**存储**：`data/portfolio_value_history.csv`

```csv
date,total_value,note
2026-06-21,1234567.89,
2026-06-22,1256789.01,
```

**触发时机**：
- 每次用户点击「🔄 一键更新价格」后，自动 append 当日记录
- 同一天多次更新只保留最后一条（按日期 upsert）

**新增模块**：`core/value_history.py`
```python
def append_value(total_usd: float, note: str = "") -> None
    # 追加/更新今日总净值到 CSV
def load_history() -> pd.DataFrame
    # 返回历史净值 DataFrame
```

**页面展示**：在 `pages/1_Portfolio.py` 新增 Tab 或折叠区 **「📈 净值历史」**

布局：
```
┌────────────────────────────────────────────────────┐
│ 📈 净值历史趋势                                       │
│ [时间范围: 1M  3M  6M  全部]                          │
├────────────────────────────────────────────────────┤
│  KPI: 当前净值 | 较昨日 +X% | 较上周 +X% | 较上月 +X% │
├────────────────────────────────────────────────────┤
│  折线图: x=日期, y=总净值(USD)                        │
│  悬停显示: 日期 / 净值 / 较前日变化%                   │
├────────────────────────────────────────────────────┤
│  柱状图: 每日涨跌幅（绿涨红跌）                       │
└────────────────────────────────────────────────────┘
```

**GitHub 同步**：历史 CSV 也通过 `sync_to_github` 写回，云端数据不丢失。

**待实现清单**：
- [ ] `core/value_history.py` — `append_value()` / `load_history()`
- [ ] `pages/1_Portfolio.py` — 新增「📈 净值历史」Tab
  - 时间范围筛选（1M/3M/6M/All）
  - KPI 卡片（较昨日 / 较上周 / 较上月变化%）
  - Plotly 折线图（总净值）+ 柱状图（日涨跌幅，绿涨红跌）
- [ ] `core/price_updater.py` / `pages/1_Portfolio.py` — 更新价格后自动调用 `append_value()`
- [ ] `sync_to_github` 同步 `data/portfolio_value_history.csv`

---

#### 6B — 个股技术指标图表

**目标**：在量能页面内嵌个股 K 线 + 技术指标，无需跳转第三方工具。

**数据来源**：yfinance `history(period, interval)` 获取 OHLCV 数据

**新增模块**：`core/technical_analysis.py`
```python
def get_ohlcv(ticker: str, period: str = "6mo", interval: str = "1d") -> pd.DataFrame
    # 返回 OHLCV + Date DataFrame

def calc_rsi(close: pd.Series, period: int = 14) -> pd.Series
    # 标准 Wilder RSI

def calc_macd(close: pd.Series,
              fast: int = 12, slow: int = 26, signal: int = 9
              ) -> tuple[pd.Series, pd.Series, pd.Series]
    # 返回 (macd_line, signal_line, histogram)

def calc_bollinger(close: pd.Series, period: int = 20, std: float = 2.0
                   ) -> tuple[pd.Series, pd.Series, pd.Series]
    # 返回 (upper, mid, lower)

def build_candlestick_chart(df: pd.DataFrame, ticker: str,
                             mas: list[int] = [5, 20, 60],
                             show_volume: bool = True,
                             show_rsi: bool = True,
                             show_macd: bool = False) -> go.Figure
    # 返回完整 Plotly Figure（多子图）
```

**图表结构（Plotly make_subplots）**：
```
┌──────────────────────────────────────────────────┐
│  [选股下拉] [周期: 1M 3M 6M 1Y] [指标: RSI MACD BB]│
├──────────────────────────────────────────────────┤
│  主图 (70%高度)                                    │
│  · 蜡烛图 (OHLC)                                  │
│  · MA5 / MA20 / MA60 叠加线                       │
│  · Bollinger Bands（可开关）                       │
├──────────────────────────────────────────────────┤
│  成交量 (15%高度，绿涨红跌)                         │
├──────────────────────────────────────────────────┤
│  RSI (15%高度，可选)                               │
│  · RSI14 折线，超买70/超卖30 水平线                 │
├──────────────────────────────────────────────────┤
│  MACD (可选，折叠展开)                             │
│  · DIF / DEA 折线 + 柱状图                        │
└──────────────────────────────────────────────────┘
```

**页面位置**：`pages/2_Momentum.py` 新增第四个 Tab **「🕯 技术图表」**

**选股范围**：持仓 + Watchlist 合集，下拉选择

**缓存**：`@st.cache_data(ttl=3600)` — OHLCV 数据 1 小时缓存

**待实现清单**：
- [ ] `core/technical_analysis.py` — 四个计算函数 + `build_candlestick_chart()`
- [ ] `pages/2_Momentum.py` — 新增「🕯 技术图表」Tab
  - 下拉选股（持仓+watchlist）
  - 周期选择（1M/3M/6M/1Y）
  - 指标开关（RSI / MACD / Bollinger）
  - Plotly 多子图 K 线图
- [ ] `requirements.txt` — 确认 `plotly>=5.22`（已有）

---

#### Phase 6 整体优先级

| 功能 | 难度 | 价值 | 优先级 |
|---|---|---|---|
| 6A 净值趋势 | ⭐⭐ 低 | 高 | 先做 |
| 6B 技术图表 | ⭐⭐⭐ 中 | 高 | 后做 |



### 7.1 持仓数据源：JSON 而非 xlsx

xlsx 格式含大量公式和手工备注，不适合作程序数据源。  
`portfolio.json` 作为**唯一真相源（Single Source of Truth）**，xlsx 可由程序生成/导出，但不再作为输入。

### 7.2 价格更新策略：缓存 + 按需刷新

- UI 启动时**先读缓存**（`price_cache.json`），不自动触发 yfinance 请求
- 用户点击「🔄 一键更新」才真正发起请求，防止每次刷新浏览器都打 API
- FX 汇率缓存 60 分钟（市场时间内基本稳定）

### 7.3 量能计算：仅对持仓跑（快），全宇宙扫描（慢）

- 页面 2（量能健康）只跑 ~35 只持仓，响应快（< 15s）
- 页面 3（Watch List）跑全宇宙 500+ 只，用 `st.spinner` + 后台线程，预计 3-5 分钟

### 7.4 多货币处理

非 USD 持仓（港股、韩股、台股、英股、欧股）在 `core/price_updater.py` 统一换算，  
UI 层只处理 USD 价格，不关心原始货币细节。

---

## 8. 开发约定

- **Python 版本**: 3.11+
- **代码风格**: 文件内保持中文注释（团队习惯），函数名用英文
- **启动命令**: `streamlit run app.py`
- **端口**: 默认 8501
- **数据目录**: `fund_app/data/`（不提交到 git，加入 .gitignore）
- **snapshot 归档**: `fund_app/data/snapshots/YYYY-MM-DD.csv`

---

## 9. 下一步行动（Phase 1 开始）

```
Step 1  创建 requirements.txt + config.py
Step 2  初始化 data/portfolio.json（从 xlsx 手工迁移一次）
Step 3  迁移 core/price_updater.py（重构为函数，输出写缓存）
Step 4  写 app.py（多页骨架）
Step 5  写 pages/1_Portfolio.py（表格 + 一键更新，先跑通逻辑）
```

> 每个 Step 完成后更新本文档的进度状态。

---

## 10. 期权支持 + 希腊字母 + 日间对比（新增）

### 10.1 背景
期权持仓此前只能手填市值，无法自动更新，也看不到 IV/greeks。本模块让期权像股票
一样自动抓价，并计算完整希腊字母，同时为**所有持仓**（股票 + 期权）提供与前一交易日
的对比。

### 10.2 `core/options.py` — 期权抓价 + Black-Scholes
- **数据来源**：yfinance 的 `option_chain(expiry)` 提供 lastPrice / bid / ask /
  **impliedVolatility** / inTheMoney，但**不提供 delta/gamma/theta/vega/rho**。
- **希腊字母**：用 Black-Scholes 公式自行计算，仅依赖标准库 `math.erf`（**无需 scipy**）。
  输入 = 标的价 S、行权价 K、剩余年限 T、IV σ、无风险利率 r（默认 `RISK_FREE_RATE=0.045`）。
  - theta 已转「每日」（/365），vega/rho 已缩放为「每 1% 变化」。
- **OCC 合约代码**：`标的 + YYMMDD + C/P + 行权价×1000(补零8位)`
  - 例：`GLW270617C00155000` = GLW / 2027-06-17 / Call / 行权价 155
  - `build_occ(underlying, expiry, type, strike)` 生成、`parse_occ(symbol)` 解析。
- **估值口径：中值优先**。`mark_price = (bid+ask)/2`，无 bid/ask 时回退 `lastPrice`。
  （lastPrice 常为过时成交价，价差大的期权如 Marvell 会明显偏离，故用中值。）
- **每张市值** = `mark_price × 100`（`CONTRACT_MULTIPLIER = 100`）。

### 10.3 手动覆盖 + 偏移检测 — `resolve_option_value()`
应对「抓不到」或「与券商(Fidelity/moomoo)偏移较大」两种情况：

| 场景 | 行为 | source |
|------|------|--------|
| 无手填，抓取成功 | 用抓取中值 | `fetched` |
| 有手填 `manual_mark` | **以手填为准**（用户显式修正） | `manual` |
| 有手填 + 抓取偏移 > 15% | 仍用手填，但 `flagged=True` 提示复核 | `manual` |
| 抓不到 + 有手填 | 兜底用手填 | `manual` |
| 抓不到 + 无手填 | `value=None`（无法估值） | `none` |

- 偏移阈值 `DEVIATION_THRESHOLD = 0.15`（15%）。
- `deviation = (fetched - manual) / manual`，UI 可据 `flagged` 高亮提醒。

### 10.4 全持仓日间对比 — `core/snapshots.py`
- 每次「一键更新价格」时 `save_snapshot(positions)` 把当日**所有持仓**（股票 + 期权）
  的价格与关键指标写入 `data/snapshots/YYYY-MM-DD.json`（**同日覆盖**）。
  - key = 股票 `yf_ticker` 或期权 OCC 合约代码。
  - 每条记录含 `price / value / kind`，期权额外含 `iv/delta/gamma/theta/vega/underlying_price`。
- `latest_prior_snapshot(before=今天)`：取**严格早于今天**的最近一个快照，
  自动跳过周末/节假日无数据的日子。
- `compute_changes(current, prior)`：逐支持仓算 `price/value/iv/delta/...` 的
  `chg`（绝对变化）与 `pct`（百分比）。首次无历史时 chg/pct 为 `None`。
- 注意：`data/snapshots/*.json` 属于运行时数据，不进 git（同 `data/` 目录约定）。

### 10.5 portfolio.json 期权数据模型（计划）
```jsonc
"options": [
  { "display": "GLW 155 Call", "contract": "GLW270617C00155000",
    "contracts": 1, "sector": "光",
    "manual_mark": null,   // 每股手动覆盖价；null=用抓取中值
    "note": "..." }
]
```
市值 = `mark × 100 × contracts`；`manual_mark` 非空时以手填为准。

### 10.6 已验证的期权合约（测试基准，2026-06-30）
| 期权 | 合约代码 | 张数 | 中值/股 | 每张 | IV | Delta |
|------|---------|-----|--------|------|-----|-------|
| GLW 155 Call | `GLW270617C00155000` | 1 | 133.6 | $13,360 | 93.9% | 0.85 |
| DRAM 55 Call | `DRAM270617C00055000` | 2 | 33.55 | $3,355 | 94.7% | 0.80 |
| DRAM 56 Call | `DRAM270617C00056000` | 1 | 33.12 | $3,312 | 94.7% | 0.79 |
| MRVL 160 Call | `MRVL270617C00160000` | 1 | 170.28 | $17,028 | 100.5% | 0.88 |
| NOK 13 Call | `NOK261218C00013000` | 5 | 2.92 | $292 | 78.3% | 0.64 |

### 10.7 UI 接入（已完成）
- [x] `pages/1_Portfolio.py`：期权行接入自动抓价 + `manual_mark` 编辑列
- [x] 持仓表新增「当日涨跌%」「日变化 USD」列（来自 `compute_changes`）
- [x] 新增「🎯 期权明细 · 希腊字母」区块：IV/Delta/Gamma/Theta/Vega + ΔIV/ΔDelta
- [x] 更新价格时 `update_all_prices` 内部调 `save_snapshot` 归档当日快照
- [x] 编辑页新增「🎯 期权持仓」编辑器：标的/到期/方向/行权价/张数/板块/手动价，
      保存时 `build_occ` 自动生成合约代码
- [x] `manual_values` 内的 6 个期权已迁移为 `options` 数组（含合约代码）

> 注：`update_all_prices` 现返回的 cache 多了 `options` 段（每合约 mark/value/greeks/
> source/flagged）；`_build_view_df(portfolio, cache)` 签名由原 `prices` 改为整个 cache。
> 期权按各自 `sector`（光/存/半导体）计入板块饼图，不再单列「期权」类。

---

## 11. 期权数据韧性（三级兜底）+ 期权复盘页（新增）

> **日期**: 2026-07-03　**状态**: ✅ 已上线

### 11.1 背景

线上（Streamlit Cloud 共享 IP）与本地都出现过 Yahoo 限流 `option_chain` 的情况：
要么整条链抓不到，要么"抓到了但返回近零 IV（≈1e-5）"。前者导致期权价格空白，
后者导致希腊字母退化（delta≈1、其余≈0），表现为"刷新后没有希腊字母、全是 last price"。

### 11.2 `core/options.py` 抓价三级兜底

`fetch_option()` 现按以下顺序取数，任一级成功即用，保证"有价就有希腊字母"：

| 级别 | 数据源 | 提供 | 触发条件 |
|---|---|---|---|
| ① | Yahoo `option_chain` | last/bid/ask/IV（IV→BS 算 greeks） | 默认首选 |
| ② | **CBOE 免费延迟报价** | **真实 IV + delta/gamma/theta/vega/rho + bid/ask** | ① 拿不到 IV 或整行为空 |
| ③ | 单合约行情 + **IV 反解** | 单合约 last → 二分法反解 IV → BS 算 greeks | ①② 都无 bid/ask/IV |

- CBOE 端点：`https://cdn.cboe.com/api/global/delayed_quotes/options/{ROOT}.json`
  （备用 host `www.cboe.com`）。免费、无需 key、独立于 Yahoo，不会一起被限流。
  返回字段直接含 `iv/delta/gamma/theta/vega/rho/bid/ask/last_trade_price`，
  约定与本模块一致（theta/日、vega 每 1%）。同一进程内按 root 缓存整条链（`_CBOE_CACHE`）。
- **近零 IV 过滤**：Yahoo 返回 `iv < 0.01` 视为无效（置 None），从而触发 CBOE 兜底。
- 新增函数：`implied_vol_from_price()`（二分法反解 IV）、`_fetch_cboe_chain()`、
  `_cboe_lookup()`。新增依赖 `requests`（yfinance 已传递依赖，无需额外安装）。

### 11.3 `core/options_review.py`（新模块）

围绕"跌的是哪一块、什么时候抄底"三大能力，全部复用 `data/snapshots/*.json`：

| 函数 | 作用 |
|---|---|
| `attribute_change(prev, curr, prev_date, curr_date, contracts)` | 单支期权日间价格变化归因，用**上一日**希腊字母做一阶泰勒展开：`ΔV ≈ delta·ΔS + ½·gamma·ΔS² + theta·Δt + vega·ΔIV(点)`，返回各分量合约级 $ + `residual`（残差吸收高阶/rho/误差，各分量之和恒等于实际变化） |
| `portfolio_attribution(cache)` | 对所有期权做归因并汇总组合级各分量。取**最近两个不同日期**的快照对比（`curr=最新快照日`，`prev=严格早于它的最近快照`） |
| `decay_metrics(opt)` | 时间衰减：`theta_day_usd`（每日 $ 损耗）、`theta_pct`（占市值%/日）、`dte`、未来 7/30 日线性粗估 |
| `iv_history(contract)` / `iv_stats(contract, current_iv)` | 遍历历史快照收集 IV 序列，算 **IV Rank**（区间位置）与 **IV Percentile**（低于当前的天数比例），需 ≥2 个数据点 |
| `entry_signal(iv_rank, dte, moneyness)` | 买方视角启发式：IV Rank ≤30% → 🟢抄底良机；≥70% → 🔴偏贵/观望；结合 DTE、价内外程度 |
| `moneyness_of(contract, S)` | 由 OCC 解析行权价，算 `S/K` |

约定：theta=每日、vega=每 1% IV、iv=小数。归因数学已单元验证（分量和=实际变化）。

### 11.4 `pages/5_Options_Review.py`（新页面「🎯 期权复盘」）

注册在 `app.py` 的「投资组合」分组下。五个板块：

1. **组合级期权敞口** — 期权总市值、净 Delta 敞口（delta 折算美元）、每日 Theta 损耗、Vega 敞口
2. **涨跌归因** — 组合级 metrics + Plotly 瀑布图（标的Δ+Γ / 时间Θ / IV V / 残差 → 净变化）+ 逐支明细表
3. **时间衰减报告** — 逐支 Theta/日 $、日损耗率、剩余天数、未来 7/30 日损耗估算
4. **抄底/进场信号** — 🟢🟡🔴 信号 + IV Rank/分位 + S/K + 剩余天数 + 逐支解读 expander
5. **IV 历史走势** — 各期权 IV 时间序列折线图

### 11.5 数据积累依赖（重要）

归因（②）、IV Rank/分位（④）、IV 走势（⑤）都依赖**多天的期权快照**。
目前仅有一天含期权的快照，故这些区块先显示"数据积累中"提示。
**每天点一次「🔄 一键更新价格」**（会写入当日快照），历史攒够后自动填充。
这也符合"抄底时机"的本质：需一段 IV 历史才能判断当前 IV 算高还是低。

---

## 12. 主力/机构 吸筹信号 + 13F 机构持仓（新增）

> **日期**: 2026-07-04　**状态**: ✅ 已上线（量能页第 3 个 tab）

### 12.1 数据边界（重要）

yfinance **只有**日线 OHLCV 与季度 13F 机构持仓，**没有** Level-2 逐笔大单 /
主力资金实时净流入（属付费行情）。因此：
- **量价代理信号**（12.2）：用公开量价行为**推断**吸筹/派发，非真实资金流向。
- **13F 机构持仓**（12.3）：真实数据，但按季披露、滞后约 1~2 个月。
两者互补：前者看短期动向，后者看中长期机构态度。页面均已明确标注，避免误导。

### 12.2 `core/accumulation.py` — 量价吸筹/派发信号

| 函数 | 作用 |
|---|---|
| `fetch_ohlcv_batch(tickers, period)` | 一次性批量下载所有持仓的 OHLCV（`yf.download`，含 Volume） |
| `compute_signals(df)` | 单只股票算 6 个信号 + 综合评分 + 判定 |
| `scan_holdings(portfolio)` | 扫描全部持仓，返回按评分降序的 DataFrame |

六个信号（均基于日线 OHLCV）：`_cmf`(Chaikin 资金流,20)、`_obv_slope`(能量潮斜率,20)、
`_vol_ratio`(量比)、`_ud_vol_ratio`(涨跌量比,20)、`_mfi`(资金流量指标,14)、放量突破(20日新高+量比>1.5)。

评分：CMF>0.05 +2 / OBV上行 +1 / 涨跌量比>1.2 +1 / 放量上涨 +1 / 放量突破 +2 /
MFI<20 +1；反向对称扣分。**评分 ≥3 → 🟢疑似吸筹，≤-3 → 🔴疑似派发，其余 🟡中性**。

### 12.3 `core/accumulation.py` — 13F 机构持仓

`institutional_summary(ticker, top_n=12)` 抓取：
- `major_holders` → 机构持股占比 `inst_pct`、机构家数 `inst_count`、内部人持股 `insider_pct`
- `institutional_holders` → Top 机构明细（机构/持股占比/股数/市值/季度变化 `pctChange`）、
  披露日 `as_of`、**份额加权季度净增减 `net_pct`**（Σ(Shares×pctChange)/ΣShares，>0 净增持）、
  增持/减持家数 `n_up/n_down`
- 部分海外/小盘股无 13F 明细 → 优雅降级（净增减显示「—」）。

### 12.4 `pages/2_Momentum.py` — 「🏦 主力吸筹」tab

在原「📈 量能报告 / 🕯 技术图表」间插入第 3 个 tab：
1. **吸筹信号扫描表** — 汇总卡（吸筹/中性/派发家数）+ 全持仓评分表 + 评分逻辑说明 + 逐股解读 expander。缓存 30 分钟（`_cached_accum`）。
2. **🏛️ 机构 13F 持仓** — 按标的下拉选择，展示 机构持股%/家数/季度净增减/披露日 四个指标 + Top 机构明细表。缓存 6 小时（`_cached_13f`）。

> 注：新增函数在运行中的 Streamlit 里需**重启服务**才能被 core 模块热加载（Streamlit 只重跑页面脚本、不会重新 import 已加载的 core 模块）。
> `background_gradient` 需要 matplotlib（未安装），故表格用 emoji 判定列代替色阶，不引入新依赖。

---

## 13. 行业资讯聚合板块（新增）

> **日期**: 2026-07-04　**状态**: ✅ 已上线（分析工具 → 📰 行业资讯）

### 13.1 数据边界（重要）

本板块聚合的是**公开新闻 / 新闻稿 RSS**，**不是**卖方深度研报或付费投研数据。
真正的 TrendForce / Yole / 大行研报正文都在付费墙内、无公开 API。因此页面顶部
明确标注「公开资讯聚合，非投研建议」。全部数据源**免费、无需 API key**。

### 13.2 `core/news.py` — 资讯聚合引擎

纯 `requests` + stdlib `xml.etree`（**未引入 feedparser 等新依赖**），兼容
RSS 2.0 与 Atom 两种格式，单个源抓取失败自动跳过。

| 函数 | 作用 |
|---|---|
| `fetch_google_news(query, limit)` | Google News RSS 按关键词搜索（`when:30d`，英文） |
| `fetch_feed(source_name, url, limit)` | 抓取单个行业 RSS/Atom feed |
| `fetch_ticker_news(tickers, limit_per)` | yfinance 逐票近期新闻（兼容新版 `content` 与旧版扁平结构） |
| `fetch_theme(theme_name)` | 聚合某主题的关键词 Google News + 专属 feeds，去重按时间倒序 |
| `theme_tickers(portfolio, sector_tag)` | 按 `sector`（存/光）挑出该赛道持仓 |
| `portfolio_tickers(portfolio)` | 全部持仓标的（去 CASH） |
| 内部：`_http_get / _parse_feed / _parse_date / _strip_html / _dedupe / _sort_by_date` | 抓取、解析、清洗、去重、排序 |

**主题定义 `THEMES`**（关键词 + 专属 feed + 对应 sector 标签）：

| 主题 | 关键词（Google News） | 专属 feed | sector |
|---|---|---|---|
| 💾 存储 | HBM / DRAM price / NAND / HBM4 / memory shortage… | **Blocks & Files** + **TrendForce**(`/news/feed/` 公开新闻稿) | 存 |
| 🔦 光通信 | 800G / 1.6T / silicon photonics / CPO / DCI… | 无（纯关键词，噪声更少）| 光 |
| 🔬 半导体大盘 | semiconductor / AI accelerator / TSMC capex / WFE | SEMI + EE Times | — |

> feed 选型踩坑：TrendForce 的 `/presscenter/rss` 是 404，正确的是 `/news/feed/`；
> Gazettabyte（Squarespace `?format=rss`）被 Cloudflare 403、Lightwave 各路径均 404，
> 故光通信改为纯 Google News 关键词，实测相关性反而更高。

### 13.3 `pages/6_资讯.py` — 资讯页面

- 四个 tab：**💾 存储 / 🔦 光通信 / 🔬 半导体大盘 / 💼 我的持仓**
- 主题 tab：展示关键词、专属源、**该赛道持仓**（按 `sector` 自动匹配）+ 卡片流
- 我的持仓 tab：`multiselect` 选标的（默认前 10），yfinance 逐票新闻
- 卡片：标题（超链接）+ 摘要 + 🗞来源 · 🕒相对时间（刚刚/N小时前/昨天/N天前/日期）
- 缓存 30 分钟（`_cached_theme` / `_cached_ticker_news`）+ 手动「🔄 刷新资讯」（`.clear()` + rerun）
- 已在 `app.py` 的「分析工具」组注册

实测：存储 64 条、光通信 31 条、半导体大盘 44 条，均含新鲜内容（含 TrendForce
「DRAM 现货价更新」这类研报口吻新闻稿）。

---

## 14. LLM 分析层：AI 晨报 + 持仓健康诊断（新增）

> **日期**: 2026-07-04　**状态**: ✅ 已上线（OpenAI）

### 14.1 定位与边界

LLM 只做**解读层**：消费我们已经算好的量化信号（量能、吸筹、13F、资讯），
输出人话的综合判断。**禁止 LLM 臆造价格/指标**，所有数字由我们提供（grounding）。
全部输出强制 JSON + 低 temperature，页面渲染成卡片；每处标注「AI 生成，非投资建议」。

### 14.2 `core/llm.py` — OpenAI 轻封装

- **密钥三级读取**：`st.secrets["OPENAI_API_KEY"]`（Cloud 部署）→ 环境变量 → 本地 `openaitoken.txt`（开发兜底，已 gitignore）。`available()` 供页面判断是否显示按钮。
- `chat_json(system, user, model, temperature=0.2, max_tokens)`：强制 `response_format={"type":"json_object"}`，返回 dict，附带 `_usage` / `_cost_usd`（粗估）/ `_model`。异常统一抛 `LLMError`，页面降级提示。
- **模型分层**：`DEFAULT_MODEL="gpt-4o-mini"`（日常）、`DEEP_MODEL="gpt-4.1"`（深度）。
- 依赖：`openai>=1.40`（已加入 requirements.txt，实测 2.44）。

### 14.3 `core/ai_review.py` — 两个分析功能

| 函数 | 吃什么 | 产出 JSON |
|---|---|---|
| `news_digest(items_by_theme, holdings)` | 资讯板块聚合结果（每主题前 10 条标题+摘要） | overview / themes[{name,sentiment,summary,highlights}] / portfolio_impact / risks |
| `portfolio_diagnosis(holdings, sector_weights, news_headlines)` | 量能评分 + 吸筹信号 + 板块权重 + 资讯标题 | overall / health_score / concentration / sector_exposure / **divergences（信号背离）** / strengths / risks / watch |

共用 `_GUARDRAIL` system 前言（严谨买方研究助理、只用给定数据、中文、非投资建议）。

### 14.4 页面接入

- **B 资讯晨报**：`pages/6_资讯.py` 顶部「🤖 AI 晨报」expander + 按钮，结果缓存 30 分钟（cache_key 含日期+各主题条数）。
- **A 持仓诊断**：新页 `pages/7_AI_Review.py`（挂在「投资组合」组），模型档位单选 + 是否结合资讯复选 + 「🩺 生成持仓诊断」按钮。渲染健康分/集中度/赛道暴露/信号背离/亮点/风险/关注，底部显示 token 与成本，另有「查看喂给 AI 的原始数据」expander。

### 14.5 踩坑：板块权重的货币换算

诊断首版用动量模块的 `latest_close`（**原生货币**）× shares 算市值，导致
韩元(000660.KS)/港元(7709.HK)持仓被严重高估，存储权重一度显示 99.3%。
修复：改用 `price_cache.json` 里 **price_updater 已换算成 USD** 的 `prices`
（与持仓净值页同源），修复后权重回归合理（存 37.7% / 光 35.4% / 配置 18.6% …）。

> 成本参考：一次晨报或持仓诊断约 5k tokens、gpt-4o-mini 下 ~$0.0009，可忽略。

