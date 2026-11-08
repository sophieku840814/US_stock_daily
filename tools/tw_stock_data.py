"""Free market-data layer for the Claude Finance skills (comps / DCF).

Uses yfinance (Yahoo Finance) so it works for Taiwan stocks (2330.TW, 5347.TWO)
and overseas peers (GFS, 0981.HK). Outputs JSON so Claude skills can read it.

Usage:
    python tools/tw_stock_data.py comps 2330.TW 2303.TW 5347.TWO 6770.TW GFS 0981.HK
    python tools/tw_stock_data.py dcf 2330.TW
"""
import json
import sys
from datetime import date

import yfinance as yf


def _f(v):
    try:
        v = float(v)
        return None if v != v else v  # drop NaN
    except (TypeError, ValueError):
        return None


def comps_row(ticker: str) -> dict:
    info = yf.Ticker(ticker).info
    return {
        "ticker": ticker,
        "name": info.get("shortName"),
        "currency": info.get("currency"),
        "price": _f(info.get("currentPrice") or info.get("regularMarketPrice")),
        "market_cap": _f(info.get("marketCap")),
        "enterprise_value": _f(info.get("enterpriseValue")),
        "revenue_ttm": _f(info.get("totalRevenue")),
        "ebitda_ttm": _f(info.get("ebitda")),
        "revenue_growth_yoy": _f(info.get("revenueGrowth")),
        "gross_margin": _f(info.get("grossMargins")),
        "operating_margin": _f(info.get("operatingMargins")),
        "net_margin": _f(info.get("profitMargins")),
        "roe": _f(info.get("returnOnEquity")),
        "pe_ttm": _f(info.get("trailingPE")),
        "pe_fwd": _f(info.get("forwardPE")),
        "pb": _f(info.get("priceToBook")),
        "ev_ebitda": _f(info.get("enterpriseToEbitda")),
        "ev_revenue": _f(info.get("enterpriseToRevenue")),
        "dividend_yield": _f(info.get("dividendYield")),
        "beta": _f(info.get("beta")),
    }


def dcf_inputs(ticker: str) -> dict:
    t = yf.Ticker(ticker)
    info = t.info
    inc, cf, bs = t.income_stmt, t.cashflow, t.balance_sheet

    def row(df, *names):
        for n in names:
            if n in df.index:
                return {str(c.date()): _f(v) for c, v in df.loc[n].items()}
        return {}

    return {
        "ticker": ticker,
        "name": info.get("shortName"),
        "currency": info.get("financialCurrency") or info.get("currency"),
        "as_of": str(date.today()),
        "source": "Yahoo Finance via yfinance (annual statements)",
        "price": _f(info.get("currentPrice") or info.get("regularMarketPrice")),
        "shares_outstanding": _f(info.get("sharesOutstanding")),
        "beta": _f(info.get("beta")),
        "total_debt": _f(info.get("totalDebt")),
        "total_cash": _f(info.get("totalCash")),
        "annual": {
            "revenue": row(inc, "Total Revenue"),
            "gross_profit": row(inc, "Gross Profit"),
            "operating_income": row(inc, "Operating Income"),
            "ebitda": row(inc, "EBITDA", "Normalized EBITDA"),
            "net_income": row(inc, "Net Income"),
            "tax_rate": row(inc, "Tax Rate For Calcs"),
            "d_and_a": row(cf, "Depreciation And Amortization"),
            "capex": row(cf, "Capital Expenditure"),
            "change_in_working_capital": row(cf, "Change In Working Capital"),
            "operating_cash_flow": row(cf, "Operating Cash Flow"),
            "free_cash_flow": row(cf, "Free Cash Flow"),
            "total_debt": row(bs, "Total Debt"),
            "cash": row(bs, "Cash And Cash Equivalents"),
        },
    }


if __name__ == "__main__":
    mode, tickers = sys.argv[1], sys.argv[2:]
    if mode == "comps":
        out = [comps_row(tk) for tk in tickers]
    elif mode == "dcf":
        out = dcf_inputs(tickers[0])
    else:
        sys.exit("mode must be 'comps' or 'dcf'")
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
