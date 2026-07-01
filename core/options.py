"""core/options.py — 期权抓价 + Black-Scholes 希腊字母（原型/测试用）

yfinance 的期权链只提供隐含波动率（impliedVolatility），不提供
delta/gamma/theta/vega/rho。本模块用 Black-Scholes 公式（仅依赖标准库
math，无需 scipy）根据 IV + 标的价 + 剩余期限计算这些希腊字母。

OCC 合约代码格式： 标的 + YYMMDD + C/P + 行权价×1000（补零到 8 位）
例： GLW270617C00155000 = GLW / 2027-06-17 / Call / 行权价 155
"""

import math
import re
from dataclasses import dataclass, asdict
from datetime import date, datetime

import requests
import yfinance as yf

# 年化无风险利率（近似，用于 BS 贴现项，可后续接入真实国债利率）
RISK_FREE_RATE = 0.045
# 每张标准股票期权对应 100 股
CONTRACT_MULTIPLIER = 100

_OCC_RE = re.compile(r"^(?P<root>[A-Z]+)(?P<date>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")


@dataclass
class OptionQuote:
    contract: str          # OCC 合约代码
    underlying: str        # 标的
    option_type: str       # "call" / "put"
    strike: float
    expiry: str            # YYYY-MM-DD
    days_to_expiry: int
    underlying_price: float | None
    last_price: float | None   # 期权每股报价（上次成交）
    bid: float | None
    ask: float | None
    mid_price: float | None    # (bid+ask)/2 中值，无则回退 last_price
    mark_price: float | None   # 估值采用的价格（中值优先）
    iv: float | None           # 隐含波动率（小数）
    # 希腊字母（BS 计算）
    delta: float | None
    gamma: float | None
    theta: float | None        # 每日 theta（已 /365）
    vega: float | None         # 每 1% IV 变化
    rho: float | None          # 每 1% 利率变化
    in_the_money: bool | None
    contract_value: float | None   # last_price × 100（每张市值）

    def as_dict(self) -> dict:
        return asdict(self)


def parse_occ(symbol: str) -> dict | None:
    """解析 OCC 合约代码，返回 {root, expiry, option_type, strike}。"""
    m = _OCC_RE.match(symbol.strip().upper())
    if not m:
        return None
    yy = int(m.group("date")[0:2])
    mm = int(m.group("date")[2:4])
    dd = int(m.group("date")[4:6])
    year = 2000 + yy
    return {
        "root": m.group("root"),
        "expiry": f"{year:04d}-{mm:02d}-{dd:02d}",
        "option_type": "call" if m.group("cp") == "C" else "put",
        "strike": int(m.group("strike")) / 1000.0,
    }


def build_occ(underlying: str, expiry: str, option_type: str, strike: float) -> str:
    """由标的/到期日(YYYY-MM-DD)/方向/行权价 生成 OCC 合约代码。"""
    d = datetime.strptime(expiry, "%Y-%m-%d").date()
    cp = "C" if option_type.lower().startswith("c") else "P"
    strike_int = int(round(strike * 1000))
    return f"{underlying.upper()}{d:%y%m%d}{cp}{strike_int:08d}"


def _norm_cdf(x: float) -> float:
    """标准正态分布 CDF（用 math.erf，无需 scipy）。"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_greeks(
    S: float, K: float, T: float, sigma: float,
    r: float = RISK_FREE_RATE, option_type: str = "call",
) -> dict:
    """Black-Scholes 期权价格 + 希腊字母。
    S 标的价, K 行权价, T 剩余年限, sigma 年化波动率, r 无风险利率。
    theta 已转为「每日」，vega/rho 已缩放为「每 1% 变化」。
    """
    is_call = option_type.lower().startswith("c")
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        # 到期或无效：退化为内在价值
        intrinsic = max(S - K, 0.0) if is_call else max(K - S, 0.0)
        return {"price": intrinsic, "delta": (1.0 if is_call and S > K else 0.0),
                "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}

    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    disc = math.exp(-r * T)
    pdf_d1 = _norm_pdf(d1)

    if is_call:
        price = S * _norm_cdf(d1) - K * disc * _norm_cdf(d2)
        delta = _norm_cdf(d1)
        theta = (-(S * pdf_d1 * sigma) / (2 * sqrtT)
                 - r * K * disc * _norm_cdf(d2))
        rho = K * T * disc * _norm_cdf(d2)
    else:
        price = K * disc * _norm_cdf(-d2) - S * _norm_cdf(-d1)
        delta = _norm_cdf(d1) - 1.0
        theta = (-(S * pdf_d1 * sigma) / (2 * sqrtT)
                 + r * K * disc * _norm_cdf(-d2))
        rho = -K * T * disc * _norm_cdf(-d2)

    gamma = pdf_d1 / (S * sigma * sqrtT)
    vega = S * pdf_d1 * sqrtT
    return {
        "price": price,
        "delta": delta,
        "gamma": gamma,
        "theta": theta / 365.0,      # 每日
        "vega": vega / 100.0,        # 每 1% IV
        "rho": rho / 100.0,          # 每 1% 利率
    }


def implied_vol_from_price(
    price: float, S: float, K: float, T: float,
    r: float = RISK_FREE_RATE, option_type: str = "call",
) -> float | None:
    """用二分法从期权价格反解隐含波动率（当外部数据源都拿不到 IV 时兜底）。

    这样即使只拿到期权成交价，也能算出与该价格自洽的希腊字母。
    返回小数形式的年化波动率，无解时返回 None。
    """
    if not (price and S and K and T and T > 0 and price > 0):
        return None
    # 价格不能低于内在价值（否则无解）
    is_call = option_type.lower().startswith("c")
    intrinsic = max(S - K, 0.0) if is_call else max(K - S, 0.0)
    if price < intrinsic - 1e-6:
        return None
    lo, hi = 1e-4, 5.0  # 0.01% ~ 500% 波动率区间
    f_lo = bs_greeks(S, K, T, lo, r, option_type)["price"] - price
    f_hi = bs_greeks(S, K, T, hi, r, option_type)["price"] - price
    if f_lo * f_hi > 0:
        return None  # 区间内无符号变化，无解
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        f_mid = bs_greeks(S, K, T, mid, r, option_type)["price"] - price
        if abs(f_mid) < 1e-4:
            return mid
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return 0.5 * (lo + hi)


# CBOE 延迟报价缓存（同一进程内按标的缓存整条期权链，避免重复下载）
_CBOE_CACHE: dict[str, dict] = {}


def _fetch_cboe_chain(root: str) -> dict:
    """抓取 CBOE 免费延迟报价（含真实 IV 与希腊字母），返回 {OCC代码: 行情dict}。

    端点： https://cdn.cboe.com/api/global/delayed_quotes/options/{ROOT}.json
    这是独立于 Yahoo 的数据源，直接提供 delta/gamma/theta/vega/rho/iv/bid/ask，
    因此当 Yahoo 期权链被限流时，仍能给出完整希腊字母。
    """
    root = root.upper()
    if root in _CBOE_CACHE:
        return _CBOE_CACHE[root]
    result: dict = {}
    for host in ("cdn.cboe.com", "www.cboe.com"):
        url = f"https://{host}/api/global/delayed_quotes/options/{root}.json"
        try:
            resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                continue
            opts = resp.json().get("data", {}).get("options", [])
            for o in opts:
                occ = o.get("option")
                if occ:
                    result[occ] = o
            if result:
                break
        except Exception:
            continue
    _CBOE_CACHE[root] = result
    return result


def _cboe_lookup(root: str, contract_symbol: str) -> dict | None:
    """在 CBOE 期权链中查找指定合约，返回其行情 dict（无则 None）。"""
    chain = _fetch_cboe_chain(root)
    return chain.get(contract_symbol)


def fetch_option(contract_symbol: str, underlying_price: float | None = None) -> OptionQuote | None:
    """抓取期权行情并计算希腊字母。
    从期权链拿 last/bid/ask/IV（比单合约 fast_info 更全），再用 BS 算 greeks。
    """
    parsed = parse_occ(contract_symbol)
    if not parsed:
        return None
    root, expiry = parsed["root"], parsed["expiry"]
    otype, strike = parsed["option_type"], parsed["strike"]

    tk = yf.Ticker(root)
    if underlying_price is None:
        try:
            underlying_price = float(tk.fast_info.get("lastPrice"))
        except Exception:
            underlying_price = None

    last_price = bid = ask = iv = None
    itm = None
    try:
        chain = tk.option_chain(expiry)
        df = chain.calls if otype == "call" else chain.puts
        row = df[df["contractSymbol"] == contract_symbol]
        if row.empty:
            row = df[df["strike"] == strike]
        if not row.empty:
            r0 = row.iloc[0]
            last_price = float(r0["lastPrice"]) if r0["lastPrice"] else None
            bid = float(r0["bid"]) if r0["bid"] else None
            ask = float(r0["ask"]) if r0["ask"] else None
            iv = float(r0["impliedVolatility"]) if r0["impliedVolatility"] else None
            itm = bool(r0.get("inTheMoney"))
    except Exception:
        pass

    # Yahoo 偶尔返回近零/异常的 IV（如 1e-5），会算出退化的希腊字母（delta≈1）。
    # 视为无效，交由下方 CBOE 兜底取真实 IV 与希腊字母。
    if iv is not None and iv < 0.01:
        iv = None

    # 兜底①：Yahoo 期权链拿不到 IV（希腊字母算不出）或整行为空时，
    # 改用 CBOE 免费延迟报价——独立数据源，直接带真实 IV + 希腊字母 + bid/ask，
    # 因此 Yahoo 被限流时仍能给出完整希腊字母。
    cboe_greeks = None
    if iv is None or (last_price is None and bid is None and ask is None):
        cb = _cboe_lookup(root, contract_symbol)
        if cb:
            def _f(v):
                try:
                    return float(v) if v not in (None, "", 0) else None
                except (TypeError, ValueError):
                    return None
            if bid is None:
                bid = _f(cb.get("bid"))
            if ask is None:
                ask = _f(cb.get("ask"))
            if last_price is None:
                last_price = _f(cb.get("last_trade_price"))
            cb_iv = _f(cb.get("iv"))
            if iv is None and cb_iv:
                iv = cb_iv
            # CBOE 直接给的真实希腊字母（约定与本模块一致：theta/日、vega 每 1%）
            g = {k: _f(cb.get(k)) for k in ("delta", "gamma", "theta", "vega", "rho")}
            if any(v is not None for v in g.values()):
                cboe_greeks = g

    # 兜底②：期权链与 CBOE 都拿不到价时，用单合约行情端点取最新价
    # （更不易被限流，但只有价格、无 IV/希腊字母）。
    if last_price is None and bid is None and ask is None:
        try:
            oc = yf.Ticker(contract_symbol)
            lp = None
            try:
                lp = oc.fast_info.get("lastPrice")
            except Exception:
                lp = None
            if not lp:
                hist = oc.history(period="5d")
                if hist is not None and not hist.empty:
                    closes = hist["Close"].dropna()
                    if len(closes):
                        lp = float(closes.iloc[-1])
            if lp and float(lp) > 0:
                last_price = float(lp)
        except Exception:
            pass

    # 剩余期限（年）
    exp_d = datetime.strptime(expiry, "%Y-%m-%d").date()
    dte = (exp_d - date.today()).days
    T = max(dte, 0) / 365.0

    # 估值价格：中值 (bid+ask)/2 优先，其次 lastPrice
    mid_price = None
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        mid_price = (bid + ask) / 2.0
    mark_price = mid_price if mid_price is not None else last_price

    # 希腊字母：优先用 CBOE 真实值；其次用 IV 走 BS；
    # 最后兜底——用估值价反解 IV 再算，保证有价就有希腊字母。
    greeks = {"delta": None, "gamma": None, "theta": None, "vega": None, "rho": None}
    if cboe_greeks:
        greeks = {k: cboe_greeks.get(k) for k in greeks}
    elif underlying_price and iv and T > 0:
        g = bs_greeks(underlying_price, strike, T, iv, option_type=otype)
        greeks = {k: g[k] for k in greeks}
    elif underlying_price and mark_price and T > 0:
        solved = implied_vol_from_price(mark_price, underlying_price, strike, T, option_type=otype)
        if solved:
            iv = solved
            g = bs_greeks(underlying_price, strike, T, solved, option_type=otype)
            greeks = {k: g[k] for k in greeks}

    return OptionQuote(
        contract=contract_symbol,
        underlying=root,
        option_type=otype,
        strike=strike,
        expiry=expiry,
        days_to_expiry=dte,
        underlying_price=underlying_price,
        last_price=last_price,
        bid=bid,
        ask=ask,
        mid_price=mid_price,
        mark_price=mark_price,
        iv=iv,
        delta=greeks["delta"],
        gamma=greeks["gamma"],
        theta=greeks["theta"],
        vega=greeks["vega"],
        rho=greeks["rho"],
        in_the_money=itm,
        contract_value=(mark_price * CONTRACT_MULTIPLIER) if mark_price else None,
    )


# 手动值与市场值偏移超过该比例时给出提示（默认 15%）
DEVIATION_THRESHOLD = 0.15


def resolve_option_value(
    quote: OptionQuote | None,
    contracts: float = 1,
    manual_mark: float | None = None,
    deviation_threshold: float = DEVIATION_THRESHOLD,
) -> dict:
    """决定期权最终估值，支持手动覆盖 + 偏移检测。

    优先级：
      1. 有 manual_mark（用户手填每股价） → 采用手动值（source="manual"）
      2. 否则用抓取的中值 mark_price（source="fetched"）
      3. 两者都无 → value=None（source="none"，抓取失败且无手填）

    偏移检测：当手填值与抓取值同时存在且相对偏移 > 阈值时 flagged=True，
    提示用户复核（但仍以手动值为准）。

    返回 dict:
      mark        最终采用的每股价
      source      "manual" / "fetched" / "none"
      value       每股 × 100 × 张数（总市值）
      fetched_mark 抓取到的中值（供对比）
      deviation   (fetched-manual)/manual，无法计算时 None
      flagged     偏移是否超阈值
    """
    fetched = quote.mark_price if quote else None
    deviation = None
    flagged = False

    if manual_mark is not None and manual_mark > 0:
        mark, source = float(manual_mark), "manual"
        if fetched is not None and fetched > 0:
            deviation = (fetched - manual_mark) / manual_mark
            flagged = abs(deviation) > deviation_threshold
    elif fetched is not None and fetched > 0:
        mark, source = fetched, "fetched"
    else:
        mark, source = None, "none"

    value = (mark * CONTRACT_MULTIPLIER * contracts) if mark else None
    return {
        "mark": mark,
        "source": source,
        "value": value,
        "fetched_mark": fetched,
        "deviation": deviation,
        "flagged": flagged,
    }

