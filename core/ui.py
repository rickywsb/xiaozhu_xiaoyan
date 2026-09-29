"""core/ui.py — 改版公共界面：入口页标题、子版块切换、子版块运行、全站样式

入口页（views/*.py）= 标题 + 子版块切换条 + 只运行被选中的那个子版块（views/sections/*.py）。
不用 st.tabs：tabs 会把每个标签都跑一遍，重页面（风险、量能）叠在一起太慢。
选中的子版块同步到网址参数 ?tab=…，可以收藏 / 分享。
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

SECTIONS_DIR = Path(__file__).resolve().parents[1] / "views" / "sections"

# 全站补充样式：主题（.streamlit/config.toml）管不到的细节
_CSS = """
<style>
/* 内容区宽度与留白 */
.block-container { padding-top: 3.6rem; padding-bottom: 3rem; max-width: 1480px; }
/* 侧边栏导航：分组标题更淡、条目更舒展 */
[data-testid="stSidebarNav"] a { border-radius: 10px; padding-top: 0.35rem; padding-bottom: 0.35rem; }
[data-testid="stSidebarNavSeparator"] { border-color: #2B2E35; }
/* 指标卡片 */
[data-testid="stMetric"] { background: #FFFFFF; border: 1px solid #E2DFD7; border-radius: 14px; padding: 14px 18px; }
[data-testid="stMetricValue"] { font-family: "IBM Plex Mono", monospace; font-variant-numeric: tabular-nums; }
/* 页面标题行 */
.pz-title { font-size: 30px; font-weight: 700; margin: 0 0 2px 0; letter-spacing: .5px; }
.pz-sub { font-size: 13px; color: #5E5B53; margin-bottom: 10px; }
/* 通用卡片 / 标签 */
.pz-card { background:#FFFFFF; border:1px solid #E2DFD7; border-radius:16px; padding:18px 20px; }
.pz-dark { background:#16181D; color:#F7F6F2; border-radius:16px; padding:22px 26px; }
.pz-pill { display:inline-block; font-size:12px; font-weight:600; padding:3px 10px; border-radius:999px; }
.pz-num { font-family:"IBM Plex Mono",monospace; font-variant-numeric:tabular-nums; }
</style>
"""


def inject_css() -> None:
    st.html(_CSS)


def page_header(title: str, subtitle: str | None = None) -> None:
    st.html(f'<div class="pz-title">{title}</div>'
            + (f'<div class="pz-sub">{subtitle}</div>' if subtitle else ""))


def _clear_chart() -> None:
    st.session_state.pop("_chart_open", None)


def section_switch(options: dict[str, str], key: str) -> str:
    """
    子版块切换条。options = {显示名: 子版块 id}；返回被选中的 id，并同步到网址参数 ?tab=。
    切换时关闭上一个子版块里打开的技术图表弹窗。
    """
    ids = list(options.values())
    labels = list(options.keys())
    q = st.query_params.get("tab")
    default = labels[ids.index(q)] if q in ids else labels[0]
    choice = st.segmented_control(" ", labels, default=default, key=key, label_visibility="collapsed",
                                  on_change=_clear_chart)
    choice = choice or default          # 再次点击已选中项会取消选中：视为不变
    sid = options[choice]
    if st.query_params.get("tab") != sid:
        st.query_params["tab"] = sid
    return sid


def run_section(name: str, **globs) -> None:
    """执行 views/sections/<name>.py（与 Streamlit 运行页面脚本的方式相同）；globs 注入为全局变量。"""
    path = SECTIONS_DIR / f"{name}.py"
    code = compile(path.read_text(encoding="utf-8"), str(path), "exec")
    exec(code, {"__name__": "__main__", "__file__": str(path), **globs})


# ─── 统一股票行 ───────────────────────────────────────────────────────────────
_SCORE_BG = [(90, "#155E36"), (70, "#2F8A57"), (50, "#8A877E"), (30, "#C7711F"), (0, "#B3362A")]
_ACTION_CSS = {
    "持有": "background-color:#DDF0E4;color:#1F6B3E;font-weight:600",
    "可关注": "background-color:#DDF0E4;color:#1F6B3E;font-weight:600",
    "注意": "background-color:#FBEFD9;color:#8A4B0A;font-weight:600",
    "等待": "background-color:#ECEAE4;color:#5E5B53;font-weight:600",
    "考虑减仓": "background-color:#F7E0DC;color:#8E2E22;font-weight:600",
    "回避": "background-color:#F7E0DC;color:#8E2E22;font-weight:600",
}
_VOL_CSS = {"放量吸筹": "color:#1F6B3E;font-weight:600", "放量派发": "color:#8E2E22;font-weight:600",
            "缩量整理": "color:#1C4F7A"}


def _score_css(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "color:#8A877E"
    if v != v:                       # NaN：不评分
        return "color:#8A877E"
    bg = next((c for th, c in _SCORE_BG if v >= th), "#B3362A")
    return f"background-color:{bg};color:#FFFFFF;font-weight:600;text-align:center"


def _chg_css(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return ""
    return "color:#1F7A45" if v > 0 else ("color:#B3362A" if v < 0 else "")


def stock_table(df, key: str, height: int | None = None):
    """
    全站统一的股票表（点击行弹出个股详情）。df 需含列：
      ticker, name, price, chg（小数）, vr, score, d5（5 日分数变化）, spark（近 20 日收盘列表）,
      action, volume_state, signals（A/B 级信号文字）
    缺的列自动跳过。
    """
    import pandas as pd
    from core.stock_chart import clickable_table

    df = df.reset_index(drop=True)
    show = pd.DataFrame(index=df.index)
    # 代码与名称合并成一列（名称与代码相同时只显示代码），省宽度
    nm = df["name"] if "name" in df else df["ticker"]
    show["股票"] = [t if (not n or str(n).upper() == t) else f"{t}  {n}" for t, n in zip(df["ticker"], nm)]

    def _fmt(series, f):
        # 带样式的表格里 NaN 会显示成 "None"，这里先转成文字，缺失显示 "—"
        return [f(x) if x is not None and x == x else "—" for x in series]

    if "price" in df:
        show["价格"] = _fmt(df["price"], lambda x: f"{x:,.2f}")
    if "chg" in df:
        show["当日%"] = _fmt(df["chg"], lambda x: f"{x * 100:+.2f}%")
    if "vr" in df:
        show["量比"] = _fmt(df["vr"], lambda x: f"{x:.2f}×")
    if "score" in df:
        show["评分"] = _fmt(pd.to_numeric(df["score"], errors="coerce"), lambda x: f"{x:.0f}")
    if "d5" in df:
        show["5日"] = _fmt(pd.to_numeric(df["d5"], errors="coerce"), lambda x: f"{x:+.0f}")
    if "spark" in df:
        show["20日走势"] = df["spark"]
    if "action" in df:
        show["操作倾向"] = df["action"].fillna("")
    if "volume_state" in df:
        show["量能"] = df["volume_state"].fillna("")
    if "signals" in df:
        show["可靠信号"] = df["signals"].fillna("")

    def _sign_css(v) -> str:
        return "color:#1F7A45" if str(v).startswith("+") else ("color:#B3362A" if str(v).startswith("-") else "")

    sty = show.style
    if "评分" in show:
        sty = sty.map(_score_css, subset=["评分"])
    if "操作倾向" in show:
        sty = sty.map(lambda v: _ACTION_CSS.get(v, "color:#8A877E"), subset=["操作倾向"])
    if "量能" in show:
        sty = sty.map(lambda v: _VOL_CSS.get(v, "color:#5E5B53"), subset=["量能"])
    for c in ("当日%", "5日"):
        if c in show:
            sty = sty.map(_sign_css, subset=[c])
    small = {c: st.column_config.Column(c, width="small") for c in ("当日%", "量比", "评分", "5日", "量能")
             if c in show}
    cfg = {"股票": st.column_config.Column("股票", width="medium"), **small,
           "操作倾向": st.column_config.Column("操作倾向", width=80),
           "可靠信号": st.column_config.Column("可靠信号", width="large")}
    if "20日走势" in show:
        cfg["20日走势"] = st.column_config.LineChartColumn("20日走势", width="small")
    return clickable_table(sty, tickers=list(df["ticker"]), names=list(nm),
                           key=key, column_config=cfg, hide_index=True, width="stretch",
                           height=height or min(760, 80 + len(df) * 35))


# ─── 设计稿样式的股票列表（st.components.v2：自定义 HTML + 点击回传 Python）────────
_LIST_CSS = """
.pz-wrap { overflow-x: auto; font-family: "Noto Sans SC", sans-serif; color: #16181D; padding-bottom: 4px; }
.pz-wrap::-webkit-scrollbar { height: 8px; }
.pz-wrap::-webkit-scrollbar-thumb { background: #D5D1C7; border-radius: 4px; }
.pz-wrap::-webkit-scrollbar-track { background: transparent; }
.pz-list { background: #FFFFFF; border: 1px solid #E2DFD7; border-radius: 16px; padding: 6px 14px 10px; }
.pz-row { display: grid; gap: 0 12px; align-items: center; padding: 8px 8px; border-bottom: 1px solid #F0EEE8; font-size: 14px; }
.pz-row.pz-body { border-radius: 10px; }
.pz-row.pz-click { cursor: pointer; }
.pz-row.pz-body:hover { background: #F7F6F2; }
.pz-row.pz-body:last-child { border-bottom: none; }
.pz-head { font-size: 12px; color: #5E5B53; padding: 10px 8px 8px; border-bottom: 1px solid #E2DFD7; }
.pz-head span.sortable { cursor: pointer; user-select: none; }
.pz-head span.sortable:hover { color: #16181D; }
.pz-head span.active { color: #16181D; font-weight: 600; }
.pz-r { text-align: right; justify-self: end; }
.pz-t { font-weight: 700; }
.pz-n { font-size: 12px; color: #5E5B53; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.pz-num { font-family: "IBM Plex Mono", monospace; font-variant-numeric: tabular-nums; white-space: nowrap; }
.pz-up { color: #1F7A45; } .pz-dn { color: #B3362A; } .pz-mute { color: #8A877E; }
.pz-sub { font-size: 12px; margin-top: 2px; }
.pz-score { display: inline-block; min-width: 38px; text-align: center; padding: 3px 8px; border-radius: 8px;
            color: #FFFFFF; font-weight: 600; font-size: 14px; }
.pz-d5 { font-size: 12px; margin-left: 6px; }
.pz-pill { justify-self: start; font-size: 12.5px; font-weight: 600; padding: 3px 10px; border-radius: 999px; white-space: nowrap; }
.pz-hot { font-weight: 700; color: #8A4B0A; background: #FBEFD9; padding: 2px 6px; border-radius: 6px; }
.pz-text { font-size: 13px; color: #3B3A36; line-height: 1.45; }
.pz-small { font-size: 12px; color: #3B3A36; line-height: 1.5; }
.pz-bar { display: flex; align-items: center; gap: 8px; }
.pz-bar i { display: block; height: 6px; border-radius: 3px; background: #F0EEE8; flex-grow: 1; position: relative; min-width: 40px; }
.pz-bar i b { position: absolute; left: 0; top: 0; height: 6px; border-radius: 3px; }
.pz-empty { padding: 18px 8px; color: #8A877E; font-size: 13px; }
"""

_LIST_JS = """
export default function(component) {
  const { data, setTriggerValue, parentElement } = component;
  const root = parentElement.querySelector('.pz-list');
  if (!root || !data) return;
  const rows = data.rows || [], cols = data.columns || [];
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const scoreBg = (v) => v >= 90 ? '#155E36' : v >= 70 ? '#2F8A57' : v >= 50 ? '#8A877E' : v >= 30 ? '#C7711F' : '#B3362A';
  const PILL = data.pills || {};
  const sgn = (v, cls) => cls ? (v > 0 ? 'pz-up' : (v < 0 ? 'pz-dn' : '')) : '';
  const fmtNum = (v, c) => {
    if (v == null) return '<span class="pz-mute">—</span>';
    const d = c.decimals ?? 2;
    let t = Math.abs(v) >= 1000 && c.group !== false ? v.toLocaleString('en-US', {minimumFractionDigits: d, maximumFractionDigits: d}) : v.toFixed(d);
    if (c.sign && v > 0) t = '+' + t;
    t = (c.prefix || '') + t + (c.suffix || '');
    const hot = c.hot != null && v >= c.hot ? ' pz-hot' : '';
    return `<span class="pz-num ${sgn(v, c.color)}${hot}">${t}</span>`;
  };
  const spark = (vals) => {
    if (!vals || vals.length < 2) return '';
    const w = 90, h = 26, mn = Math.min(...vals), mx = Math.max(...vals);
    const pts = vals.map((v, i) => `${(i * w / (vals.length - 1)).toFixed(1)},${(h - 3 - (mx === mn ? 0.5 : (v - mn) / (mx - mn)) * (h - 6)).toFixed(1)}`).join(' ');
    const col = vals[vals.length - 1] >= vals[0] ? '#1F7A45' : '#B3362A';
    return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><polyline points="${pts}" fill="none" stroke="${col}" stroke-width="1.6" stroke-linejoin="round"/></svg>`;
  };
  const cell = (r, c) => {
    const v = r[c.key];
    switch (c.kind) {
      case 'stock':
        return `<div><div class="pz-t">${esc(r.ticker_label ?? r.ticker ?? v)}</div><div class="pz-n">${esc(r.sub || '')}</div></div>`;
      case 'price': {
        const ch = r[c.chg];
        const cls = ch == null ? 'pz-mute' : sgn(ch, true);
        const t = ch == null ? '—' : `${ch > 0 ? '+' : ''}${ch.toFixed(2)}%`;
        return `<div><div class="pz-num">${esc(r[c.key] ?? '—')}</div><div class="pz-num pz-sub ${cls}">${t}</div></div>`;
      }
      case 'num': return fmtNum(v, c);
      case 'score': {
        if (v == null) return '<span class="pz-mute">—</span>';
        const d = c.delta ? r[c.delta] : null;
        return `<span class="pz-num pz-score" style="background:${scoreBg(v)}">${v}</span>` +
          (d == null ? '' : `<span class="pz-num pz-d5 ${d > 0 ? 'pz-up' : (d < 0 ? 'pz-dn' : 'pz-mute')}">${d > 0 ? '↑' : (d < 0 ? '↓' : '→')}${Math.abs(d)}</span>`);
      }
      case 'spark': return spark(v);
      case 'pill': {
        if (!v) return '';
        const p = PILL[v];
        return p ? `<span class="pz-pill" style="background:${p[0]};color:${p[1]}">${esc(v)}</span>`
                 : `<span class="pz-small pz-mute">${esc(v)}</span>`;
      }
      case 'bar': {
        if (v == null) return '<span class="pz-mute">—</span>';
        const mx = c.max ?? 100, w = Math.max(0, Math.min(100, v / mx * 100));
        const col = c.barColor || (v / mx >= 0.6 ? '#2F8A57' : v / mx >= 0.3 ? '#C9A227' : '#C7711F');
        return `<div class="pz-bar"><span class="pz-num" style="min-width:${c.labelW || 34}px">${(c.decimals ?? 0) ? v.toFixed(c.decimals) : Math.round(v)}${c.suffix || ''}</span><i><b style="width:${w}%;background:${col}"></b></i></div>`;
      }
      case 'small': return `<span class="pz-small">${esc(v ?? '')}</span>`;
      default: {
        const colr = c.colors && c.colors[v] ? `color:${c.colors[v]};font-weight:600` : '';
        return `<span class="pz-text" style="${colr}">${esc(v ?? '')}</span>`;
      }
    }
  };
  const tmpl = cols.map(c => c.width || 'minmax(80px,1fr)').join(' ');
  root.style.minWidth = (data.minWidth || 900) + 'px';
  let key = root.dataset.sortKey ?? (data.sort || '');
  let desc = root.dataset.desc ? root.dataset.desc !== '0' : (data.desc !== false);
  const sortVal = (r, k) => { const c = cols.find(x => x.key === k); const sk = c && c.sortKey ? c.sortKey : k; return r[sk]; };
  const render = () => {
    const sorted = key ? [...rows].sort((a, b) => {
      const x = sortVal(a, key), y = sortVal(b, key);
      if (x == null && y == null) return 0; if (x == null) return 1; if (y == null) return -1;
      if (typeof x === 'string') return desc ? String(y).localeCompare(x) : String(x).localeCompare(y);
      return desc ? y - x : x - y;
    }) : rows;
    const arrow = (k) => k === key ? (desc ? ' ↓' : ' ↑') : '';
    const head = `<div class="pz-row pz-head" style="grid-template-columns:${tmpl}">` + cols.map(c =>
      `<span class="${c.sortable ? 'sortable' : ''} ${c.key === key ? 'active' : ''} ${c.align === 'right' ? 'pz-r' : ''}" ${c.sortable ? `data-sort="${c.key}"` : ''}>${esc(c.label)}${arrow(c.key)}</span>`).join('') + '</div>';
    const body = sorted.map(r => {
      const click = r.ticker ? 'pz-click' : '';
      return `<div class="pz-row pz-body ${click}" style="grid-template-columns:${tmpl}" ${r.ticker ? `data-t="${esc(r.ticker)}" title="点击查看 ${esc(r.ticker)} 的个股详情"` : ''}>` +
        cols.map(c => `<div class="${c.align === 'right' ? 'pz-r' : ''}">${cell(r, c)}</div>`).join('') + '</div>';
    }).join('');
    root.innerHTML = head + (body || `<div class="pz-empty">${esc(data.empty || '暂无数据')}</div>`);
    root.querySelectorAll('.pz-click').forEach(el => { el.onclick = () => setTriggerValue('clicked', el.dataset.t); });
    root.querySelectorAll('[data-sort]').forEach(el => {
      el.onclick = () => {
        const k = el.dataset.sort;
        if (k === key) { desc = !desc; } else { key = k; desc = true; }
        root.dataset.sortKey = key; root.dataset.desc = desc ? '1' : '0';
        render();
      };
    });
  };
  render();
}
"""

# 标签配色：操作倾向 / 象限 / 评级 / 分组 / 信号倾向……（背景, 文字）
PILLS = {
    "持有": ("#DDF0E4", "#1F6B3E"), "可关注": ("#DDF0E4", "#1F6B3E"), "注意": ("#FBEFD9", "#8A4B0A"),
    "等待": ("#ECEAE4", "#5E5B53"), "考虑减仓": ("#F7E0DC", "#8E2E22"), "回避": ("#F7E0DC", "#8E2E22"),
    "🟢 领先": ("#DDF0E4", "#1F6B3E"), "🔵 改善": ("#E6F0F8", "#1C4F7A"), "🟡 转弱": ("#FBEFD9", "#8A4B0A"),
    "🔴 落后": ("#F7E0DC", "#8E2E22"),
    "A 可靠": ("#155E36", "#FFFFFF"), "B 参考": ("#CFE8D8", "#1F6B3E"), "C 噪音": ("#ECEAE4", "#5E5B53"),
    "D 反向": ("#F6D9C2", "#8A4B0A"), "⚪ 样本不足": ("#ECEAE4", "#5E5B53"),
    "持仓": ("#E6F0F8", "#1C4F7A"), "关注": ("#ECEAE4", "#5E5B53"), "基准": ("#ECEAE4", "#5E5B53"),
    "已移出": ("#ECEAE4", "#8A877E"),
    "🟢 偏多": ("#DDF0E4", "#1F6B3E"), "🔴 偏空": ("#F7E0DC", "#8E2E22"), "🟡 分歧": ("#FBEFD9", "#8A4B0A"),
    "🟢 普涨": ("#DDF0E4", "#1F6B3E"), "🔵 普遍反弹": ("#E6F0F8", "#1C4F7A"), "🟡 分化": ("#FBEFD9", "#8A4B0A"),
    "🔴 普跌": ("#F7E0DC", "#8E2E22"), "⚠️ 少数股拉动": ("#FBEFD9", "#8A4B0A"),
    "看多": ("#DDF0E4", "#1F6B3E"), "看空": ("#F7E0DC", "#8E2E22"),
    "已持仓": ("#E6F0F8", "#1C4F7A"), "Watch List": ("#ECEAE4", "#5E5B53"),
    "领涨": ("#DDF0E4", "#1F6B3E"), "同步": ("#ECEAE4", "#5E5B53"), "落后": ("#F7E0DC", "#8E2E22"),
    "顶背驰": ("#F7E0DC", "#8E2E22"), "底背驰": ("#DDF0E4", "#1F6B3E"),
}
VOL_COLORS = {"放量吸筹": "#1F6B3E", "放量派发": "#8E2E22", "缩量整理": "#1C4F7A"}

_components: dict[tuple[int, str], object] = {}


def registered_component(name: str, **kw):
    """
    st.components.v2 组件在 Streamlit 运行时（Runtime）的注册表里注册：每个运行时各注册一次
    （服务重启 / 测试会新建运行时，模块级缓存需要按运行时区分）。
    """
    try:
        from streamlit.runtime import Runtime
        rid = id(Runtime.instance())
    except Exception:
        rid = 0
    if (rid, name) not in _components:
        _components[(rid, name)] = st.components.v2.component(name, **kw)
    return _components[(rid, name)]


def _list_comp():
    return registered_component("pz_table", html='<div class="pz-wrap"><div class="pz-list"></div></div>',
                                css=_LIST_CSS, js=_LIST_JS)


def _clean(v):
    """JSON 友好：NaN / numpy 标量 → Python 原生值。"""
    import math
    try:
        import numpy as np
        if isinstance(v, np.generic):
            v = v.item()
    except Exception:
        pass
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    return v


def pz_table(rows: list[dict], columns: list[dict], key: str, sort: str | None = None, desc: bool = True,
             min_width: int = 900, empty: str = "暂无数据", names: dict | None = None) -> None:
    """
    全站统一的设计稿样式表格（自定义 HTML）。
      rows     每行一个 dict；含 "ticker" 的行可点击 → 打开个股详情弹窗（ticker=None 不可点）
      columns  [{key, label, kind, width, sortable, align, ...}]，kind：
               stock（ticker + sub）/ price（key=价格文字, chg=涨跌% 键）/ num（decimals, sign, color, prefix, suffix, hot）
               / score（delta=5日变化键）/ spark / pill / bar（max, suffix, barColor）/ text（colors）/ small
    """
    from core import stock_chart as SC
    clean = [{k: _clean(v) for k, v in r.items()} for r in rows]
    res = _list_comp()(data={"rows": clean, "columns": columns, "sort": sort or "", "desc": desc,
                             "minWidth": min_width, "empty": empty, "pills": PILLS},
                       key=key, on_clicked_change=lambda: None)
    clicked = getattr(res, "clicked", None)
    names = names or {r.get("ticker"): r.get("name") for r in rows if r.get("ticker")}
    if clicked:
        st.session_state[SC._OPEN] = {"owner": key, "ticker": clicked, "name": names.get(clicked)}
    opened = st.session_state.get(SC._OPEN)
    if opened and opened["owner"] == key:
        SC._chart_dialog(opened["ticker"], opened.get("name"))


def pct(v, mult: float = 100.0):
    """小数 → 百分数数值（None 安全）。"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return None if v != v else round(v * mult, 2)


def stock_list(df, key: str, sectors: dict | None = None, sort: str = "score") -> None:
    """标准股票行（驾驶舱持仓评分同款）：股票 / 价格·当日 / 量比 / 评分 / 20日走势 / 操作倾向 / 量能 / 可靠信号。"""
    sectors = sectors or {}
    rows = []
    for r in df.to_dict("records"):
        t = r["ticker"]
        nm = r.get("name") or t
        price = _clean(r.get("price"))
        rows.append({
            "ticker": t, "name": nm,
            "sub": " · ".join(x for x in [nm if str(nm).upper() != t else "", sectors.get(t, "")] if x),
            "price_txt": f"{price:,.2f}" if isinstance(price, (int, float)) else None,
            "chg": pct(r.get("chg")), "vr": _clean(r.get("vr")),
            "score": int(r["score"]) if _clean(r.get("score")) is not None else None,
            "d5": int(r["d5"]) if _clean(r.get("d5")) is not None else None,
            "spark": [x for x in (r.get("spark") or []) if x == x],
            "action": r.get("action") or "", "volume_state": r.get("volume_state") or "",
            "signals": r.get("signals") or "",
        })
    cols = [
        {"key": "ticker", "label": "股票", "kind": "stock", "width": "minmax(130px,1.3fr)", "sortable": True},
        {"key": "price_txt", "label": "价格 · 当日", "kind": "price", "chg": "chg", "width": "100px",
         "sortable": True, "sortKey": "chg"},
        {"key": "vr", "label": "量比", "kind": "num", "suffix": "×", "hot": 2, "width": "62px", "sortable": True},
        {"key": "score", "label": "评分", "kind": "score", "delta": "d5", "width": "96px", "sortable": True},
        {"key": "spark", "label": "20 日走势", "kind": "spark", "width": "92px"},
        {"key": "action", "label": "操作倾向", "kind": "pill", "width": "92px"},
        {"key": "volume_state", "label": "量能", "kind": "text", "colors": VOL_COLORS, "width": "76px"},
        {"key": "signals", "label": "可靠信号", "kind": "small", "width": "minmax(140px,1.7fr)"},
    ]
    pz_table(rows, cols, key=key, sort=sort)


def frame_table(df, key: str, tickers: list, columns: list[dict], names: list | None = None,
                subs: list | None = None, sort: str | None = None, desc: bool = True, min_width: int = 900) -> None:
    """
    DataFrame 直接转 pz_table：columns 的 key 用 DataFrame 的列名；第一列可用 {"kind": "stock", "key": "ticker_label"}。
    tickers / names / subs 与行一一对应（ticker 为 None 的行不可点击）。
    """
    rows = []
    for i, r in enumerate(df.to_dict("records")):
        row = {k: _clean(v) for k, v in r.items()}
        t = tickers[i] if i < len(tickers) else None
        nm = names[i] if names else t
        row.update({"ticker": t, "name": nm, "ticker_label": nm or t,
                    "sub": subs[i] if subs else (t if nm and t and str(nm).upper() != str(t).upper() else "")})
        rows.append(row)
    pz_table(rows, columns, key=key, sort=sort, desc=desc, min_width=min_width)
