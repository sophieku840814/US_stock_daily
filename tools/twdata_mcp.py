"""MCP server exposing the free Taiwan stock data layer to Claude (and the Claude Finance skills).

Run (stdio):  python tools/twdata_mcp.py
Optional env: FINMIND_API_TOKEN (higher FinMind rate limit), TWDATA_CACHE (cache dir)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.mcpserver import MCPServer  # noqa: E402  (mcp>=2)

import twdata as t  # noqa: E402
from twdata.sources import DataSourceError  # noqa: E402

server = MCPServer(
    name="tw-stock-data",
    instructions=(
        "Free Taiwan stock data (上市/上櫃) from FinMind and TWSE/TPEx OpenAPI. "
        "Use stock ids like '2330' (no suffix). Money is NT$ millions unless noted; volumes in 張. "
        "Use these tools instead of SEC/10-K sources when a Claude Finance skill (dcf, comps, earnings, "
        "thesis, screen, catalysts, morning-note) is run on a Taiwan stock, and cite the `source` field."
    ),
)


def _safe(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except DataSourceError as e:
        return {"error": str(e)}


@server.tool()
def tw_stock_info(stock_id: str) -> dict:
    """Company basics: name, market (twse=上市 / tpex=上櫃), industry, shares issued, foreign holding %."""
    return _safe(t.stock_info, stock_id)


@server.tool()
def tw_monthly_revenue(stock_id: str, months: int = 24) -> dict:
    """Monthly revenue (月營收) with MoM, YoY, YTD YoY, TTM and record-high flag. Published by the 10th of each month."""
    return _safe(t.monthly_revenue, stock_id, months)


@server.tool()
def tw_financial_statements(stock_id: str, quarters: int = 8) -> dict:
    """Quarterly income statement, balance sheet and single-quarter cash flow (capex, D&A, FCF), with margins and YoY."""
    return _safe(t.financial_statements, stock_id, quarters)


@server.tool()
def tw_price_history(stock_id: str, days: int = 365, recent_rows: int = 10) -> dict:
    """Price summary: returns (1w-1y), 52-week range, moving averages, 20-day avg volume/turnover, recent daily bars."""
    return _safe(t.price_history, stock_id, days, recent_rows)


@server.tool()
def tw_valuation_history(stock_id: str, years: int = 5) -> dict:
    """Current PER / PBR / dividend yield vs. their own N-year range (min/median/max/percentile)."""
    return _safe(t.valuation_history, stock_id, years)


@server.tool()
def tw_institutional_flows(stock_id: str, days: int = 20) -> dict:
    """三大法人 net buy/sell (foreign, investment trust, dealer) in 張: daily, cumulative, streaks, foreign holding change."""
    return _safe(t.institutional_flows, stock_id, days)


@server.tool()
def tw_margin_trading(stock_id: str, days: int = 20) -> dict:
    """融資融券 balances in 張, change over period, 券資比 and margin utilisation."""
    return _safe(t.margin_trading, stock_id, days)


@server.tool()
def tw_dividends(stock_id: str, years: int = 8) -> dict:
    """Dividend history: cash/stock dividend per share, ex-dividend and payment dates."""
    return _safe(t.dividends, stock_id, years)


@server.tool()
def tw_market_screen(max_pe: float | None = None, min_pe: float | None = None, max_pb: float | None = None,
                     min_dividend_yield: float | None = None, min_revenue_yoy: float | None = None,
                     min_ytd_revenue_yoy: float | None = None, industry: str | None = None,
                     market: str = "all", min_volume_lots: int | None = None,
                     sort_by: str = "revenue_yoy_pct", descending: bool = True, limit: int = 30,
                     exclude_outliers: bool = True) -> dict:
    """Screen all ~2,000 上市+上櫃 stocks. Filters: PE/PB range, dividend yield %, latest monthly revenue YoY %,
    YTD revenue YoY %, industry substring (e.g. '半導體'), market ('twse'|'tpex'|'all'), min daily volume (張).
    sort_by any result field (revenue_yoy_pct, pe, dividend_yield_pct, ytd_revenue_yoy_pct, volume_lots...)."""
    return _safe(t.market_screen, max_pe, min_pe, max_pb, min_dividend_yield, min_revenue_yoy,
                 min_ytd_revenue_yoy, industry, market, min_volume_lots, sort_by, descending, limit,
                 exclude_outliers)


if __name__ == "__main__":
    server.run("stdio")
