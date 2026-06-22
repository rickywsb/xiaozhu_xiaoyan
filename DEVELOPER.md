# 小猪小眼基金公司 · 股票分析软件 — 开发者文档

> **版本**: v0.2  
> **日期**: 2026-06-21  
> **状态**: 🔨 Phase 2 开发中

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

### Phase 2：持仓编辑器 🔨 开发中 (2026-06-21)

- [x] 板块饼图 + Top 15 市值柱状图（已在 Phase 1 实现）
- [ ] `pages/1_Portfolio.py` — Tab 切换：📊 概览 / ✏️ 编辑持仓
- [ ] `st.data_editor` 可编辑表格（`num_rows="dynamic"` 支持增删行）
  - 列：显示名 / YF Ticker / 持股数 / 板块（下拉+自定义） / 货币（下拉） / 备注
  - 操作：勾选删除 / 底部「+」新增行
- [ ] 手动价值编辑器（期权等 → 直接填市值）
- [ ] 💾 保存持仓配置 → 写回 `portfolio.json`
- [ ] 🚀 保存 + 获取最新价格 → 保存后立即 fetch + rerun

### Phase 3：量能健康报告

- [ ] `core/daily_momentum.py` — 迁移 `daily_top10.py` 的评分逻辑（函数化，去 CLI）
- [ ] `pages/2_Momentum.py` — 热力图 + 动量得分榜 + 趋势箭头
- [ ] 个股 expander 卡片：价格折线 + 加速度曲线

### Phase 4：Watch List 扫描器

- [ ] `core/weekly_tracker.py` — 迁移 `weekly_tracker.py`（函数化）
- [ ] `pages/3_Watchlist.py` — 散点图 + 周度 diff + 告警
- [ ] 快照管理：自动存 `data/snapshots/`，可对比任意两日

### Phase 5：打磨

- [ ] 侧边栏全局参数控制（universe、top_n、decay 等）
- [ ] 深色主题 / 自定义 CSS
- [ ] 本地定时任务（launchd / cron）自动每日更新
- [ ] 持仓编辑 UI 写回 `portfolio.json`（不再需要手动编辑 JSON）

---

## 7. 关键设计决策

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
