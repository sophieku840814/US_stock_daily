"""Build TSMC (2330.TW) DCF model following the Claude Finance dcf-model skill.

All projections / valuation / sensitivity cells are live Excel formulas.
Inputs come from data/2330_dcf_inputs.json (yfinance) plus user-approved assumptions.

    python tools/build_dcf_2330.py
"""
import json
from datetime import date
from pathlib import Path

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.formatting.rule import ColorScaleRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

ROOT = Path(__file__).resolve().parents[1]
D = json.loads((ROOT / "data" / "2330_dcf_inputs.json").read_text(encoding="utf-8"))
A = D["annual"]
TODAY = date.today().isoformat()
OUT = ROOT / "models" / f"2330_DCF_Model_{TODAY}.xlsx"
YF = f"Source: Yahoo Finance via yfinance, pulled {TODAY}"
BN = 1e9

# ---------- styles ----------
BLUE, BLACK, GREEN = Font(color="0000FF"), Font(color="000000"), Font(color="008000")
F_HDR = PatternFill("solid", fgColor="1F4E79")
F_SUB = PatternFill("solid", fgColor="D9E1F2")
F_INP = PatternFill("solid", fgColor="F2F2F2")
F_OUT = PatternFill("solid", fgColor="BDD7EE")
NUM = '#,##0;(#,##0);"-"'
NUM1 = '#,##0.0;(#,##0.0);"-"'
PCT = '0.0%;(0.0%);"-"'
PX = '"NT$"#,##0.0;("NT$"#,##0.0);"-"'
MULT = '0.000'


def note(c, src):
    c.comment = Comment(src, "Claude")
    c.comment.width, c.comment.height = 320, 80


def inp(ws, ref, val, fmt, src):
    c = ws[ref]
    c.value, c.font, c.fill, c.number_format = val, BLUE, F_INP, fmt
    note(c, src)
    return c


def calc(ws, ref, f, fmt, bold=False, out=False):
    c = ws[ref]
    c.value, c.number_format = f, fmt
    c.font = Font(color="000000", bold=bold or out)
    if out:
        c.fill = F_OUT
    return c


def link(ws, ref, f, fmt):
    c = ws[ref]
    c.value, c.font, c.number_format = f, GREEN, fmt
    return c


def header(ws, row, text, c1="A", c2="J"):
    ws[f"{c1}{row}"] = text
    ws[f"{c1}{row}"].font = Font(bold=True, color="FFFFFF")
    for col in range(ord(c1), ord(c2) + 1):
        ws[f"{chr(col)}{row}"].fill = F_HDR
    ws.merge_cells(f"{c1}{row}:{c2}{row}")


def subhdr(ws, row, labels, start="A"):
    for i, t in enumerate(labels):
        c = ws[f"{chr(ord(start) + i)}{row}"]
        c.value, c.font, c.fill = t, Font(bold=True), F_SUB
        c.alignment = Alignment(horizontal="center" if i else "left")


def box(ws, rng, weight="medium"):
    s = Side(style=weight)
    (c1, r1), (c2, r2) = [(x.rstrip("0123456789"), int(x.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))) for x in rng.split(":")]
    for r in range(r1, r2 + 1):
        for col in range(ord(c1), ord(c2) + 1):
            cell = ws[f"{chr(col)}{r}"]
            b = cell.border
            cell.border = Border(
                left=s if col == ord(c1) else b.left,
                right=s if col == ord(c2) else b.right,
                top=s if r == r1 else b.top,
                bottom=s if r == r2 else b.bottom,
            )


wb = Workbook()
ws = wb.active
ws.title = "DCF"
wc = wb.create_sheet("WACC")
ws.column_dimensions["A"].width = 34
for col in "BCDEFGHIJ":
    ws.column_dimensions[col].width = 13
wc.column_dimensions["A"].width = 38
for col in "BCD":
    wc.column_dimensions[col].width = 14

# =====================================================================
# LOCKED ROW LAYOUT (DCF sheet)
# =====================================================================
R_SEL, R_CASE = 4, 5
R_MKT = 7            # header; 8 price, 9 shares, 10 mcap, 11 debt, 12 cash, 13 net debt
BLOCKS = {"BEAR": 15, "BASE": 27, "BULL": 39, "SELECTED": 51}  # header row; +1 col hdr; +2.. rows
ASSUMP = ["Revenue Growth (%)", "EBIT Margin (%)", "Tax Rate (%)", "D&A (% of Revenue)",
          "CapEx (% of Revenue)", "Δ NWC (% of Δ Revenue)", "Terminal Growth Rate", "WACC Adjustment vs. WACC sheet",
          "Terminal CapEx (% of terminal D&A)"]
OFF = {k: i + 2 for i, k in enumerate(["g", "m", "tax", "da", "capex", "nwc", "tg", "wadj", "tcx"])}
SINGLE = ("tg", "wadj", "tcx")   # single-value (column B only) assumptions
SEL = BLOCKS["SELECTED"]
R_WACC_SEL = SEL + 11   # selected WACC (row 62)
R_FIN = 64           # header; 65 col hdr
R = dict(rev=66, g=67, gp=68, gm=69, ebit=70, em=71, tax=72, tr=73, nopat=74,
         da=76, dap=77, capex=78, cxp=79, nwc=80, nwcp=81, fcf=82, fcfm=83)
R_DCF = 85           # header; 86 col hdr; 87 fcf, 88 period, 89 DF, 90 PV
R_TV = 92            # header; 93..102 normalized terminal year build
R_VAL = 104          # header; 105..115
R_S1, R_S2, R_S3 = 117, 127, 137

HIST = ["2022-12-31", "2023-12-31", "2024-12-31", "2025-12-31"]
HCOL = ["B", "C", "D", "E"]
PCOL = ["F", "G", "H", "I", "J"]          # FY2026E..FY2030E in financials
ACOL = ["B", "C", "D", "E", "F"]          # same years in assumption blocks
YEARS_P = ["FY2026E", "FY2027E", "FY2028E", "FY2029E", "FY2030E"]

# ---------- header ----------
ws["A1"] = "Taiwan Semiconductor Manufacturing (TSMC) — DCF Model"
ws["A1"].font = Font(bold=True, size=14)
ws["A2"] = f"Ticker: 2330.TW | Date: {TODAY} | Year End: Dec | Units: NT$ billions (per-share in NT$)"
ws[f"A{R_SEL}"] = "Case Selector (1=Bear, 2=Base, 3=Bull)"
inp(ws, f"B{R_SEL}", 2, "0", "Source: User selection — base case approved in chat 2026-11-08")
ws[f"A{R_CASE}"] = "Active Case"
calc(ws, f"B{R_CASE}", f'=CHOOSE($B${R_SEL},"Bear","Base","Bull")', "@", bold=True)

# ---------- market data ----------
header(ws, R_MKT, "MARKET DATA & KEY INPUTS", "A", "C")
ws["A8"], ws["A9"], ws["A10"], ws["A11"], ws["A12"], ws["A13"] = (
    "Current Stock Price (NT$)", "Shares Outstanding (bn)", "Market Cap (NT$bn)",
    "Total Debt (NT$bn)", "Cash & ST Investments (NT$bn)", "Net Debt / (Net Cash) (NT$bn)")
inp(ws, "B8", D["price"], PX, f"{YF}, currentPrice")
inp(ws, "B9", D["shares_outstanding"] / BN, '0.000', f"{YF}, sharesOutstanding (basic; TSMC has negligible dilution)")
calc(ws, "B10", "=B8*B9", NUM)
inp(ws, "B11", D["total_debt"] / BN, NUM1, f"{YF}, totalDebt (MRQ). Note: FY2025 annual balance sheet shows NT$1,064.6bn — verify vs MOPS")
inp(ws, "B12", D["total_cash"] / BN, NUM1, f"{YF}, totalCash (MRQ, incl. ST investments). FY2025 annual cash & equivalents NT$2,767.9bn")
calc(ws, "B13", "=B11-B12", NUM)
box(ws, "A7:C13", "medium")

# ---------- scenario blocks ----------
SCEN = {
    "BEAR": dict(g=[.20, .14, .10, .08, .06], m=[.46, .45, .44, .43, .42], tax=[.17] * 5,
                 da=[.18, .18, .17, .17, .17], capex=[.36, .35, .35, .34, .34], nwc=[.06] * 5, tg=.025, wadj=.010,
                 tcx=1.35),
    "BASE": dict(g=[.30, .22, .16, .12, .10], m=[.50] * 5, tax=[.17] * 5,
                 da=[.18, .17, .17, .16, .16], capex=[.33] * 5, nwc=[.05] * 5, tg=.030, wadj=0.0,
                 tcx=1.25),
    "BULL": dict(g=[.35, .28, .20, .15, .12], m=[.52, .53, .53, .54, .54], tax=[.17] * 5,
                 da=[.18, .17, .16, .16, .15], capex=[.32, .31, .31, .30, .30], nwc=[.04] * 5, tg=.035, wadj=-.005,
                 tcx=1.15),
}
TCX_SRC = ("Source: Analyst assumption, approved in chat 2026-11-08 — steady-state capex normalised toward D&A. "
           "Base 125% ≈ reinvestment needed for 3% growth at ~30% return on new capital (TSMC ROIC ~30-40%)")
SRC = {
    "BEAR": "Source: Analyst assumption (bear) — slower AI capex cycle, margin compression, higher capex",
    "BASE": "Source: User-approved base case in chat 2026-11-08 (growth 30%→10%, ~50% EBIT margin, capex ~33%)",
    "BULL": "Source: Analyst assumption (bull) — sustained AI demand, margin expansion, capex intensity easing",
}
for name, top in BLOCKS.items():
    header(ws, top, f"{name} CASE ASSUMPTIONS" if name != "SELECTED" else "SELECTED CASE (drives model) — CHOOSE on case selector", "A", "F")
    subhdr(ws, top + 1, ["Assumption"] + YEARS_P)
    for i, lab in enumerate(ASSUMP):
        ws[f"A{top + 2 + i}"] = lab
    for key, off in OFF.items():
        r = top + off
        cols = ACOL if key not in SINGLE else ["B"]
        for j, col in enumerate(cols):
            if name == "SELECTED":
                refs = ",".join(f"{col}{BLOCKS[b] + off}" for b in ("BEAR", "BASE", "BULL"))
                calc(ws, f"{col}{r}", f"=CHOOSE($B${R_SEL},{refs})", PCT)
            else:
                v = SCEN[name][key]
                inp(ws, f"{col}{r}", v[j] if isinstance(v, list) else v, PCT,
                    TCX_SRC if key == "tcx" else SRC[name])
    box(ws, f"A{top}:F{top + 10}", "thin")
ws[f"A{R_WACC_SEL}"] = "WACC (selected) = WACC sheet + adjustment"
calc(ws, f"B{R_WACC_SEL}", f"=WACC!$B$29+B{SEL + OFF['wadj']}", PCT, out=True)
box(ws, f"A{BLOCKS['BEAR']}:F{R_WACC_SEL}", "medium")

sel = lambda key, acol: f"${acol}${SEL + OFF[key]}"  # noqa: E731
TG, W = f"$B${SEL + OFF['tg']}", f"$B${R_WACC_SEL}"

# ---------- historical & projected financials ----------
header(ws, R_FIN, "HISTORICAL & PROJECTED FINANCIALS (NT$bn)")
subhdr(ws, R_FIN + 1, ["Income Statement / FCF", "2022A", "2023A", "2024A", "2025A"] + [y[2:] for y in YEARS_P])
labels = {R["rev"]: "Revenue", R["g"]: "  % growth", R["gp"]: "Gross Profit (memo)", R["gm"]: "  % gross margin",
          R["ebit"]: "EBIT (Operating Income)", R["em"]: "  % EBIT margin", R["tax"]: "(-) Taxes on EBIT",
          R["tr"]: "  tax rate", R["nopat"]: "NOPAT", R["da"]: "(+) D&A", R["dap"]: "  % of revenue",
          R["capex"]: "(-) CapEx", R["cxp"]: "  % of revenue", R["nwc"]: "(-) Δ NWC (increase = use of cash)",
          R["nwcp"]: "  % of Δ revenue", R["fcf"]: "Unlevered Free Cash Flow", R["fcfm"]: "  % FCF margin"}
for r, t in labels.items():
    ws[f"A{r}"] = t
    if t.startswith("  "):
        ws[f"A{r}"].font = Font(italic=True)
ws[f"A{R['fcf']}"].font = Font(bold=True)

for col, y in zip(HCOL, HIST):
    inp(ws, f"{col}{R['rev']}", A["revenue"][y] / BN, NUM, f"{YF}, income_stmt Total Revenue FY{y[:4]}")
    inp(ws, f"{col}{R['gp']}", A["gross_profit"][y] / BN, NUM, f"{YF}, income_stmt Gross Profit FY{y[:4]}")
    inp(ws, f"{col}{R['ebit']}", A["operating_income"][y] / BN, NUM, f"{YF}, income_stmt Operating Income FY{y[:4]}")
    inp(ws, f"{col}{R['tr']}", A["tax_rate"][y], PCT, f"{YF}, income_stmt Tax Rate For Calcs FY{y[:4]}")
    inp(ws, f"{col}{R['da']}", A["d_and_a"][y] / BN, NUM, f"{YF}, cashflow Depreciation And Amortization FY{y[:4]}")
    inp(ws, f"{col}{R['capex']}", -A["capex"][y] / BN, NUM, f"{YF}, cashflow Capital Expenditure FY{y[:4]} (sign flipped)")
    inp(ws, f"{col}{R['nwc']}", -A["change_in_working_capital"][y] / BN, NUM,
        f"{YF}, cashflow Change In Working Capital FY{y[:4]} (sign flipped: + = cash outflow)")
    calc(ws, f"{col}{R['gm']}", f"={col}{R['gp']}/{col}{R['rev']}", PCT)
    calc(ws, f"{col}{R['em']}", f"={col}{R['ebit']}/{col}{R['rev']}", PCT)
    calc(ws, f"{col}{R['tax']}", f"={col}{R['ebit']}*{col}{R['tr']}", NUM)
    calc(ws, f"{col}{R['nopat']}", f"={col}{R['ebit']}-{col}{R['tax']}", NUM)
    calc(ws, f"{col}{R['dap']}", f"={col}{R['da']}/{col}{R['rev']}", PCT)
    calc(ws, f"{col}{R['cxp']}", f"={col}{R['capex']}/{col}{R['rev']}", PCT)
    calc(ws, f"{col}{R['fcf']}", f"={col}{R['nopat']}+{col}{R['da']}-{col}{R['capex']}-{col}{R['nwc']}", NUM, bold=True)
    calc(ws, f"{col}{R['fcfm']}", f"={col}{R['fcf']}/{col}{R['rev']}", PCT)
for i in range(1, 4):
    c, p = HCOL[i], HCOL[i - 1]
    calc(ws, f"{c}{R['g']}", f"={c}{R['rev']}/{p}{R['rev']}-1", PCT)
    calc(ws, f"{c}{R['nwcp']}", f"={c}{R['nwc']}/({c}{R['rev']}-{p}{R['rev']})", PCT)

prev = "E"
for pc, ac in zip(PCOL, ACOL):
    calc(ws, f"{pc}{R['rev']}", f"={prev}{R['rev']}*(1+{sel('g', ac)})", NUM)
    calc(ws, f"{pc}{R['g']}", f"={pc}{R['rev']}/{prev}{R['rev']}-1", PCT)
    calc(ws, f"{pc}{R['ebit']}", f"={pc}{R['rev']}*{sel('m', ac)}", NUM)
    calc(ws, f"{pc}{R['em']}", f"={pc}{R['ebit']}/{pc}{R['rev']}", PCT)
    calc(ws, f"{pc}{R['tax']}", f"={pc}{R['ebit']}*{sel('tax', ac)}", NUM)
    calc(ws, f"{pc}{R['tr']}", f"={pc}{R['tax']}/{pc}{R['ebit']}", PCT)
    calc(ws, f"{pc}{R['nopat']}", f"={pc}{R['ebit']}-{pc}{R['tax']}", NUM)
    calc(ws, f"{pc}{R['da']}", f"={pc}{R['rev']}*{sel('da', ac)}", NUM)
    calc(ws, f"{pc}{R['dap']}", f"={pc}{R['da']}/{pc}{R['rev']}", PCT)
    calc(ws, f"{pc}{R['capex']}", f"={pc}{R['rev']}*{sel('capex', ac)}", NUM)
    calc(ws, f"{pc}{R['cxp']}", f"={pc}{R['capex']}/{pc}{R['rev']}", PCT)
    calc(ws, f"{pc}{R['nwc']}", f"=({pc}{R['rev']}-{prev}{R['rev']})*{sel('nwc', ac)}", NUM)
    calc(ws, f"{pc}{R['nwcp']}", f"={pc}{R['nwc']}/({pc}{R['rev']}-{prev}{R['rev']})", PCT)
    calc(ws, f"{pc}{R['fcf']}", f"={pc}{R['nopat']}+{pc}{R['da']}-{pc}{R['capex']}-{pc}{R['nwc']}", NUM, bold=True)
    calc(ws, f"{pc}{R['fcfm']}", f"={pc}{R['fcf']}/{pc}{R['rev']}", PCT)
    prev = pc
box(ws, f"A{R_FIN}:J{R['fcfm']}", "medium")
box(ws, f"B{R_FIN + 1}:E{R['fcfm']}", "thin")

# ---------- discounting ----------
header(ws, R_DCF, "DCF VALUATION — mid-year convention")
subhdr(ws, R_DCF + 1, ["", "", "", "", ""] + YEARS_P)
ws[f"A{R_DCF + 2}"], ws[f"A{R_DCF + 3}"], ws[f"A{R_DCF + 4}"], ws[f"A{R_DCF + 5}"] = (
    "Unlevered FCF (NT$bn)", "Discount Period (years)", "Discount Factor", "PV of FCF (NT$bn)")
for i, pc in enumerate(PCOL):
    calc(ws, f"{pc}{R_DCF + 2}", f"={pc}{R['fcf']}", NUM)
    if i == 0:
        inp(ws, f"{pc}{R_DCF + 3}", 0.5, "0.0", "Source: Mid-year convention, valuation date 1 Jan 2026 (simplification — actual date is Nov 2026)")
    else:
        calc(ws, f"{pc}{R_DCF + 3}", f"={PCOL[i - 1]}{R_DCF + 3}+1", "0.0")
    calc(ws, f"{pc}{R_DCF + 4}", f"=1/(1+{W})^{pc}{R_DCF + 3}", "0.0000")
    calc(ws, f"{pc}{R_DCF + 5}", f"={pc}{R_DCF + 2}*{pc}{R_DCF + 4}", NUM)
box(ws, f"A{R_DCF}:J{R_DCF + 5}", "medium")

header(ws, R_TV, "TERMINAL VALUE — perpetuity growth on a normalised steady-state year", "A", "C")
t = R_TV
LAST_A = "F"   # FY2030E column in assumption blocks
tv_rows = [
    ("Terminal-year Revenue (FY2030E × (1+g))", f"=J{R['rev']}*(1+{TG})", NUM, False),
    ("NOPAT (FY2030E margin & tax)", f"=B{t + 1}*{sel('m', LAST_A)}*(1-{sel('tax', LAST_A)})", NUM, False),
    ("(+) D&A (FY2030E % of revenue)", f"=B{t + 1}*{sel('da', LAST_A)}", NUM, False),
    ("(-) Normalised CapEx (= D&A × terminal ratio)", f"=B{t + 3}*$B${SEL + OFF['tcx']}", NUM, False),
    ("(-) Δ NWC", f"=(B{t + 1}-J{R['rev']})*{sel('nwc', LAST_A)}", NUM, False),
    ("Terminal FCF (NT$bn)", f"=B{t + 2}+B{t + 3}-B{t + 4}-B{t + 5}", NUM, True),
    ("Terminal Value (NT$bn)", f"=B{t + 6}/({W}-{TG})", NUM, False),
    ("PV of Terminal Value (NT$bn)", f"=B{t + 7}/(1+{W})^J{R_DCF + 3}", NUM, False),
    ("Check: reinvestment rate (net capex + ΔNWC) / NOPAT", f"=(B{t + 4}-B{t + 3}+B{t + 5})/B{t + 2}", PCT, False),
    ("Check: implied return on new capital (g ÷ reinvestment)", f"={TG}/B{t + 9}", PCT, False),
]
for i, (lab, f, fmt, bold) in enumerate(tv_rows, start=1):
    ws[f"A{t + i}"] = lab
    calc(ws, f"B{t + i}", f, fmt, bold=bold)
box(ws, f"A{R_TV}:C{R_TV + len(tv_rows)}", "medium")

header(ws, R_VAL, "VALUATION SUMMARY", "A", "C")
v = R_VAL
rows = [("Sum of PV of FCFs (NT$bn)", f"=SUM(F{R_DCF + 5}:J{R_DCF + 5})", NUM, False),
        ("PV of Terminal Value (NT$bn)", f"=B{R_TV + 8}", NUM, False),
        ("Enterprise Value (NT$bn)", f"=B{v + 1}+B{v + 2}", NUM, True),
        ("(-) Net Debt / (+) Net Cash (NT$bn)", "=B13", NUM, False),
        ("Equity Value (NT$bn)", f"=B{v + 3}-B{v + 4}", NUM, True),
        ("Shares Outstanding (bn)", "=B9", "0.000", False),
        ("IMPLIED PRICE PER SHARE (NT$)", f"=B{v + 5}/B{v + 6}", PX, True),
        ("Current Stock Price (NT$)", "=B8", PX, False),
        ("Implied Upside / (Downside)", f"=B{v + 7}/B{v + 8}-1", PCT, True),
        ("Terminal Value as % of EV", f"=B{v + 2}/B{v + 3}", PCT, False),
        ("Implied EV / FY2025A EBITDA", f"=B{v + 3}/(E{R['ebit']}+E{R['da']})", '0.0"x"', False)]
for i, (lab, f, fmt, out) in enumerate(rows, start=1):
    ws[f"A{v + i}"] = lab
    calc(ws, f"B{v + i}", f, fmt, out=out)
R_PRICE = v + 7
box(ws, f"A{R_VAL}:C{v + len(rows)}", "medium")

# ---------- sensitivity tables (full DCF recalcs) ----------
FCF_RNG, PER_RNG = f"$F${R_DCF + 2}:$J${R_DCF + 2}", f"$F${R_DCF + 3}:$J${R_DCF + 3}"
LASTP, LASTF = f"$J${R_DCF + 3}", f"$J${R_DCF + 2}"


def term_fcf(rev_t, g, dm="0"):
    """Normalised terminal FCF from final-year revenue — mirrors the TERMINAL VALUE block."""
    m, tax, da, nwc = (sel(k, LAST_A) for k in ("m", "tax", "da", "nwc"))
    return (f"({rev_t}*(1+{g})*(({m}+{dm})*(1-{tax})+{da}*(1-$B${SEL + OFF['tcx']}))"
            f"-{rev_t}*{g}*{nwc})")


def price_from_wacc(w, g):
    """Full DCF on the model's FCF row with a given WACC and terminal growth."""
    return (f"=(SUMPRODUCT({FCF_RNG}/(1+{w})^{PER_RNG})"
            f"+{term_fcf(f'$J${R['rev']}', g)}/({w}-{g})/(1+{w})^{LASTP}-$B$13)/$B$9")


def sens_table(top, title, row_lab, col_lab, row_center, col_center, row_step, col_step, cell_formula, fmt_r, fmt_c, step_src):
    header(ws, top, title, "A", "G")
    ws[f"A{top + 1}"] = f"Rows: {row_lab} | Columns: {col_lab} | Output: implied price per share (NT$)"
    ws[f"A{top + 1}"].font = Font(italic=True)
    ws[f"I{top + 1}"], ws[f"I{top + 2}"] = "Row step", "Col step"
    inp(ws, f"J{top + 1}", row_step, fmt_r, step_src)
    inp(ws, f"J{top + 2}", col_step, fmt_c, step_src)
    hr = top + 2
    ws[f"B{hr}"] = f"{row_lab} ↓ / {col_lab} →"
    ws[f"B{hr}"].font, ws[f"B{hr}"].fill = Font(bold=True, size=8), F_SUB
    cols = ["C", "D", "E", "F", "G"]
    for j, cc in enumerate(cols):
        f = f"={col_center}+({j - 2})*$J${top + 2}"
        calc(ws, f"{cc}{hr}", f, fmt_c, bold=True).fill = F_SUB
    for i in range(5):
        r = hr + 1 + i
        calc(ws, f"B{r}", f"={row_center}+({i - 2})*$J${top + 1}", fmt_r, bold=True).fill = F_SUB
        for cc in cols:
            calc(ws, f"{cc}{r}", cell_formula(f"$B{r}", f"{cc}${hr}"), PX)
    center = ws[f"E{hr + 3}"]
    center.fill, center.font = F_OUT, Font(bold=True)
    ws.conditional_formatting.add(f"C{hr + 1}:G{hr + 5}", ColorScaleRule(
        start_type="min", start_color="F8696B", mid_type="percentile", mid_value=50,
        mid_color="FFFFFF", end_type="max", end_color="63BE7B"))
    box(ws, f"A{top}:G{hr + 5}", "medium")


STEP_SRC = "Source: Sensitivity step size (analyst choice)"
sens_table(R_S1, "SENSITIVITY 1 — WACC vs Terminal Growth", "WACC", "Terminal g",
           W, TG, 0.005, 0.005, lambda r, c: price_from_wacc(r, c), PCT, PCT, STEP_SRC)


def price_growth_margin(dg, dm):
    """Full re-projection: dg added to every year's growth, dm added to every year's EBIT margin."""
    rev = []
    fcfs = []
    for t, ac in enumerate(ACOL):
        growth = "*".join(f"(1+{sel('g', a)}+{dg})" for a in ACOL[: t + 1])
        rev.append(f"($E${R['rev']}*{growth})")
        prev_rev = f"$E${R['rev']}" if t == 0 else rev[t - 1]
        fcfs.append(f"({rev[t]}*(({sel('m', ac)}+{dm})*(1-{sel('tax', ac)})+{sel('da', ac)}-{sel('capex', ac)})"
                    f"-({rev[t]}-{prev_rev})*{sel('nwc', ac)})")
    pv = "+".join(f"{fcfs[t]}/(1+{W})^{PCOL[t]}${R_DCF + 3}" for t in range(5))
    tv = f"{term_fcf(rev[4], TG, dm)}/({W}-{TG})/(1+{W})^{LASTP}"
    return f"=({pv}+{tv}-$B$13)/$B$9"


sens_table(R_S2, "SENSITIVITY 2 — Revenue Growth vs EBIT Margin (shift applied to all years)",
           "Δ growth (pp)", "Δ EBIT margin (pp)", "0", "0", 0.02, 0.02, price_growth_margin, PCT, PCT, STEP_SRC)


def price_beta_rf(beta, rf):
    w = (f"(WACC!$B$26*({rf}+{beta}*WACC!$B$6)+WACC!$B$27*WACC!$C$27"
         f"+$B${SEL + OFF['wadj']})")
    return price_from_wacc(w, TG)


sens_table(R_S3, "SENSITIVITY 3 — Beta vs Risk-Free Rate", "Beta", "Risk-free rate",
           "WACC!$B$5", "WACC!$B$4", 0.10, 0.0025, price_beta_rf, '0.00', '0.00%', STEP_SRC)

# =====================================================================
# WACC sheet
# =====================================================================
wc["A1"] = "TSMC (2330.TW) — Cost of Capital (TWD)"
wc["A1"].font = Font(bold=True, size=14)
header(wc, 3, "COST OF EQUITY (CAPM)", "A", "D")
wc["A4"], wc["A5"], wc["A6"], wc["A7"] = "Risk-Free Rate (Taiwan 10Y govt bond)", "Beta (5Y monthly)", "Equity Risk Premium (incl. Taiwan country risk)", "Cost of Equity"
inp(wc, "B4", 0.016, '0.00%', "Source: Analyst estimate of Taiwan 10Y government bond yield (~1.5–1.7%) — verify current yield before relying on output")
inp(wc, "B5", D["beta"], '0.000', f"{YF}, beta (5Y monthly vs market)")
inp(wc, "B6", 0.060, '0.00%', "Source: Analyst assumption — ~4.5–5% mature-market ERP + ~1% Taiwan country/geopolitical risk")
calc(wc, "B7", "=B4+B5*B6", '0.00%', bold=True)
box(wc, "A3:D7", "medium")

header(wc, 9, "COST OF DEBT", "A", "D")
wc["A10"], wc["A11"], wc["A12"], wc["A13"] = "Credit Rating (S&P)", "Pre-Tax Cost of Debt", "Tax Rate (selected case, FY1)", "After-Tax Cost of Debt"
inp(wc, "B10", "AA-", "@", "Source: S&P Global Ratings long-term issuer rating for TSMC — verify current rating")
inp(wc, "B11", 0.020, '0.00%', "Source: Analyst estimate — TSMC TWD corporate bonds / USD bonds swapped; verify with recent issuance")
link(wc, "B12", f"=DCF!$B${SEL + OFF['tax']}", '0.0%')
calc(wc, "B13", "=B11*(1-B12)", '0.00%', bold=True)
box(wc, "A9:D13", "medium")

header(wc, 15, "CAPITAL STRUCTURE (market values, NT$bn)", "A", "D")
for r, lab, f, fmt in [(16, "Current Stock Price (NT$)", "=DCF!$B$8", PX), (17, "Shares Outstanding (bn)", "=DCF!$B$9", '0.000'),
                       (19, "Total Debt", "=DCF!$B$11", NUM), (20, "Cash & ST Investments", "=DCF!$B$12", NUM)]:
    wc[f"A{r}"] = lab
    link(wc, f"B{r}", f, fmt)
wc["A18"], wc["A21"], wc["A22"] = "Market Capitalization", "Net Debt / (Net Cash)", "Enterprise Value"
calc(wc, "B18", "=B16*B17", NUM)
calc(wc, "B21", "=B19-B20", NUM)
calc(wc, "B22", "=B18+B21", NUM)
box(wc, "A15:D22", "medium")

header(wc, 24, "WACC CALCULATION (gross-debt weights; TSMC is in net cash)", "A", "D")
subhdr(wc, 25, ["Component", "Weight", "Cost", "Contribution"])
wc["A26"], wc["A27"] = "Equity", "Debt"
calc(wc, "B26", "=B18/(B18+B19)", PCT)
calc(wc, "C26", "=B7", '0.00%')
calc(wc, "D26", "=B26*C26", '0.00%')
calc(wc, "B27", "=B19/(B18+B19)", PCT)
calc(wc, "C27", "=B13", '0.00%')
calc(wc, "D27", "=B27*C27", '0.00%')
wc["A29"] = "WEIGHTED AVERAGE COST OF CAPITAL"
wc["A29"].font = Font(bold=True)
calc(wc, "B29", "=D26+D27", '0.00%', out=True)
box(wc, "A24:D29", "medium")

ws.freeze_panes = "B4"
OUT.parent.mkdir(exist_ok=True)
wb.save(OUT)
print(OUT)
