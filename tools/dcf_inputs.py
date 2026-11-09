"""通用 DCF 輸入資料產生器：輸入股票代號，輸出 data/dcf_inputs/{TICKER}.json（公開資料，可進 repo）。

用法：
    python -X utf8 tools/dcf_inputs.py 2330.TW
    python -X utf8 tools/dcf_inputs.py AMD

資料來源：
- 台股（.TW / .TWO）：專案台股資料層 tools/twdata（FinMind，原始資料來自 TWSE/TPEx/MOPS）。
  季報（financial_statements，現金流量已由累計值還原為單季）彙總成年度；
  稅額、利息費用、營運資金變動、短期投資等 financial_statements 沒有的欄位，
  直接讀同一批 FinMind 原始報表（twdata.sources.finmind）。
  最近一個不完整年度另存為近四季（TTM）。現價取 twdata 最新收盤與 yfinance 即時價中日期較新者，股數取 twdata 發行股數。
- 營運資金另以資產負債表計算（應收＋存貨−應付，年增加＝delta_op_nwc）；折舊與攤銷分開存（depreciation / amortization）。
- 美股：yfinance 年報（沿用 tw_stock_data.dcf_inputs 的欄位邏輯）＋近四季 TTM。
- 兩者共同：beta、分析師營收／EPS 預估（yfinance revenue_estimate / growth_estimates，有才存）、
  美股無風險利率（yfinance ^TNX，報價單位是「百分比」，例如 4.86 代表 4.86%，存檔時已除以 100）。

金額一律為原幣「元」（不是百萬），稅率為小數。抓不到的欄位存 null，不編造。
"""
from __future__ import annotations

import json
import math
import sys
from datetime import date
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

OUT_DIR = ROOT / "data" / "dcf_inputs"
TODAY = date.today()


def _f(v):
    try:
        v = float(v)
        return None if math.isnan(v) or math.isinf(v) else v
    except (TypeError, ValueError):
        return None


def _is_tw(ticker: str) -> bool:
    t = ticker.upper()
    return t.endswith(".TW") or t.endswith(".TWO")


# ====================================================================== yfinance (both markets)
def _yf_extras(ticker: str) -> dict:
    """beta、分析師預估、名稱、幣別（台美股共用）。失敗的欄位留 None。"""
    import yfinance as yf

    t = yf.Ticker(ticker)
    out: dict = {"beta": None, "name": None, "currency": None, "estimates": None,
                 "yf_price": None, "yf_shares": None, "info": {}}
    try:
        info = t.info or {}
    except Exception:  # noqa: BLE001
        info = {}
    out["info"] = info
    out["beta"] = _f(info.get("beta"))
    out["name"] = info.get("longName") or info.get("shortName")
    out["currency"] = info.get("financialCurrency") or info.get("currency")
    out["yf_price"] = _f(info.get("currentPrice") or info.get("regularMarketPrice"))
    out["yf_shares"] = _f(info.get("sharesOutstanding"))
    ts = info.get("regularMarketTime")
    out["yf_price_date"] = date.fromtimestamp(ts).isoformat() if isinstance(ts, (int, float)) else None

    est: dict = {}
    try:
        re_ = t.revenue_estimate
        if re_ is not None and len(re_):
            est["revenue"] = {p: {k: _f(re_.loc[p].get(k)) for k in ("avg", "low", "high", "yearAgoRevenue", "growth",
                                                                       "numberOfAnalysts")}
                              for p in re_.index if p in ("0y", "+1y")}
    except Exception:  # noqa: BLE001
        pass
    try:
        ge = t.growth_estimates
        if ge is not None and len(ge):
            est["eps_growth"] = {p: _f(ge.loc[p].get("stockTrend")) for p in ge.index}
    except Exception:  # noqa: BLE001
        pass
    try:
        ee = t.earnings_estimate
        if ee is not None and len(ee):
            est["eps"] = {p: {k: _f(ee.loc[p].get(k)) for k in ("avg", "yearAgoEps", "growth", "numberOfAnalysts")}
                          for p in ee.index if p in ("0y", "+1y")}
    except Exception:  # noqa: BLE001
        pass
    out["estimates"] = est or None
    return out


def _us10y() -> dict | None:
    """^TNX 收盤價（單位：百分比）→ 小數。"""
    import yfinance as yf

    try:
        h = yf.Ticker("^TNX").history(period="10d")["Close"].dropna()
        if len(h):
            return {"value": round(float(h.iloc[-1]) / 100, 5), "as_of": str(h.index[-1].date()),
                    "source": "Yahoo Finance ^TNX (CBOE 10Y Treasury yield index, quoted in %; divided by 100)"}
    except Exception:  # noqa: BLE001
        pass
    return None


# ====================================================================== Taiwan (twdata)
def _tw_inputs(ticker: str) -> dict:
    from twdata.api import FINMIND_SRC, _ago, _pivot, financial_statements, monthly_revenue, price_history, stock_info
    from twdata.sources import finmind

    sid = ticker.upper().replace(".TWO", "").replace(".TW", "")
    Q = 20  # 5 年季資料：足夠 4 個完整年度 + TTM
    fs = financial_statements(sid, quarters=Q)
    start = _ago(int(92 * (Q + 5)))  # 與 financial_statements 相同 → 走快取
    inc = _pivot(finmind("TaiwanStockFinancialStatements", sid, start, ttl=86400))
    bal = _pivot(finmind("TaiwanStockBalanceSheet", sid, start, ttl=86400))
    cfc = _pivot(finmind("TaiwanStockCashFlowsStatement", sid, start, ttl=86400))  # 年初至今累計

    def cf_q(d: str, key: str):
        """單季現金流量（由累計值還原）。"""
        cur = cfc.get(d, {}).get(key)
        if cur is None:
            return None
        if d[5:7] == "03":
            return cur
        prev = f"{d[:4]}-{ {'06': '03-31', '09': '06-30', '12': '09-30'}[d[5:7]] }"
        if prev not in cfc:
            return None
        return cur - (cfc[prev].get(key) or 0)

    def wc_q(d: str):
        """營運資金變動（yfinance 慣例：負值＝資金被營運資金占用）＝營運產生之現金 − 稅前淨利 − 收益費損調整項。"""
        vals = [cf_q(d, k) for k in ("CashReceivedThroughOperations", "NetIncomeBeforeTax", "TotalIncomeLossItems")]
        return None if any(v is None for v in vals) else vals[0] - vals[1] - vals[2]

    def debt_at(d: str):
        b = bal.get(d)
        if not b:
            return None
        return sum(b.get(k, 0) or 0 for k in ("ShorttermBorrowings", "ShortTermBorrowings", "BondsPayable",
                                                 "LongtermBorrowings", "CurrentPortionOfLongTermLiabilities",
                                                 "CurrentPortionOfBondsPayable"))

    def cash_at(d: str):
        b = bal.get(d)
        if not b or b.get("CashAndCashEquivalents") is None:
            return None, None
        st = sum(b.get(k, 0) or 0 for k in ("CurrentFinancialAssetsAtFairvalueThroughProfitOrLoss",
                                               "FinancialAssetsAtFairvalueThroughOtherComprehensiveIncome",
                                               "FinancialAssetsAtAmortizedCost"))
        return b["CashAndCashEquivalents"], b["CashAndCashEquivalents"] + st

    def op_nwc_at(d: str):
        """營業營運資金 = 應收帳款（含關係人）＋存貨 − 應付帳款（含關係人）。"""
        b = bal.get(d)
        if not b or b.get("Inventories") is None and b.get("AccountsReceivableNet") is None:
            return None
        g = lambda k: b.get(k) or 0  # noqa: E731
        return (g("AccountsReceivableNet") + g("AccountsReceivableDuefromRelatedPartiesNet") + g("Inventories")
                - g("AccountsPayable") - g("AccountsPayableToRelatedParties"))

    def year_ago(d: str) -> str:
        return f"{int(d[:4]) - 1}{d[4:]}"

    qs = {q["period_end"]: q for q in fs["quarters"]}

    def agg(ends: list[str]) -> dict | None:
        if any(e not in qs or e not in inc for e in ends):
            return None
        def s(fn):
            vals = [fn(e) for e in ends]
            return None if any(v is None for v in vals) else sum(vals)

        rev = s(lambda e: inc[e].get("Revenue"))
        pbt = s(lambda e: inc[e].get("PreTaxIncome"))
        tax = s(lambda e: inc[e].get("TAX"))
        dep = s(lambda e: cf_q(e, "Depreciation"))
        amo = s(lambda e: cf_q(e, "AmortizationExpense") or 0.0)
        capex = s(lambda e: cf_q(e, "PropertyAndPlantAndEquipment"))  # 負值＝支出（與 yfinance 同號）
        ocf = s(lambda e: cf_q(e, "CashFlowsFromOperatingActivities") or cf_q(e, "NetCashInflowFromOperatingActivities"))
        intx = s(lambda e: cf_q(e, "InterestExpense"))
        last = ends[-1]
        cash, cash_st = cash_at(last)
        nwc_now, nwc_ago = op_nwc_at(last), op_nwc_at(year_ago(last))
        return {
            "period_end": last,
            "revenue": rev,
            "gross_profit": s(lambda e: inc[e].get("GrossProfit")),
            "operating_income": s(lambda e: inc[e].get("OperatingIncome")),
            "pretax_income": pbt,
            "tax": tax,
            "tax_rate": (tax / pbt) if tax is not None and pbt else None,
            "d_and_a": (dep + amo) if dep is not None else None,
            "depreciation": dep,
            "amortization": amo,
            "op_nwc": nwc_now,
            "delta_op_nwc": (nwc_now - nwc_ago) if nwc_now is not None and nwc_ago is not None else None,
            "capex": capex,
            "change_in_working_capital": s(wc_q),
            "operating_cash_flow": ocf,
            "free_cash_flow": (ocf + capex) if ocf is not None and capex is not None else None,
            "interest_expense": intx,
            "total_debt": debt_at(last),
            "cash": cash,
            "cash_and_st_investments": cash_st,
        }

    ends = sorted(qs)
    years = sorted({e[:4] for e in ends})
    annual = {}
    for y in years:
        yq = [f"{y}-03-31", f"{y}-06-30", f"{y}-09-30", f"{y}-12-31"]
        a = agg(yq)
        if a and a["revenue"]:
            annual[f"{y}-12-31"] = a
    annual = dict(sorted(annual.items())[-4:])
    ttm = agg(ends[-4:]) if len(ends) >= 4 else None
    if ttm:
        ttm["period"] = f"{qs[ends[-4]]['quarter']}~{qs[ends[-1]]['quarter']}"

    # 平均負債與利息費用 → 舉債成本估計（最近完整年度）
    kd = None
    ys = sorted(annual)
    if ys:
        last_y = annual[ys[-1]]
        prev_debt = annual[ys[-2]]["total_debt"] if len(ys) > 1 else None
        if last_y["interest_expense"] and last_y["total_debt"]:
            avg = (last_y["total_debt"] + prev_debt) / 2 if prev_debt else last_y["total_debt"]
            kd = {"value": last_y["interest_expense"] / avg if avg else None, "interest_expense": last_y["interest_expense"],
                  "avg_debt": avg, "fiscal_year": ys[-1][:4],
                  "method": "FY 利息費用（現金流量表加回之利息費用，含租賃利息）÷ 期初期末平均有息負債（借款＋應付公司債）"}

    px = price_history(sid, days=15)
    info = stock_info(sid)
    mr = monthly_revenue(sid, months=13)
    latest_bs = ends[-1]
    cash, cash_st = cash_at(latest_bs)
    shares = _f(info.get("shares_issued"))
    yfx = _yf_extras(ticker)
    # 現價：twdata 最新收盤 vs yfinance 即時價，取日期較新者（同日取 yfinance，因可能為盤中／當日收盤）
    price, price_date, price_src = _f(px.get("close")), px.get("as_of"), f"twdata.price_history close ({FINMIND_SRC})"
    if yfx["yf_price"] and yfx["yf_price_date"] and (not price_date or yfx["yf_price_date"] >= price_date):
        price_src = (f"yfinance info.currentPrice（報價時間 {yfx['yf_price_date']}；"
                     f"較 twdata 最新收盤 {price_date} NT${price} 新）")
        price, price_date = yfx["yf_price"], yfx["yf_price_date"]
    ytd = None
    if mr.get("latest") and mr["latest"].get("ytd_yoy_pct") is not None:
        ytd = {"through_month": mr["latest"]["month"], "ytd_revenue": mr["latest"]["ytd_revenue_mn"] * 1e6,
               "ytd_yoy": mr["latest"]["ytd_yoy_pct"] / 100, "source": "twdata.monthly_revenue (MOPS 月營收)"}
    return {
        "market": "TW",
        "name": info.get("name") or yfx["name"],
        "name_en": yfx["name"],
        "currency": "TWD",
        "price": price,
        "price_date": price_date,
        "price_source": price_src,
        "shares_outstanding": shares if shares else yfx["yf_shares"],
        "shares_source": "twdata.stock_info NumberOfSharesIssued (FinMind TaiwanStockShareholding)" if shares
        else "yfinance sharesOutstanding",
        "beta": yfx["beta"],
        "beta_source": "yfinance info.beta (Yahoo 5Y monthly)",
        "total_debt": debt_at(latest_bs),
        "total_cash": cash_st,
        "cash_only": cash,
        "balance_sheet_date": latest_bs,
        "debt_definition": "短期借款＋應付公司債＋長期借款＋一年內到期長期負債（不含租賃負債）",
        "cash_definition": "現金及約當現金＋流動金融資產（FVTPL、FVOCI、按攤銷後成本衡量）",
        "fiscal_year_end": "12-31",
        "annual": annual,
        "ttm": ttm,
        "current_year_ytd": ytd,
        "cost_of_debt_estimate": kd,
        "estimates": yfx["estimates"],
        "risk_free": None,
        "source": f"{FINMIND_SRC} via tools/twdata (季報彙總為年度); beta/分析師預估: Yahoo Finance via yfinance",
    }


# ====================================================================== US (yfinance)
def _us_inputs(ticker: str) -> dict:
    import yfinance as yf

    t = yf.Ticker(ticker)
    yfx = _yf_extras(ticker)
    info = yfx["info"]
    inc, cf, bs = t.income_stmt, t.cashflow, t.balance_sheet
    qinc, qcf, qbs = t.quarterly_income_stmt, t.quarterly_cashflow, t.quarterly_balance_sheet

    def val(df, col, *names):
        for n in names:
            if df is not None and n in df.index and col in df.columns:
                v = _f(df.loc[n, col])
                if v is not None:
                    return v
        return None

    def op_nwc(B, col):
        ar = val(B, col, "Accounts Receivable", "Receivables")
        inv = val(B, col, "Inventory")
        ap = val(B, col, "Accounts Payable")
        if ar is None and inv is None:
            return None
        return (ar or 0) + (inv or 0) - (ap or 0)

    def row_for(col, I, C, B):
        rev = val(I, col, "Total Revenue")
        pbt = val(I, col, "Pretax Income")
        tax = val(I, col, "Tax Provision")
        ocf = val(C, col, "Operating Cash Flow")
        capex = val(C, col, "Capital Expenditure")
        return {
            "period_end": str(col.date()),
            "revenue": rev,
            "gross_profit": val(I, col, "Gross Profit"),
            "operating_income": val(I, col, "Operating Income"),
            "pretax_income": pbt,
            "tax": tax,
            "tax_rate": (tax / pbt) if tax is not None and pbt else val(I, col, "Tax Rate For Calcs"),
            "d_and_a": val(C, col, "Depreciation And Amortization", "Depreciation Amortization Depletion"),
            "depreciation": val(C, col, "Depreciation"),
            "amortization": val(C, col, "Amortization Of Intangibles", "Amortization Cash Flow"),
            "op_nwc": op_nwc(B, col),
            "capex": capex,
            "change_in_working_capital": val(C, col, "Change In Working Capital"),
            "operating_cash_flow": ocf,
            "free_cash_flow": val(C, col, "Free Cash Flow"),
            "interest_expense": val(I, col, "Interest Expense", "Interest Expense Non Operating"),
            "total_debt": val(B, col, "Total Debt"),
            "cash": val(B, col, "Cash And Cash Equivalents"),
            "cash_and_st_investments": val(B, col, "Cash Cash Equivalents And Short Term Investments"),
        }

    annual = {}
    for col in sorted(inc.columns):
        r = row_for(col, inc, cf, bs)
        if r["revenue"] and r["operating_income"] is not None:
            annual[r["period_end"]] = r
    for col in sorted(bs.columns):  # 年增營運資金（需上一年度資產負債表）
        k = str(col.date())
        if k in annual:
            prev = [c for c in bs.columns if c.year == col.year - 1]
            p = op_nwc(bs, prev[0]) if prev else None
            n = annual[k]["op_nwc"]
            annual[k]["delta_op_nwc"] = (n - p) if n is not None and p is not None else None
    annual = dict(sorted(annual.items())[-4:])

    ttm = None
    qcols = sorted(qinc.columns)[-4:] if qinc is not None else []
    if len(qcols) == 4:
        rows = [row_for(c, qinc, qcf, qbs) for c in qcols]
        flow = ("revenue", "gross_profit", "operating_income", "pretax_income", "tax", "d_and_a", "depreciation",
                "amortization", "capex",
                "change_in_working_capital", "operating_cash_flow", "free_cash_flow", "interest_expense")
        ttm = {k: (None if any(r[k] is None for r in rows) else sum(r[k] for r in rows)) for k in flow}
        ttm["tax_rate"] = ttm["tax"] / ttm["pretax_income"] if ttm["tax"] is not None and ttm["pretax_income"] else None
        for k in ("total_debt", "cash", "cash_and_st_investments"):
            ttm[k] = rows[-1][k]
        ttm["period_end"] = rows[-1]["period_end"]
        ttm["period"] = f"{rows[0]['period_end']}~{rows[-1]['period_end']} (4 fiscal quarters)"
        ttm["op_nwc"] = rows[-1]["op_nwc"]
        ago = [c for c in qbs.columns if (qcols[-1] - c).days in range(350, 380)]
        p = op_nwc(qbs, ago[0]) if ago else None
        ttm["delta_op_nwc"] = (ttm["op_nwc"] - p) if ttm["op_nwc"] is not None and p is not None else None

    kd = None
    ys = sorted(annual)
    if ys:
        a = annual[ys[-1]]
        prev_debt = annual[ys[-2]]["total_debt"] if len(ys) > 1 else None
        if a["interest_expense"] and a["total_debt"]:
            avg = (a["total_debt"] + prev_debt) / 2 if prev_debt else a["total_debt"]
            kd = {"value": a["interest_expense"] / avg, "interest_expense": a["interest_expense"], "avg_debt": avg,
                  "fiscal_year": ys[-1][:4], "method": "FY Interest Expense ÷ average Total Debt (yfinance annual)"}

    last_fy = ys[-1] if ys else None
    return {
        "market": "US",
        "name": yfx["name"],
        "name_en": yfx["name"],
        "currency": yfx["currency"] or "USD",
        "price": yfx["yf_price"],
        "price_date": yfx["yf_price_date"] or str(TODAY),
        "price_source": "yfinance info.currentPrice",
        "shares_outstanding": yfx["yf_shares"],
        "shares_source": "yfinance info.sharesOutstanding (basic)",
        "beta": yfx["beta"],
        "beta_source": "yfinance info.beta (Yahoo 5Y monthly)",
        "total_debt": _f(info.get("totalDebt")),
        "total_cash": _f(info.get("totalCash")),
        "cash_only": None,
        "balance_sheet_date": ttm["period_end"] if ttm else last_fy,
        "debt_definition": "yfinance info.totalDebt (MRQ, incl. leases)",
        "cash_definition": "yfinance info.totalCash (MRQ, cash + short-term investments)",
        "fiscal_year_end": last_fy[5:] if last_fy else "12-31",
        "annual": annual,
        "ttm": ttm,
        "current_year_ytd": None,
        "cost_of_debt_estimate": kd,
        "estimates": yfx["estimates"],
        "risk_free": _us10y(),
        "source": "Yahoo Finance via yfinance (annual + quarterly statements)",
    }


def build(ticker: str) -> dict:
    ticker = ticker.upper()
    body = _tw_inputs(ticker) if _is_tw(ticker) else _us_inputs(ticker)
    return {"ticker": ticker, "valuation_date": str(TODAY), "as_of": str(TODAY), **body}


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: python -X utf8 tools/dcf_inputs.py TICKER")
    d = build(sys.argv[1])
    if not d["annual"] or len(d["annual"]) < 3:
        sys.exit(f"{d['ticker']}: 完整年度資料不足 3 年（取得 {len(d['annual'])} 年），無法建立 DCF")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{d['ticker']}.json"
    out.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
