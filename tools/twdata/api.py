"""Taiwan stock data layer — compact, analysis-ready outputs for Claude Finance skills.

Every function returns plain JSON-serialisable dicts with explicit units and a
`source` field, so a skill can cite where numbers came from.
Money is in NT$ millions unless a field name says otherwise; share counts in 張 (1,000 shares).
"""
from __future__ import annotations

import statistics as st
from datetime import date, timedelta

from .sources import finmind, tpex, twse

M = 1e6
FINMIND_SRC = "FinMind API (api.finmindtrade.com), data originally from TWSE/TPEx/MOPS"


# ---------------------------------------------------------------- helpers
def _ago(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


def _num(v):
    try:
        f = float(str(v).replace(",", ""))
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def _r(v, n=2):
    return None if v is None else round(v, n)


def _pct(a, b):
    return None if a is None or not b else round((a / b - 1) * 100, 2)


def _roc_date(s: str) -> str:
    """'1151106' -> '2026-11-06'; '11509' -> '2026-09'."""
    s = str(s).strip()
    if len(s) == 7:
        return f"{int(s[:3]) + 1911}-{s[3:5]}-{s[5:]}"
    if len(s) == 5:
        return f"{int(s[:3]) + 1911}-{s[3:5]}"
    return s


def _sid(stock_id: str) -> str:
    return str(stock_id).upper().replace(".TWO", "").replace(".TW", "").strip()


# ---------------------------------------------------------------- per stock
def stock_info(stock_id: str) -> dict:
    sid = _sid(stock_id)
    rows = finmind("TaiwanStockInfo", sid, ttl=7 * 86400)
    if not rows:
        return {"stock_id": sid, "error": "unknown stock id"}
    hold = finmind("TaiwanStockShareholding", sid, _ago(10))
    last = hold[-1] if hold else {}
    return {
        "stock_id": sid,
        "name": rows[0]["stock_name"],
        "market": rows[0]["type"],  # twse = 上市, tpex = 上櫃
        "industries": sorted({r["industry_category"] for r in rows}),
        "shares_issued": last.get("NumberOfSharesIssued"),
        "foreign_holding_pct": last.get("ForeignInvestmentSharesRatio"),
        "as_of": last.get("date"),
        "source": FINMIND_SRC,
    }


def monthly_revenue(stock_id: str, months: int = 24) -> dict:
    """Monthly revenue with MoM / YoY / YTD-YoY — Taiwan's key high-frequency fundamental."""
    sid = _sid(stock_id)
    # need 24 months for TTM YoY and back to January of the prior year for YTD YoY
    rows = finmind("TaiwanStockMonthRevenue", sid, _ago(31 * (max(months, 24) + 13)))
    by = {(r["revenue_year"], r["revenue_month"]): r["revenue"] for r in rows}
    keys = sorted(by)[-months:]
    out = []
    for y, m in keys:
        rev = by[(y, m)]
        prev = by.get((y, m - 1) if m > 1 else (y - 1, 12))
        ytd = sum(by.get((y, k), 0) for k in range(1, m + 1))
        ytd_ly = sum(by.get((y - 1, k), 0) for k in range(1, m + 1))
        full_ytd = all((y, k) in by for k in range(1, m + 1)) and all((y - 1, k) in by for k in range(1, m + 1))
        out.append({
            "month": f"{y}-{m:02d}",
            "revenue_mn": _r(rev / M, 1),
            "mom_pct": _pct(rev, prev),
            "yoy_pct": _pct(rev, by.get((y - 1, m))),
            "ytd_revenue_mn": _r(ytd / M, 1) if full_ytd else None,
            "ytd_yoy_pct": _pct(ytd, ytd_ly) if full_ytd else None,
        })
    last12 = [by[k] for k in sorted(by)[-12:]]
    prev12 = [by[k] for k in sorted(by)[-24:-12]]
    return {
        "stock_id": sid,
        "units": "NT$ millions",
        "latest": out[-1] if out else None,
        "ttm_revenue_mn": _r(sum(last12) / M, 1) if len(last12) == 12 else None,
        "ttm_yoy_pct": _pct(sum(last12), sum(prev12)) if len(prev12) == 12 else None,
        "is_record_high": bool(out) and by[keys[-1]] == max(by.values()),
        "months": out,
        "note": "Monthly revenue is published by the 10th of the following month (MOPS).",
        "source": FINMIND_SRC,
    }


def price_history(stock_id: str, days: int = 365, recent_rows: int = 10) -> dict:
    sid = _sid(stock_id)
    rows = finmind("TaiwanStockPrice", sid, _ago(int(days * 1.0) + 10))
    rows = [r for r in rows if r.get("close")]
    if not rows:
        return {"stock_id": sid, "error": "no price data"}
    closes = [r["close"] for r in rows]
    last = rows[-1]

    def ret(n):
        return _pct(closes[-1], closes[-1 - n]) if len(closes) > n else None

    def ma(n):
        return _r(st.mean(closes[-n:]), 2) if len(closes) >= n else None

    yr = rows[-250:]
    return {
        "stock_id": sid,
        "as_of": last["date"],
        "close": last["close"],
        "change": last.get("spread"),
        "returns_pct": {"1w": ret(5), "1m": ret(21), "3m": ret(63), "6m": ret(126), "1y": ret(245)},
        "high_52w": max(r["max"] for r in yr),
        "low_52w": min(r["min"] for r in yr),
        "ma": {"ma20": ma(20), "ma60": ma(60), "ma120": ma(120), "ma240": ma(240)},
        "avg_volume_20d_lots": _r(st.mean(r["Trading_Volume"] for r in rows[-20:]) / 1000, 0),
        "avg_turnover_20d_mn": _r(st.mean(r["Trading_money"] for r in rows[-20:]) / M, 1),
        "recent": [{"date": r["date"], "open": r["open"], "high": r["max"], "low": r["min"], "close": r["close"],
                    "volume_lots": round(r["Trading_Volume"] / 1000)} for r in rows[-recent_rows:]],
        "source": FINMIND_SRC,
    }


def valuation_history(stock_id: str, years: int = 5) -> dict:
    """Current PER / PBR / dividend yield and where they sit in their own history."""
    sid = _sid(stock_id)
    rows = [r for r in finmind("TaiwanStockPER", sid, _ago(365 * years + 5)) if r.get("PER") is not None]
    if not rows:
        return {"stock_id": sid, "error": "no valuation data"}
    last = rows[-1]

    def band(key):
        vals = [r[key] for r in rows if r.get(key) and r[key] > 0]
        cur = last.get(key)
        if not vals or not cur:
            return None
        return {"current": cur, "min": min(vals), "median": _r(st.median(vals)), "max": max(vals),
                "percentile": _r(sum(v <= cur for v in vals) / len(vals) * 100, 1)}

    return {
        "stock_id": sid,
        "as_of": last["date"],
        "period_years": years,
        "per": band("PER"),
        "pbr": band("PBR"),
        "dividend_yield_pct": band("dividend_yield"),
        "note": "PER uses trailing 4-quarter EPS (TWSE definition). percentile = % of days at or below current.",
        "source": FINMIND_SRC,
    }


INST_MAP = {"Foreign_Investor": "foreign", "Foreign_Dealer_Self": "foreign", "Investment_Trust": "trust",
            "Dealer_self": "dealer", "Dealer_Hedging": "dealer"}


def institutional_flows(stock_id: str, days: int = 20) -> dict:
    """Net buy/sell of 三大法人 in 張, with streaks and foreign holding %."""
    sid = _sid(stock_id)
    rows = finmind("TaiwanStockInstitutionalInvestorsBuySell", sid, _ago(int(days * 1.6) + 10))
    daily: dict[str, dict] = {}
    for r in rows:
        k = INST_MAP.get(r["name"])
        if not k:
            continue
        d = daily.setdefault(r["date"], {"foreign": 0, "trust": 0, "dealer": 0})
        d[k] += (r["buy"] - r["sell"]) / 1000
    dates = sorted(daily)[-days:]
    series = [{"date": d, **{k: round(v) for k, v in daily[d].items()},
               "total": round(sum(daily[d].values()))} for d in dates]

    def streak(k):
        n, sign = 0, 0
        for row in reversed(series):
            s = (row[k] > 0) - (row[k] < 0)
            if s == 0 or (sign and s != sign):
                break
            sign, n = s, n + 1
        return n * sign  # +5 = 5 consecutive net-buy days, -3 = 3 net-sell days

    hold = finmind("TaiwanStockShareholding", sid, _ago(int(days * 1.6) + 10))
    return {
        "stock_id": sid,
        "units": "張 (1,000 shares), net = buy - sell",
        "period": f"{dates[0]} ~ {dates[-1]}" if dates else None,
        "cumulative": {k: round(sum(r[k] for r in series)) for k in ("foreign", "trust", "dealer", "total")},
        "streak_days": {k: streak(k) for k in ("foreign", "trust", "dealer")},
        "foreign_holding_pct": {"latest": hold[-1]["ForeignInvestmentSharesRatio"] if hold else None,
                                "start": hold[0]["ForeignInvestmentSharesRatio"] if hold else None},
        "daily": series,
        "source": FINMIND_SRC,
    }


def margin_trading(stock_id: str, days: int = 20) -> dict:
    """融資融券: margin/short balances in 張 and 券資比."""
    sid = _sid(stock_id)
    rows = finmind("TaiwanStockMarginPurchaseShortSale", sid, _ago(int(days * 1.6) + 10))[-days:]
    if not rows:
        return {"stock_id": sid, "error": "no margin data"}
    series = [{"date": r["date"], "margin_balance": r["MarginPurchaseTodayBalance"],
               "short_balance": r["ShortSaleTodayBalance"],
               "short_margin_ratio_pct": _r(r["ShortSaleTodayBalance"] / r["MarginPurchaseTodayBalance"] * 100)
               if r["MarginPurchaseTodayBalance"] else None} for r in rows]
    f, l = series[0], series[-1]
    return {
        "stock_id": sid,
        "units": "張",
        "latest": l,
        "change_over_period": {"margin": l["margin_balance"] - f["margin_balance"],
                               "short": l["short_balance"] - f["short_balance"]},
        "margin_utilisation_pct": _r(rows[-1]["MarginPurchaseTodayBalance"] / rows[-1]["MarginPurchaseLimit"] * 100)
        if rows[-1].get("MarginPurchaseLimit") else None,
        "daily": series,
        "source": FINMIND_SRC,
    }


# ---------------------------------------------------------------- financial statements
def _pivot(rows: list[dict]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        out.setdefault(r["date"], {})[r["type"]] = r["value"]
    return out


def _first(d: dict, *keys):
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    return None


def financial_statements(stock_id: str, quarters: int = 8) -> dict:
    """Quarterly income statement, balance sheet and cash flow (single-quarter, de-cumulated)."""
    sid = _sid(stock_id)
    start = _ago(int(92 * (quarters + 5)))
    inc = _pivot(finmind("TaiwanStockFinancialStatements", sid, start, ttl=86400))
    bal = _pivot(finmind("TaiwanStockBalanceSheet", sid, start, ttl=86400))
    cf_cum = _pivot(finmind("TaiwanStockCashFlowsStatement", sid, start, ttl=86400))

    # Taiwan cash-flow statements are year-to-date; convert to single quarters.
    cf: dict[str, dict[str, float]] = {}
    for d in sorted(cf_cum):
        prev = None if d[5:7] == "03" else f"{d[:4]}-{ {'06': '03-31', '09': '06-30', '12': '09-30'}[d[5:7]] }"
        cf[d] = {k: v - (cf_cum.get(prev, {}).get(k, 0) if prev else 0) for k, v in cf_cum[d].items()}
        if prev and prev not in cf_cum:
            cf[d] = {}  # cannot de-cumulate without the prior quarter

    out = []
    for d in sorted(inc)[-quarters:]:
        i, b, c = inc[d], bal.get(d, {}), cf.get(d, {})
        rev = i.get("Revenue")
        ocf = _first(c, "CashFlowsFromOperatingActivities", "NetCashInflowFromOperatingActivities")
        capex = c.get("PropertyAndPlantAndEquipment")
        debt = sum(b.get(k, 0) or 0 for k in ("ShorttermBorrowings", "ShortTermBorrowings", "BondsPayable",
                                                 "LongtermBorrowings", "CurrentPortionOfLongTermLiabilities",
                                                 "CurrentPortionOfBondsPayable"))
        cash = b.get("CashAndCashEquivalents")

        def mn(v):
            return _r(v / M, 1) if v is not None else None

        def margin(v):
            return _r(v / rev * 100) if v is not None and rev else None

        out.append({
            "quarter": f"{d[:4]}Q{(int(d[5:7]) + 2) // 3}",
            "period_end": d,
            "income": {
                "revenue_mn": mn(rev), "gross_profit_mn": mn(i.get("GrossProfit")),
                "operating_income_mn": mn(i.get("OperatingIncome")), "pretax_income_mn": mn(i.get("PreTaxIncome")),
                "net_income_parent_mn": mn(_first(i, "EquityAttributableToOwnersOfParent", "IncomeAfterTaxes")),
                "eps": i.get("EPS"),
                "gross_margin_pct": margin(i.get("GrossProfit")),
                "operating_margin_pct": margin(i.get("OperatingIncome")),
                "net_margin_pct": margin(_first(i, "EquityAttributableToOwnersOfParent", "IncomeAfterTaxes")),
                "effective_tax_pct": _r(i["TAX"] / i["PreTaxIncome"] * 100)
                if i.get("TAX") is not None and i.get("PreTaxIncome") else None,
            },
            "balance": {
                "cash_mn": mn(cash), "total_debt_mn": mn(debt), "net_debt_mn": mn(debt - cash) if cash is not None else None,
                "inventories_mn": mn(b.get("Inventories")), "receivables_mn": mn(b.get("AccountsReceivableNet")),
                "ppe_mn": mn(b.get("PropertyPlantAndEquipment")), "total_assets_mn": mn(b.get("TotalAssets")),
                "total_liabilities_mn": mn(b.get("Liabilities")),
                "equity_parent_mn": mn(_first(b, "EquityAttributableToOwnersOfParent", "Equity")),
            },
            "cash_flow": {
                "operating_cf_mn": mn(ocf), "capex_mn": mn(-capex if capex is not None else None),
                "depreciation_mn": mn(c.get("Depreciation")), "amortization_mn": mn(c.get("AmortizationExpense")),
                "free_cash_flow_mn": mn(ocf + capex) if ocf is not None and capex is not None else None,
            },
        })
    for k, q in enumerate(out):
        prev_y = next((p for p in out if p["quarter"] == f"{int(q['quarter'][:4]) - 1}{q['quarter'][4:]}"), None)
        q["income"]["revenue_yoy_pct"] = _pct(q["income"]["revenue_mn"], prev_y["income"]["revenue_mn"]) if prev_y else None
        q["income"]["eps_yoy_pct"] = _pct(q["income"]["eps"], prev_y["income"]["eps"]) if prev_y and prev_y["income"]["eps"] else None
        if k:
            q["income"]["revenue_qoq_pct"] = _pct(q["income"]["revenue_mn"], out[k - 1]["income"]["revenue_mn"])
    ttm = out[-4:]
    return {
        "stock_id": sid,
        "units": "NT$ millions; EPS in NT$; cash flow is single-quarter (de-cumulated from YTD filings)",
        "ttm": {
            "revenue_mn": _r(sum(q["income"]["revenue_mn"] or 0 for q in ttm), 1),
            "eps": _r(sum(q["income"]["eps"] or 0 for q in ttm), 2),
            "free_cash_flow_mn": _r(sum(q["cash_flow"]["free_cash_flow_mn"] or 0 for q in ttm), 1),
        } if len(ttm) == 4 else None,
        "quarters": out,
        "note": "Quarterly reports are due 45 days after quarter end (Q4/annual: end of March).",
        "source": FINMIND_SRC,
    }


def dividends(stock_id: str, years: int = 8) -> dict:
    sid = _sid(stock_id)
    rows = finmind("TaiwanStockDividend", sid, _ago(365 * years), ttl=86400)
    keep = ("date", "year", "CashEarningsDistribution", "CashStatutorySurplus", "StockEarningsDistribution",
            "StockStatutorySurplus", "CashExDividendTradingDate", "CashDividendPaymentDate")
    out = []
    for r in rows:
        row = {k: r.get(k) for k in keep if k in r}
        row["cash_dividend"] = _r((r.get("CashEarningsDistribution") or 0) + (r.get("CashStatutorySurplus") or 0), 4)
        row["stock_dividend"] = _r((r.get("StockEarningsDistribution") or 0) + (r.get("StockStatutorySurplus") or 0), 4)
        out.append(row)
    return {"stock_id": sid, "units": "NT$ per share", "dividends": out, "source": FINMIND_SRC}


# ---------------------------------------------------------------- whole market
def _market_universe() -> list[dict]:
    """Merge TWSE + TPEx snapshots: valuation, latest monthly revenue, price & volume."""
    uni: dict[str, dict] = {}
    for r in twse("exchangeReport/BWIBBU_ALL"):
        uni[r["Code"]] = {"stock_id": r["Code"], "name": r["Name"], "market": "twse", "as_of": _roc_date(r["Date"]),
                          "pe": _num(r["PEratio"]), "pb": _num(r["PBratio"]), "dividend_yield_pct": _num(r["DividendYield"])}
    for r in tpex("tpex_mainboard_peratio_analysis"):
        uni[r["SecuritiesCompanyCode"]] = {
            "stock_id": r["SecuritiesCompanyCode"], "name": r["CompanyName"], "market": "tpex",
            "as_of": _roc_date(r["Date"]), "pe": _num(r["PriceEarningRatio"]), "pb": _num(r["PriceBookRatio"]),
            "dividend_yield_pct": _num(r["YieldRatio"])}
    for src in (twse("opendata/t187ap05_L"), tpex("mopsfin_t187ap05_O")):
        for r in src:
            u = uni.get(r["公司代號"])
            if not u:
                continue
            u.update({"industry": r["產業別"], "revenue_month": _roc_date(r["資料年月"]),
                      "revenue_mn": _r((_num(r["營業收入-當月營收"]) or 0) / 1000, 1),
                      "revenue_yoy_pct": _r(_num(r["營業收入-去年同月增減(%)"])),
                      "revenue_mom_pct": _r(_num(r["營業收入-上月比較增減(%)"])),
                      "ytd_revenue_yoy_pct": _r(_num(r["累計營業收入-前期比較增減(%)"]))})
    for r in twse("exchangeReport/STOCK_DAY_ALL"):
        u = uni.get(r.get("Code"))
        if u:
            u["close"] = _num(r.get("ClosingPrice"))
            vol = _num(r.get("TradeVolume"))
            u["volume_lots"] = round(vol / 1000) if vol else None
    for r in tpex("tpex_mainboard_daily_close_quotes"):
        u = uni.get(r.get("SecuritiesCompanyCode"))
        if u:
            u["close"] = _num(r.get("Close"))
            vol = _num(r.get("TradingShares"))
            u["volume_lots"] = round(vol / 1000) if vol else None
    return list(uni.values())


def market_screen(max_pe: float | None = None, min_pe: float | None = None, max_pb: float | None = None,
                  min_dividend_yield: float | None = None, min_revenue_yoy: float | None = None,
                  min_ytd_revenue_yoy: float | None = None, industry: str | None = None,
                  market: str = "all", min_volume_lots: int | None = None,
                  sort_by: str = "revenue_yoy_pct", descending: bool = True, limit: int = 30,
                  exclude_outliers: bool = True) -> dict:
    """Screen all listed (上市) + OTC (上櫃) stocks on valuation, monthly revenue growth and liquidity.

    exclude_outliers drops rows whose source data looks broken (dividend yield > 25%)."""
    rows = _market_universe()
    if exclude_outliers:
        rows = [r for r in rows if (r.get("dividend_yield_pct") or 0) <= 25]

    def ok(r):
        checks = [
            (max_pe, lambda: r.get("pe") is not None and 0 < r["pe"] <= max_pe),
            (min_pe, lambda: r.get("pe") is not None and r["pe"] >= min_pe),
            (max_pb, lambda: r.get("pb") is not None and r["pb"] <= max_pb),
            (min_dividend_yield, lambda: (r.get("dividend_yield_pct") or 0) >= min_dividend_yield),
            (min_revenue_yoy, lambda: r.get("revenue_yoy_pct") is not None and r["revenue_yoy_pct"] >= min_revenue_yoy),
            (min_ytd_revenue_yoy, lambda: r.get("ytd_revenue_yoy_pct") is not None and r["ytd_revenue_yoy_pct"] >= min_ytd_revenue_yoy),
            (industry, lambda: industry in (r.get("industry") or "")),
            (min_volume_lots, lambda: (r.get("volume_lots") or 0) >= min_volume_lots),
        ]
        if market in ("twse", "tpex") and r["market"] != market:
            return False
        return all(f() for v, f in checks if v is not None)

    hits = [r for r in rows if ok(r)]
    hits.sort(key=lambda r: (r.get(sort_by) is None, -(r.get(sort_by) or 0) if descending else (r.get(sort_by) or 0)))
    return {
        "universe_size": len(rows),
        "matches": len(hits),
        "sorted_by": sort_by,
        "results": hits[:limit],
        "units": "revenue NT$ millions (latest published month); volume in 張",
        "caveat": "Single-month revenue YoY is lumpy (construction, asset sales); confirm with tw_monthly_revenue.",
        "source": "TWSE OpenAPI (openapi.twse.com.tw) + TPEx OpenAPI (tpex.org.tw/openapi)",
    }
