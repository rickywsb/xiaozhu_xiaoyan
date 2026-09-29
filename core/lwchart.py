"""core/lwchart.py — TradingView Lightweight Charts™ K 线（个股详情弹窗）+ TradingView 官方图表嵌入

  · lw_chart()      Lightweight Charts（Apache 2.0 开源，需保留 TradingView 标识——attributionLogo 保持开启）。
                    数据由我们提供：K 线、均线、布林、成交量 / RSI / MACD 子图、Fib 与筹码价位线、A/B 级信号箭头。
  · tv_widget()     TradingView 官方「Advanced Chart」嵌入小组件（免费，带 TradingView 标识）：
                    行情由 TradingView 提供，可用它的画线工具与指标；与我们的系统隔离（读不到数据、画不上信号）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

LWC_URL = "https://cdn.jsdelivr.net/npm/lightweight-charts@5.2.1/dist/lightweight-charts.standalone.production.mjs"

_CSS = """
.pz-lw { width: 100%; background: #FFFFFF; border: 1px solid #E2DFD7; border-radius: 14px; overflow: hidden; }
.pz-lw-msg { padding: 24px; color: #8A877E; font-size: 13px; font-family: "Noto Sans SC", sans-serif; }
"""

_JS = """
const URL = '__LWC_URL__';
let libP = null;
export default async function(component) {
  const { data, parentElement } = component;
  const el = parentElement.querySelector('.pz-lw');
  if (!el || !data) return;
  el.style.height = (data.height || 560) + 'px';
  let L;
  try { libP = libP || import(URL); L = await libP; }
  catch (e) { el.innerHTML = '<div class="pz-lw-msg">图表库加载失败（需要能访问 cdn.jsdelivr.net）</div>'; return; }
  if (el._chart) { el._chart.remove(); el._chart = null; }
  const chart = L.createChart(el, {
    autoSize: true,
    layout: { background: { color: '#FFFFFF' }, textColor: '#3B3A36', fontSize: 11,
              fontFamily: 'Noto Sans SC, IBM Plex Mono, sans-serif', attributionLogo: true,
              panes: { separatorColor: '#E2DFD7', separatorHoverColor: '#D5D1C7' } },
    grid: { vertLines: { color: '#F3F1EC' }, horzLines: { color: '#F3F1EC' } },
    rightPriceScale: { borderColor: '#E2DFD7' },
    timeScale: { borderColor: '#E2DFD7', rightOffset: 4 },
    crosshair: { mode: 0 },
    localization: { locale: 'zh-CN' },
  });
  el._chart = chart;
  const noLabel = { priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };

  const pf = { type: 'price', precision: data.precision ?? 2, minMove: data.precision === 0 ? 1 : 0.01 };
  const candle = chart.addSeries(L.CandlestickSeries, {
    upColor: '#2F8A57', downColor: '#C0473A', borderVisible: false,
    wickUpColor: '#2F8A57', wickDownColor: '#C0473A', priceFormat: pf });
  candle.setData(data.candles || []);

  (data.lines || []).forEach(ln => {
    const s = chart.addSeries(L.LineSeries, { color: ln.color, lineWidth: ln.width || 1.5,
      lineStyle: ln.style || 0, title: ln.title || '', priceFormat: pf, ...noLabel });
    s.setData(ln.points);
  });
  (data.levels || []).forEach(lv => candle.createPriceLine({ price: lv.price, color: lv.color, lineWidth: 1,
      lineStyle: 2, axisLabelVisible: true, title: lv.title }));
  if (data.markers && data.markers.length) L.createSeriesMarkers(candle, data.markers);

  let pane = 0;
  if (data.volume) {
    pane += 1;
    const v = chart.addSeries(L.HistogramSeries, { priceFormat: { type: 'volume' }, ...noLabel }, pane);
    v.setData(data.volume);
  }
  if (data.rsi) {
    pane += 1;
    const r = chart.addSeries(L.LineSeries, { color: '#7B4CC2', lineWidth: 1.5, title: 'RSI', ...noLabel }, pane);
    r.setData(data.rsi);
    r.createPriceLine({ price: 70, color: '#C0473A', lineWidth: 1, lineStyle: 2, axisLabelVisible: false });
    r.createPriceLine({ price: 30, color: '#2F8A57', lineWidth: 1, lineStyle: 2, axisLabelVisible: false });
  }
  if (data.macd) {
    pane += 1;
    const h = chart.addSeries(L.HistogramSeries, { ...noLabel }, pane);
    h.setData(data.macd.hist);
    chart.addSeries(L.LineSeries, { color: '#1C4F7A', lineWidth: 1.3, title: 'DIF', ...noLabel }, pane).setData(data.macd.dif);
    chart.addSeries(L.LineSeries, { color: '#C7711F', lineWidth: 1.3, title: 'DEA', ...noLabel }, pane).setData(data.macd.dea);
  }
  const panes = chart.panes();
  panes.forEach((p, i) => p.setStretchFactor(i === 0 ? 3.2 : 1));
  chart.timeScale().fitContent();
}
""".replace("__LWC_URL__", LWC_URL)


def _comp():
    from core.ui import registered_component
    return registered_component("pz_lwchart", html='<div class="pz-lw"></div>', css=_CSS, js=_JS)


def _t(ts) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m-%d")


def lw_chart(ohlcv: pd.DataFrame, key: str, *, mas=(5, 20, 60), show_volume=True, show_rsi=True,
             show_macd=False, show_bollinger=False, levels=None, markers=None, height: int | None = None) -> None:
    """ohlcv：get_ohlcv() 的结果（含 Date / Open / High / Low / Close / Volume）。"""
    from core.technical_analysis import calc_bollinger, calc_macd, calc_rsi
    df = ohlcv.dropna(subset=["Open", "High", "Low", "Close"]).copy()
    df["t"] = [_t(d) for d in df["Date"]]
    df = df.drop_duplicates("t", keep="last").sort_values("t")
    c = df["Close"]
    num = lambda v: None if v is None or v != v else round(float(v), 6)
    pts = lambda s: [{"time": t, "value": num(v)} for t, v in zip(df["t"], s) if num(v) is not None]

    data: dict = {
        "candles": [{"time": t, "open": num(o), "high": num(h), "low": num(lo), "close": num(cl)}
                    for t, o, h, lo, cl in zip(df["t"], df["Open"], df["High"], df["Low"], c)],
        "lines": [], "levels": levels or [], "markers": markers or [],
    }
    colors = {5: "#E8A84C", 20: "#1C4F7A", 60: "#7B4CC2", 120: "#8A877E"}
    for m in mas:
        data["lines"].append({"title": f"MA{m}", "color": colors.get(m, "#8A877E"),
                              "points": pts(c.rolling(m, min_periods=max(2, m // 2)).mean())})
    if show_bollinger:
        up, mid, lo = calc_bollinger(c)
        for s_, t in ((up, "BOLL上"), (lo, "BOLL下")):
            data["lines"].append({"title": t, "color": "#8A877E", "width": 1, "style": 2, "points": pts(s_)})
    if show_volume and "Volume" in df:
        data["volume"] = [{"time": t, "value": num(v) or 0,
                           "color": "rgba(47,138,87,0.45)" if cl >= o else "rgba(192,71,58,0.45)"}
                          for t, v, cl, o in zip(df["t"], df["Volume"], c, df["Open"])]
    if show_rsi:
        data["rsi"] = pts(calc_rsi(c))
    if show_macd:
        dif, dea, hist = calc_macd(c)
        data["macd"] = {"dif": pts(dif), "dea": pts(dea),
                        "hist": [{"time": t, "value": num(v), "color": "#2F8A57" if (num(v) or 0) >= 0 else "#C0473A"}
                                 for t, v in zip(df["t"], hist) if num(v) is not None]}
    data["precision"] = 0 if float(c.iloc[-1]) >= 1000 else 2       # 韩元等大额价格不显示小数
    n_sub = sum(bool(x) for x in (data.get("volume"), data.get("rsi"), data.get("macd")))
    data["height"] = height or (430 + 110 * n_sub)
    _comp()(data=data, key=key)


# ─── TradingView 官方图表嵌入 ─────────────────────────────────────────────────
_TV_EXCHANGE = {".KS": "KRX", ".KQ": "KRX", ".HK": "HKEX", ".L": "LSE", ".ST": "OMXSTO", ".PA": "EURONEXT",
                ".TWO": "TPEX", ".TW": "TWSE", ".T": "TSE", ".DE": "XETR", ".TO": "TSX", ".SS": "SSE", ".SZ": "SZSE"}


def tv_symbol(ticker: str) -> str:
    """yfinance 代码 → TradingView 代码：000660.KS → KRX:000660，BRK-B → BRK.B；美股直接用代码（TV 自动识别交易所）。"""
    t = ticker.upper()
    if "." in t:
        base, suf = t.rsplit(".", 1)
        ex = _TV_EXCHANGE.get("." + suf)
        if ex:
            if ex == "HKEX":
                base = base.lstrip("0") or "0"
            return f"{ex}:{base}"
    if t.startswith("^"):
        return {"^VIX": "CBOE:VIX", "^GSPC": "SP:SPX", "^IXIC": "NASDAQ:IXIC"}.get(t, t[1:])
    return t.replace("-", ".")


def tv_embeddable(ticker: str) -> bool:
    """
    TradingView 嵌入式小组件只能显示美股（含 OTC）；港股 / 伦敦 / 韩国等受交易所授权限制，
    嵌入时只提示「仅在 TradingView 上可用」（2026-09 实测 HKEX / LSE / KRX 均不可嵌入）。
    """
    return "." not in ticker and not ticker.startswith("^")


def tv_url(ticker: str) -> str:
    from urllib.parse import quote
    return f"https://www.tradingview.com/chart/?symbol={quote(tv_symbol(ticker))}"


def tv_widget(ticker: str, height: int = 620) -> None:
    import streamlit.components.v1 as components
    # 嵌入框架里网页高度为 0，autosize 会把图压扁：改用固定高度
    cfg = {"autosize": False, "width": "100%", "height": height - 20, "symbol": tv_symbol(ticker), "interval": "D", "timezone": "America/New_York",
           "theme": "light", "style": "1", "locale": "zh_CN", "allow_symbol_change": True,
           "hide_side_toolbar": False, "withdateranges": True, "details": False, "calendar": False,
           "support_host": "https://www.tradingview.com"}
    html = f"""
<div class="tradingview-widget-container" style="width:100%">
  <div class="tradingview-widget-container__widget"></div>
  <script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js" async>
  {json.dumps(cfg)}
  </script>
</div>"""
    components.html(html, height=height)
