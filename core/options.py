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

    # 兜底：期权链抓不到时（Streamlit Cloud 常屏蔽 option_chain 端点），
    # 改用单合约行情端点取最新价（更不易被限流）。此路径拿不到 bid/ask/IV，
    # 故无希腊字母，但至少能给出价格与市值。
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

    greeks = {"delta": None, "gamma": None, "theta": None, "vega": None, "rho": None}
    if underlying_price and iv and T > 0:
        g = bs_greeks(underlying_price, strike, T, iv, option_type=otype)
        greeks = {k: g[k] for k in ("delta", "gamma", "theta", "vega", "rho")}

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

