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
.pz-wrap { overflow-x: auto; font-family: "Noto Sans SC", sans-serif; color: #16181D; }
.pz-list { min-width: 900px; background: #FFFFFF; border: 1px solid #E2DFD7; border-radius: 16px; padding: 6px 14px 10px; }
.pz-row { display: grid; grid-template-columns: minmax(130px,1.3fr) 100px 62px 96px 92px 92px 76px minmax(140px,1.7fr);
          gap: 0 12px; align-items: center; padding: 8px 8px; border-bottom: 1px solid #F0EEE8; font-size: 14px; }
.pz-row.pz-body { cursor: pointer; border-radius: 10px; }
.pz-row.pz-body:hover { background: #F7F6F2; }
.pz-row.pz-body:last-child { border-bottom: none; }
.pz-head { font-size: 12px; color: #5E5B53; padding: 10px 8px 8px; border-bottom: 1px solid #E2DFD7; }
.pz-head span.sortable { cursor: pointer; user-select: none; }
.pz-head span.sortable:hover { color: #16181D; }
.pz-head span.active { color: #16181D; font-weight: 600; }
.pz-t { font-weight: 700; }
.pz-n { font-size: 12px; color: #5E5B53; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.pz-num { font-family: "IBM Plex Mono", monospace; font-variant-numeric: tabular-nums; }
.pz-up { color: #1F7A45; } .pz-dn { color: #B3362A; } .pz-mute { color: #8A877E; }
.pz-sub { font-size: 12px; margin-top: 2px; }
.pz-score { display: inline-block; min-width: 38px; text-align: center; padding: 3px 8px; border-radius: 8px;
            color: #FFFFFF; font-weight: 600; font-size: 14px; }
.pz-d5 { font-size: 12px; margin-left: 6px; }
.pz-pill { justify-self: start; font-size: 13px; font-weight: 600; padding: 4px 10px; border-radius: 999px; white-space: nowrap; }
.pz-vr-hot { font-weight: 700; color: #8A4B0A; background: #FBEFD9; padding: 2px 6px; border-radius: 6px; }
.pz-vol { font-size: 12px; }
.pz-sig { font-size: 12px; color: #3B3A36; line-height: 1.5; }
.pz-empty { padding: 18px 8px; color: #8A877E; font-size: 13px; }
"""

_LIST_JS = """
export default function(component) {
  const { data, setTriggerValue, parentElement } = component;
  const root = parentElement.querySelector('.pz-list');
  if (!root || !data) return;
  const rows = data.rows || [];
  const SORTABLE = { chg: '当日', vr: '量比', score: '评分' };
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const scoreBg = (v) => v >= 90 ? '#155E36' : v >= 70 ? '#2F8A57' : v >= 50 ? '#8A877E' : v >= 30 ? '#C7711F' : '#B3362A';
  const ACTION = { '持有':['#DDF0E4','#1F6B3E'], '可关注':['#DDF0E4','#1F6B3E'], '注意':['#FBEFD9','#8A4B0A'],
                   '等待':['#ECEAE4','#5E5B53'], '考虑减仓':['#F7E0DC','#8E2E22'], '回避':['#F7E0DC','#8E2E22'] };
  const VOL = { '放量吸筹':'#1F6B3E', '放量派发':'#8E2E22', '缩量整理':'#1C4F7A' };
  const spark = (vals) => {
    if (!vals || vals.length < 2) return '';
    const w = 90, h = 26, mn = Math.min(...vals), mx = Math.max(...vals);
    const pts = vals.map((v, i) => `${(i * w / (vals.length - 1)).toFixed(1)},${(h - 3 - (mx === mn ? 0.5 : (v - mn) / (mx - mn)) * (h - 6)).toFixed(1)}`).join(' ');
    const col = vals[vals.length - 1] >= vals[0] ? '#1F7A45' : '#B3362A';
    return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><polyline points="${pts}" fill="none" stroke="${col}" stroke-width="1.6" stroke-linejoin="round"/></svg>`;
  };
  let key = root.dataset.sortKey || data.sort || 'score';
  let desc = root.dataset.desc !== '0';

  const render = () => {
    const sorted = [...rows].sort((a, b) => {
      const x = a[key], y = b[key];
      if (x == null && y == null) return 0; if (x == null) return 1; if (y == null) return -1;
      return desc ? y - x : x - y;
    });
    const arrow = (k) => k === key ? (desc ? ' ↓' : ' ↑') : '';
    const head = `<div class="pz-row pz-head">
      <span>股票</span>
      <span class="sortable ${key==='chg'?'active':''}" data-sort="chg">价格 · 当日${arrow('chg')}</span>
      <span class="sortable ${key==='vr'?'active':''}" data-sort="vr">量比${arrow('vr')}</span>
      <span class="sortable ${key==='score'?'active':''}" data-sort="score">评分${arrow('score')}</span>
      <span>20 日走势</span><span>操作倾向</span><span>量能</span><span>可靠信号</span></div>`;
    const body = sorted.map(r => {
      const chgCls = r.chg == null ? 'pz-mute' : (r.chg > 0 ? 'pz-up' : (r.chg < 0 ? 'pz-dn' : ''));
      const chg = r.chg == null ? '—' : `${r.chg > 0 ? '+' : ''}${r.chg.toFixed(2)}%`;
      const vr = r.vr == null ? '<span class="pz-mute">—</span>'
               : `<span class="pz-num ${r.vr >= 2 ? 'pz-vr-hot' : ''}">${r.vr.toFixed(2)}×</span>`;
      const score = r.score == null ? '<span class="pz-mute">—</span>'
               : `<span class="pz-num pz-score" style="background:${scoreBg(r.score)}">${r.score}</span>` +
                 (r.d5 == null ? '' : `<span class="pz-num pz-d5 ${r.d5 > 0 ? 'pz-up' : (r.d5 < 0 ? 'pz-dn' : 'pz-mute')}">${r.d5 > 0 ? '↑' : (r.d5 < 0 ? '↓' : '→')}${Math.abs(r.d5)}</span>`);
      const a = ACTION[r.action];
      const action = a ? `<span class="pz-pill" style="background:${a[0]};color:${a[1]}">${esc(r.action)}</span>`
                       : `<span class="pz-mute" style="font-size:12px">${esc(r.action || '')}</span>`;
      const vol = r.volume_state ? `<span class="pz-vol" style="color:${VOL[r.volume_state] || '#5E5B53'};${VOL[r.volume_state] ? 'font-weight:600' : ''}">${esc(r.volume_state)}</span>` : '';
      return `<div class="pz-row pz-body" data-t="${esc(r.ticker)}" title="点击查看 ${esc(r.ticker)} 的个股详情">
        <div><div class="pz-t">${esc(r.ticker)}</div><div class="pz-n">${esc(r.sub || '')}</div></div>
        <div><div class="pz-num">${esc(r.price_txt || '—')}</div><div class="pz-num pz-sub ${chgCls}">${chg}</div></div>
        <div>${vr}</div><div>${score}</div><div>${spark(r.spark)}</div><div>${action}</div><div>${vol}</div>
        <div class="pz-sig">${esc(r.signals || '')}</div></div>`;
    }).join('');
    root.innerHTML = head + (body || '<div class="pz-empty">暂无数据</div>');
    root.querySelectorAll('.pz-body').forEach(el => { el.onclick = () => setTriggerValue('clicked', el.dataset.t); });
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

_list_component = None


def _list_comp():
    global _list_component
    if _list_component is None:
        _list_component = st.components.v2.component(
            "pz_stock_list", html='<div class="pz-wrap"><div class="pz-list"></div></div>',
            css=_LIST_CSS, js=_LIST_JS)
    return _list_component


def stock_list(df, key: str, sectors: dict | None = None, sort: str = "score") -> None:
    """
    设计稿样式的股票列表（自定义 HTML）：点击一行打开个股详情弹窗，点击「价格·当日 / 量比 / 评分」表头排序。
    df 列同 stock_table：ticker, name, price, chg, vr, score, d5, spark, action, volume_state, signals。
    sectors：{ticker: 板块}，显示在名称后。
    """
    import math
    from core import stock_chart as SC

    sectors = sectors or {}

    def num(x):
        try:
            x = float(x)
        except (TypeError, ValueError):
            return None
        return None if math.isnan(x) else x

    rows, names = [], {}
    for r in df.to_dict("records"):
        t = r["ticker"]
        nm = r.get("name") or t
        names[t] = nm
        sub = " · ".join(x for x in [nm if str(nm).upper() != t else "", sectors.get(t, "")] if x)
        price, chg, score, d5 = num(r.get("price")), num(r.get("chg")), num(r.get("score")), num(r.get("d5"))
        rows.append({
            "ticker": t, "sub": sub,
            "price_txt": f"{price:,.2f}" if price is not None else None,
            "chg": round(chg * 100, 2) if chg is not None else None,
            "vr": round(num(r.get("vr")), 2) if num(r.get("vr")) is not None else None,
            "score": int(score) if score is not None else None,
            "d5": int(d5) if d5 is not None else None,
            "spark": [x for x in (r.get("spark") or []) if x == x],
            "action": r.get("action") or "", "volume_state": r.get("volume_state") or "",
            "signals": r.get("signals") or "",
        })
    res = _list_comp()(data={"rows": rows, "sort": sort}, key=key, on_clicked_change=lambda: None)
    clicked = getattr(res, "clicked", None)
    if clicked:
        st.session_state[SC._OPEN] = {"owner": key, "ticker": clicked, "name": names.get(clicked)}
    opened = st.session_state.get(SC._OPEN)
    if opened and opened["owner"] == key:
        SC._chart_dialog(opened["ticker"], opened.get("name"))
